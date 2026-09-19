from conftest import SignedClient
from homeai.integrations import skill_context
from homeai.db import scope
from homeai.security import Actor


def test_builtin_catalog_without_installed_providers(system,alice):
    items=alice.request('GET','/api/v1/builtins').json()['items']
    assert len(items)==2 and all(i['status']=='not_installed' for i in items)
    assert alice.request('PUT','/api/v1/builtins/local-model',{'variant':'qwen3-4b','enabled':True}).status_code==202
    assert alice.request('PUT','/api/v1/builtins/local-model',{'variant':'../../evil','enabled':True}).status_code==422
    bob=SignedClient(system[1],system[2])
    assert bob.request('PUT','/api/v1/builtins/web-search',{'variant':'searxng','enabled':True}).status_code==403


def test_instruction_skill_scope_and_disable(system,alice):
    content='---\nname: simple\ndescription: 回答格式\n---\n适用时先给结论，再列出来源。'
    response=alice.request('POST','/api/v1/integrations/skills',{'content':content})
    assert response.status_code==200,response.text
    identifier=response.json()['id']
    assert alice.request('POST','/api/v1/integrations/skills/'+identifier+'/enable').status_code==200
    bob=SignedClient(system[1],system[2])
    with system[2]() as db:
        scope(db,bob.user_id,'h1')
        assert content in skill_context(system[0].state,db,Actor(bob.user_id,'h1',bob.device_id,'adult'))
        assert skill_context(system[0].state,db,Actor('outsider','different','device','adult'))==''
    assert bob.request('POST','/api/v1/integrations/skills/'+identifier+'/disable').status_code==403
    assert alice.request('POST','/api/v1/integrations/skills/'+identifier+'/disable').status_code==200
    with system[2]() as db:
        assert not skill_context(system[0].state,db,Actor(bob.user_id,'h1',bob.device_id,'adult'))
    assert alice.request('POST','/api/v1/integrations/skills',{'content':'bad'}).status_code==422
