"""导出或校验公共契约，不连接数据库、不读取实际主密钥。"""
import argparse
import json
from pathlib import Path
from homeai.api import create_app
from homeai.config import Settings
from homeai.crypto import Vault
from homeai.contracts import DataRecord, TaskRequest, ProviderManifest, Skill, TaskStateNotification
from homeai.chat_contracts import ClientContext, MessagePart, Mention, DataReference
from homeai.chat_outputs import ChatOutputPart, SourceReference, CloudCapabilitiesReport
from homeai.conversations import Message
from homeai.media import UploadInput
from homeai.model_routing import ConfigurationInput
from homeai.client_actions import ResponseInput, DenialInput
from homeai.auto_review import AutoReviewInput
from homeai.shell_tools import ShellRequest, HostRequest
from homeai.host_inspection_tools import HostInspectionRequest
from homeai.android_workspace import InputRequest as AndroidInputRequest, ControlRequest as AndroidControlRequest, BindingRequest as AndroidBindingRequest, InputFocusRequest
from homeai.android_stream import StreamOffer, DisplayRequest
from homeai.android_gestures import GesturePacket
from homeai.android_tools import AndroidLaunch

MODELS = {
    **{model.__name__: model for model in (DataRecord, TaskRequest, ProviderManifest, Skill, TaskStateNotification, ClientContext, MessagePart, Mention, DataReference, ChatOutputPart, SourceReference, CloudCapabilitiesReport)},
    'ConversationMessage': Message,
    'MediaUploadRequest': UploadInput,
    'ModelRoutingConfiguration': ConfigurationInput,
    'ClientActionResponse': ResponseInput,
    'ClientActionDenial': DenialInput,
    'DeviceAutoReviewInput': AutoReviewInput,
    'ShellRunRequest': ShellRequest,
    'HostActionRequest': HostRequest,
    'HostInspectionRequest': HostInspectionRequest,
    'AndroidInputRequest': AndroidInputRequest,
    'AndroidControlRequest': AndroidControlRequest,
    'AndroidBindingRequest': AndroidBindingRequest,
    'AndroidStreamOffer': StreamOffer,
    'AndroidDisplayRequest': DisplayRequest,
    'AndroidInputFocusRequest': InputFocusRequest,
    'AndroidGesturePacket': GesturePacket,
    'AndroidLaunchRequest': AndroidLaunch,
}


def documents():
    app = create_app(Settings(environment='development'), Vault(b'0'*32), db_factory=lambda: None)
    result = {'openapi': app.openapi()}
    result.update({name: model.model_json_schema() for name, model in MODELS.items()})
    return {name+'.json': json.dumps(value, ensure_ascii=False, indent=2) for name, value in result.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='只校验已提交契约，发现差异时失败')
    args = parser.parse_args()
    target = Path('contracts')
    stale = []
    for name, content in documents().items():
        path = target / name
        if args.check:
            if not path.is_file() or path.read_text() != content:stale.append(name)
        else:
            target.mkdir(exist_ok=True)
            path.write_text(content)
    if stale:
        raise SystemExit('公共契约未更新：'+'、'.join(stale)+'；请运行 scripts/export_contracts.py')
    print('公共契约校验通过' if args.check else '公共契约已导出')


if __name__ == '__main__':
    main()
