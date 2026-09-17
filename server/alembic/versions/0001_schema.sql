CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE approvals (
	invocation_id VARCHAR NOT NULL,
	arguments_hash VARCHAR NOT NULL,
	expires_at FLOAT NOT NULL,
	decision VARCHAR NOT NULL,
	id VARCHAR NOT NULL,
	household_id VARCHAR NOT NULL,
	owner_id VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (invocation_id)
)

;
CREATE INDEX ix_approvals_household_id ON approvals (household_id);
CREATE INDEX ix_approvals_owner_id ON approvals (owner_id);
ALTER TABLE approvals ENABLE ROW LEVEL SECURITY;
ALTER TABLE approvals FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_access ON approvals USING (owner_id = current_setting('homeai.user_id', true) AND household_id = current_setting('homeai.household_id', true)) WITH CHECK (owner_id = current_setting('homeai.user_id', true) AND household_id = current_setting('homeai.household_id', true));

CREATE TABLE audit_entries (
	action VARCHAR NOT NULL,
	resource_id VARCHAR NOT NULL,
	details TEXT NOT NULL,
	created_at FLOAT NOT NULL,
	id VARCHAR NOT NULL,
	household_id VARCHAR NOT NULL,
	owner_id VARCHAR NOT NULL,
	PRIMARY KEY (id)
)

;
CREATE INDEX ix_audit_entries_owner_id ON audit_entries (owner_id);
CREATE INDEX ix_audit_entries_household_id ON audit_entries (household_id);
ALTER TABLE audit_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_entries FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_access ON audit_entries USING (owner_id = current_setting('homeai.user_id', true) AND household_id = current_setting('homeai.household_id', true)) WITH CHECK (owner_id = current_setting('homeai.user_id', true) AND household_id = current_setting('homeai.household_id', true));

CREATE TABLE automations (
	name VARCHAR NOT NULL,
	cron VARCHAR NOT NULL,
	timezone VARCHAR NOT NULL,
	skill TEXT NOT NULL,
	enabled BOOLEAN NOT NULL,
	next_run FLOAT NOT NULL,
	id VARCHAR NOT NULL,
	household_id VARCHAR NOT NULL,
	owner_id VARCHAR NOT NULL,
	PRIMARY KEY (id)
)

;
CREATE INDEX ix_automations_owner_id ON automations (owner_id);
CREATE INDEX ix_automations_household_id ON automations (household_id);
ALTER TABLE automations ENABLE ROW LEVEL SECURITY;
ALTER TABLE automations FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_access ON automations USING (owner_id = current_setting('homeai.user_id', true) AND household_id = current_setting('homeai.household_id', true)) WITH CHECK (owner_id = current_setting('homeai.user_id', true) AND household_id = current_setting('homeai.household_id', true));

CREATE TABLE cloud_disclosures (
	task_id VARCHAR NOT NULL,
	provider_id VARCHAR NOT NULL,
	categories TEXT NOT NULL,
	bytes_sent INTEGER NOT NULL,
	status VARCHAR NOT NULL,
	created_at FLOAT NOT NULL,
	id VARCHAR NOT NULL,
	household_id VARCHAR NOT NULL,
	owner_id VARCHAR NOT NULL,
	PRIMARY KEY (id)
)

;
CREATE INDEX ix_cloud_disclosures_household_id ON cloud_disclosures (household_id);
CREATE INDEX ix_cloud_disclosures_owner_id ON cloud_disclosures (owner_id);
ALTER TABLE cloud_disclosures ENABLE ROW LEVEL SECURITY;
ALTER TABLE cloud_disclosures FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_access ON cloud_disclosures USING (owner_id = current_setting('homeai.user_id', true) AND household_id = current_setting('homeai.household_id', true)) WITH CHECK (owner_id = current_setting('homeai.user_id', true) AND household_id = current_setting('homeai.household_id', true));

CREATE TABLE credentials (
	digest VARCHAR NOT NULL,
	user_id VARCHAR NOT NULL,
	device_id VARCHAR,
	kind VARCHAR NOT NULL,
	expires_at FLOAT NOT NULL,
	PRIMARY KEY (digest)
)

