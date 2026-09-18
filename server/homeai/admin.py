import json
from fastapi import APIRouter, Depends, Request, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from .security import Actor, authenticate, owner, credential
from .db import Principal, Secret, Provider, uid, scope
from .data import audit

router=APIRouter(prefix='/api/v1',tags=['管理'])


class MemberInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name:str=Field(min_length=1,max_length=100)


@router.post('/members')
def invite(body:MemberInput,request:Request,actor:Actor=Depends(authenticate)):
    owner(actor)
    with request.app.state.db() as db:
        scope(db,actor.user_id,actor.household_id)
        user=Principal(id=uid(),household_id=actor.household_id,name=body.name,role='adult')
        db.add(user)
        db.flush()
        audit(db,actor,'member.invite',user.id)
        db.commit()
        return {'user_id':user.id}


@router.get('/members')
def members(request:Request,actor:Actor=Depends(authenticate)):
    with request.app.state.db() as db:
        return [{'id':u.id,'name':u.name,'role':u.role} for u in db.scalars(select(Principal).where(Principal.household_id==actor.household_id))]


class SecretInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    provider_id:str
    value:str=Field(min_length=1,max_length=8192)


@router.post('/secrets')
def secret(body:SecretInput,request:Request,actor:Actor=Depends(authenticate)):
    with request.app.state.db() as db:
        scope(db,actor.user_id,actor.household_id)
        if not db.get(Provider,body.provider_id):raise HTTPException(404,'Provider 不存在')
        row=Secret(id=uid(),household_id=actor.household_id,owner_id=actor.user_id,provider_id=body.provider_id,value='')
        row.value=request.app.state.vault.seal(body.value,actor.user_id+':secret:'+row.id)
        db.add(row)
        audit(db,actor,'secret.create',row.id)
        db.commit()
        return {'id':row.id,'provider_id':row.provider_id}
