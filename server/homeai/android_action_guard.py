"""Core 根据实际控件判定安卓动作；模型不能声明动作安全或复用旧坐标。"""
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from fastapi import HTTPException


class AndroidNeedsObservation(Exception):
    def __init__(self, reason='安卓界面已变化，需要重新观察后继续', code='screen_changed'):
        self.reason = self.detail = reason
        self.code = code
        super().__init__(reason)


class AndroidAssistanceRequired(Exception):
    def __init__(self, reason, code='manual_required'):
        self.reason = self.detail = reason
        self.code = code
        super().__init__(reason)


_SEAL = object()
_DANGER = re.compile(r'付款|支付|下单|提交|确认订单|结算|转账|汇款|充值|购买|立即购买|删除|移除|清空|清除数据|卸载|安装|发送|发布|发表|授权|允许|授予|同意并|注销|恢复出厂|格式化|取消订单|确认叫车|确认呼叫|呼叫快车|叫车|放弃修改|不保存|丢弃|(?<![a-z])(?:pay|payment|purchase|checkout|submit|place.?order|transfer|delete|remove|erase|uninstall|install|send|publish|grant|allow|authorize|factory.?reset|discard)(?![a-z])', re.I)
_VERIFY = re.compile(r'密码|验证码|口令|指纹|人脸|安全验证|(?<![a-z])(?:password|passcode|captcha|verification.?code|otp|pin.?code)(?![a-z])', re.I)


def _verification_reason(label):
    if re.search(r'密码|口令|password|passcode|pin.?code', label, re.I):
        return '本次目标要求密码，请本人输入'
    if re.search(r'验证码|captcha|verification.?code|otp', label, re.I):
        return '本次目标要求验证码，请本人处理'
    return '本次目标需要本人完成身份验证'


def _danger_reason(label):
    for pattern, category in (
        (r'付款|支付|结算|转账|汇款|充值|(?<![a-z])(?:pay|payment|checkout|transfer)(?![a-z])', '付款'),
        (r'删除|移除|清空|清除数据|格式化|恢复出厂|(?<![a-z])(?:delete|remove|erase|factory.?reset)(?![a-z])', '删除'),
        (r'发送|发布|发表|(?<![a-z])(?:send|publish)(?![a-z])', '对外发送'),
        (r'授权|允许|授予|(?<![a-z])(?:grant|allow|authorize)(?![a-z])', '权限授予'),
        (r'卸载|安装|(?<![a-z])(?:uninstall|install)(?![a-z])', '软件安装或卸载'),
        (r'下单|订单|购买|叫车|确认呼叫|呼叫快车|(?<![a-z])(?:purchase|place.?order)(?![a-z])', '订单操作'),
        (r'放弃修改|不保存|丢弃|(?<![a-z])discard(?![a-z])', '放弃修改'),
    ):
        if re.search(pattern, label, re.I):
            return f'本次目标涉及{category}，请本人确认并操作'
    return '本次目标需要本人确认，请本人操作'


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def parse(raw):
    if len(raw) > 512 * 1024 or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise HTTPException(422, '安卓界面树不合法')
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        raise HTTPException(502, '未能读取可操作的安卓界面') from None


def editable(node):
    return node.get('editable') == 'true' or node.get('class', '').endswith('EditText')


def blocking_reason(root):
    for node in root.iter('node'):
        if node.get('visible-to-user') == 'false':
            continue
        if node.get('password', '').lower() == 'true':
            return ('当前页面包含密码输入框，请本人接管', 'password_required')
        # “支付设置”等普通标题不是验证要求；仅验证输入或真实验证控件触发。
        identity = ' '.join(node.get(key, '') for key in ('resource-id', 'class', 'content-desc', 'text'))
        if (editable(node) and _VERIFY.search(identity)) or re.search(r'captcha|biometricprompt|fingerprint_dialog', identity, re.I):
            return (_verification_reason(identity), 'verification_required')
    return None


def _bounds(node, width, height):
    match = re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', node.get('bounds', ''))
    if not match:
        return None
    x1, y1, x2, y2 = map(int, match.groups())
    return (x1, y1, x2, y2) if 0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height else None


def _label(node):
    values = []
    for child in node.iter('node'):
        # 输入框原文不是控件身份，也不进入安全证据。
        values.append(child.get('content-desc', ''))
        identifier = child.get('resource-id', '')
        # 包名如 AlipayGphone 不表示支付按钮；只检查控件自身的名称。
        if '/' in identifier:
            values.append(identifier.rsplit('/', 1)[-1])
        if not editable(child):
            values.append(child.get('text', ''))
    return ' '.join(values)[:4000]


