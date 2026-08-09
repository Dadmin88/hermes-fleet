CREATE TABLE fleet_state_schema (
    version INTEGER PRIMARY KEY CHECK (version = 1)
) STRICT;

INSERT INTO fleet_state_schema(version) VALUES (1);

CREATE TABLE managed_projections (
    source TEXT NOT NULL,
    network_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    projection_generation TEXT NOT NULL,
    membership_generation TEXT NOT NULL,
    binding_generation TEXT NOT NULL,
    document_json TEXT NOT NULL CHECK (json_valid(document_json)),
    PRIMARY KEY (source, network_id, device_id)
) STRICT;

CREATE TABLE operator_projection_denies (
    source TEXT NOT NULL,
    network_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (
        operation IN ('fleet.health', 'fleet.inventory', 'fleet.message')
    ),
    PRIMARY KEY (source, network_id, device_id, operation)
) STRICT;

CREATE TABLE run_bindings (
    task_id TEXT PRIMARY KEY,
    state_json TEXT NOT NULL CHECK (json_valid(state_json))
) STRICT;
