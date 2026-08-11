CREATE TABLE workflow_definitions (
    workflow_id TEXT PRIMARY KEY,
    latest_version INTEGER NOT NULL CHECK (latest_version >= 1),
    deleted INTEGER NOT NULL CHECK (deleted IN (0, 1)),
    created_at_ms INTEGER NOT NULL CHECK (created_at_ms > 0),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms > 0)
) STRICT;

CREATE TABLE workflow_versions (
    workflow_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    content_hash TEXT NOT NULL
        CHECK (length(content_hash) = 64)
        CHECK (content_hash = lower(content_hash)),
    document_json TEXT NOT NULL CHECK (json_valid(document_json)),
    created_at_ms INTEGER NOT NULL CHECK (created_at_ms > 0),
    PRIMARY KEY (workflow_id, version),
    FOREIGN KEY (workflow_id) REFERENCES workflow_definitions(workflow_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
) STRICT;

DROP TABLE fleet_state_schema;

CREATE TABLE fleet_state_schema (
    version INTEGER PRIMARY KEY CHECK (version = 5)
) STRICT;

INSERT INTO fleet_state_schema(version) VALUES (5);