def _navigation_label(node):
    labels = [item.get('text', '').strip() for item in node.iter('node') if item.get('text', '').strip()]
    return len(labels) == 1 and labels[0] in {'支付设置', '支付方式', '付款方式', '支付记录', '交易记录', '授权管理', '已授权应用', '回收站', '密码设置', '安全设置', '账户安全', '修改密码'}


def _caption(node):
    labels = []
    for child in node.iter('node'):
        for key in ('text', 'content-desc'):
            value = child.get(key, '').strip()
            if value and value not in labels:
                labels.append(value)
    return ' '.join(labels)


def controls(root, width, height):
    result = []
    for node in root.iter('node'):
        bounds = _bounds(node, width, height)
        if not bounds or node.get('enabled') == 'false' or node.get('visible-to-user') == 'false':
            continue
        if not (node.get('clickable') == 'true' or node.get('scrollable') == 'true' or editable(node)):
            continue
        label = (' '.join(node.get(key, '') for key in ('content-desc', 'resource-id'))
                 if node.get('scrollable') == 'true' else _label(node))
        identity = {key: node.get(key, '') for key in ('package', 'resource-id', 'class', 'bounds', 'clickable', 'scrollable', 'password', 'focused', 'checked', 'selected')}
        identity['label'] = label
        result.append({'node': node, 'bounds': bounds, 'label': label, 'fingerprint': _digest(identity)})
    return result


def snapshot(raw, width, height):
    """只保存控件语义摘要；时钟等不可操作动态文字不使坐标证据失效。"""
    root = parse(raw)
    rows = controls(root, width, height)
    parents = {child: parent for parent in root.iter() for child in parent}
    def depth(node):
        value = 0
        while node in parents:
            value += 1
            node = parents[node]
        return value
    surface = _digest([root.get('rotation', ''), sorted({item['node'].get('package', '') for item in rows}), width, height])
    return {'width': width, 'height': height, 'rotation': root.get('rotation', ''), 'fingerprint': surface,
            'controls': [{'fingerprint': item['fingerprint'], 'bounds': list(item['bounds']),
                          'depth': depth(item['node']),
                          'clickable': item['node'].get('clickable') == 'true',
                          'scrollable': item['node'].get('scrollable') == 'true',
                          'editable': editable(item['node']), 'focused': item['node'].get('focused') == 'true'} for item in rows]}


async def window_fingerprint(settings, serial):
    from . import android_adb
    raw = await android_adb._run(android_adb.load_policy(settings), serial, ['shell', 'dumpsys', 'window'], limit=2 * 1024 * 1024)
    match = re.search(rb'mCurrentFocus=Window\{([A-Za-z0-9]+)\s(?:u\d+\s+)?([A-Za-z][A-Za-z0-9_.]+)/[^}\s]+', raw)
    if match is None:
        raise AndroidAssistanceRequired('当前设备窗口无法可靠识别，请本人处理', 'window_unavailable')
    return hashlib.sha256(match.group()).hexdigest()


async def assert_connected(app, row):
    from . import android_adb
    devices = await android_adb.devices(app.settings)
    current = next((device for device in devices if device['serial'] == row.serial), None)
    if current is None or current.get('status') != 'online':
        raise AndroidAssistanceRequired('已绑定的安卓设备当前不在线，请恢复连接后继续', 'device_offline')


def remembered_target(remembered, action):
    if action.action in {'home', 'launch', 'back'}:
        return _digest([action.action, action.package])
    rows = remembered.get('controls', [])
    if action.action == 'text':
        candidates = [row for row in rows if row['editable'] and row['focused']]
    else:
        x, y = action.x * remembered['width'], action.y * remembered['height']
        candidates = [row for row in rows if row['bounds'][0] <= x < row['bounds'][2] and row['bounds'][1] <= y < row['bounds'][3]
                      and (row['scrollable'] if action.action == 'swipe' else row['clickable'] or row['editable'])]
        if candidates:
            candidates = [max(candidates, key=lambda row: row.get('depth', 0))]
    if len(candidates) != 1:
        raise AndroidAssistanceRequired('本次观察中没有可靠的操作目标，请本人处理', 'target_unavailable')
    return candidates[0]['fingerprint']


