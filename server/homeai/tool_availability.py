"""规划目录依据当前身份和配置生成；可见不替代执行时鉴权或实时故障处理。"""
from urllib.parse import urlparse
from fastapi import HTTPException
from .db import Secret
from .planner import TOOLS, MAPPING

OPTIONAL_TOOLS = {
    'call_user':'主动语音来电',
    'search_web':'联网搜索', 'read_home_states':'智能家居设备状态',
    'generate_image':'图片生成', 'edit_image':'图片编辑',
    'run_shell':'隔离 Shell', 'execute_host_action':'宿主管理',
    'inspect_server':'服务器信息读取',
    'open_android_app':'打开个人安卓应用', 'observe_android':'个人安卓设备', 'operate_android':'个人安卓操作',
}


def configured_capability(app, db, actor, capability):
    try:
        if capability == 'voice.call@v1':
            from sqlalchemy import select
            from .db import now
            from .voice_calls import preferences, configured, VoiceCall
            busy = db.scalar(select(VoiceCall.id).where(VoiceCall.owner_id == actor.user_id,
                VoiceCall.status.in_(['RINGING','ACTIVE']), VoiceCall.expires_at > now()).limit(1))
            return not busy and preferences(app,db,actor).incoming_enabled and all(item['configured'] for item in configured(app,db,actor).values())
        if capability in {'android.launch@v1', 'android.observe@v1', 'android.action@v1'}:
            from .android_tools import permitted
            return permitted(app, db, actor)
        if capability == 'host.inspect@v1':
            from .host_inspection_tools import allowed_targets
            return bool(allowed_targets(app,actor))
        if capability in {'shell.run@v1','host.execute@v1'}:
            from .shell_tools import permitted
            return permitted(app, actor, capability)
        if capability in {'image.generate@v1', 'image.edit@v1'}:
            from .model_routing import resolve_role, shared_key
            role = 'image_generation' if capability == 'image.generate@v1' else 'image_editing'
            manifest = resolve_role(app, db, actor, role)
            if manifest.secret_id:
                secret = db.get(Secret, manifest.secret_id)
                owned = secret and secret.owner_id == actor.user_id and secret.provider_id == manifest.id
                if not owned and not shared_key(app.vault, db, actor, manifest): return False
        elif capability in {'home.states@v1', 'web.search@v1'}:
            manifest = app.registry.resolve(db, capability, cloud=False)
        else:
            return True
        host = urlparse(manifest.endpoint).hostname
        if host not in manifest.allowed_hosts or (manifest.cloud and urlparse(manifest.endpoint).scheme != 'https'):
            return False
        if capability == 'home.states@v1':
            if manifest.adapter == 'homeassistant' and not manifest.secret_id: return False
            from .home_control import validate
            validate(manifest, capability, {})
        return True
    except HTTPException:
        # 此处不发健康探测请求；超时和临时上游故障仍在执行阶段如实呈现。
        return False


def available_tools(app, db, actor):
    tools, unavailable = [], {}
    for tool in TOOLS:
        name = tool['function']['name']
        if name in OPTIONAL_TOOLS and not configured_capability(app, db, actor, MAPPING[name]):
            if name in {'open_android_app', 'observe_android', 'operate_android'}:
                from .android_tools import unavailable_reason
                try:
                    unavailable[name] = unavailable_reason(app, db, actor) or '安卓设备暂不可用'
                except HTTPException as error:
                    unavailable[name] = str(error.detail)
            else:
                unavailable[name] = OPTIONAL_TOOLS[name] + '尚未接入、已停用或当前成员未获使用授权'
        elif name in {'open_android_app', 'observe_android', 'operate_android'}:
            import copy
            from .android_workspace import get_binding, ai_packages
            configured = copy.deepcopy(tool)
            packages = ai_packages(get_binding(app, db, actor))
            configured['function']['description'] += ' 当前AI允许范围：' + ('全部App；支付和验证码仍需本人处理' if packages == ['*'] else '、'.join(packages)) + '。本人手动接管不受此AI范围限制。'
            if name == 'open_android_app' and packages != ['*']:
                configured['function']['parameters']['properties']['package']['enum'] = packages
            tools.append(configured)
        elif name == 'inspect_server':
            import copy
            from .host_inspection_tools import allowed_targets
            configured=copy.deepcopy(tool)
            configured['function']['parameters']['properties']['targets']['items']['enum']=allowed_targets(app,actor)
            tools.append(configured)
        elif name == 'execute_host_action':
            import copy
            from .shell_configuration import load_policy
            actions = load_policy(app.settings).host_actions
            configured = copy.deepcopy(tool)
            configured['function']['parameters']['properties']['action_id']['enum'] = [action.id for action in actions]
            configured['function']['description'] += ' 当前可选动作：' + '；'.join(action.id + '：' + action.title + '（' + action.description + '）' for action in actions)
            tools.append(configured)
        else:
            tools.append(tool)
    return tools, unavailable
