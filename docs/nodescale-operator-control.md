# Nodescale operator control V1

`nodescale.operator.v1` is the separate local read-only contract through which Fleet can inspect Nodescale-owned durable device authority without reading Nodescale storage.

It is distinct from both:

- `nodescale.observations.v1`, which exposes provider observations without device authority; and
- `fleet.managed-projection.v1`, through which Nodescale projects bounded desired managed state into Fleet-owned storage.

## Current Fleet slice

Fleet currently provides a strict Python client for the read-only Nodescale contract. It supports only:

- `capabilities`;
- bounded `devices.list` for one exact network; and
- exact `devices.inspect` for one exact network/device identity.

The client uses the Nodescale-owned Unix-domain socket. Nodescale authenticates the connecting Fleet service through the exact configured Linux peer UID before parsing a request. Fleet does not query Nodescale SQLite, send a caller-selected principal, or use provider observations as an authorization substitute.

This slice does not expose operator mutations or add Nodescale device evidence to the Desktop overview schema. Trust/revoke and invitation operations remain separately gated future work.

## Wire contract

Each request and response is one unsigned four-byte big-endian length followed by UTF-8 JSON. Requests are limited to 8 KiB, responses to 64 KiB, and the client requires response EOF after the single frame.

The schema identifier is exactly:

```text
nodescale.operator.v1
```

Capabilities must advertise exactly the three current read operations, an empty mutation set, a maximum page size of 32, and a maximum response size of 65536 bytes. Device listing is capped locally at 256 records and 32 pages. Pagination uses canonical device UUIDs as monotonic current-state cursors; it is not a frozen snapshot.

The client rejects malformed JSON, duplicate members, non-finite JSON numbers, unknown response fields, unsupported enum values, oversized frames, trailing bytes, identity mismatches, duplicate or non-monotonic devices, incoherent optional evidence, and unsupported authority claims.

## Authority semantics

A validated operator device record may expose Nodescale-owned durable evidence including:

- exact network and device identity;
- membership lifecycle and roles;
- credential, Keryx-binding, and Fleet-projection generations;
- durable Fleet projection status;
- provider identity when present;
- durable trust and provider-binding lifecycle evidence when present;
- latest Nodescale Keryx-binding lifecycle evidence when present; and
- bounded creation, update, and revocation timestamps.

Two fields deliberately preserve unavailable live facts:

```text
live_trust_evidence = not_reconciled_by_operator_read
live_keryx_binding_health = not_exposed
```

The read does not reconcile the provider, prove current provider trust, prove Keryx transport health, admit a device into Fleet, derive scheduler readiness, or grant any Fleet operation. Those authorities remain separate.

## Failure behavior

Nodescale returns only fixed error categories: `invalid_request`, `not_found`, or `unavailable`. Fleet maps them to payload-independent local errors and never includes a remote response body in the public message.

An exact `devices.inspect` response is authoritative read-back for the requested durable Nodescale identity. Fleet requires both the response network ID and returned device ID to match the exact request.

## Deployment

The Nodescale operator socket is optional and separate from the observation socket. Its parent and listener are Nodescale-owned; the Nodescale service is responsible for private filesystem permissions, exact `SO_PEERCRED` peer-UID authorization, and safe socket lifecycle. Fleet needs only the absolute configured socket path and exact network UUID.

Do not reuse the provider-observation socket or loosen filesystem permissions as a substitute for the correct peer UID.
