# Fleet managed projection V1

## Status and scope

This document describes the accepted minimal N7 V1 exercised by Fleet local
control and the Nodescale client. Its two-repository exact-tree proof, separate
SIGTERM proof, cleanup verification, bounded release review, and fresh CI are
complete. It remains separate from the Fleet v0.1 Katana-to-VPS communication
baseline in [Architecture](architecture.md).

N7 is a local authenticated projection boundary for Nodescale-managed Fleet
state. It is not a Keryx transport, public API, remote control endpoint, Fleet
execution path, or direct Nodescale access to Fleet data.

## Local transport and peer authentication

Fleet listens only on a Linux Unix-domain socket (UDS). Before reading a request
frame it obtains `SO_PEERCRED`; it dispatches only when the peer UID exactly
equals the configured Nodescale service UID. A JSON UID, token, shared secret,
process name, socket pathname, PID, GID, group membership, or forwarded
credential is not authentication. Absent, unreadable, unsupported, or
mismatched peer credentials close the connection before dispatch.

The socket is Fleet-owned and local-only. No TCP, Tailscale, relay, HTTP, or
public listener is created by this surface. There is no bearer credential or
caller-chosen principal in V1.

### Local path provisioning

Before Fleet starts, an administrator must pre-provision both parent directories
at the exact absolute paths supplied to `fleet-managed-projection`. Fleet never
creates either parent. It walks every existing lexical path component with
`lstat` and rejects a symlink, missing component, or non-directory before using
the socket or database path. This prevents a configured parent from redirecting
Fleet through symlink traversal.

The service UID must own both final parent directories. The database parent must
be private `0700`: it requires owner read/write/traverse permission and has no
group or other permission bits. The socket parent is also `0700` by default. A
configured `--socket-gid GID` may instead use a service-UID-owned `0750` socket
parent whose GID is exactly `GID`; no other permission bits are allowed. Group
transport access never affects the exact `SO_PEERCRED` UID authorization check.
World read, write, or traversal is never permitted on either final parent.

Fleet creates a missing database file only after those parent checks and creates
or tightens it to a regular service-UID-owned `0600` file. A pre-existing
database must be a regular service-UID-owned file; symlinks and other file types
are rejected. Fleet creates its socket only after the same socket-parent check,
then verifies its service UID, inode, GID (when configured), and exact mode:
`0600` without `--socket-gid`, or `0660` with it.

### Systemd user-unit modes

The supplied `ops/systemd/fleet-managed-projection.service` is deliberately a
same-UID launch: its environment file supplies only
`FLEET_MANAGED_PROJECTION_SOCKET`, `FLEET_MANAGED_PROJECTION_DATABASE`, and
`FLEET_MANAGED_PROJECTION_ALLOWED_UID`, with the allowed UID equal to the Fleet
service UID. It does not expand an optional socket-GID variable, so an omitted
GID cannot make the user unit fail at launch. Pre-provision a service-UID-owned
`0700` socket parent for this default; Fleet creates a `0600` socket. `UMask=0077`
is defense in depth and does not replace parent validation.

A distinct Nodescale UID is an explicit cross-UID deployment choice, not a
property of the supplied unit. Pre-provision a service-UID-owned `0750` socket
parent with the configured group as its exact GID (group write remains
forbidden), set `FLEET_MANAGED_PROJECTION_ALLOWED_UID` to the distinct Nodescale
UID, and add this user-unit drop-in at
`~/.config/systemd/user/fleet-managed-projection.service.d/cross-uid.conf`:

```ini
[Service]
Environment=FLEET_MANAGED_PROJECTION_SOCKET_GID=12345
ExecStart=
ExecStart=%h/.local/share/hermes-fleet/venv/bin/fleet-managed-projection --socket ${FLEET_MANAGED_PROJECTION_SOCKET} --database ${FLEET_MANAGED_PROJECTION_DATABASE} --allowed-uid ${FLEET_MANAGED_PROJECTION_ALLOWED_UID} --socket-gid ${FLEET_MANAGED_PROJECTION_SOCKET_GID} --shutdown-timeout 20 --log-level INFO
```

Replace `12345` with the pre-provisioned group ID, then run
`systemctl --user daemon-reload` and restart the unit. This explicit mode
creates a `0660` socket for group transport only; it never relaxes the exact
`SO_PEERCRED` allowed-UID check.

## Exact wire contract

