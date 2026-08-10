# Fleet Canvas topology

Fleet Canvas is the native Hermes Desktop visual surface for current managed Fleet machines. It consumes the same authenticated `fleet.desktop.v1` overview as the Fleet page and never reads Fleet SQLite directly.

## Truth model

Every visible machine is a current managed-projection row. Node labels use the API's naming precedence, so an alias or provider name is rendered when Fleet supplies one and the managed device identity remains the fallback.

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
- Status filters use Fleet's supplied managed/readiness fields without recomputation.

Filtering creates an induced view only; it never changes managed state or deletes layout.

## Accessibility

The SVG exposes a tree of machine nodes. The selected node is the roving tab stop. Arrow keys move focus and selection between visible nodes; `Home` and `End` move to the first and last visible nodes; `Enter` or `Space` selects. Every node has an accessible label containing its display name, status, worker capacity, and full stable identity. Canvas pan, zoom, fit, center, selection, and node positioning have keyboard paths, the minimum zoom preserves a 44-pixel node target, and the Canvas adds no required animation.

## Scale boundary

The D1 API and Canvas are bounded to 256 managed nodes. Layout is deterministic, rendering is dependency-free SVG, labels are compact, and no per-node network request is made. Search/filtering is local over the validated overview snapshot.

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
