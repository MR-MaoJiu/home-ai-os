"""Home Assistant 实体权限与最小结果投影。"""
import math
from fastapi import HTTPException


def validate(manifest, capability, arguments):
    allowed = set(manifest.home_entities)
    if not allowed:
        raise HTTPException(403, 'Home Assistant 尚未授权任何实体')
    if capability == 'home.states@v1':
        entities = arguments.get('entity_ids', manifest.home_entities)
        if set(arguments) - {'entity_ids'} or not isinstance(entities, list) or not 1 <= len(entities) <= 50 or any(not isinstance(item, str) or item not in allowed for item in entities):
            raise HTTPException(403, '只能读取显式授权的家居实体')
        return list(dict.fromkeys(entities))
    if capability != 'home.execute@v1':
        raise HTTPException(403, '未授权的家居能力')
    domain, service, entity = arguments.get('domain'), arguments.get('service'), arguments.get('entity_id')
    operations = {(domain, service) for domain in ('light', 'switch', 'climate') for service in ('turn_on', 'turn_off')} | {('climate', 'set_temperature')}
    if not isinstance(domain, str) or not isinstance(service, str) or (domain, service) not in operations:
        raise HTTPException(403, '首版只开放灯、开关和温控白名单操作')
    if not isinstance(entity, str) or entity not in allowed or not entity.startswith(domain + '.'):
        raise HTTPException(403, '此家居实体没有操作授权')
    fields = {'domain', 'service', 'entity_id'} | ({'temperature'} if service == 'set_temperature' else set())
    if set(arguments) != fields:
        raise HTTPException(422, '家居操作参数无效')
    payload = {'entity_id': entity}
    if service == 'set_temperature':
        temperature = arguments['temperature']
        if type(temperature) not in (int, float) or not math.isfinite(temperature) or not 16 <= temperature <= 30:
            raise HTTPException(422, '温度必须为 16 至 30 的有限数值')
        payload['temperature'] = temperature
    return domain, service, payload


def project_state(value, allowed):
    if not isinstance(value, dict) or value.get('entity_id') not in allowed or not isinstance(value.get('state'), str):
        raise HTTPException(502, '家居服务返回未授权或无效实体')
    attributes = value.get('attributes', {})
    if not isinstance(attributes, dict):
        raise HTTPException(502, '家居实体属性无效')
    if len(value['state']) > 500:
        raise HTTPException(502, '家居状态长度无效')
    projected = {}
    for key in ('friendly_name', 'unit_of_measurement', 'device_class', 'hvac_action'):
        if isinstance(attributes.get(key), str):
            projected[key] = attributes[key][:500]
    for key in ('temperature', 'current_temperature', 'min_temp', 'max_temp'):
        number = attributes.get(key)
        if type(number) in (int, float) and math.isfinite(number):
            projected[key] = number
    modes = attributes.get('hvac_modes')
    if isinstance(modes, list) and len(modes) <= 30 and all(isinstance(mode, str) and len(mode) <= 100 for mode in modes):
        projected['hvac_modes'] = modes
    return {'entity_id': value['entity_id'], 'state': value['state'],
            'attributes': projected, 'last_updated': value.get('last_updated') if isinstance(value.get('last_updated'), str) else None}
