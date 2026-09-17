
CREATE TABLE memory_candidates (
	source_ids TEXT NOT NULL,
	content TEXT NOT NULL,
	status VARCHAR NOT NULL,
	created_at FLOAT NOT NULL,
	id VARCHAR NOT NULL,
	household_id VARCHAR NOT NULL,
	owner_id VARCHAR NOT NULL,
	PRIMARY KEY (id)
)

;
CREATE INDEX ix_memory_candidates_household_id ON memory_candidates (household_id);
CREATE INDEX ix_memory_candidates_owner_id ON memory_candidates (owner_id);
ALTER TABLE memory_candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_candidates FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_access ON memory_candidates USING (owner_id = current_setting('homeai.user_id', true) AND household_id = current_setting('homeai.household_id', true)) WITH CHECK (owner_id = current_setting('homeai.user_id', true) AND household_id = current_setting('homeai.household_id', true));

CREATE TABLE memory_vectors (
	record_id VARCHAR NOT NULL,
	model VARCHAR NOT NULL,
	embedding TEXT NOT NULL,
	id VARCHAR NOT NULL,
	household_id VARCHAR NOT NULL,
	owner_id VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (record_id)
)

;
CREATE INDEX ix_memory_vectors_household_id ON memory_vectors (household_id);
CREATE INDEX ix_memory_vectors_owner_id ON memory_vectors (owner_id);
ALTER TABLE memory_vectors ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_vectors FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_access ON memory_vectors USING (owner_id = current_setting('homeai.user_id', true) AND household_id = current_setting('homeai.household_id', true)) WITH CHECK (owner_id = current_setting('homeai.user_id', true) AND household_id = current_setting('homeai.household_id', true));

CREATE TABLE memory_deletion_jobs (
	provider_id VARCHAR NOT NULL,
	event_id VARCHAR NOT NULL,
	status VARCHAR NOT NULL,
	attempts INTEGER NOT NULL,
	error VARCHAR,
	id VARCHAR NOT NULL,
	household_id VARCHAR NOT NULL,
	owner_id VARCHAR NOT NULL,
	PRIMARY KEY (id)
)

;
CREATE INDEX ix_memory_deletion_jobs_owner_id ON memory_deletion_jobs (owner_id);
CREATE INDEX ix_memory_deletion_jobs_household_id ON memory_deletion_jobs (household_id);
ALTER TABLE memory_deletion_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_deletion_jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_access ON memory_deletion_jobs USING (owner_id = current_setting('homeai.user_id', true) AND household_id = current_setting('homeai.household_id', true)) WITH CHECK (owner_id = current_setting('homeai.user_id', true) AND household_id = current_setting('homeai.household_id', true));
ALTER TABLE memory_vectors ADD COLUMN search_vector vector;
