"""家居授权契约的拒绝边界，不需要或伪造设备响应。"""
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from homeai.contracts import ProviderManifest
from homeai.home_control import validate


def test_entity_permissions_and_dangerous_operations_are_closed():
    base = {'id': 'home.test', 'version': '1', 'adapter': 'homeassistant', 'endpoint': 'http://127.0.0.1:58123',
            'allowed_hosts': ['127.0.0.1'], 'capabilities': {'home.states@v1': 'states', 'home.execute@v1': 'execute'}}
    manifest = ProviderManifest(**base)
    with pytest.raises(HTTPException): validate(manifest, 'home.states@v1', {})
    with pytest.raises(ValidationError): ProviderManifest(**base, home_entities=['switch.*'])
    manifest = ProviderManifest(**base, home_entities=['lock.front_door', 'climate.protocol'])
    with pytest.raises(HTTPException): validate(manifest, 'home.execute@v1', {'domain': 'lock', 'service': 'unlock', 'entity_id': 'lock.front_door'})
    for temperature in (float('nan'), float('inf'), True, '20', 40):
        with pytest.raises(HTTPException):
            validate(manifest, 'home.execute@v1', {'domain': 'climate', 'service': 'set_temperature', 'entity_id': 'climate.protocol', 'temperature': temperature})
