# Layered node-readiness proof

This proof exercises real compatibility and control components. Availability transitions are scripted samples sent through the production Python observation client; the separate node-service tests cover live publisher collection and lifecycle.

```text
real Nodescale Rust FleetClient
  -> Fleet managed projection over the local Rust control service
  -> typed node observation
  -> fleet-state SQLite persistence
  -> Rust readiness derivation
  -> capacity exhaustion and Keryx layer loss
  -> recovery
  -> Fleet process restart
  -> last-known observation recovery
  -> bounded real-time freshness expiry and refresh
```

Build `fleet-managed-control`, then run:

```bash
python3 proofs/node-readiness/run_readiness_e2e.py \
  --nodescale-root /path/to/nodescale \
  --fleet-binary target/debug/fleet-managed-control
```

The Nodescale checkout must contain `crates/nodescale-fleet-client`. The proof creates a private disposable runtime and probe package, leaves no database/socket residue, and emits `REAL_NODESCALE_TO_RUST_READINESS_PROOF=PASS` only after every transition passes.

The proof does not create Keryx heartbeat tasks, schedule work, reserve capacity, install profiles, or dispatch Hermes Runs.