;

CREATE TABLE data_records (
	source VARCHAR NOT NULL,
	source_id VARCHAR NOT NULL,
	kind VARCHAR NOT NULL,
	sensitivity VARCHAR NOT NULL,
	cloud_policy VARCHAR NOT NULL,
	version INTEGER NOT NULL,
	payload TEXT NOT NULL,
	deleted BOOLEAN NOT NULL,
	updated_at FLOAT NOT NULL,
	id VARCHAR NOT NULL,
	household_id VARCHAR NOT NULL,
	owner_id VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (owner_id, source, source_id)
)

;
CREATE INDEX ix_data_records_owner_id ON data_records (owner_id);
CREATE INDEX ix_data_records_household_id ON data_records (household_id);
ALTER TABLE data_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_records FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_access ON data_records USING (owner_id = current_setting('homeai.user_id', true) AND household_id = current_setting('homeai.household_id', true)) WITH CHECK (owner_id = current_setting('homeai.user_id', true) AND household_id = current_setting('homeai.household_id', true));

CREATE TABLE devices (
	id VARCHAR NOT NULL,
	user_id VARCHAR NOT NULL,
	public_key TEXT NOT NULL,
	name VARCHAR NOT NULL,
	revoked BOOLEAN NOT NULL,
	PRIMARY KEY (id)
)

;
CREATE INDEX ix_devices_user_id ON devices (user_id);

CREATE TABLE event_consumptions (
	id VARCHAR NOT NULL,
	created_at FLOAT NOT NULL,
	PRIMARY KEY (id)
)

;

CREATE TABLE event_outbox (
	id SERIAL NOT NULL,
	event_id VARCHAR NOT NULL,
	household_id VARCHAR NOT NULL,
	owner_id VARCHAR NOT NULL,
	kind VARCHAR NOT NULL,
	resource_id VARCHAR NOT NULL,
	published BOOLEAN NOT NULL,
	created_at FLOAT NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (event_id)
)

;
ALTER TABLE event_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE event_outbox FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_access ON event_outbox USING (owner_id = current_setting('homeai.user_id', true) AND household_id = current_setting('homeai.household_id', true)) WITH CHECK (owner_id = current_setting('homeai.user_id', true) AND household_id = current_setting('homeai.household_id', true));

CREATE TABLE grants (
	record_id VARCHAR NOT NULL,
	grantee_id VARCHAR NOT NULL,
	id VARCHAR NOT NULL,
	household_id VARCHAR NOT NULL,
	owner_id VARCHAR NOT NULL,
	PRIMARY KEY (id)
)

;
CREATE INDEX ix_grants_owner_id ON grants (owner_id);
CREATE INDEX ix_grants_household_id ON grants (household_id);
CREATE INDEX ix_grants_grantee_id ON grants (grantee_id);
CREATE INDEX ix_grants_record_id ON grants (record_id);
ALTER TABLE grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE grants FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_access ON grants USING (owner_id = current_setting('homeai.user_id', true) AND household_id = current_setting('homeai.household_id', true)) WITH CHECK (owner_id = current_setting('homeai.user_id', true) AND household_id = current_setting('homeai.household_id', true));

CREATE TABLE principals (
	id VARCHAR NOT NULL,
	household_id VARCHAR NOT NULL,
	name VARCHAR NOT NULL,
	role VARCHAR NOT NULL,
	PRIMARY KEY (id)
)

;
CREATE INDEX ix_principals_household_id ON principals (household_id);

CREATE TABLE providers (
	id VARCHAR NOT NULL,
	manifest TEXT NOT NULL,
	previous_manifest TEXT,
	enabled BOOLEAN NOT NULL,
	health VARCHAR NOT NULL,
	PRIMARY KEY (id)
)

;

CREATE TABLE record_versions (
	record_id VARCHAR NOT NULL,
	version INTEGER NOT NULL,
	payload TEXT NOT NULL,
	id VARCHAR NOT NULL,
	household_id VARCHAR NOT NULL,
	owner_id VARCHAR NOT NULL,
	PRIMARY KEY (id)
)

