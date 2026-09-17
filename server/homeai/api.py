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
    app = FastAPI(title="Home AI OS", version="0.1.0", docs_url=None if settings.environment == "production" else "/docs")
    from .limits import BodyLimitMiddleware
    app.add_middleware(BodyLimitMiddleware)
    app.state.settings = settings
    app.state.vault = vault or Vault.from_file(settings.master_key_file)
    app.state.db = db_factory or database(settings.database_url)[1]
    app.state.policy = policy or Policy(settings.opa_url)
    app.state.registry = registry or Registry(app.state.vault)
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

    @app.post("/api/v1/pair")
    def pair(body: PairRequest):
        verify(body.public_key, body.signature, ("homeai-pair:" + body.token).encode())
        with app.state.db() as db:
            invitation = db.scalar(select(Credential).where(Credential.digest == digest(body.token.encode())).with_for_update())
            if not invitation or invitation.kind != "pair" or invitation.expires_at <= now():
                raise HTTPException(401, "配对码失效")
            device = Device(id=uid(), user_id=invitation.user_id, public_key=body.public_key, name=body.name)
            db.add(device)
            token = credential(db, invitation.user_id, "access", settings.session_seconds, device.id)
            refresh = credential(db, invitation.user_id, "refresh", 30 * 86400, device.id)
            db.delete(invitation)
            db.commit()
            return {"access_token": token, "device_id": device.id, "refresh_token": refresh, "expires_in": settings.session_seconds}

    @app.post("/api/v1/session/renew")
    def renew(request: Request, actor: Actor = auth):
        with app.state.db() as db:
            token = credential(db, actor.user_id, "access", settings.session_seconds, actor.device_id)
            incoming = request.headers.get("authorization", "").removeprefix("Bearer ")
            old = db.scalar(select(Credential).where(Credential.digest == digest(incoming.encode())).with_for_update())
            if not old or old.expires_at <= now():
                raise HTTPException(401, "刷新凭据已失效")
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
            audit(db, actor, "device.revoke", device_id)
            db.commit()
            return {"revoked": True}

    @app.post("/api/v1/data/sync")
    def sync(body: SyncBatch, actor: Actor = auth):
        with app.state.db() as db:
            results = [serialize(ingest(db, actor, item, v), v) for item in body.records]
            db.commit()
            return {"records": results, "acknowledged": len(results)}

    @app.get("/api/v1/data")
    def records(actor: Actor = auth, after: str = "", limit: int = 100):
        with app.state.db() as db:
            rows = db.scalars(accessible(db, actor).where(Record.id > after).order_by(Record.id).limit(min(max(limit, 1), 200))).all()
            return {"records": [serialize(r, v) for r in rows], "next_cursor": rows[-1].id if rows else None}

    @app.get("/api/v1/data/changes")
    def changes(after: int = 0, actor: Actor = auth):
        with app.state.db() as db:
            scope(db, actor.user_id, actor.household_id)
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

    @app.put("/api/v1/data/{record_id}/grants/{user_id}")
    def share(record_id: str, user_id: str, actor: Actor = auth):
        with app.state.db() as db:
            record = own(db, Record, record_id, actor)
            subject = db.get(Principal, user_id)
            if not subject or subject.household_id != actor.household_id or record.deleted:
                raise HTTPException(404, "成员或数据不存在")
            if record.sensitivity == "SECRET":
                raise HTTPException(403, "秘密不能共享")
            existing = db.scalar(select(Grant).where(Grant.record_id == record.id, Grant.grantee_id == user_id))
            if not existing:
                db.add(Grant(household_id=actor.household_id, owner_id=actor.user_id, record_id=record.id, grantee_id=user_id))
            audit(db, actor, "data.share", record.id)
            db.commit()
            return {"shared": True}

    @app.delete("/api/v1/data/{record_id}/grants/{user_id}")
    def unshare(record_id: str, user_id: str, actor: Actor = auth):
        with app.state.db() as db:
            own(db, Record, record_id, actor)
            db.execute(delete(Grant).where(Grant.record_id == record_id, Grant.grantee_id == user_id))
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
            record = ingest(db, actor, DataRecord(source="files", source_id=digest(content), kind="document.file", version=1, payload={"name": Path(file.filename or "附件").name, "size": len(content), "sha256": digest(content)}), v)
            directory = settings.state_dir / "blobs"
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / record.id
            target.write_text(v.seal(base64.b64encode(content).decode(), actor.user_id + ":blob:" + record.id))
            target.chmod(0o600)
            db.commit()
            return serialize(record, v)

    @app.post("/api/v1/tasks", status_code=202)
    def create_task(body: TaskRequest, actor: Actor = auth):
        with app.state.db() as db:
            task = submit(db, actor, body, v)
            db.commit()
            return {"id": task.id, "status": task.status}

    def task_view(task):
        payload = v.open(task.request, task.owner_id + ":task:" + task.id)
        return {"id": task.id, "status": task.status, "error": task.error, "result": v.open(task.result, task.owner_id + ":task-result:" + task.id) if task.result else None, "execution": {"agent": bool(payload.get("_agent")), "planned_steps": len(payload.get("steps", [])), "max_steps": payload.get("max_steps", 8), "model_rounds": payload.get("_model_rounds", 0), "model_token_charge": payload.get("_model_token_charge", 0), "max_model_tokens": payload.get("max_model_tokens"), "deadline": task.deadline}}

    @app.get("/api/v1/tasks/{task_id}")
    def get_task(task_id: str, actor: Actor = auth):
        with app.state.db() as db:
            return task_view(own(db, Task, task_id, actor))

    @app.post("/api/v1/tasks/{task_id}/cancel")
    def cancel(task_id: str, actor: Actor = auth):
        with app.state.db() as db:
            task = own(db, Task, task_id, actor)
            task.cancel_requested = True
            if task.status in {"RECEIVED", "AWAITING_APPROVAL", "APPROVED"}:
                task.status = "CANCELED"
            db.commit()
            return task_view(task)

    @app.get("/api/v1/approvals")
    def approvals(actor: Actor = auth):
        with app.state.db() as db:
            scope(db, actor.user_id, actor.household_id)
            rows = db.scalars(select(Approval).where(Approval.owner_id == actor.user_id, Approval.decision == "PENDING", Approval.expires_at > now())).all()
            result = []
            for row in rows:
                inv = db.get(Invocation, row.invocation_id)
                result.append({"id": row.id, "capability": inv.capability, "arguments": v.open(inv.arguments, actor.user_id + ":invocation:" + inv.id), "expires_at": row.expires_at})
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
            return [{"id": a.id, "name": a.name, "cron": a.cron, "enabled": a.enabled, "next_run": a.next_run} for a in db.scalars(select(Automation).where(Automation.owner_id == actor.user_id))]

    @app.post("/api/v1/automations")
    def create_automation(body: AutomationInput, actor: Actor = auth):
        try:
            tz = ZoneInfo(body.timezone)
            next_run = croniter(body.cron, datetime.now(tz)).get_next(float)
        except Exception:
            raise HTTPException(422, "无效的时区或 Cron") from None
        if any(s.capability not in CAPABILITIES for s in body.skill.steps):
            raise HTTPException(422, "Skill 包含未知能力")
        if any(s.capability in {"memory.semantic.index@v1", "memory.semantic.purge@v1", "memory.graph.index@v1", "memory.graph.purge@v1"} for s in body.skill.steps):
            raise HTTPException(403, "Skill 不能直接修改派生索引")
        with app.state.db() as db:
            scope(db, actor.user_id, actor.household_id)
            row = Automation(id=uid(), household_id=actor.household_id, owner_id=actor.user_id, name=body.name, cron=body.cron, timezone=body.timezone, skill="", enabled=body.enabled, next_run=next_run)
            row.skill = v.seal({**body.skill.model_dump(), "device_id": actor.device_id}, actor.user_id + ":automation:" + row.id)
            db.add(row)
            db.commit()
            return {"id": row.id}

    @app.delete("/api/v1/automations/{automation_id}")
    def stop_automation(automation_id: str, actor: Actor = auth):
        with app.state.db() as db:
            row = own(db, Automation, automation_id, actor)
            row.enabled = False
            db.commit()
            return {"enabled": False}

    from .task_control import router as task_control_router
    app.include_router(task_control_router)

    from .memory import router as memory_router
    app.include_router(memory_router)
    from .admin import router as admin_router
    app.include_router(admin_router)
    from .browser_auth import router as browser_router
    app.include_router(browser_router)
    from .management import router as management_router
    app.include_router(management_router)
    from .remote import router as remote_router
    app.include_router(remote_router)
    from fastapi.staticfiles import StaticFiles
    admin_dist = Path(__file__).resolve().parents[2] / "admin-web" / "dist"
    if admin_dist.is_dir():
        app.mount("/admin", StaticFiles(directory=admin_dist, html=True), name="admin-web")
    return app
