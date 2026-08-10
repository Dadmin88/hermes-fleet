# Fleet Canvas topology

Fleet Canvas is the native Hermes Desktop visual surface for current managed Fleet machines and distinct observed/unmanaged provider nodes. It consumes the authenticated composed `fleet.desktop.v2` overview and never reads Fleet or Nodescale SQLite directly.

## Truth model

Managed cards are current managed-projection rows. Their labels use Fleet's naming precedence, so an alias or provider name is rendered when Fleet supplies one and managed device identity remains the fallback.

Observed cards are current Nodescale provider observations. They are dashed, marked **Observed · unmanaged**, display Headscale/Tailscale provider evidence, and use given name, hostname, then provider node ID as presentation fallback. They do not reuse managed aliases or imply Fleet admission, trust, readiness, reservation, scheduler eligibility, operations, execution binding, or authority. Canvas performs no name/IP/tag heuristic deduplication between observed and managed rows.

The current API does **not** expose authoritative relationships between machines. Canvas therefore renders real machine nodes without connecting lines. Its graph engine supports edges, but the adapter returns an empty edge set until Fleet adds a versioned relationship record with source, target, kind, observation time, and provenance. Shared provider, network, readiness, or reachability fields are not treated as edges.

Readiness colors are presentation of Fleet's derived values:

- **Ready:** `scheduler_ready` is true.
- **Needs attention:** the node is active and alive but not scheduler-ready.
- **Awaiting evidence:** the node is active without current alive evidence.
- **Inactive:** managed projection marks it inactive.

Canvas does not recompute or broaden readiness and never interprets readiness as authorization.

## Stable layout

Initial positions are deterministic from stable node identity and do not change when observations refresh. Dragged positions are UI-only state persisted through Hermes plugin-scoped storage under `topology-layout.v1`.

Saved positions:

- never enter managed projection or Fleet authority;
- are bounded and validated before use;
- follow stable identity, not display name;
- remain available if a temporarily absent node returns;
- do not affect future workflow layout or execution intent.

## Controls

- Drag the background to pan.
- Focus the Canvas and use arrow keys to pan without a pointer.
- Use the mouse wheel or `+` / `-` to zoom.
- Press `0` or choose **Fit all** to frame visible nodes.
- Select a node and choose **Center selected**, or double-click it.
- Drag a node to save its local topology position.
- Focus a node and use `Shift` + arrow keys to move and save it.
- Search uses case-insensitive AND matching over supplied names, identity fields, status labels, and advertised operations.
- Status filters use Fleet's supplied managed/readiness fields without recomputation; **Observed** selects the separate evidence-only cards.

Filtering creates an induced view only; it never changes managed state or deletes layout.

## Inspector and names

Selecting a node opens a semantic Inspector beside the Canvas. It shows the exact managed identity and generations, presentation-name provenance, readiness reasons and seven-step ladder, freshness, worker capacity, CPU/RAM/swap/disk/GPU evidence, advertised operations, and raw technical details.

Name precedence is backend-owned: durable alias, then authoritative provider name when supplied, then the stable device-ID fallback. Aliases are presentation only. Alias writes carry the exact identity and expected `binding_generation`; stale Inspector writes fail closed. Reset clears the alias and returns to provider name or stable fallback. Aliases never become selectors, provider renames, readiness, execution bindings, or authority.

Selecting an observed card opens a separate evidence-only Inspector. It shows provider/network/instance/node identity, classification, provider online/expiry signals, observation timestamps, addresses, and tags. It contains no alias mutation, readiness ladder, capacity, operations, managed binding, reservation, or execution controls.

## Live reconciliation

The plugin API exposes typed `fleet.desktop-events.v1` WebSocket invalidation frames derived from validated composed `fleet.desktop.v2` snapshots. Frames remain signals only; REST is authoritative. Hermes Desktop connects through the authenticated `ctx.socket` SDK door and reconciles the shared React Query cache. The existing 15-second query remains the required fallback and is the normal path for OAuth remotes where plugin sockets intentionally resolve to a no-op.

The page reports **Live**, **Polling**, or **Reconnecting** without discarding last-known authoritative data. Added nodes and readiness recovery use `motion-safe` transitions; reduced-motion users receive no required animation. Session-bounded Activity records describe only observed snapshot differences. The status bar and command palette reuse the same query cache.

## Accessibility

The SVG exposes a tree of machine nodes. The selected node is the roving tab stop. Arrow keys move focus and selection between visible nodes; `Home` and `End` move to the first and last visible nodes; `Enter` or `Space` selects. Managed labels contain display name, status, worker capacity, and stable Fleet identity. Observed labels contain display name, unmanaged status, provider, and opaque observed identity. Canvas pan, zoom, fit, center, selection, and node positioning have keyboard paths, the minimum zoom preserves a 44-pixel node target, and the Canvas adds no required animation.

## Scale boundary

The managed API remains bounded to 256 managed nodes. The Nodescale client separately caps observed inventory at 256 and reports truncation. Layout is deterministic, persisted positions remain bounded to 256 validated identities, rendering is dependency-free SVG, labels are compact, and no per-node network request is made. Search/filtering is local over the validated composed snapshot.

## Troubleshooting

### Nodes appear without connections

This is expected until Fleet supplies authoritative relationship evidence. Canvas does not infer edges from shared network IDs or reachability.

### A dragged position was ignored

Invalid, non-finite, excessively large, or over-limit stored positions are discarded. Use **Fit all** and drag the node again.

### A selected node disappears while filtering

Canvas moves selection to the first visible result. Clearing search/filter restores the complete current managed-node view.

## Focused validation

```bash
python -m pytest -q tests/unit/test_desktop_plugin_assets.py
node --check desktop/plugin.js
python scripts/check_public_hygiene.py
```

Run the complete repository CI bundle before release.
