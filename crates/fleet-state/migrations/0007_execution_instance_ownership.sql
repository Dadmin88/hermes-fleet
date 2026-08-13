CREATE UNIQUE INDEX execution_instances_backend_realization_owner
ON execution_instances(
    json_extract(state_json, '$.phase.backend_kind'),
    json_extract(state_json, '$.phase.realization_id')
)
WHERE json_extract(state_json, '$.phase.backend_kind') IS NOT NULL
  AND json_extract(state_json, '$.phase.realization_id') IS NOT NULL;

CREATE UNIQUE INDEX execution_instances_keryx_task_owner
ON execution_instances(json_extract(state_json, '$.phase.keryx_task_id'))
WHERE json_extract(state_json, '$.phase.keryx_task_id') IS NOT NULL;

DROP TABLE fleet_state_schema;

CREATE TABLE fleet_state_schema (
    version INTEGER PRIMARY KEY CHECK (version = 7)
) STRICT;

INSERT INTO fleet_state_schema(version) VALUES (7);
