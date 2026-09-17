from typing import Literal, Any
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, StrictBool, StrictFloat, field_validator, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PairRequest(Contract):
    token: str = Field(min_length=20, max_length=200)
    public_key: str = Field(max_length=2000)
    signature: str = Field(max_length=500)
    name: str = Field(min_length=1, max_length=100)


class DataRecord(Contract):
    source: str = Field(min_length=1, max_length=100)
    source_id: str = Field(min_length=1, max_length=200)
    kind: str = Field(pattern=r"^[a-z][a-z0-9_.]{1,80}$")
    version: int = Field(ge=1)
    sensitivity: Literal["PUBLIC", "HOUSEHOLD", "PRIVATE", "SENSITIVE", "SECRET"] = "PRIVATE"
    cloud_policy: Literal["LOCAL_ONLY", "REDACT_AND_ALLOW"] = "LOCAL_ONLY"
    payload: dict[str, Any]


class SyncBatch(Contract):
    batch_id: str | None = Field(default=None, min_length=8, max_length=200)
    records: list[DataRecord] = Field(max_length=100)


class Predicate(Contract):
    step: int = Field(ge=0, le=15, strict=True)
    path: list[StrictStr | StrictInt] = Field(default_factory=list, max_length=20)
    operator: Literal["exists", "not_exists", "not_empty", "equals", "not_equals", "gt", "gte", "lt", "lte"]
    value: StrictStr | StrictInt | StrictFloat | StrictBool | None = None

    @model_validator(mode="after")
    def validate_comparison(self):
        import math
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("比较值必须为有限数值")
        if isinstance(self.value, str) and len(self.value) > 2000:
            raise ValueError("比较文本过长")
        if self.operator in {"gt", "gte", "lt", "lte"} and type(self.value) not in {int, float}:
            raise ValueError("大小比较只接受数值")
        if any(type(key) is int and key < 0 for key in self.path):
            raise ValueError("数组路径不能使用负下标")
        return self


class StepCondition(Contract):
    mode: Literal["all", "any"] = "all"
    predicates: list[Predicate] = Field(min_length=1, max_length=16)


def validate_conditions(steps):
    for index, step in enumerate(steps):
        if step.when and any(predicate.step >= index for predicate in step.when.predicates):
            raise ValueError("条件只能引用本工作流中的前序步骤")


class Step(Contract):
    capability: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    when: StepCondition | None = None


class TaskRequest(Contract):
    message: str = Field(default="", max_length=20000)
    idempotency_key: str = Field(min_length=8, max_length=200)
    mode: Literal["local", "cloud"] = "local"
    record_ids: list[str] = Field(default_factory=list, max_length=20)
    capability: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    max_steps: int = Field(default=8, ge=1, le=16)
    max_output_tokens: int = Field(default=1024, ge=1, le=4096)
    steps: list[Step] = Field(default_factory=list, max_length=16)
    timeout_seconds: int = Field(default=600, ge=10, le=3600)
    step_timeout_seconds: int = Field(default=120, ge=1, le=300)
    max_model_tokens: int = Field(default=32768, ge=256, le=262144)
    max_read_retries: int = Field(default=1, ge=0, le=3)
    timezone: str = Field(default="Asia/Shanghai", max_length=100)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try: ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError): raise ValueError("需要有效 IANA 时区") from None
        return value

    @model_validator(mode="after")
    def validate_workflow(self):
        if self.steps and (self.capability or self.mode != "local"):
            raise ValueError("多步骤工作流必须使用本地模式且不能同时指定单个能力")
        if len(self.steps) > self.max_steps:
            raise ValueError("工作流超过步骤预算")
        validate_conditions(self.steps)
        return self


class Decision(Contract):
    decision: Literal["APPROVED", "REJECTED"]


class Skill(Contract):
    name: str = Field(min_length=1, max_length=100)
    steps: list[Step] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def validate_steps(self):
        validate_conditions(self.steps)
        return self


class AutomationInput(Contract):
    name: str = Field(min_length=1, max_length=100)
    cron: str = ""
    trigger_kind: Literal["cron", "event"] = "cron"
    event_type: Literal["record.changed", "record.deleted", "record.revoked"] | None = None
    record_kind: str | None = Field(default=None, min_length=1, max_length=100)
    record_source: str | None = Field(default=None, min_length=1, max_length=100)
    include_shared: bool = False
    cooldown_seconds: int = Field(default=0, ge=0, le=86400)
    timezone: str = "Asia/Shanghai"
    skill: Skill
    enabled: bool = False


class ProviderManifest(Contract):
    schema_version: Literal["1.0"] = "1.0"
    id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,80}$")
    version: str
    adapter: Literal["openai", "homeassistant", "searxng", "http", "mcp"]
    endpoint: str
    cloud: bool = False
    model: str | None = None
    embedding_query_prefix: str = Field(default="", max_length=500)
    embedding_document_prefix: str = Field(default="", max_length=500)
    capabilities: dict[str, str]
    timeout_seconds: int = Field(default=60, ge=1, le=300)
    image_digest: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")
    secret_id: str | None = None
    allowed_hosts: list[str] = Field(min_length=1)
    home_events: bool = False
    home_entities: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("home_entities")
    @classmethod
    def home_entity_ids(cls, values):
        import re
        if len(set(values)) != len(values) or any(len(value) > 150 or not re.fullmatch(r"[a-z][a-z0-9_]*\.[a-z0-9_]+", value) for value in values):
            raise ValueError("家居实体必须是明确且不重复的 domain.entity_id，禁止通配符")
        return values


    @field_validator("endpoint")
    @classmethod
    def endpoint_url(cls, v):
        from urllib.parse import urlparse
        p = urlparse(v)
        if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password or p.query or p.fragment:
            raise ValueError("端点必须为不含凭据的 HTTP(S) 地址")
        return v.rstrip("/")


class TaskStepState(Contract):
    step: int = Field(ge=0)
    status: str = Field(max_length=64)


class TaskState(Contract):
    id: str
    status: str = Field(max_length=64)
    cancel_requested: bool
    steps: list[TaskStepState]


class TaskStateNotification(Contract):
    schema_version: Literal['1.0'] = '1.0'
    type: Literal['task.snapshot', 'heartbeat']
    has_more: bool = False
    tasks: list[TaskState] = Field(default_factory=list, max_length=100)
