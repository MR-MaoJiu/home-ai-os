"""无向量模型时的有限主题检索；测试数据均在独立临时数据库。"""
import pytest

from conftest import SignedClient
from homeai.db import Record, scope
from homeai.memory import literal_search
from homeai.security import Actor
from test_auto_memory import learn
from test_security_data import put, record


def search(alice, query):
    response=alice.request('GET','/api/v1/memory/search?q='+__import__('urllib.parse',fromlist=['quote']).quote(query))
    assert response.status_code==200,response.text
    assert response.json()['mode']=='authorized_literal'
    return response.json()['records']


def stored(alice,content,**kwargs):
    return put(alice,{**record(kind='memory.fact',source_id=content,payload={'content':content}),**kwargs})


@pytest.mark.parametrize('query',['你还记得我叫什么吗？','我的昵称是什么','应该怎么称呼我','preferred_name'])
def test_natural_member_name_question_only_returns_addressing(system,alice,query):
    names=learn(system,alice,'你叫小竹，我叫小林')['record_id']
    unrelated=stored(alice,'我的健康记录：不应因“我”这个词被检索出来',sensitivity='SENSITIVE')
    bob=SignedClient(system[1],system[2])
    learn(system,bob,'你叫小河，我叫小桥')
    assert [item['id'] for item in search(alice,query)]==[names]
    assert unrelated not in [item['id'] for item in search(alice,query)]


@pytest.mark.parametrize('query',['你叫什么名字','我该怎么叫你','你的昵称是什么','assistant_name'])
def test_assistant_name_question_has_correct_direction(system,alice,query):
    assistant=learn(system,alice,'你叫小竹')['record_id']
    stored(alice,'我叫其他名字是测试句，并非完整称呼设置')
    assert [item['id'] for item in search(alice,query)]==[assistant]


def test_topic_query_and_chinese_terms_find_actual_content_without_full_sentence_match(system,alice):
    beverage=learn(system,alice,'我喜欢喝茶')['record_id']
    sport=learn(system,alice,'我喜欢游泳')['record_id']
    paper=stored(alice,'我通常选择哑光纸张打印照片')
    stored(alice,'我有花生过敏',sensitivity='SENSITIVE')
    assert [item['id'] for item in search(alice,'你还记得我平时喝什么吗')]==[beverage]
    assert [item['id'] for item in search(alice,'茶')]==[beverage]
    assert [item['id'] for item in search(alice,'我喜欢什么运动')]==[sport]
    assert [item['id'] for item in search(alice,'关于哑光纸的偏好')]==[paper]
    assert [item['id'] for item in search(alice,'哑光纸张选择')]==[paper]


@pytest.mark.parametrize('query',['','我的记忆','你还记得吗','不存在的星际主题'])
def test_empty_or_unspecific_queries_do_not_dump_all_memories(system,alice,query):
    learn(system,alice,'我喜欢喝茶')
    stored(alice,'我有花生过敏',sensitivity='SENSITIVE')
    assert search(alice,query)==[]


def test_literal_search_filters_owner_deleted_secret_and_metadata_matches(system,alice):
    personal=stored(alice,'我喜欢周末徒步')
    secret=stored(alice,'周末徒步的秘密',sensitivity='SECRET')
    deleted=stored(alice,'周末徒步旧习惯')
    other_house=SignedClient(system[1],system[2],household='h2')
    stored(other_house,'我也喜欢周末徒步')
    ordinary=put(alice,record(kind='document.text',source_id='document',payload={'content':'周末徒步资料'}))
    metadata=put(alice,record(kind='memory.fact',source_id='metadata-only',payload={'content':'没有目标词的另一项偏好','turn_id':'private-metadata-marker'}))
    with system[2]() as db:
        scope(db,alice.user_id,'h1');db.get(Record,deleted).deleted=True;db.commit()
    assert [item['id'] for item in search(alice,'徒步')]==[personal]
    assert search(alice,'private-metadata-marker')==[]


def test_results_are_bounded_and_exact_subject_ranks_first(system,alice):
    for index in range(24):stored(alice,f'跑步路线习惯{index}')
    exact=stored(alice,'我喜欢晨间跑步路线')
    results=search(alice,'晨间跑步路线')
    assert len(results)==8 and results[0]['id']==exact
    assert len(search(alice,'跑步'))==8
    actor=Actor(alice.user_id,'h1',alice.device_id,'adult')
    with system[2]() as db:
        assert len(literal_search(system[0].state,db,actor,'跑步',limit=100))==20
