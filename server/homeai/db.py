import uuid
from datetime import datetime, timezone
from sqlalchemy import create_engine, String, Text, Integer, Boolean, UniqueConstraint, event, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker, Session


def uid() -> str:
    return str(uuid.uuid4())


def now() -> float:
    return datetime.now(timezone.utc).timestamp()


class Base(DeclarativeBase):
    pass


class Principal(Base):
    __tablename__ = "principals"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    household_id: Mapped[str] = mapped_column(String, index=True)
    name: Mapped[str] = mapped_column(String)
    role: Mapped[str] = mapped_column(String, default="adult")


class Device(Base):
    __tablename__ = "devices"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(String, index=True)
    public_key: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(String)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class Credential(Base):
    __tablename__ = "credentials"
    digest: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String)
    device_id: Mapped[str | None] = mapped_column(String, nullable=True)
    kind: Mapped[str] = mapped_column(String)
    expires_at: Mapped[float]


class Nonce(Base):
    __tablename__ = "request_nonces"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    expires_at: Mapped[float]


class Owned:
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    household_id: Mapped[str] = mapped_column(String, index=True)
    owner_id: Mapped[str] = mapped_column(String, index=True)


class Record(Owned, Base):
    __tablename__ = "data_records"
    __table_args__ = (UniqueConstraint("owner_id", "source", "source_id"),)
    source: Mapped[str] = mapped_column(String)
    source_id: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)
    sensitivity: Mapped[str] = mapped_column(String)
    cloud_policy: Mapped[str] = mapped_column(String, default="LOCAL_ONLY")
    version: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[str] = mapped_column(Text)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[float] = mapped_column(default=now)


class Revision(Owned, Base):
    __tablename__ = "record_versions"
    record_id: Mapped[str] = mapped_column(String, index=True)
    version: Mapped[int] = mapped_column(Integer)
    payload: Mapped[str] = mapped_column(Text)


class Grant(Owned, Base):
    __tablename__ = "grants"
    record_id: Mapped[str] = mapped_column(String, index=True)
    grantee_id: Mapped[str] = mapped_column(String, index=True)


class Task(Owned, Base):
    __tablename__ = "tasks"
    __table_args__ = (UniqueConstraint("owner_id", "idempotency_key"),)
    idempotency_key: Mapped[str] = mapped_column(String)
    request_hash: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="RECEIVED")
    request: Mapped[str] = mapped_column(Text)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[float] = mapped_column(default=now)
    deadline: Mapped[float]
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)


class Invocation(Owned, Base):
    __tablename__ = "tool_calls"
    __table_args__ = (UniqueConstraint("task_id", "step"),)
    task_id: Mapped[str] = mapped_column(String, index=True)
    step: Mapped[int] = mapped_column(Integer)
    capability: Mapped[str] = mapped_column(String)
    arguments: Mapped[str] = mapped_column(Text)
    arguments_hash: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="PENDING")
    result: Mapped[str | None] = mapped_column(Text, nullable=True)


class Approval(Owned, Base):
    __tablename__ = "approvals"
    invocation_id: Mapped[str] = mapped_column(String, unique=True)
    arguments_hash: Mapped[str] = mapped_column(String)
    expires_at: Mapped[float]
    decision: Mapped[str] = mapped_column(String, default="PENDING")


class Audit(Owned, Base):
    __tablename__ = "audit_entries"
    action: Mapped[str] = mapped_column(String)
    resource_id: Mapped[str] = mapped_column(String)
    details: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[float] = mapped_column(default=now)


class Outbox(Base):
    __tablename__ = "event_outbox"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String, unique=True, default=uid)
    household_id: Mapped[str] = mapped_column(String)
    owner_id: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)
    resource_id: Mapped[str] = mapped_column(String)
    record_kind: Mapped[str | None] = mapped_column(String, nullable=True)
    record_source: Mapped[str | None] = mapped_column(String, nullable=True)
    record_owner_id: Mapped[str | None] = mapped_column(String, nullable=True)
    record_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    automation_chain: Mapped[str] = mapped_column(Text, default="[]")
    published: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[float] = mapped_column(default=now)