;
CREATE INDEX ix_record_versions_household_id ON record_versions (household_id);
CREATE INDEX ix_record_versions_record_id ON record_versions (record_id);
CREATE INDEX ix_record_versions_owner_id ON record_versions (owner_id);
ALTER TABLE record_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE record_versions FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_access ON record_versions USING (owner_id = current_setting('homeai.user_id', true) AND household_id = current_setting('homeai.household_id', true)) WITH CHECK (owner_id = current_setting('homeai.user_id', true) AND household_id = current_setting('homeai.household_id', true));

CREATE TABLE request_nonces (
	id VARCHAR NOT NULL,
	expires_at FLOAT NOT NULL,
	PRIMARY KEY (id)
)

;

CREATE TABLE secrets (
	provider_id VARCHAR NOT NULL,
	value TEXT NOT NULL,
	id VARCHAR NOT NULL,
	household_id VARCHAR NOT NULL,
	owner_id VARCHAR NOT NULL,
	PRIMARY KEY (id)
)

;
CREATE INDEX ix_secrets_household_id ON secrets (household_id);
CREATE INDEX ix_secrets_owner_id ON secrets (owner_id);
ALTER TABLE secrets ENABLE ROW LEVEL SECURITY;
ALTER TABLE secrets FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_access ON secrets USING (owner_id = current_setting('homeai.user_id', true) AND household_id = current_setting('homeai.household_id', true)) WITH CHECK (owner_id = current_setting('homeai.user_id', true) AND household_id = current_setting('homeai.household_id', true));

CREATE TABLE tasks (
	idempotency_key VARCHAR NOT NULL,
	request_hash VARCHAR NOT NULL,
	status VARCHAR NOT NULL,
	request TEXT NOT NULL,
	result TEXT,
	error VARCHAR,
	created_at FLOAT NOT NULL,
	deadline FLOAT NOT NULL,
	cancel_requested BOOLEAN NOT NULL,
	id VARCHAR NOT NULL,
	household_id VARCHAR NOT NULL,
	owner_id VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (owner_id, idempotency_key)
)

;
CREATE INDEX ix_tasks_household_id ON tasks (household_id);
CREATE INDEX ix_tasks_owner_id ON tasks (owner_id);
ALTER TABLE tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE tasks FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_access ON tasks USING (owner_id = current_setting('homeai.user_id', true) AND household_id = current_setting('homeai.household_id', true)) WITH CHECK (owner_id = current_setting('homeai.user_id', true) AND household_id = current_setting('homeai.household_id', true));

CREATE TABLE tool_calls (
	task_id VARCHAR NOT NULL,
	step INTEGER NOT NULL,
	capability VARCHAR NOT NULL,
	arguments TEXT NOT NULL,
	arguments_hash VARCHAR NOT NULL,
	status VARCHAR NOT NULL,
	result TEXT,
	id VARCHAR NOT NULL,
	household_id VARCHAR NOT NULL,
	owner_id VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (task_id, step)
)

;
CREATE INDEX ix_tool_calls_owner_id ON tool_calls (owner_id);
CREATE INDEX ix_tool_calls_household_id ON tool_calls (household_id);
CREATE INDEX ix_tool_calls_task_id ON tool_calls (task_id);
ALTER TABLE tool_calls ENABLE ROW LEVEL SECURITY;
ALTER TABLE tool_calls FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_access ON tool_calls USING (owner_id = current_setting('homeai.user_id', true) AND household_id = current_setting('homeai.household_id', true)) WITH CHECK (owner_id = current_setting('homeai.user_id', true) AND household_id = current_setting('homeai.household_id', true));
CREATE POLICY granted_records ON data_records FOR SELECT USING (household_id = current_setting('homeai.household_id', true) AND NOT deleted AND id IN (SELECT record_id FROM grants WHERE grantee_id = current_setting('homeai.user_id', true)));
CREATE POLICY received_grants ON grants FOR SELECT USING (household_id = current_setting('homeai.household_id', true) AND grantee_id = current_setting('homeai.user_id', true));