Every request and response is one four-byte unsigned big-endian length followed
by UTF-8 JSON; the payload length must be `1..=32768`. The server bounds
allocation before reading the payload. A client writes exactly one request frame
and then write-half-closes the stream (`shutdown(Write)` / `SHUT_WR`). Before
JSON parsing or dispatch, the server reads to that EOF under the bounded
connection timeout; absence of the half-close or any trailing byte is rejected.
Zero, oversized, truncated, invalid-UTF-8 and malformed request frames use the
closed `invalid_request` response when the server can safely write one.

Request JSON is closed: duplicate keys, number literals, unknown/missing keys,
non-object payloads, coercion, and alternate envelopes are rejected. Requests
have top-level `schema` and `kind` only, plus the variant argument:

| Kind | Exact top-level keys |
| --- | --- |
| `capabilities` | `schema`, `kind` |
| `apply` | `schema`, `kind`, `document` |
| `inspect` | `schema`, `kind`, `selector` |

`schema` is exactly `fleet.managed-projection.v1`. There is no `request_id`,
`body`, token, principal, or extension object. `apply.document` has exactly
`source`, `network_id`, `device_id`, `projection_generation`,
`membership_generation`, `binding_generation`, `content_hash`, `operation`,
`generated_operations`, and `provenance`. `provenance` has exactly `source`,
`network_id`, `device_id`, and `snapshot`; its identity must match the document.
`inspect.selector` has exactly `source`, `network_id`, and `device_id`.

## Exact responses and durable outcomes

The successful response forms are:

```json
{"schema":"fleet.managed-projection.v1","kind":"capabilities","ok":true,"result":{"kinds":["capabilities","apply","inspect"]}}
{"schema":"fleet.managed-projection.v1","kind":"apply","ok":true,"result":{"outcome":"applied"}}
{"schema":"fleet.managed-projection.v1","kind":"inspect","ok":true,"result":{"generated":null,"effective":null}}
```

A request failure that reaches response handling is exactly:

```json
{"schema":"fleet.managed-projection.v1","kind":"error","ok":false,"error":"invalid_request"}
```

The exact durable `apply` outcomes are `applied`, `already_applied`, `conflict`,
`stale`, and `gap`. `ok:true` is a handled local-control response, not proof of
future durable observation. An exact replay is `already_applied`; a
same-generation non-identical projection is `conflict`; lower generation is
`stale`; a non-successor generation is `gap`.

`inspect` is the authoritative Fleet durable read-back. An absent record is
exactly `generated:null` plus `effective:null`. A present `generated` result
contains state, the three persisted generations, content hash, allowed
operations, and provenance. Its separate `effective` result contains state,
allowed operations, and `operator_denied_operations`. Apply request data or an
apply response cannot substitute for this read-back, including after restart or
an uncertain response.

## Ownership and generated authorization

Fleet stores managed projections in its own durable store, separate from Keryx
task/result data, Fleet execution-binding records, and operator-owned inventory.
The durable identity is `(source, network_id, device_id)`. Fleet persists
generated state separately from local operator-deny policy; a Nodescale
projection cannot remove or override local deny state.

Generated operation names are an exact allowlist:

- `fleet.health`
- `fleet.inventory`
- `fleet.message`

The allowlist is enforced at durable write and effective authorization. N7
cannot generate `fleet.hermes.run`, execution, shell, file, process, admin,
wildcard, enrollment, role-implied, or future-operation authority. Effective
authorization requires an active generated record, an allowlisted generated
operation, and no local operator deny. Disable or remove transitions materialize
no generated grants.

## Non-goals and acceptance boundary

N7 does not add a network listener, public API, direct database access for
Nodescale, operator-policy writer, or remote Hermes execution. It does not
change Fleet v0.1 execution authority: `fleet.hermes.run` is never a generated
N7 grant.

Acceptance used the archived Nodescale and Fleet candidate trees documented in
Nodescale's `proofs/n7/README.md`. The proof demonstrated real peer-UID
enforcement, framing and closed-parser failures, exact request/response forms and
outcomes, generated-grant allowlisting, durable restart/read-back, and a
separate SIGTERM cleanup run. Both repository release heads then passed bounded
review and fresh CI before merge.

## License

This document describes Hermes Fleet under the GNU Affero General Public License
v3.0 only (`AGPL-3.0-only`). See [LICENSE](../LICENSE).
