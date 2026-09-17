from typing import Literal, Any
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


class Step(Contract):
    capability: str
    arguments: dict[str, Any] = Field(default_factory=dict)


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

    @model_validator(mode="after")
    def validate_workflow(self):
        if self.steps and (self.capability or self.mode != "local"):
            raise ValueError("多步骤工作流必须使用本地模式且不能同时指定单个能力")
        if len(self.steps) > self.max_steps:
            raise ValueError("工作流超过步骤预算")
        return self


class Decision(Contract):
    decision: Literal["APPROVED", "REJECTED"]


class Skill(Contract):
    name: str = Field(min_length=1, max_length=100)
    steps: list[Step] = Field(min_length=1, max_length=16)


class AutomationInput(Contract):
    name: str = Field(min_length=1, max_length=100)
    cron: str
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

    @field_validator("endpoint")
    @classmethod
    def endpoint_url(cls, v):
        from urllib.parse import urlparse
        p = urlparse(v)
        if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password or p.query or p.fragment:
            raise ValueError("端点必须为不含凭据的 HTTP(S) 地址")
        return v.rstrip("/")
