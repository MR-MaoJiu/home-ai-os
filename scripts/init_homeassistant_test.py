"""创建独立 Home Assistant 协议验收配置，辅助实体不代表物理设备。"""
import os
from pathlib import Path
root = Path(__file__).resolve().parents[1] / 'state/homeassistant-test'
root.mkdir(parents=True, exist_ok=True, mode=0o700)
path = root / 'configuration.yaml'
configuration = '''homeassistant:
  name: Home AI Protocol Lab
  latitude: 0
  longitude: 0
  elevation: 0
  unit_system: metric
  time_zone: Asia/Shanghai
  country: CN
api:
frontend:
config:
input_boolean:
  homeai_protocol:
    name: Home AI Protocol Helper
    initial: false
template:
  - switch:
      - name: Home AI Protocol Switch
        unique_id: homeai_protocol_switch
        state: "{{ is_state('input_boolean.homeai_protocol', 'on') }}"
        turn_on:
          - action: input_boolean.turn_on
            target:
              entity_id: input_boolean.homeai_protocol
        turn_off:
          - action: input_boolean.turn_off
            target:
              entity_id: input_boolean.homeai_protocol
'''
try:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
except FileExistsError:
    print('保留已有 Home Assistant 协议验收配置')
else:
    with os.fdopen(fd, 'w') as file: file.write(configuration)
    print('已创建独立配置，仅包含软件辅助实体')
