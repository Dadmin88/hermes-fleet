CREATE TABLE managed_node_aliases (
    source TEXT NOT NULL,
    network_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    binding_generation TEXT NOT NULL,
    alias TEXT NOT NULL
        CHECK (length(alias) BETWEEN 1 AND 128)
        CHECK (alias = trim(alias)),
    PRIMARY KEY (source, network_id, device_id),
    FOREIGN KEY (source, network_id, device_id)
        REFERENCES managed_projections(source, network_id, device_id)
        ON UPDATE CASCADE ON DELETE CASCADE
) STRICT;

DROP TABLE fleet_state_schema;

CREATE TABLE fleet_state_schema (
    version INTEGER PRIMARY KEY CHECK (version = 4)
) STRICT;

INSERT INTO fleet_state_schema(version) VALUES (4);
