import pytest
from fastapi import HTTPException
from homeai.planner import decode_proposal


def proposal(name,args):return {'choices':[{'message':{'tool_calls':[{'function':{'name':name,'arguments':args}}]}}]}

def test_model_cannot_expand_tools_or_arguments():
    assert decode_proposal(proposal('create_reminder','{"title":"买牛奶"}'))==('reminder.create@v1',{'title':'买牛奶'})
    with pytest.raises(HTTPException):decode_proposal(proposal('shell','{"command":"x"}'))
    with pytest.raises(HTTPException):decode_proposal(proposal('create_reminder','{"title":"x","owner_id":"someone-else"}'))
    with pytest.raises(HTTPException):decode_proposal(proposal('create_reminder','{"title":{}}'))
