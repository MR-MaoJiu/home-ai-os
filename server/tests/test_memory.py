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
