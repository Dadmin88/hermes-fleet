DELETE FROM node_observations;

DROP TABLE fleet_state_schema;

CREATE TABLE fleet_state_schema (
    version INTEGER PRIMARY KEY CHECK (version = 3)
) STRICT;

INSERT INTO fleet_state_schema(version) VALUES (3);