def _dangerous_dialog(root, target_node=None):
    """通用确认按钮必须连同所属确认对话框检查，不能只看“确定”。"""
    parents = {child: parent for parent in root.iter() for child in parent}
    current = target_node
    containers = []
    while current is not None:
        identity = current.get('resource-id', '') + ' ' + current.get('class', '')
        if re.search(r'dialog|alert|parentPanel', identity, re.I):
            containers.append(current)
        elif re.search(r'是否.{0,12}(?:删除|支付|放弃|取消)|确认(?:删除|支付|转账|订单)|删除后|不可恢复|无法恢复|支付金额|订单总计|delete[^?]{0,80}\?|are you sure', _caption(current), re.I):
            # 自绘对话框没有标准资源名时，仍识别实际确认语句。
            containers.append(current)
        current = parents.get(current)
    if target_node is None:
        containers = [node for node in root.iter('node') if re.search(r'dialog|alert|parentPanel', node.get('resource-id', '') + ' ' + node.get('class', ''), re.I)]
    return next((_danger_reason(_label(node)) for node in containers if _DANGER.search(_label(node))), None)


def target(raw, width, height, action):
    root = parse(raw)
    if action.action not in {'back', 'home', 'launch'} and (blocked := blocking_reason(root)):
        raise AndroidAssistanceRequired(*blocked)
    rows = controls(root, width, height)
    if action.action in {'back', 'home', 'launch'}:
        if action.action == 'back' and (reason := _dangerous_dialog(root)):
            raise AndroidAssistanceRequired(reason, 'sensitive_confirmation')
        return _digest([action.action, action.package]), None
    if action.action == 'text':
        candidates = [item for item in rows if editable(item['node']) and item['node'].get('focused') == 'true']
    else:
        x, y = action.x * width, action.y * height
        candidates = [item for item in rows if item['bounds'][0] <= x < item['bounds'][2] and item['bounds'][1] <= y < item['bounds'][3]
                      and (item['node'].get('scrollable') == 'true' if action.action == 'swipe' else item['node'].get('clickable') == 'true' or editable(item['node']))]
        if candidates:
            parents = {child: parent for parent in root.iter() for child in parent}
            def ancestors(node):
                chain = []
                while node in parents:
                    node = parents[node]
                    chain.append(node)
                return chain
            chains = {item['node']: ancestors(item['node']) for item in candidates}
            for index, left in enumerate(candidates):
                for right in candidates[index+1:]:
                    if left['node'] not in chains[right['node']] and right['node'] not in chains[left['node']]:
                        raise AndroidAssistanceRequired('本次位置有重叠操作控件，无法确定实际目标，请本人处理', 'target_ambiguous')
            candidates = [max(candidates, key=lambda item: len(chains[item['node']]))]
    if len(candidates) != 1:
        raise AndroidAssistanceRequired('当前操作目标无法可靠识别，请本人处理', 'target_unavailable')
    item = candidates[0]
    if action.action == 'swipe':
        # 只允许在真实滚动容器内垂直浏览；条目侧滑可能删除或触发交易。
        x2, y2 = action.x2 * width, action.y2 * height
        box = item['bounds']
        if not (box[0] <= x2 < box[2] and box[1] <= y2 < box[3]) or abs(y2-y) < abs(x2-x)*2:
            raise AndroidAssistanceRequired('本次滑动可能触发条目操作，请本人处理', 'gesture_uncertain')
    else:
        caption = _caption(item['node'])
        plain_exit = caption in {'拒绝', '不允许', '取消', '关闭', '返回', 'Deny', 'Cancel', 'Close', 'Back', "Don't Allow"}
        if _VERIFY.search(item['label']) and not _navigation_label(item['node']):
            raise AndroidAssistanceRequired(_verification_reason(item['label']), 'verification_required')
        if _DANGER.search(item['label']) and not _navigation_label(item['node']) and not plain_exit:
            raise AndroidAssistanceRequired(_danger_reason(item['label']), 'sensitive_action')
        if re.fullmatch(r'确定|确认|继续|完成|好|是|OK|Confirm|Continue|Yes', caption, re.I) and (reason := _dangerous_dialog(root, item['node'])):
            raise AndroidAssistanceRequired(reason, 'sensitive_confirmation')
        if not item['label'].strip():
            raise AndroidAssistanceRequired('本次控件没有可核对的操作说明，请本人处理', 'target_unavailable')
    return item['fingerprint'], item


@dataclass(frozen=True)
class ActionAssessment:
    arguments_hash: str
    input_hash: str
    owner_id: str
    household_id: str
    device_id: str
    task_id: str
    workspace_id: str
    epoch: int
    frame_id: str
    width: int
    height: int
    screen_fingerprint: str
    target_fingerprint: str
    focus_fingerprint: str | None = None
    window_fingerprint: str | None = None
    _seal: object = field(default=None, repr=False, compare=False)

    def permits(self, arguments, actor=None):
        return (self._seal is _SEAL and self.arguments_hash == _digest(arguments)
                and (actor is None or (actor.user_id, actor.household_id, actor.device_id) == (self.owner_id, self.household_id, self.device_id)))


def _input_hash(value):
    return _digest({key: item for key, item in value.model_dump().items() if key != 'focus_token'})


