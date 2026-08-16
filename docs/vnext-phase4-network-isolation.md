# Hermes Fleet vNext Phase 4 acceptance: network isolation

Status: **COMPLETE**

Phase 4 turns workshop networking into an explicit, authority-bound, fail-closed surface. The default remains offline. Model-provider traffic remains host-side whenever possible. A workshop receives direct egress only through a Fleet-owned mediated path that the Agent cannot bypass by ignoring proxy configuration.

This phase deliberately does not implement the full Phase 10 `RunAuthority` object early. `NetworkAuthorityScope` is the Phase 4 enforcement adapter that consumes an already-verified RunAuthority hash and, for the broadest mode, an explicit set of separately approved internet-approval hashes. Phase 10 will become the producer of that verified scope.

## Four explicit modes

Phase 4 accepts exactly four modes:

| Mode | Workshop topology | Direct workshop egress | Additional authority |
| --- | --- | --- | --- |
| `none` | Docker `network=none` | none | none |
| `provider-only` | Docker `network=none` | none; provider calls stay host-side | exact RunAuthority hash |
| `project-allowlist` | one Fleet internal-only Docker network | exact pinned destinations through Fleet gateway | exact RunAuthority hash |
| `explicitly-approved-internet` | one Fleet internal-only Docker network | still exact pinned destinations through Fleet gateway | exact RunAuthority hash plus separate approval hash |

`explicitly-approved-internet` is not unrestricted internet. The approval permits the broader posture, while the run still carries an exact destination/IP/port set. The Agent cannot add hosts, IPs, ports, another proxy, another Docker network, or another address family.

## Authority and DNS binding

`NetworkDestination` binds one normalized hostname or literal public IPv4 destination to:

- the complete public IPv4 answer set observed before authorization;
- an exact permitted port set.

`NetworkGrant` binds:

- one of the four modes;
- the exact verified RunAuthority hash;
- the exact destination set for direct modes;
- a separate approval hash for `explicitly-approved-internet`.

`NetworkAuthorityScope` permits a grant only when the RunAuthority hash is exact. The explicit-internet mode additionally requires its approval hash to be present in the scope's explicit approved-internet set. That approval hash may not equal the RunAuthority hash.

For hostname destinations Fleet performs DNS verification twice:

1. before any Docker mutation, Fleet resolves the hostname and requires the full public IPv4 answer set to equal the authorization-time pinned set;
2. on every CONNECT request, the gateway resolves independently and again requires the full runtime public IPv4 answer set to equal the pinned set.

A changed, private, special, empty, or otherwise unprovable answer set fails closed. The gateway never silently adopts a new DNS answer.

IPv6 direct egress is deliberately unsupported in this first slice, preventing an unverified second address family from becoming a bypass path.

## Public-only direct destination policy

Direct egress rejects special/non-public destination classes, including:

- loopback;
- RFC1918/private LAN space;
- shared CGNAT/Tailscale-style address space;
- link-local/cloud-metadata space;
- documentation/reserved space;
- multicast;
- unspecified addresses;
- IPv6 in the current direct-egress slice.

Direct DNS ports and ordinary remote Docker daemon/swarm ports are categorically forbidden. Other ports must still be present in the exact destination grant.

Hostnames such as localhost/local/internal/LAN-style names are rejected before enforcement construction.

## Topology-enforced proxy non-bypass

Direct modes do not place the workshop on the ordinary Docker bridge.

Fleet creates a per-execution Docker `--internal` network with exact ownership labels. The workshop is attached only to that internal network and has no ordinary external default route. Its direct DNS is disabled with an unusable loopback resolver, so it cannot independently resolve external domains.

The workshop receives only these proxy bindings:

- `HTTP_PROXY`;
- `HTTPS_PROXY`;
- lowercase equivalents;
- empty `NO_PROXY`/`no_proxy`.

`ALL_PROXY` and alternate proxy state are not accepted by the independent Hermes verifier.

Because the workshop itself has no external route, raw sockets cannot bypass policy merely by ignoring proxy environment variables. The proxy variables are a usability mechanism; the internal-only network is the enforcement boundary.

## Fleet-owned egress gateway

For direct modes Fleet creates a disposable egress-gateway sidecar using a digest-pinned image. The gateway is not a host shell and receives no host mounts.

The gateway is hardened with:

- numeric non-root UID/GID;
- read-only root filesystem;
- `CapDrop=ALL`;
- no added capabilities;
- `no-new-privileges`;
- positive CPU/RAM/PID limits;
- bounded private tmpfs for generated policy/runtime files;
- no host bind or named-volume mounts;
- bounded Docker `local` audit logs;
- exact Fleet execution, network-policy, network-authority, role, and gateway-script labels.

The generated proxy script and non-secret policy are passed as bounded base64 environment material, decoded only into the gateway's disposable tmpfs, and then used by an exact verified startup command.

Fleet verifies the gateway's observed Docker document, including:

- exact image;
- exact startup command;
- exact generated script hash;
- exact policy hash and authority labels;
- exact non-root user and hardening;
- exact CPU/RAM/PID limits;
- no host mounts;
- exact bounded log-driver configuration;
- exact internal-network membership;
- exact internal IP once running.

## Safe gateway startup ordering

Docker assigns the gateway's internal IP only once the container starts. Phase 4 therefore uses this order:

