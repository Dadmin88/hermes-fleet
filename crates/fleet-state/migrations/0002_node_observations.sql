DROP TABLE fleet_state_schema;

CREATE TABLE fleet_state_schema (
    version INTEGER PRIMARY KEY CHECK (version = 2)
) STRICT;

INSERT INTO fleet_state_schema(version) VALUES (2);

CREATE TABLE node_observations (
    source TEXT NOT NULL,
    network_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    observed_at_ms INTEGER NOT NULL CHECK (observed_at_ms > 0),
    received_at_ms INTEGER NOT NULL CHECK (received_at_ms > 0),
    observation_json TEXT NOT NULL CHECK (json_valid(observation_json)),
    PRIMARY KEY (source, network_id, device_id),
    FOREIGN KEY (source, network_id, device_id)
        REFERENCES managed_projections(source, network_id, device_id)
        ON UPDATE CASCADE ON DELETE CASCADE
) STRICT;