async def preflight(app, db, actor, task, body, args):
    from . import android_adb, android_workspace as android
    from .android_tools import AndroidAction
    action = AndroidAction.model_validate(args).input
    if body.get('_android_needs_observation'):
        raise AndroidNeedsObservation()
    remembered = body.get('_android_observation', {})
    if action.action not in {'home', 'launch'} and (remembered.get('frame_id') != action.frame_id or remembered.get('epoch') != action.epoch):
        raise AndroidNeedsObservation('缺少本次任务的有效界面，请重新观察', 'observation_missing')
    from .android_tasks import acquire
    await acquire(app, actor, task.id, body)
    async with android._lock(app, android._member_key(actor)):
        android._active_device(db, actor)
        row = android._required(app, db, actor)
        db.refresh(row)
        if row.mode != 'ai' or row.epoch != action.epoch or row.active_task_id != task.id:
            raise HTTPException(409, '设备控制权已变化，旧计划不能继续')
        await assert_connected(app, row)
        await android.assert_ai_package_allowed(app, actor, row, action=action)
        if action.action in {'home', 'launch'}:
            return ActionAssessment(_digest(args), _input_hash(action), actor.user_id, actor.household_id, actor.device_id,
                task.id, row.id, row.epoch, action.frame_id, row.frame_width, row.frame_height, '',
                _digest([action.action, action.package]), None, None, _SEAL)
        raw = await android_adb.hierarchy(app.settings, row.serial)
        current = snapshot(raw, remembered['width'], remembered['height'])
        if current['fingerprint'] != remembered.get('fingerprint'):
            raise AndroidNeedsObservation()
        window = await window_fingerprint(app.settings, row.serial)
        if window != remembered.get('window_fingerprint'):
            raise AndroidNeedsObservation('前台窗口已变化，需要重新观察', 'window_changed')
        fingerprint, _ = target(raw, remembered['width'], remembered['height'], action)
        if fingerprint != remembered_target(remembered, action):
            raise AndroidNeedsObservation()
        focus = None
        if action.action == 'text':
            signature = await android_adb.focus_signature(app.settings, row.serial, xml=raw)
            if signature['password']:
                raise AndroidAssistanceRequired('当前输入框要求密码，请本人输入', 'password_required')
            if signature['fingerprint'] != remembered.get('focus_fingerprint'):
                raise AndroidNeedsObservation('输入焦点已变化，请重新观察', 'focus_changed')
            focus = signature['fingerprint']
        return ActionAssessment(_digest(args), _input_hash(action), actor.user_id, actor.household_id, actor.device_id,
            task.id, row.id, row.epoch, action.frame_id, remembered['width'], remembered['height'], current['fingerprint'],
            fingerprint, focus, window, _SEAL)


async def revalidate(app, db, actor, row, action, assessment):
    """在已有设备锁内重验，匹配同一语义焦点后才重签文字输入许可。"""
    from . import android_adb, android_workspace as android
    if (not isinstance(assessment, ActionAssessment) or assessment._seal is not _SEAL or assessment.input_hash != _input_hash(action)
        or (actor.user_id, actor.household_id, actor.device_id) != (assessment.owner_id, assessment.household_id, assessment.device_id)
        or row.id != assessment.workspace_id or row.active_task_id != assessment.task_id or row.epoch != assessment.epoch or row.mode != 'ai'):
        raise HTTPException(403, '安卓动作缺少有效的 Core 安全核验')
    if action.action in {'home', 'launch'}:
        return action
    raw = await android_adb.hierarchy(app.settings, row.serial)
    current = snapshot(raw, assessment.width, assessment.height)
    if current['fingerprint'] != assessment.screen_fingerprint:
        raise AndroidNeedsObservation()
    if await window_fingerprint(app.settings, row.serial) != assessment.window_fingerprint:
        raise AndroidNeedsObservation('前台窗口已变化，需要重新观察', 'window_changed')
    fingerprint, _ = target(raw, assessment.width, assessment.height, action)
    if fingerprint != assessment.target_fingerprint:
        raise AndroidNeedsObservation()
    if action.action == 'text':
        signature = await android_adb.focus_signature(app.settings, row.serial, xml=raw)
        if signature['password']:
            raise AndroidAssistanceRequired('当前输入框要求密码，请本人输入', 'password_required')
        if signature['fingerprint'] != assessment.focus_fingerprint:
            raise AndroidNeedsObservation('输入焦点已变化，请重新观察', 'focus_changed')
        claim = android._focus_claim(db, row, actor, 'ai', signature['fingerprint'])
        action = action.model_copy(update={'focus_token': claim['focus_token']})
    row.frame_width, row.frame_height = assessment.width, assessment.height
    return action