class AutomationDelivery(Owned, Base):
    __tablename__ = "automation_deliveries"
    __table_args__ = (UniqueConstraint("automation_id", "event_id"),)
    automation_id: Mapped[str] = mapped_column(String)
    event_id: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="PENDING")
    task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[float] = mapped_column(default=now)


class Consumption(Base):
    __tablename__ = "event_consumptions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[float] = mapped_column(default=now)


class Provider(Base):
    __tablename__ = "providers"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    manifest: Mapped[str] = mapped_column(Text)
    previous_manifest: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    health: Mapped[str] = mapped_column(String, default="offline")


class Automation(Owned, Base):
    __tablename__ = "automations"
    name: Mapped[str] = mapped_column(String)
    cron: Mapped[str] = mapped_column(String)
    timezone: Mapped[str] = mapped_column(String, default="Asia/Shanghai")
    skill: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    next_run: Mapped[float]
    trigger_kind: Mapped[str] = mapped_column(String, default="cron")
    event_type: Mapped[str | None] = mapped_column(String, nullable=True)
    record_kind: Mapped[str | None] = mapped_column(String, nullable=True)
    record_source: Mapped[str | None] = mapped_column(String, nullable=True)
    include_shared: Mapped[bool] = mapped_column(Boolean, default=False)
    cooldown_seconds: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[float] = mapped_column(default=now)
    last_trigger_at: Mapped[float] = mapped_column(default=0)


class HomeObservation(Owned, Base):
    __tablename__ = "home_observations"
    __table_args__ = (UniqueConstraint("owner_id", "provider_id", "entity_id"),)
    provider_id: Mapped[str] = mapped_column(String)
    entity_id: Mapped[str] = mapped_column(String)
    payload: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    observed_at: Mapped[float] = mapped_column(default=now)


class HomeConnection(Owned, Base):
    __tablename__ = "home_connections"
    __table_args__ = (UniqueConstraint("owner_id", "provider_id"),)
    provider_id: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    error_type: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[float] = mapped_column(default=now)
    lease_owner: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_until: Mapped[float] = mapped_column(default=0)



class Secret(Owned, Base):
    __tablename__ = "secrets"
    provider_id: Mapped[str] = mapped_column(String)
    value: Mapped[str] = mapped_column(Text)


class Disclosure(Owned, Base):
    __tablename__ = "cloud_disclosures"
    task_id: Mapped[str] = mapped_column(String)
    provider_id: Mapped[str] = mapped_column(String)
    categories: Mapped[str] = mapped_column(Text)
    bytes_sent: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String, default="ATTEMPTED")
    created_at: Mapped[float] = mapped_column(default=now)


def database(url: str, test: bool = False):
    if url.startswith("sqlite") and not test:
        raise RuntimeError("SQLite 仅允许测试；实际运行必须使用 PostgreSQL")
    engine = create_engine(url, **({"connect_args": {"check_same_thread": False}} if test else {"pool_pre_ping": True}))
    factory = sessionmaker(engine, expire_on_commit=False)
    return engine, factory


def scope(db, user_id: str, household_id: str):
    db.info.update(user_id=user_id, household_id=household_id)
    if db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT set_config('homeai.user_id', :u, true), set_config('homeai.household_id', :h, true)"), {"u": user_id, "h": household_id})


@event.listens_for(Session, "after_begin")
def restore_scope(session, transaction, connection):
    if connection.dialect.name == "postgresql" and "user_id" in session.info:
        connection.execute(text("SELECT set_config('homeai.user_id', :u, true), set_config('homeai.household_id', :h, true)"), {"u": session.info["user_id"], "h": session.info["household_id"]})

class MemoryCandidate(Owned, Base):
    __tablename__ = "memory_candidates"
    source_ids: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, default="PENDING")
    created_at: Mapped[float] = mapped_column(default=now)


