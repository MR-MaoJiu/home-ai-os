"""Core 内的家居观察者：租约排他、按成员持久化、失联后重新快照。"""
import asyncio
import hashlib
import json
from fastapi import HTTPException
from sqlalchemy import select
from .contracts import ProviderManifest
from .db import Principal, Provider, Secret, HomeConnection, HomeObservation, scope, now, uid
from .security import Actor
from .crypto import canonical
from .data import emit
from .home_events import events


def binding(db, actor, provider_id):
    principal = db.get(Principal, actor.user_id, populate_existing=True)
    if not principal or principal.household_id != actor.household_id or principal.role != actor.role:
        raise HTTPException(403, '家居观察身份已变化')
    scope(db, actor.user_id, actor.household_id)
    provider = db.get(Provider, provider_id, populate_existing=True)
    if not provider or not provider.enabled:
        raise HTTPException(403, '家居 Provider 已停用')
    manifest = ProviderManifest.model_validate_json(provider.manifest)
    if manifest.adapter != 'homeassistant' or not manifest.home_events or not manifest.home_entities or not manifest.secret_id or 'home.states@v1' not in manifest.capabilities:
        raise HTTPException(403, '家居事件未授权')
    secret = db.get(Secret, manifest.secret_id, populate_existing=True)
    if not secret or secret.owner_id != actor.user_id or secret.provider_id != provider_id:
        raise HTTPException(403, '家居凭据未授权')
    signature = hashlib.sha256(canonical({'manifest': manifest.model_dump(), 'household': actor.household_id, 'role': actor.role}) + secret.value.encode()).hexdigest()
    return manifest, secret.value, signature


def lease(app, actor, provider_id, worker_id, signature, status='CONNECTED', error=None):
    with app.db() as db:
        _, _, current = binding(db, actor, provider_id)
        if current != signature:
            raise HTTPException(409, '家居配置已变化')
        row = db.scalar(select(HomeConnection).where(HomeConnection.owner_id == actor.user_id, HomeConnection.provider_id == provider_id).with_for_update())
        if row and row.lease_owner != worker_id and row.lease_until > now():
            return False
        if not row:
            row = HomeConnection(owner_id=actor.user_id, household_id=actor.household_id, provider_id=provider_id, status=status)
            db.add(row)
        row.status, row.error_type, row.updated_at = status, error, now()
        row.lease_owner, row.lease_until = worker_id, now() + 30
        db.commit()
        return True


def store(app, actor, provider_id, worker_id, signature, batch):
    with app.db() as db:
        manifest, _, current = binding(db, actor, provider_id)
        if current != signature:
            raise HTTPException(409, '家居配置已变化')
        connection = db.scalar(select(HomeConnection).where(HomeConnection.owner_id == actor.user_id, HomeConnection.provider_id == provider_id).with_for_update())
        if not connection or connection.lease_owner != worker_id or connection.lease_until <= now():
            raise HTTPException(409, '家居观察租约已失效')
        for state in batch['states']:
            entity = state['entity_id']
            if entity not in manifest.home_entities:
                raise HTTPException(403, '家居实体授权已撤回')
            row = db.scalar(select(HomeObservation).where(HomeObservation.owner_id == actor.user_id, HomeObservation.provider_id == provider_id, HomeObservation.entity_id == entity).with_for_update())
            if row:
                previous = app.vault.open(row.payload, actor.user_id + ':home:' + row.id)
                if previous == state:
                    row.observed_at = now()
                    continue
                row.revision += 1
            else:
                row = HomeObservation(id=uid(), owner_id=actor.user_id, household_id=actor.household_id, provider_id=provider_id, entity_id=entity, payload='', revision=1)
                db.add(row)
            row.payload = app.vault.seal(state, actor.user_id + ':home:' + row.id)
            row.observed_at = now()
            # 独立事件命名空间；跨系统因果关系未绑定前，不冒充 record.changed 自动化输入。
            emit(db, actor, 'home.state_changed', row.id)
        connection.status, connection.updated_at = 'CONNECTED', now()
        db.commit()


