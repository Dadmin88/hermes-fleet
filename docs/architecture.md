# Hermes Fleet Architecture

Hermes Fleet coordinates work across Hermes-capable machines.

The easiest way to understand the stack is to ask four different questions:

```text
Nodescale: Who is this device, and is it trusted?
Keryx:     Which application peer is speaking, and how does data travel?
Fleet:     What may this node do, and where should work run?
Hermes:    How is the work actually performed?
```

Keeping those questions separate is one of the main design rules of the project.

## Responsibility boundaries

### Hermes Agent

Hermes owns local AI execution:

- models;
- tools;
- skills;
- profiles;
- files and local permissions;
- memory and sessions;
- the actual agent run.

Fleet does not reimplement the Hermes reasoning or tool loop.

### Keryx

Keryx owns authenticated application transport:

- peer identity;
- registration and discovery;
- relay routing;
- durable tasks and results;
- claims and leases;
- deadlines;
- retries and dead letters;
- result delivery;
- artifact descriptors;
- reconnect and offline-mailbox behavior.

Fleet does not maintain a second transport or a second Keryx task database.

### Nodescale

Nodescale owns managed device identity and trust. It can project a trusted, Keryx-bound device into Fleet through the managed-projection contract.

Nodescale does not decide final Fleet execution authority.

### Hermes Fleet

Fleet owns the application-level coordination layer:

- friendly node identity;
- exact node selection;
- Fleet operation envelopes;
- local policy and deny rules;
- managed baseline grants;
- dispatch;
- Hermes execution binding;
- operator-facing state;
- future capacity, profile, reservation, and scheduling decisions.

## Two important paths

Fleet currently has two distinct integration paths.

### 1. Nodescale managed projection

Nodescale can tell Fleet that a managed device is active, disabled, or removed.

```text
Nodescale trusted DeviceId
        ↓
authenticated Keryx binding
        ↓
managed Fleet projection
        ↓
Fleet-owned durable managed state
```

The local control connection uses an authenticated Unix-domain socket on Linux. Fleet verifies the caller identity before reading a request.

Generated authority is deliberately small:

```text
fleet.health
fleet.inventory
fleet.message
```

A managed projection never grants `fleet.hermes.run` automatically. A local operator deny also overrides generated authority.

See [Managed projection V1](managed-projection-v1.md) for the protocol details.

### 2. Keryx communication and execution

Fleet uses Keryx for node-to-node communication.

```text
Fleet controller
    ↓
Keryx authenticated transport
    ↓
fleet-node dispatcher
    ├─ direct handler
    └─ Hermes execution handler
```

The direct operations are health, inventory, and message. They do not create Hermes runs.

Only `fleet.hermes.run` reaches the executable handler.

## Operation model

| Operation | Type | Starts a Hermes run? |
| --- | --- | ---: |
| `fleet.health` | direct | No |
| `fleet.inventory` | direct | No |
| `fleet.message` | direct | No |
| `fleet.hermes.run` | executable | Yes |

This distinction matters because receiving a Fleet request should not automatically mean executing AI work.

## Request validation

A Fleet worker validates the request before choosing a handler.

The important checks include:

1. authenticated sender identity from Keryx;
2. exact destination identity;
3. supported Fleet envelope version;
4. exact operation name;
5. matching Keryx metadata and Fleet envelope values;
6. deadline and payload limits;
7. local operation policy.

Caller-supplied sender text is never authoritative. Keryx provides the authenticated sender identity.

## Trusted identity is not trusted content

Authentication answers:

> Which peer sent this?

It does not answer:

> Is everything this peer returned safe or correct?

Fleet therefore treats peer-produced health data, inventory data, message acknowledgements, and Hermes result text as untrusted content for presentation purposes.

The selected node, Keryx task ID, routed peer, and delivery route come from trusted local/Keryx boundaries and cannot be overwritten by response text.

## Duplicate-safe Hermes execution

Remote AI execution has an awkward crash case:

```text
Fleet asks Hermes to start a run
        ↓
Hermes starts it
        ↓
Fleet crashes before safely recording what happened
```

Blindly retrying could create a second run.

Fleet therefore keeps a small execution-binding record keyed by the Keryx task ID.

Important states:

- `creating`: Fleet reserved the task but does not yet know a durable Hermes run ID;
- `running`: the exact Hermes run ID is known;
- `completed`: a terminal result is stored for safe replay;
- `indeterminate`: Fleet cannot prove what happened and fails closed.

If the run ID is known, Fleet can resume watching the same run. If the final result is already stored, Fleet can replay it to Keryx. If Fleet cannot prove whether a run started, it does not guess.

The Rust implementation is being built against the same recovery behavior using shared Python/Rust fixtures.

## Managed state and local policy

Nodescale-generated state and operator-managed state remain separate.

The key rule is:

> Generated authority may make a node more visible, but it may not override a local deny.

Rejected managed-projection updates, including stale generations and conflicting same-generation content, must not partially mutate the durable Fleet record.

Disabled or removed managed nodes expose no generated effective grants.

## Implementation strategy

Hermes Fleet is one product.

The repository currently contains:

- a proven Python implementation used as the behavioral reference and current production prototype;
- a growing Rust implementation intended to become the permanent Fleet runtime.

Shared compatibility fixtures are used so Rust behavior is checked against real Python production decisions rather than a rewritten interpretation of them.

The implementation is being moved in layers:

```text
fleet-domain
→ durable fleet-state
→ Rust managed-control/service layer
→ Keryx integration
→ Hermes execution parity
→ inventory/readiness
→ profiles
→ scheduling
```

## Future scheduling model

Fleet is intended to become the component that answers:

> Which authorized, ready machine should run this work?

Future scheduler inputs will include:

- Nodescale/Fleet authorization;
- Keryx binding health;
- current readiness;
- CPU and RAM;
- GPU and VRAM where available;
- worker capacity;
- required capabilities;
- whether the required Hermes profile is already ready.

The first scheduler should stay deterministic and explainable. Machine-learning placement is not required.

## Deliberate non-goals

Fleet is not intended to become:

- another Keryx relay;
- a shared database for the whole Hermes stack;
- a hidden SSH fallback;
- a generic remote-root shell;
- a Kubernetes replacement;
- a distributed consensus system;
- an LLM inference engine itself.

Those boundaries keep the system understandable and let each component do one job well.