1. create gateway attached only to the Fleet internal-only network;
2. verify the pre-start container posture without pretending an IP exists yet;
3. start the gateway while no external bridge is attached;
4. derive Docker's assigned internal IPv4 address;
5. verify the gateway listener is bound only to that exact internal IPv4 address and port;
6. prove there is no matching IPv6 listener;
7. only then attach the gateway to Docker's outbound bridge;
8. re-inspect the container and prove the internal IP and listener did not change or widen.

A recovered gateway that already has the outbound bridge while not running is rejected rather than restarted in an ambiguous network posture.

## Gateway enforcement

The gateway accepts CONNECT only. Each request is normalized and evaluated against the exact grant.

For each request it:

1. normalizes host and port;
2. rejects categorically forbidden ports;
3. resolves the hostname itself;
4. rejects non-public/special resolution;
5. requires exactly one matching authorized destination;
6. requires the requested port in the authorized port set;
7. requires the complete runtime DNS answer set to equal the pinned set;
8. opens the upstream connection only after all checks pass.

Failures produce a denial rather than fallback to another route or proxy.

## Lateral and management-network defense

Fleet continuously verifies the internal Docker network. The permitted membership is only:

- the exact Fleet egress gateway; and
- when running, the exact expected workshop container.

An unexpected third container joining the internal network causes verification to fail closed.

The live Docker proof additionally verifies:

- direct workshop TCP to a public destination is unavailable without the gateway;
- direct workshop DNS is unavailable;
- cloud-metadata-class destinations are denied;
- Tailscale-management/shared-space destinations are denied;
- unlisted public destinations are denied;
- remote Docker management ports are denied;
- an injected lateral peer is detected;
- gateway/network resources are absent after cleanup.

## Audit

Every mediated gateway decision emits a bounded `FLEET_EGRESS_V1` record containing only non-secret decision evidence:

- timestamp;
- allow/deny decision;
- reason code;
- requested host;
- requested port;
- runtime resolved public IP set;
- exact network-policy hash.

Fleet parses these records through a bounded audit surface and rejects malformed records, policy-hash mismatch, unsafe IP data, or excessive log volume.

Topology-level raw bypass attempts are blocked before reaching the gateway and are proven separately by the live network-isolation tests. Every request that reaches the mediated network decision point is audited by the gateway.

## Independent Hermes verification

Hermes Agent branch `vnext/phase4-network-isolation`, commit `e069391d0`, extends the existing attach-only Fleet workshop verifier.

Hermes now accepts an expected Phase 4 binding as trusted run input without gaining policy ownership. It independently checks the observed Docker container against that binding.

For `none`, Hermes retains the original exact `network=none` contract.

For `provider-only`, Hermes requires:

- `network=none`;
- exact provider-only mode label;
- exact network-policy hash;
- exact RunAuthority hash;
- no gateway or direct workshop route.

For mediated direct modes, Hermes requires:

- exact Fleet internal network name;
- exact network mode;
- exact policy hash;
- exact RunAuthority hash;
- exact gateway container ID;
- exact gateway IP expected by the run binding;
- `hermes-egress=proxy`;
- only the exact Fleet internal network attached;
- direct DNS disabled;
- exact HTTP/HTTPS proxy upper/lowercase bindings;
- empty `NO_PROXY`/`no_proxy`;
- no `ALL_PROXY`/alternate proxy binding;
- all existing Phase 2/3 non-root, capability, filesystem, mount, resource, and deadline guarantees.

Hermes still uses only `docker exec` against the exact Fleet container. It does not create, start, stop, restart, replace, or delete the workshop.

A real-Docker Agent test creates a mediated-network workshop, enters it through `FleetWorkshopEnvironment`, executes a command, then proves Hermes cleanup leaves the container running for the lifecycle owner to remove.

## Tests and current proof

Fleet Phase 4 current proof:

- full Python suite: **798 passed**;
- focused Phase 4 policy + live-Docker suite: **16 passed**;
- combined Phase 2–4 OCI/workspace/network slice: **66 passed** before the final explicit-internet and verifier tightening, with the later Phase 4 slice green afterward;
- full Ruff: PASS;
- `git diff --check`: PASS at the pre-document closure gate;
- public-hygiene scan: PASS;
- real Docker gateway and internal-network residue after the suite: zero.

Hermes Agent Phase 4 proof:

- Docker/workshop verifier suite including real mediated-network attachment: **81 passed**;
- preserved Phase 1 API-run + Phase 2/3/4 Docker regression slice: **179 passed, 2 skipped**;
- Ruff: PASS;
- `git diff --check`: PASS;
- branch pushed to `Dadmin88/hermes-agent` as `vnext/phase4-network-isolation`, commit `e069391d0`.

The two skipped Agent tests are pre-existing conditional API-run tests and are not Phase 4 networking failures.

## Explicit non-goals retained for later phases

Phase 4 does not implement:

- generic host power or arbitrary host effects: Phase 5 broker;
- persistent Agent Instance orchestration: Phase 6;
- run-scoped Hermes `fleet_runtime`: Phase 7;
- final Run Capsule lifecycle/orphan reconciliation: Phase 8;
- full principal identity and immutable RunAuthority issuance/signature/replay protection: Phases 9–10;
- Templar evaluation: later numbered phases.

The network enforcement surface is now ready for those later authorities without granting them early.
