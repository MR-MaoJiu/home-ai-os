"""客户端只提交输入、已上传资源和上下文快照，不提交执行计划。"""
from datetime import datetime, timezone
from typing import Literal
from pydantic import Field, model_validator
from .contracts import Contract


class CoarseLocation(Contract):
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    accuracy_meters: float = Field(ge=500, allow_inf_nan=False)
    observed_at: datetime
    precision: Literal['coarse'] = 'coarse'

    @model_validator(mode='after')
    def coarse(self):
        if self.observed_at.tzinfo is None:
            raise ValueError('采样时间必须带时区')
        if abs(self.latitude * 100 - round(self.latitude * 100)) > 0.0001 or abs(self.longitude * 100 - round(self.longitude * 100)) > 0.0001:
            raise ValueError('常用上下文仅允许降精度位置')
        return self


class Availability(Contract):
    battery: Literal['available', 'unavailable'] = 'unavailable'
    network: Literal['available', 'unavailable'] = 'unavailable'
    location: Literal['available', 'not_authorized', 'unavailable', 'stale'] = 'unavailable'


class ClientContext(Contract):
    schema_version: Literal['1.0'] = '1.0'
    sampled_at: datetime
    timezone: str = Field(max_length=100)
    locale: str = Field(max_length=100)
    app_version: str = Field(default='', max_length=50)
    battery_level: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    battery_state: Literal['unknown', 'unplugged', 'charging', 'full'] = 'unknown'
    low_power_mode: bool = False
    network_type: Literal['unknown', 'wifi', 'cellular', 'wired', 'other', 'offline'] = 'unknown'
    location: CoarseLocation | None = None
    availability: Availability = Field(default_factory=Availability)

    @model_validator(mode='after')
    def timestamp(self):
        if self.sampled_at.tzinfo is None or (self.sampled_at-datetime.now(timezone.utc)).total_seconds() > 60:
            raise ValueError('上下文必须是带时区的实际采样，不能来自未来')
        if self.location:
            # 比较秒数而非截断坐标，拒绝未来或旧定位冒充新快照。
            delta = (self.sampled_at-self.location.observed_at).total_seconds()
            if not -60 <= delta <= 900:
                raise ValueError('位置采样已经过期')
        return self


class Mention(Contract):
    member_id: str = Field(min_length=1, max_length=100)


class MessagePart(Contract):
    type: Literal['text', 'image', 'video', 'file']
    text: str | None = Field(default=None, max_length=10000)
    record_id: str | None = Field(default=None, max_length=100)
    version: int | None = Field(default=None, ge=1)

    @model_validator(mode='after')
    def validate_part(self):
        if self.type == 'text':
            if not self.text or self.record_id or self.version is not None:
                raise ValueError('文字块格式无效')
        elif not self.record_id or self.version is None or self.text is not None:
            raise ValueError('附件必须引用已上传的资源和版本')
        return self
