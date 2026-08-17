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

Fleet verifies the gateway's observed Docker document rather than trusting the create argv, including:

- exact image, startup command, working directory, generated script hash, policy hash, authority labels, and lifecycle state;
- exact numeric non-root identity, read-only root, `CapDrop=ALL`, no added capabilities, `no-new-privileges`, and no `unconfined` security posture;
- exact CPU/RAM/PID limits and a non-restarting container policy;
- no host binds, named volumes, devices/device requests, or published host ports;
- an empty Docker `Mounts` set that must be observable rather than omitted;
- exact private tmpfs posture: `rw,nosuid,nodev,noexec`, fixed byte bound, exact UID/GID, and mode `0700`;
- exact bounded Docker `local` audit-log configuration;
- exact policy/script environment material, with duplicate environment names and extra proxy/control/credential authority rejected;
- exact internal-network membership and exact internal IP once running.

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

Recovery also fails closed before mutation when the pre-existing egress network is not the exact local internal Docker bridge or already contains an unexpected peer. If the deterministic gateway already exists, Fleet permits only that exact recovered gateway ID during network re-verification; arbitrary pre-existing members are rejected.

The resulting `EgressBinding.execution_id` is inseparable from the workshop execution plan: a workshop cannot reuse another execution's otherwise-valid network binding.

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

Hermes Agent Phase 4 is canonical on `Dadmin88/hermes-agent` `main` through PR #4 (`Phase 4: verify Fleet network isolation`), merge commit `a9f3fde16b11319d5ad08888176a55ad32ea5467`.

Hermes accepts the expected Phase 4 binding as trusted run input without gaining policy ownership. It independently inspects both the exact workshop container and, for mediated modes, the exact Docker network before attachment and before every command.

For `none`, Hermes retains the exact `network=none` contract.

For `provider-only`, Hermes requires:

- `network=none`;
- exact provider-only mode label;
- exact network-policy hash;
- exact RunAuthority hash;
- no gateway, proxy binding, or direct workshop route.

For mediated direct modes, Hermes requires the workshop to carry:

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

Hermes additionally performs `docker network inspect` and independently requires:

- exact network name;
- Docker `bridge` driver with local scope;
- `Internal=true`, `Attachable=false`, `Ingress=false`, and IPv6 disabled;
- exact Fleet execution, role, mode, policy, and authority labels;
- membership consisting of exactly the expected workshop and gateway IDs;
- the gateway attachment carrying the exact expected private IPv4 address.

An injected lateral peer therefore invalidates Hermes' next pre-command verification even if the workshop container itself has not changed.

Hermes still uses only `docker exec` against the exact Fleet container. It does not create, connect, start, stop, restart, replace, or delete Fleet containers or networks.

The real-Docker Agent proof now creates a real labeled internal network plus a real gateway peer, enters the exact mediated workshop through `FleetWorkshopEnvironment`, verifies the topology from Docker state, executes a command, and proves Hermes cleanup leaves lifecycle resources for Fleet to remove.

## Tests and current proof

Fleet Phase 4 current proof on the implementation branch:

- full Python suite: **867 passed**;
- focused Phase 4 policy + live-Docker suite: **19 passed**;
- full Ruff: PASS;
- `git diff --check`: PASS;
- public-hygiene scan: PASS;
- real Docker workshop/gateway/network residue after the cross-repository proof: zero.

The focused suite covers all four modes, authority non-widening, exact DNS/IP/port policy, provider-only offline behavior, explicit-internet approval, DNS rebinding, topology-enforced proxy bypass prevention, management/Tailscale/metadata denial, exact execution-to-network binding, gateway adoption hardening, and lateral-peer detection.

Hermes Agent Phase 4 proof:

- focused exact-workshop + independent-network verifier suite: **47 passed**;
- preserved Phase 1–4 regression slice: **149 passed, 2 platform skips**;
- Ruff and `git diff --check`: PASS;
- PR #4 required CI: PASS after rerunning one unrelated PTY surrogateescape flake;
- PR #4 merge commit: `a9f3fde16b11319d5ad08888176a55ad32ea5467`;
- resulting Agent `main` CI run `32049338859`: PASS;
- resulting Agent Docker build/test workflow `32049338033`: PASS.

The cross-repository real-Docker proof returned `PHASE4_CROSS_REPO_DOCKER_PROOF_OK`. Fleet created the exact internal network, hardened gateway, and workshop; Hermes independently verified the same Docker workshop and network; an injected lateral peer caused Hermes' next verification to fail; removal of that peer restored the exact topology; Hermes released without lifecycle authority; and Fleet alone removed the execution resources.

The two skipped Agent tests are platform-conditional cases in the preserved regression slice and are not Phase 4 networking failures.

## Explicit non-goals retained for later phases

Phase 4 does not implement:

- generic host power or arbitrary host effects: Phase 5 broker;
- persistent Agent Instance orchestration: Phase 6;
- run-scoped Hermes `fleet_runtime`: Phase 7;
- final Run Capsule lifecycle/orphan reconciliation: Phase 8;
- full principal identity and immutable RunAuthority issuance/signature/replay protection: Phases 9–10;
- Templar evaluation: later numbered phases.

The network enforcement surface is now ready for those later authorities without granting them early.
