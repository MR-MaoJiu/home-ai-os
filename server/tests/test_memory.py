from test_security_data import put


def test_candidate_needs_confirmation(system,alice):
    source=put(alice)
    candidate=alice.request('POST','/api/v1/memory/candidates',{'source_ids':[source],'content':'喜欢下午开会'})
    assert candidate.status_code==200,candidate.text
    cid=candidate.json()['id']
    assert alice.request('GET','/api/v1/memory/search?q=开会').json()['records']==[]
    confirmed=alice.request('POST',f'/api/v1/memory/candidates/{cid}/confirm')
    assert confirmed.status_code==200,confirmed.text
    assert len(alice.request('GET','/api/v1/memory/search?q=开会').json()['records'])==1
    assert alice.request('POST',f'/api/v1/memory/candidates/{cid}/confirm').status_code==409


def test_delete_source_removes_derived_fact(alice):
    source=put(alice)
    candidate=alice.request('POST','/api/v1/memory/candidates',{'source_ids':[source],'content':'来源派生的私人内容'}).json()['id']
    fact=alice.request('POST',f'/api/v1/memory/candidates/{candidate}/confirm').json()['record_id']
    deleted=alice.request('DELETE','/api/v1/data/'+source)
    assert deleted.status_code==200,deleted.text
    assert fact in deleted.json()['deleted_ids']
    assert alice.request('GET','/api/v1/data/'+fact).status_code==404
    assert alice.request('GET','/api/v1/memory/candidates').json()==[]


def test_index_status_scoped_and_rebuild_requires_auth(system, alice):
    from conftest import SignedClient
    from homeai.db import MemoryVector, scope
    from sqlalchemy import select
    bob = SignedClient(system[1], system[2])
    rid = put(alice, {'source':'manual','source_id':'status-fact','kind':'memory.fact','version':1,'payload':{'content':'我的记忆'}})
    assert alice.request('GET', '/api/v1/memory/index').json()['eligible'] == 1
    assert bob.request('GET', '/api/v1/memory/index').json()['eligible'] == 0
    assert system[1].post('/api/v1/memory/index/rebuild').status_code == 401
    assert bob.request('POST', '/api/v1/memory/index/rebuild').status_code == 200
    assert alice.request('GET', '/api/v1/data/' + rid).status_code == 200


def test_reject_invalid_embedding():
    import pytest
    from homeai.vector_index import vector_value
    for values in ([True, False], [0.0, 0.0], [float('nan')], [float('inf')], []):
        with pytest.raises(ValueError):
            vector_value({'data': [{'embedding': values}]})


def test_derived_checkpoint_rejects_changed_canonical_data(system, alice):
    import pytest
    from fastapi import HTTPException
    from homeai.contracts import ProviderManifest
    from homeai.db import DerivedJob, scope
    from homeai.security import Actor
    from homeai.derived_memory import checkpoint, job_id, require_ready
    manifest = ProviderManifest(id='memory.checkpoint', version='1', adapter='http', endpoint='http://127.0.0.1:8090', allowed_hosts=['127.0.0.1'], capabilities={'memory.semantic.search@v1': '/invoke/search'})
    actor = Actor(alice.user_id, 'h1', alice.device_id, 'adult')
    with system[2]() as db:
        scope(db, actor.user_id, actor.household_id)
        with pytest.raises(HTTPException):
            require_ready(db, actor, manifest)
        db.add(DerivedJob(id=job_id(actor.user_id, manifest.id), owner_id=actor.user_id, household_id=actor.household_id, provider_id=manifest.id, event_id=checkpoint(db, actor.user_id, manifest), status='READY'))
        db.commit()
        require_ready(db, actor, manifest)
    rid = put(alice)
    with system[2]() as db:
        scope(db, actor.user_id, actor.household_id)
        with pytest.raises(HTTPException):
            require_ready(db, actor, manifest)


async def test_derived_worker_does_not_use_another_members_secret(system,alice):
    from homeai.db import Provider,Secret,DerivedJob,scope
    from homeai.contracts import ProviderManifest
    from homeai.derived_memory import reconcile_provider,job_id
    manifest=ProviderManifest(id='foreign.memory',version='1',adapter='http',endpoint='http://127.0.0.1:9',allowed_hosts=['127.0.0.1'],secret_id='foreign.key',capabilities={'memory.semantic.index@v1':'/invoke/index','memory.semantic.purge@v1':'/invoke/purge'})
    with system[2]() as db:
        db.add(Secret(id='foreign.key',owner_id='another-member',household_id='h1',provider_id=manifest.id,value='not-readable'))
        db.add(Provider(id=manifest.id,manifest=manifest.model_dump_json(),enabled=True))
        db.commit()
    assert alice.request('GET','/api/v1/memory/derived').json()==[]
    assert alice.request('POST','/api/v1/memory/derived/'+manifest.id+'/rebuild').status_code==403
    await reconcile_provider(system[0].state,alice.user_id,'h1',manifest.id)
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        assert db.get(DerivedJob,job_id(alice.user_id,manifest.id)) is None
