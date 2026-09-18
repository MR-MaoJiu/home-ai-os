import asyncio
import base64
import json
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo
from croniter import croniter
from fastapi import FastAPI, Depends, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from sqlalchemy import select, delete, func
from sqlalchemy.exc import IntegrityError
from .config import Settings
from .db import *
from .contracts import PairRequest, SyncBatch, TaskRequest, Decision, AutomationInput, ProviderManifest
from .crypto import Vault, digest, canonical
from .security import Actor, authenticate, credential, verify, own, owner
from .policy import Policy, CAPABILITIES
from .providers import Registry
from .data import accessible, serialize, ingest, read_record, emit, audit
from .runtime import submit


def create_app(settings=None, vault=None, db_factory=None, policy=None, registry=None):
    settings = settings or Settings()
    @asynccontextmanager
    async def lifespan(app):
        signalling = None
        if app.state.settings.direct_enabled:
            import aiortc  # 显式启用时要求真实传输依赖就绪。
            from .connect_signalling import run_agent
            signalling = asyncio.create_task(run_agent(app))
        try:
            yield
        finally:
            if signalling:
                signalling.cancel()
                await asyncio.gather(signalling, return_exceptions=True)
            manager = getattr(app.state, 'direct_sessions', None)
            if manager:
                await manager.close()
    app = FastAPI(lifespan=lifespan, title="Home AI OS", version="0.1.0", docs_url=None if settings.environment == "production" else "/docs")
    from .limits import BodyLimitMiddleware
    app.add_middleware(BodyLimitMiddleware)
    app.state.settings = settings
    app.state.vault = vault or Vault.from_file(settings.master_key_file)
    app.state.db = db_factory or database(settings.database_url)[1]
    app.state.policy = policy or Policy(settings.opa_url)
    app.state.registry = registry or Registry(app.state.vault)
    from .sharing import router as sharing_router
    app.include_router(sharing_router)
    v = app.state.vault
    auth = Depends(authenticate)

    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=422, content={"detail": "请求不符合契约", "fields": [".".join(str(x) for x in e["loc"]) for e in exc.errors()]})

    @app.exception_handler(IntegrityError)
    async def conflict(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=409, content={"detail": "并发写入冲突，请使用相同幂等键重试"})

    @app.get("/health/live")
    def live():
        return {"status": "alive"}

    from .pairing import router as pairing_router
    app.include_router(pairing_router)
    from .service_application import router as application_router
    app.include_router(application_router)
    from .conversations import router as conversation_router
    app.include_router(conversation_router)

    @app.post("/api/v1/pair")
    def pair(body: PairRequest):
        from .pairing import consume
        with app.state.db() as db:
            result=consume(app.state,db,body)
            db.commit()
            return result

    @app.post("/api/v1/session/renew")
    def renew(request: Request, actor: Actor = auth):
        with app.state.db() as db:
            token = credential(db, actor.user_id, "access", settings.session_seconds, actor.device_id)
            incoming = request.headers.get("authorization", "").removeprefix("Bearer ")
            old = db.scalar(select(Credential).where(Credential.digest == digest(incoming.encode())).with_for_update())
            if not old or old.kind != "refresh" or old.expires_at <= now():
                raise HTTPException(401, "需要有效的刷新凭据")
            if old.kind == "refresh":
                db.delete(old)
            refresh = credential(db, actor.user_id, "refresh", 30 * 86400, actor.device_id)
            db.commit()
            return {"access_token": token, "refresh_token": refresh, "expires_in": settings.session_seconds}

    @app.get("/api/v1/me")
    def me(actor: Actor = auth):
        return actor.__dict__

    @app.get("/api/v1/devices")
    def devices(actor: Actor = auth):
        with app.state.db() as db:
            return [{"id": d.id, "name": d.name, "revoked": d.revoked} for d in db.scalars(select(Device).where(Device.user_id == actor.user_id))]

    @app.delete("/api/v1/devices/{device_id}")
    def revoke_device(device_id: str, actor: Actor = auth):
        with app.state.db() as db:
            d = db.get(Device, device_id)
            if not d or d.user_id != actor.user_id:
                raise HTTPException(404, "设备不存在")
            d.revoked = True
            db.execute(delete(Credential).where(Credential.device_id == device_id))
            scope(db, actor.user_id, actor.household_id)
            for table in (SyncSnapshot, SyncCursor, SyncReceipt):
                db.execute(delete(table).where(table.owner_id == actor.user_id, table.device_id == device_id))
            audit(db, actor, "device.revoke", device_id)
            db.commit()
            return {"revoked": True}

    @app.post("/api/v1/data/sync")
    def sync(body: SyncBatch, actor: Actor = auth):
        with app.state.db() as db:
            from .sync_batch import apply_batch
            result = apply_batch(db, actor, body, v)
            db.commit()
            return result

    @app.get("/api/v1/data")
    def records(actor: Actor = auth, after: str = "", limit: int = 100):
        with app.state.db() as db:
            rows = db.scalars(accessible(db, actor).where(Record.id > after).order_by(Record.id).limit(min(max(limit, 1), 200))).all()
            return {"records": [serialize(r, v) for r in rows], "next_cursor": rows[-1].id if rows else None}

    @app.get("/api/v1/data/changes")
    def changes(after: int = 0, actor: Actor = auth):
        with app.state.db() as db:
            scope(db, actor.user_id, actor.household_id)
            from .sync_order import lock_changes
            lock_changes(db)
            rows = db.scalars(select(Outbox).where(Outbox.owner_id == actor.user_id, Outbox.id > after, Outbox.kind.like("record.%")).order_by(Outbox.id).limit(100)).all()
            return {"changes": [{"cursor": r.id, "type": r.kind, "record_id": r.resource_id} for r in rows], "next_cursor": rows[-1].id if rows else after}

    @app.get("/api/v1/data/{record_id}")
    def get_record(record_id: str, actor: Actor = auth):
        with app.state.db() as db:
            return serialize(read_record(db, actor, record_id), v)

    @app.delete("/api/v1/data/{record_id}")
    def delete_record(record_id: str, actor: Actor = auth):
        with app.state.db() as db:
            record = own(db, Record, record_id, actor)
            from .deletion import delete_tree
            deleted_ids = delete_tree(db, actor, record, v, settings.state_dir)
            return {"deleted": True, "deleted_ids": deleted_ids, "derived_purge": "pending"}

    @app.get("/api/v1/data/{record_id}/grants")
    def record_grants(record_id: str, actor: Actor = auth):
        with app.state.db() as db:
            record=own(db,Record,record_id,actor)
            if record.deleted:raise HTTPException(404,"数据已删除")
            return {"grantee_ids":list(db.scalars(select(Grant.grantee_id).where(Grant.record_id==record.id,Grant.owner_id==actor.user_id)))}

    @app.put("/api/v1/data/{record_id}/grants/{user_id}")
    def share(record_id: str, user_id: str, actor: Actor = auth):
        with app.state.db() as db:
            from .sync_order import lock_changes, notify_recipients
            scope(db, actor.user_id, actor.household_id)
            lock_changes(db)
            record = own(db, Record, record_id, actor)
            subject = db.get(Principal, user_id)
            if not subject or subject.household_id != actor.household_id or record.deleted:
                raise HTTPException(404, "成员或数据不存在")
            if record.sensitivity == "SECRET":
                raise HTTPException(403, "秘密不能共享")
            existing = db.scalar(select(Grant).where(Grant.record_id == record.id, Grant.grantee_id == user_id))
            if not existing:
                db.add(Grant(household_id=actor.household_id, owner_id=actor.user_id, record_id=record.id, grantee_id=user_id))
            if not existing:
                db.flush()
                notify_recipients(db, actor, 'record.changed', record.id, [user_id])
            audit(db, actor, "data.share", record.id)
            db.commit()
            return {"shared": True}

    @app.delete("/api/v1/data/{record_id}/grants/{user_id}")
    def unshare(record_id: str, user_id: str, actor: Actor = auth):
        with app.state.db() as db:
            from .sync_order import lock_changes, notify_recipients
            scope(db, actor.user_id, actor.household_id)
            lock_changes(db)
            own(db, Record, record_id, actor)
            existing = db.scalar(select(Grant).where(Grant.record_id == record_id, Grant.grantee_id == user_id))
            if existing:
                from .sync_order import invalidate_snapshots
                invalidate_snapshots(db, actor, [user_id])
                db.delete(existing)
                db.flush()
                notify_recipients(db, actor, 'record.revoked', record_id, [user_id])
            audit(db, actor, "data.unshare", record_id)
            db.commit()
            return {"revoked": True}

    @app.post("/api/v1/files")
    async def upload(file: UploadFile, actor: Actor = auth):
        from .contracts import DataRecord
        content = await file.read(settings.max_upload_bytes + 1)
        if len(content) > settings.max_upload_bytes:
            raise HTTPException(413, "附件超过限制")
        with app.state.db() as db:
            from .sync_order import lock_changes
            scope(db, actor.user_id, actor.household_id)
            lock_changes(db)
            identifier = digest(content)
            existing = db.scalar(select(Record).where(Record.owner_id == actor.user_id, Record.source == "files", Record.source_id == identifier).with_for_update())
            version, sensitivity, cloud_policy = 1, "PRIVATE", "LOCAL_ONLY"
            metadata = {"name": Path(file.filename or "附件").name, "size": len(content), "sha256": identifier}
            if existing:
                if existing.deleted: raise HTTPException(409, "已删除来源不能自动恢复")
                previous = serialize(existing, v)['payload']
                sensitivity, cloud_policy = existing.sensitivity, existing.cloud_policy
                if existing.kind == 'document.import':
                    try: legacy = base64.b64decode(previous.get('content_base64', ''), validate=True)
                    except Exception: raise HTTPException(409, '旧文件内容无效，不能自动迁移') from None
                    if digest(legacy) != identifier: raise HTTPException(409, '旧文件来源与内容不一致')
                    version = existing.version + 1
                elif existing.kind == 'document.file':
                    metadata['name'] = previous['name'] if isinstance(previous.get('name'), str) else metadata['name']
                    version = existing.version if metadata == previous else existing.version + 1
                else: raise HTTPException(409, '来源标识已用于其他数据类型')
            record = ingest(db, actor, DataRecord(source="files", source_id=identifier, kind="document.file", version=version, sensitivity=sensitivity, cloud_policy=cloud_policy, payload=metadata), v)
            directory = settings.state_dir / "blobs"
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / record.id
            target.write_text(v.seal(base64.b64encode(content).decode(), actor.user_id + ":blob:" + record.id))
            target.chmod(0o600)
            db.commit()
            return serialize(record, v)

    @app.get("/api/v1/files/{record_id}/content")
    def file_content(record_id: str, actor: Actor = auth):
        from fastapi.responses import Response
        from urllib.parse import quote
        with app.state.db() as db:
            record = read_record(db, actor, record_id)
            metadata = serialize(record, v)['payload']
            if record.kind == 'document.import':
                encoded = metadata.get('content_base64', '')
            elif record.kind == 'document.file':
                path = settings.state_dir / 'blobs' / record.id
                if not path.is_file() or path.is_symlink(): raise HTTPException(404, '文件不可用')
                encoded = v.open(path.read_text(), record.owner_id + ':blob:' + record.id)
            else: raise HTTPException(422, '此记录不是文件')
            try: contents = base64.b64decode(encoded, validate=True)
            except Exception: raise HTTPException(422, '文件内容无效') from None
            if len(contents) > settings.max_upload_bytes: raise HTTPException(413, '文件超过限制')
            filename = metadata.get('name', 'attachment')
            filename = filename[:200] if isinstance(filename, str) else 'attachment'
            return Response(contents, media_type='application/octet-stream', headers={'Content-Disposition': "attachment; filename*=UTF-8''" + quote(filename, safe=''), 'Cache-Control':'no-store', 'X-Content-Type-Options':'nosniff'})

    @app.post("/api/v1/files/{record_id}/parse", status_code=202)
    def parse_file(record_id: str, actor: Actor = auth):
        with app.state.db() as db:
            record = own(db, Record, record_id, actor)
            if record.deleted or record.kind != "document.file" or record.sensitivity == "SECRET":
                raise HTTPException(403, "来源不能解析")
            db.refresh(record, with_for_update=True)
            prefix = "parse:" + record.id + ":" + str(record.version)
            previous = db.scalar(select(Task).where(Task.owner_id == actor.user_id, Task.idempotency_key.startswith(prefix + ":")).order_by(Task.created_at.desc()).limit(1))
            if previous and previous.status not in {"FAILED", "CANCELED"}:
                reusable = True
                if previous.status == 'SUCCEEDED':
                    from .result_access import check_dependencies
                    try: check_dependencies(db, actor, v.open(previous.request, actor.user_id + ':task:' + previous.id))
                    except HTTPException: reusable = False
                if reusable: return {"id": previous.id, "status": previous.status}
            task = submit(db, actor, TaskRequest(idempotency_key=prefix + ":" + uid(), capability="document.parse@v1", record_ids=[record.id], arguments={"record_id": record.id}, step_timeout_seconds=300), v)
            db.commit()
            return {"id": task.id, "status": task.status}

    @app.post("/api/v1/tasks", status_code=202)
    def create_task(body: TaskRequest, actor: Actor = auth):
        with app.state.db() as db:
            task = submit(db, actor, body, v)
            db.commit()
            return {"id": task.id, "status": task.status}

    def task_view(task, db, actor):
        payload = v.open(task.request, task.owner_id + ":task:" + task.id)
        from .result_access import check_dependencies
        redacted = False
        try: check_dependencies(db, actor, payload)
        except HTTPException: redacted = True
        return {"id": task.id, "status": task.status, "error": "来源授权或版本已变化，旧结果已隐藏" if redacted else task.error, "result_redacted": redacted, "result": v.open(task.result, task.owner_id + ":task-result:" + task.id) if task.result and not redacted else None, "execution": {"agent": bool(payload.get("_agent")), "planned_steps": len(payload.get("steps", [])), "max_steps": payload.get("max_steps", 8), "model_rounds": payload.get("_model_rounds", 0), "model_token_charge": payload.get("_model_token_charge", 0), "max_model_tokens": payload.get("max_model_tokens"), "deadline": task.deadline}}

    @app.get("/api/v1/tasks/{task_id}")
    def get_task(task_id: str, actor: Actor = auth):
        with app.state.db() as db:
            return task_view(own(db, Task, task_id, actor), db, actor)

    @app.post("/api/v1/tasks/{task_id}/cancel")
    def cancel(task_id: str, actor: Actor = auth):
        with app.state.db() as db:
            task = own(db, Task, task_id, actor)
            task.cancel_requested = True
            if task.status in {"RECEIVED", "AWAITING_APPROVAL", "APPROVED"}:
                task.status = "CANCELED"
            db.commit()
            return task_view(task, db, actor)

    @app.get("/api/v1/approvals")
    def approvals(actor: Actor = auth):
        with app.state.db() as db:
            scope(db, actor.user_id, actor.household_id)
            rows = db.scalars(select(Approval).where(Approval.owner_id == actor.user_id, Approval.decision == "PENDING", Approval.expires_at > now())).all()
            result = []
            for row in rows:
                inv = db.get(Invocation, row.invocation_id)
                task = db.get(Task, inv.task_id) if inv else None
                if not task or task.status != "AWAITING_APPROVAL" or task.cancel_requested:continue
                from .result_access import check_dependencies
                try: check_dependencies(db, actor, v.open(task.request, actor.user_id + ':task:' + task.id))
                except HTTPException: continue
                result.append({"id": row.id, "task_id": task.id, "capability": inv.capability, "arguments": v.open(inv.arguments, actor.user_id + ":invocation:" + inv.id), "expires_at": row.expires_at})
            return result

    @app.post("/api/v1/approvals/{approval_id}")
    def decide(approval_id: str, body: Decision, actor: Actor = auth):
        with app.state.db() as db:
            approval = own(db, Approval, approval_id, actor)
            invocation = db.get(Invocation, approval.invocation_id)
            task = own(db, Task, invocation.task_id, actor)
            db.refresh(task, with_for_update=True)
            db.refresh(approval, with_for_update=True)
            if approval.decision != "PENDING" or approval.expires_at <= now():
                raise HTTPException(409, "审批已处理或过期")
            if task.status != "AWAITING_APPROVAL" or task.cancel_requested:
                raise HTTPException(409, "任务不再等待审批")
            if body.decision == "APPROVED":
                from .result_access import check_dependencies
                check_dependencies(db, actor, v.open(task.request, actor.user_id + ':task:' + task.id))
            approval.decision = body.decision
            task.status = "APPROVED" if body.decision == "APPROVED" else "CANCELED"
            if body.decision == "REJECTED":
                task.cancel_requested = True
            audit(db, actor, "approval." + body.decision.lower(), approval.id)
            db.commit()
            return {"status": task.status}

    @app.get("/api/v1/activity")
    def activity(actor: Actor = auth):
        with app.state.db() as db:
            scope(db, actor.user_id, actor.household_id)
            return {"entries": [{"id": a.id, "action": a.action, "resource_id": a.resource_id, "created_at": a.created_at} for a in db.scalars(select(Audit).where(Audit.owner_id == actor.user_id).order_by(Audit.created_at.desc()).limit(100))], "disclosures": [{"id": d.id, "provider": d.provider_id, "bytes_sent": d.bytes_sent, "status": d.status} for d in db.scalars(select(Disclosure).where(Disclosure.owner_id == actor.user_id).limit(100))]}

    @app.get("/api/v1/providers")
    def providers(actor: Actor = auth):
        owner(actor)
        with app.state.db() as db:
            return [{"id": p.id, "enabled": p.enabled, "health": p.health, "manifest": json.loads(p.manifest)} for p in db.scalars(select(Provider))]

    @app.put("/api/v1/providers/{provider_id}")
    def install(provider_id: str, body: ProviderManifest, actor: Actor = auth):
        owner(actor)
        if body.id != provider_id or set(body.capabilities) - CAPABILITIES.keys():
            raise HTTPException(422, "Provider 标识或能力不匹配")
        with app.state.db() as db:
            row = db.get(Provider, provider_id)
            if row:
                row.previous_manifest, row.manifest, row.enabled = row.manifest, body.model_dump_json(), False
            else:
                row = Provider(id=provider_id, manifest=body.model_dump_json())
                db.add(row)
            scope(db, actor.user_id, actor.household_id)
            audit(db, actor, "provider.register", provider_id)
            db.commit()
            return {"id": row.id, "enabled": False}

    @app.post("/api/v1/providers/{provider_id}/{action}")
    async def manage_provider(provider_id: str, action: str, actor: Actor = auth):
        owner(actor)
        with app.state.db() as db:
            row = db.get(Provider, provider_id)
            if not row:
                raise HTTPException(404, "Provider 不存在")
            if action == "disable":
                row.enabled, row.health = False, "offline"
            elif action == "rollback" and row.previous_manifest:
                row.manifest, row.previous_manifest, row.enabled = row.previous_manifest, row.manifest, False
            elif action == "enable":
                # 注册端点不等于完成沙箱部署，生产环境必须由受控部署工具启用。
                if settings.environment == "production":
                    raise HTTPException(409, "生产启用需先完成沙箱与供应链验收")
                row.enabled, row.health = True, "degraded"
            else:
                raise HTTPException(422, "不支持的生命周期操作")
            scope(db, actor.user_id, actor.household_id)
            audit(db, actor, "provider." + action, provider_id)
            db.commit()
            return {"id": row.id, "enabled": row.enabled, "health": row.health}

    @app.get("/api/v1/automations")
    def list_automations(actor: Actor = auth):
        with app.state.db() as db:
            scope(db, actor.user_id, actor.household_id)
            return [{"id": a.id, "name": a.name, "cron": a.cron, "enabled": a.enabled, "next_run": a.next_run if a.trigger_kind == "cron" else None, "trigger_kind": a.trigger_kind, "event_type": a.event_type, "record_kind": a.record_kind, "record_source": a.record_source, "include_shared": a.include_shared, "cooldown_seconds": a.cooldown_seconds} for a in db.scalars(select(Automation).where(Automation.owner_id == actor.user_id))]

    @app.post("/api/v1/automations")
    def create_automation(body: AutomationInput, actor: Actor = auth):
        try:
            tz = ZoneInfo(body.timezone)
            if body.trigger_kind == "event":
                if not body.event_type or body.cron:
                    raise ValueError("事件触发必须提供事件类型且不设置 Cron")
                next_run = 0
            else:
                if body.event_type:
                    raise ValueError("定时触发不能提供事件类型")
                next_run = croniter(body.cron, datetime.now(tz)).get_next(float)
        except Exception:
            raise HTTPException(422, "无效的时区、Cron 或事件触发配置") from None
        if any(s.capability not in CAPABILITIES for s in body.skill.steps):
            raise HTTPException(422, "Skill 包含未知能力")
        if any(s.capability in {"memory.semantic.index@v1", "memory.semantic.purge@v1", "memory.graph.index@v1", "memory.graph.purge@v1"} for s in body.skill.steps):
            raise HTTPException(403, "Skill 不能直接修改派生索引")
        with app.state.db() as db:
            scope(db, actor.user_id, actor.household_id)
            row = Automation(id=uid(), household_id=actor.household_id, owner_id=actor.user_id, name=body.name, cron=body.cron, timezone=body.timezone, skill="", enabled=body.enabled, next_run=next_run, trigger_kind=body.trigger_kind, event_type=body.event_type, record_kind=body.record_kind, record_source=body.record_source, include_shared=body.include_shared, cooldown_seconds=body.cooldown_seconds)
            row.skill = v.seal({**body.skill.model_dump(), "device_id": actor.device_id}, actor.user_id + ":automation:" + row.id)
            db.add(row)
            db.commit()
            return {"id": row.id}

    @app.get("/api/v1/automations/{automation_id}/deliveries")
    def automation_deliveries(automation_id: str, actor: Actor = auth, limit: int = 50):
        from .db import AutomationDelivery
        with app.state.db() as db:
            own(db, Automation, automation_id, actor)
            rows = db.scalars(select(AutomationDelivery).where(AutomationDelivery.automation_id == automation_id,
                AutomationDelivery.owner_id == actor.user_id).order_by(AutomationDelivery.created_at.desc()).limit(max(1, min(limit, 100))))
            return [{"id": row.id, "event_id": row.event_id, "status": row.status, "task_id": row.task_id,
                     "reason": row.reason, "created_at": row.created_at} for row in rows]

    @app.delete("/api/v1/automations/{automation_id}")
    def stop_automation(automation_id: str, actor: Actor = auth):
        with app.state.db() as db:
            row = own(db, Automation, automation_id, actor)
            db.refresh(row, with_for_update=True)
            row.enabled = False
            from .db import AutomationDelivery
            for delivery in db.scalars(select(AutomationDelivery).where(AutomationDelivery.automation_id == row.id, AutomationDelivery.status == "PENDING").with_for_update()):
                delivery.status, delivery.reason = "CANCELED", "自动化已停用"
            db.commit()
            return {"enabled": False}

    from .home_observer import router as home_observer_router
    app.include_router(home_observer_router)

    from .task_events import router as task_events_router
    app.include_router(task_events_router)

    from .task_control import router as task_control_router
    app.include_router(task_control_router)

    from .device_sync import router as device_sync_router
    app.include_router(device_sync_router)

    from .knowledge import router as knowledge_router
    app.include_router(knowledge_router)

    from .memory import router as memory_router
    app.include_router(memory_router)
    from .admin import router as admin_router
    app.include_router(admin_router)
    from .browser_auth import router as browser_router
    app.include_router(browser_router)
    from .management import router as management_router
    app.include_router(management_router)
    from .direct_sessions import router as direct_router
    app.include_router(direct_router)
    from .remote import router as remote_router
    app.include_router(remote_router)
    from .server_identity import router as server_identity_router
    app.include_router(server_identity_router)
    from fastapi.staticfiles import StaticFiles
    admin_dist = settings.admin_dist
    if admin_dist.is_dir():
        app.mount("/admin", StaticFiles(directory=admin_dist, html=True), name="admin-web")
    return app