async def watch(app, actor, provider_id, worker_id=None):
    worker_id = worker_id or uid()
    retry = 0
    while True:
        iterator = None
        receive = None
        has_snapshot = False
        failure = 'connection_closed'
        try:
            with app.db() as db:
                manifest, encrypted, signature = binding(db, actor, provider_id)
                token = app.vault.open(encrypted, actor.user_id + ':secret:' + manifest.secret_id)
            if not lease(app, actor, provider_id, worker_id, signature, 'CONNECTING'):
                await asyncio.sleep(5)
                continue
            await app.policy.check(actor, 'home.states@v1')
            iterator = events(manifest, token)
            receive = asyncio.create_task(anext(iterator))
            while True:
                done, _ = await asyncio.wait({receive}, timeout=5)
                if not lease(app, actor, provider_id, worker_id, signature, 'CONNECTED' if has_snapshot else 'CONNECTING'):
                    raise RuntimeError('家居租约由其他进程接管')
                await app.policy.check(actor, 'home.states@v1')
                if done:
                    batch = receive.result()
                    store(app, actor, provider_id, worker_id, signature, batch)
                    retry = 0
                    has_snapshot = True
                    receive = asyncio.create_task(anext(iterator))
        except asyncio.CancelledError:
            raise
        except HTTPException as exc:
            failure = 'HTTP_' + str(exc.status_code)
            if exc.status_code in {401, 403}:
                return
            retry += 1
        except Exception as exc:
            failure = type(exc).__name__
            retry += 1
        finally:
            if receive:
                receive.cancel()
                await asyncio.gather(receive, return_exceptions=True)
            if iterator:
                await iterator.aclose()
            try:
                with app.db() as db:
                    scope(db, actor.user_id, actor.household_id)
                    row = db.scalar(select(HomeConnection).where(HomeConnection.owner_id == actor.user_id, HomeConnection.provider_id == provider_id).with_for_update())
                    if row and row.lease_owner == worker_id:
                        row.status, row.error_type, row.updated_at = 'DISCONNECTED', failure, now()
                        row.lease_until = 0
                        db.commit()
            except Exception:
                pass
        await asyncio.sleep(min(30, 2 ** min(retry, 5)))


async def main():
    from .api import create_app
    app = create_app().state
    workers = {}
    try:
        while True:
            targets = {}
            with app.db() as db:
                for user in list(db.scalars(select(Principal))):
                    actor = Actor(user.id, user.household_id, 'home-observer', user.role)
                    scope(db, user.id, user.household_id)
                    for provider in list(db.scalars(select(Provider).where(Provider.enabled.is_(True)))):
                        try:
                            _, _, signature = binding(db, actor, provider.id)
                            targets[(user.id, provider.id)] = (actor, signature)
                        except HTTPException:
                            continue
            for key, (task, signature) in list(workers.items()):
                if key not in targets or targets[key][1] != signature:
                    task.cancel(); await asyncio.gather(task, return_exceptions=True); del workers[key]
            for key, (actor, signature) in targets.items():
                if key not in workers:
                    workers[key] = (asyncio.create_task(watch(app, actor, key[1])), signature)
            await asyncio.sleep(5)
    finally:
        for task, _ in workers.values(): task.cancel()
        await asyncio.gather(*(task for task, _ in workers.values()), return_exceptions=True)


from fastapi import APIRouter, Depends, Request
from .security import authenticate
router = APIRouter(prefix='/api/v1/home', tags=['家居观察'])


@router.get('/observations')
def observations(request: Request, actor: Actor = Depends(authenticate)):
    app = request.app.state
    with app.db() as db:
        scope(db, actor.user_id, actor.household_id)
        result = []
        for provider in list(db.scalars(select(Provider).where(Provider.enabled.is_(True)))):
            try:
                manifest, _, _ = binding(db, actor, provider.id)
            except HTTPException:
                continue
            connection = db.scalar(select(HomeConnection).where(HomeConnection.owner_id == actor.user_id, HomeConnection.provider_id == provider.id))
            status = connection.status if connection and connection.lease_until > now() else 'DISCONNECTED'
            rows = db.scalars(select(HomeObservation).where(HomeObservation.owner_id == actor.user_id, HomeObservation.provider_id == provider.id, HomeObservation.entity_id.in_(manifest.home_entities)))
            result.append({'provider_id': provider.id, 'status': status,
                'updated_at': connection.updated_at if connection else None,
                'error_type': connection.error_type if connection else None,
                'states': [{'revision': row.revision, 'observed_at': row.observed_at,
                            'state': app.vault.open(row.payload, actor.user_id + ':home:' + row.id)} for row in rows]})
        return result


if __name__ == '__main__':
    asyncio.run(main())
