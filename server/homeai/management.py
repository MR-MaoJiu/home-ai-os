from pathlib import Path
from fastapi import APIRouter, Depends, Request, HTTPException
from sqlalchemy import select,func
from .security import Actor,authenticate,owner
from .db import Record,Task,Device,Provider,Principal,scope
router=APIRouter(prefix='/api/v1/manage',tags=['管理概览'])

@router.get('/overview')
def overview(request:Request,actor:Actor=Depends(authenticate)):
    with request.app.state.db() as db:
        scope(db,actor.user_id,actor.household_id)
        return {'records':db.scalar(select(func.count()).select_from(Record).where(Record.owner_id==actor.user_id,Record.deleted.is_(False))), 'tasks':db.scalar(select(func.count()).select_from(Task).where(Task.owner_id==actor.user_id)), 'devices':db.scalar(select(func.count()).select_from(Device).where(Device.user_id==actor.user_id,Device.revoked.is_(False))), 'environment':request.app.state.settings.environment,'role':actor.role}

@router.get('/tasks')
def tasks(request:Request,actor:Actor=Depends(authenticate)):
    with request.app.state.db() as db:
        scope(db,actor.user_id,actor.household_id)
        return [{'id':t.id,'status':t.status,'error':t.error,'created_at':t.created_at} for t in db.scalars(select(Task).where(Task.owner_id==actor.user_id).order_by(Task.created_at.desc()).limit(100))]

@router.get('/backups')
def backups(request:Request,actor:Actor=Depends(authenticate)):
    owner(actor)
    directory=request.app.state.settings.state_dir/'backups'
    return [{'name':p.name,'bytes':p.stat().st_size,'modified_at':p.stat().st_mtime} for p in sorted(directory.glob('*.haib')) if p.is_file() and not p.is_symlink()]

@router.get('/readiness')
def readiness(request:Request,actor:Actor=Depends(authenticate)):
    owner(actor)
    settings=request.app.state.settings
    with request.app.state.db() as db:
        db.execute(select(1))
    return {'database':True,'master_key_loaded':True,'environment':settings.environment,'production_sandbox_verified':False,'remote_configured':False}
