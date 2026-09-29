"""服务端富消息输出契约。只验证结构，不授权资源访问，也不替代受控 HTML 模板。"""
from typing import Annotated, Literal
from pydantic import ConfigDict, Field, StrictBool, StrictInt, model_validator
from .contracts import Contract

Identifier = Annotated[str, Field(min_length=1, max_length=100)]


class ChatOutputPart(Contract):
    """客户端输入仍使用 MessagePart；此模型只供服务端注册的输出构造器使用。"""
    model_config = ConfigDict(extra='forbid', json_schema_extra={'allOf': [
        {'if':{'properties':{'type':{'const':'text'}}},
         'then':{'required':['text'],'properties':{'text':{'type':'string','minLength':1,'pattern':r'\S'}}},
         'else':{'properties':{'text':{'type':'null'}}}},
        {'if':{'properties':{'type':{'enum':['image','video','file']}}},
         'then':{'required':['record_id','version'],'properties':{'record_id':{'type':'string','minLength':1},'version':{'type':'integer','minimum':1}}},
         'else':{'properties':{'record_id':{'type':'null'},'version':{'type':'null'}}}},
        {'if':{'properties':{'type':{'enum':['choice','approval','data_request']}}},
         'then':{'required':['action_id'],'properties':{'action_id':{'type':'string','minLength':1}}},
         'else':{'properties':{'action_id':{'type':'null'}}}},
        {'if':{'properties':{'type':{'const':'task'}}},
         'then':{'required':['task_id'],'properties':{'task_id':{'type':'string','minLength':1}}}},
        {'if':{'properties':{'type':{'const':'h5'}}},
         'then':{'required':['html','template'],'properties':{'html':{'type':'string','minLength':1,'pattern':r'\S'},'template':{'const':'table.v1'}}},
         'else':{'properties':{'html':{'type':'null'},'template':{'type':'null'}}}},
    ]})
    type: Literal['text', 'image', 'video', 'file', 'task', 'choice', 'approval', 'data_request', 'h5']
    text: str | None = Field(default=None, max_length=20000)
    record_id: Identifier | None = None
    version: StrictInt | None = Field(default=None, ge=1)
    action_id: Identifier | None = None
    task_id: Identifier | None = None
    status: str | None = Field(default=None, min_length=1, max_length=80)
    title: str | None = Field(default=None, max_length=200)
    summary: str | None = Field(default=None, max_length=20000)
    html: str | None = Field(default=None, max_length=65536, description='仅服务端注册模板生成；UTF-8 编码最多 65536 字节', json_schema_extra={'x-max-utf8-bytes':65536})
    template: Literal['table.v1'] | None = None

    @model_validator(mode='after')
    def matching_content(self):
        media = self.type in {'image', 'video', 'file'}
        interactive = self.type in {'choice', 'approval', 'data_request'}
        if self.type == 'text':
            if not self.text or not self.text.strip():raise ValueError('文本输出不能为空')
        elif self.text is not None:
            raise ValueError('只有文本输出使用 text 字段')
        if media:
            if not self.record_id or self.version is None:raise ValueError('附件输出必须引用规范资源和版本')
        elif self.record_id is not None or self.version is not None:
            raise ValueError('只有附件输出使用 record_id 与 version')
        if interactive:
            if not self.action_id:raise ValueError('交互输出必须引用服务端请求')
        elif self.action_id is not None:
            raise ValueError('只有交互输出使用 action_id')
        if self.type == 'task' and not self.task_id:
            raise ValueError('任务输出必须引用服务端任务')
        if self.type == 'h5':
            if not self.html or not self.html.strip() or self.template is None:
                raise ValueError('H5 输出必须由已注册模板生成')
            if len(self.html.encode('utf-8')) > 65536:
                raise ValueError('H5 输出不能超过 64 KiB')
        elif self.html is not None or self.template is not None:
            raise ValueError('只有受控 H5 输出可以携带 HTML 与模板标识')
        return self


class SourceReference(Contract):
    record_id: Identifier
    version: StrictInt = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    task_id: Identifier | None = None


class CloudCapabilitiesReport(Contract):
    text: StrictBool
    tools: StrictBool
    vision: StrictBool
    privacy: StrictBool
    status: Literal['configured', 'verified', 'unavailable']
    verified_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    resolved_model: str | None = Field(default=None, max_length=500)
    errors: dict[Literal['text', 'tools', 'vision', 'privacy'], Annotated[str, Field(max_length=300)]] = Field(default_factory=dict)