class MemoryVector(Owned, Base):
    __tablename__ = "memory_vectors"
    record_id: Mapped[str] = mapped_column(String, unique=True)
    model: Mapped[str] = mapped_column(String)
    # JSON 兼容单元测试，生产迁移创建 vector 列并由 SQL 操作。
    embedding: Mapped[str] = mapped_column(Text)


class DerivedJob(Owned, Base):
    __tablename__ = "memory_deletion_jobs"
    provider_id: Mapped[str] = mapped_column(String)
    event_id: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String, nullable=True)

class BrowserAccount(Base):
    __tablename__ = "browser_accounts"
    username: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    totp_secret: Mapped[str] = mapped_column(Text)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    last_totp_counter: Mapped[int] = mapped_column(Integer, default=-1)


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    window_start: Mapped[float] = mapped_column(default=now)


class SyncSnapshot(Owned, Base):
    __tablename__ = 'sync_snapshots'
    device_id: Mapped[str] = mapped_column(String)
    watermark: Mapped[int] = mapped_column(Integer)
    payload: Mapped[str] = mapped_column(Text)
    next_offset: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[float] = mapped_column()


class SyncCursor(Owned, Base):
    __tablename__ = 'sync_cursors'
    initialized: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint('owner_id', 'device_id'),)
    device_id: Mapped[str] = mapped_column(String)
    acknowledged: Mapped[int] = mapped_column(Integer, default=0)
    offered: Mapped[int] = mapped_column(Integer, default=0)


class SyncReceipt(Owned, Base):
    __tablename__ = 'sync_receipts'
    __table_args__ = (UniqueConstraint('owner_id', 'device_id', 'batch_id'),)
    device_id: Mapped[str] = mapped_column(String)
    batch_id: Mapped[str] = mapped_column(String)
    request_hash: Mapped[str] = mapped_column(String)
    payload: Mapped[str] = mapped_column(Text)


class KnowledgeChunk(Owned, Base):
    __tablename__ = 'knowledge_chunks'
    __table_args__ = (UniqueConstraint('record_id', 'version', 'model', 'position'),)
    record_id: Mapped[str] = mapped_column(String, index=True)
    version: Mapped[int] = mapped_column(Integer)
    model: Mapped[str] = mapped_column(String)
    position: Mapped[int] = mapped_column(Integer)
    start: Mapped[int] = mapped_column(Integer)
    end: Mapped[int] = mapped_column(Integer)


class PairEnrollment(Base):
    """短期身份引导账本，密文不承载业务资料；读取必须核对家庭。"""
    __tablename__ = 'pair_enrollments'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    household_id: Mapped[str] = mapped_column(String)
    user_id: Mapped[str] = mapped_column(String)
    expires: Mapped[float]
    ciphertext: Mapped[str] = mapped_column(Text)


class Conversation(Owned, Base):
    __tablename__ = 'conversations'
    title: Mapped[str] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(default=now)
    updated_at: Mapped[float] = mapped_column(default=now)
    next_sequence: Mapped[int] = mapped_column(Integer,default=0)


class ConversationTurn(Owned, Base):
    __tablename__ = 'conversation_turns'
    __table_args__ = (UniqueConstraint('conversation_id','sequence'),UniqueConstraint('conversation_id','client_key'),UniqueConstraint('task_id'))
    conversation_id: Mapped[str] = mapped_column(String,index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    client_key: Mapped[str] = mapped_column(String)
    request_hash: Mapped[str] = mapped_column(String)
    user_message: Mapped[str] = mapped_column(Text)
    task_id: Mapped[str] = mapped_column(String,index=True)
    created_at: Mapped[float] = mapped_column(default=now)


class SharingRule(Owned, Base):
    __tablename__ = "sharing_rules"
    category: Mapped[str] = mapped_column(String)
    grantee_id: Mapped[str] = mapped_column(String)
    __table_args__ = (UniqueConstraint("owner_id", "category", "grantee_id"),)


class BuiltinDeployment(Base):
    __tablename__ = 'builtin_deployments'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    variant: Mapped[str] = mapped_column(String)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String, default='queued')
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[float] = mapped_column(default=now)


class AgentSkill(Owned, Base):
    __tablename__ = 'agent_skills'
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
