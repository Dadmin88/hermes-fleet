# Rust managed-control proof

This disposable harness proves the managed-projection compatibility boundary through the real Rust components:

```text
Nodescale nodescale-fleet-client
  -> fleet-managed-control Unix socket
  -> fleet-domain
  -> fleet-state SQLite
  -> process restart and authoritative inspect
```

The Fleet CI job checks out a pinned contract-compatible Nodescale revision at
`c0a9a7c873d7086375ac53245e6fd689a3686c7d` and runs:

```bash
python3 proofs/managed-control/run_nodescale_rust_e2e.py \
  --nodescale-root /path/to/pinned/nodescale \
  --fleet-binary target/debug/fleet-managed-control \
  --cargo cargo
```

The harness creates one private temporary runtime, uses the real Nodescale Rust
`FleetClient` for apply/inspect/replay/stale/conflict/gap/disable/remove, restarts
Rust Fleet twice against the same `fleet-state` database, sends SIGTERM for each
shutdown, verifies socket cleanup, and deletes the complete temporary runtime.
Local-deny precedence and wrong-UID closure are Fleet-owned integration tests
because neither operation is an authority exposed to Nodescale.
