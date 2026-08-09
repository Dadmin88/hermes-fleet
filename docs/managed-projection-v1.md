# Fleet Managed Projection V1

`fleet.managed-projection.v1` is the local authenticated contract used to project Nodescale-managed state into Hermes Fleet.

It is not a Keryx transport, public network API, remote execution path, or direct database-sharing mechanism. Fleet owns the receiving service and the durable state it creates.

The production boundary is the permanent Rust `fleet-managed-control` binary in `crates/fleet-control`. It authenticates the UDS peer, parses this language-neutral protocol, delegates projection decisions to `fleet-domain`, and persists only through `fleet-state`. The Python implementation remains the independent behavioral oracle and compatibility implementation.

## Security boundary

Fleet listens on a Linux Unix-domain socket and authenticates the connecting process with `SO_PEERCRED` before reading or dispatching a request.

The authenticated peer UID must exactly match the configured Nodescale service UID. The following are not substitutes for that check:

- a UID or identity field inside JSON;
- a bearer token;
- process name;
- socket pathname;
- PID or GID alone;
- forwarded credentials;
- group membership.

If peer credentials are missing, unsupported, unreadable, or mismatched, Fleet closes the connection without dispatching the request.

No TCP, HTTP, Tailscale, relay, or public listener is created by this interface.

## Filesystem requirements

The socket and database parent directories are administrator-provisioned. Fleet validates every existing path component and rejects symlink traversal or unexpected file types.

Recommended defaults:

- Fleet service UID owns the final socket and database parents;
- database parent mode is `0700`;
- socket parent mode is `0700` for same-UID deployments;
- a deliberately configured cross-UID deployment may use a service-owned `0750` socket parent with an exact configured group;
- the database file is a regular service-owned `0600` file;
- the socket is `0600` by default or `0660` only when an explicit socket GID is configured.

Group access affects only transport reachability. Authorization still requires the exact configured peer UID from `SO_PEERCRED`.

## Wire format

Each request and response is:

1. a four-byte unsigned big-endian length;
2. a UTF-8 JSON payload of `1..=32768` bytes.

The server bounds allocation before reading the body. A client sends exactly one request and then write-half-closes the connection. Trailing bytes, missing half-close, truncation, invalid UTF-8, malformed JSON, duplicate keys, unknown fields, and unsupported schema values are rejected.

The schema identifier is exactly:

```text
fleet.managed-projection.v1
```

Supported request kinds are:

| Kind | Required top-level fields |
| --- | --- |
| `capabilities` | `schema`, `kind` |
| `apply` | `schema`, `kind`, `document` |
| `inspect` | `schema`, `kind`, `selector` |

The protocol has no caller-selected principal, request identity, bearer token, or generic extension object.

## Managed identity

The durable managed identity is:

```text
(source, network_id, device_id)
```

An `apply` document carries:

- source;
- network ID;
- device ID;
- projection generation;
- membership generation;
- binding generation;
- content hash;
- managed operation/state transition;
- generated operation set;
- provenance.

The provenance identity must match the document identity.

An `inspect` selector contains only source, network ID, and device ID.

## Responses

Successful responses use the same schema and an explicit result object. Examples:

```json
{"schema":"fleet.managed-projection.v1","kind":"capabilities","ok":true,"result":{"kinds":["capabilities","apply","inspect"]}}
```

```json
{"schema":"fleet.managed-projection.v1","kind":"apply","ok":true,"result":{"outcome":"applied"}}
```

```json
{"schema":"fleet.managed-projection.v1","kind":"inspect","ok":true,"result":{"generated":null,"effective":null}}
```

A request failure that reaches response handling uses the closed error form:

```json
{"schema":"fleet.managed-projection.v1","kind":"error","ok":false,"error":"invalid_request"}
```

## Durable apply semantics

The recognized durable outcomes are:

- `applied`;
- `already_applied`;
- `conflict`;
- `stale`;
- `regression`;
- `gap`.

An exact replay may return `already_applied`. A same-generation request with different content is a conflict. Lower generations are stale. A newer projection that moves membership or binding generation backward is a regression. A request that skips the required successor projection generation is a gap.

An `ok: true` apply response means the request was handled. It does not replace authoritative read-back after an uncertain client outcome or restart.

`inspect` is the durable read-back surface.

## Generated authorization

Managed projection can generate only this baseline operation set:

- `fleet.health`;
- `fleet.inventory`;
- `fleet.message`.

It cannot generate:

- `fleet.hermes.run`;
- shell or process execution;
- file access;
- administrative wildcard authority;
- future operations by implication;
- role-derived grants outside the explicit operation set.

Fleet enforces this allowlist both when durable state is written and when effective authorization is calculated.

## Operator deny precedence

Managed state is stored separately from operator-owned deny policy.

Effective authorization requires:

1. an active managed record;
2. the requested operation to be explicitly generated and allowlisted;
3. no applicable local operator deny.

A Nodescale projection cannot remove or override local deny state.

## State ownership

Fleet owns the managed projection database. Nodescale communicates only through the local control protocol and does not read or write Fleet databases directly.

Managed state is also separate from:

- Keryx task/result state;
- Fleet task-to-Hermes-run bindings;
- operator-managed node inventory.

This separation keeps lifecycle, transport, execution, and generated policy from becoming one ambiguous state machine.

## Systemd deployment

The supplied `ops/systemd/fleet-managed-projection.service` is a same-UID default. Its environment file provides the socket path, database path, and allowed UID; the stock unit does not add a socket GID. Use a private `0700` socket parent for this mode.

A distinct Nodescale UID is an explicit cross-UID deployment choice. Pre-provision a Fleet-service-owned `0750` socket parent with the intended group as its exact GID, set `FLEET_MANAGED_PROJECTION_ALLOWED_UID` to the Nodescale service UID, and create the user-unit drop-in:

```text
~/.config/systemd/user/fleet-managed-projection.service.d/cross-uid.conf
```

Example:

```ini
[Service]
Environment=FLEET_MANAGED_PROJECTION_SOCKET_GID=12345
ExecStart=
ExecStart=%h/.local/bin/fleet-managed-control --socket ${FLEET_MANAGED_PROJECTION_SOCKET} --database ${FLEET_MANAGED_PROJECTION_DATABASE} --allowed-uid ${FLEET_MANAGED_PROJECTION_ALLOWED_UID} --socket-gid ${FLEET_MANAGED_PROJECTION_SOCKET_GID}
```

Replace `12345` with the pre-provisioned group ID, then reload the user systemd manager and restart the unit. This mode creates a `0660` socket for group transport only. It does not weaken the exact `SO_PEERCRED` allowed-UID authorization check.

Do not loosen filesystem permissions as a substitute for configuring the correct peer UID.

## Verification requirements

A conforming implementation should test at least:

- exact peer-UID enforcement;
- symlink and file-type rejection;
- frame length, half-close, trailing-byte, UTF-8, and strict-JSON failures;
- exact request and response shapes;
- generation replay, conflict, stale, and gap behavior;
- durable restart and `inspect` read-back;
- generated-operation allowlisting;
- local deny precedence;
- rejection of any attempt to generate `fleet.hermes.run`;
- bounded shutdown and cleanup behavior.

The verification should run against the exact revision being evaluated. Historical test results are release history, not proof for a changed tree.

## Non-goals

Managed projection does not create:

- a public Fleet control API;
- remote Nodescale database access;
- a Keryx task;
- a Hermes run;
- operator-policy mutation;
- implicit role-to-operation authorization.

Its purpose is narrow: authenticated local projection of bounded managed Fleet state with durable generation semantics and authoritative Fleet-owned read-back.
