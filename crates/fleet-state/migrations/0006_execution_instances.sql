CREATE TABLE execution_instances (
    instance_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    generation INTEGER NOT NULL CHECK (generation > 0),
    state_json TEXT NOT NULL CHECK (json_valid(state_json)),
    created_at_ms INTEGER NOT NULL CHECK (created_at_ms > 0),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms)
) STRICT;

DROP TABLE fleet_state_schema;

CREATE TABLE fleet_state_schema (
    version INTEGER PRIMARY KEY CHECK (version = 6)
) STRICT;

INSERT INTO fleet_state_schema(version) VALUES (6);
