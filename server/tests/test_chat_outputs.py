"""公共输出结构与客户端输入边界；不把输出模型当成资源权限检查或HTML清洗器。"""
import pytest
from pydantic import ValidationError
from homeai.chat_contracts import MessagePart
from homeai.chat_outputs import ChatOutputPart, SourceReference, CloudCapabilitiesReport
from homeai.presentations import render


@pytest.mark.parametrize('value',[
    {'type':'text','text':'真实服务端回答'},
    {'type':'image','record_id':'asset-1','version':1},
    {'type':'video','record_id':'asset-2','version':2,'task_id':'authorized-task'},
    {'type':'file','record_id':'asset-3','version':3,'title':'附件'},
    {'type':'task','task_id':'task-1','status':'WAITING_CLIENT','summary':'等待本人授权'},
    {'type':'choice','action_id':'action-1'},
    {'type':'approval','action_id':'action-2','task_id':'task-2'},
    {'type':'data_request','action_id':'action-3','title':'选择资料'},
])
def test_registered_output_shapes_roundtrip(value):
    part=ChatOutputPart.model_validate(value)
    assert part.model_dump(exclude_none=True)==value
    assert ChatOutputPart.model_validate_json(part.model_dump_json())==part


def test_server_table_template_output_and_multibyte_size_limit():
    result=render({'title':'服务端表格','columns':['资料'],'rows':[['<script>不会执行</script>']]})
    part=ChatOutputPart.model_validate(result['parts'][0])
    assert part.template=='table.v1'
    assert '<script>' not in part.html and '&lt;script&gt;' in part.html
    with pytest.raises(ValidationError):
        ChatOutputPart(type='h5',template='table.v1',html='中'*22000)


@pytest.mark.parametrize('value',[
    {'type':'text','text':'  '},
    {'type':'image','record_id':'asset-1'},
    {'type':'video','record_id':'asset-1','version':True},
    {'type':'file','record_id':'asset-1','version':0},
    {'type':'approval'},
    {'type':'data_request','action_id':'a','html':'<script>bad</script>'},
    {'type':'task','status':'RECEIVED'},
    {'type':'h5','html':'<p>没有注册模板</p>'},
    {'type':'h5','html':'<p>未知模板</p>','template':'arbitrary-code.v1'},
    {'type':'iframe','url':'https://external.invalid'},
    {'type':'text','text':'示例','script':'alert(1)'},
])
def test_incomplete_mixed_or_unregistered_outputs_are_rejected(value):
    with pytest.raises(ValidationError):ChatOutputPart.model_validate(value)


@pytest.mark.parametrize('type',['task','choice','approval','data_request','h5'])
def test_server_output_types_are_not_accepted_as_client_input(type):
    with pytest.raises(ValidationError):MessagePart.model_validate({'type':type})


def test_source_reference_and_capabilities_are_explicit():
    source=SourceReference(record_id='record-1',version=3,title='来源',task_id='scoped-task')
    assert source.version==3 and source.task_id=='scoped-task'
    with pytest.raises(ValidationError):SourceReference(record_id='record-1',version='3',title='来源')
    with pytest.raises(ValidationError):SourceReference(record_id='record-1',version=1,title='来源',owner_id='spoof')
    report=CloudCapabilitiesReport(text=True,tools=True,vision=False,privacy=False,status='verified',verified_at=100,
        resolved_model='provider-reported-model',errors={'vision':'http_400','privacy':'not_applicable'})
    assert report.vision is False and report.tools is True
    with pytest.raises(ValidationError):CloudCapabilitiesReport(text='true',tools=True,vision=False,privacy=False,status='verified')
    with pytest.raises(ValidationError):CloudCapabilitiesReport(text=True,tools=False,vision=False,privacy=False,status='verified',verified_at=float('nan'))
    with pytest.raises(ValidationError):CloudCapabilitiesReport(text=True,tools=False,vision=False,status='configured')


def test_exported_json_schema_enforces_per_type_required_fields():
    from jsonschema import Draft202012Validator
    schema=ChatOutputPart.model_json_schema()
    Draft202012Validator.check_schema(schema)
    validator=Draft202012Validator(schema)
    for valid in ({'type':'text','text':'回答'}, {'type':'image','record_id':'r','version':1},
                  {'type':'approval','action_id':'a'}, {'type':'h5','template':'table.v1','html':'<p>由模板生成</p>'}):
        assert validator.is_valid(valid)
        assert validator.is_valid(ChatOutputPart.model_validate(valid).model_dump())
    for invalid in ({'type':'file'}, {'type':'approval','action_id':None}, {'type':'task'},
                    {'type':'text','text':'  '}, {'type':'h5','html':'<p>缺少模板</p>'}):
        assert not validator.is_valid(invalid)
