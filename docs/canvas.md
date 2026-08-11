# Fleet Canvas Engine Contract

## Purpose

Fleet Canvas is a provider-neutral visual foundation for two distinct product modes:

- **Topology Mode** presents what Fleet can truthfully observe and manage now.
- **Workflow Mode** edits what an operator may want to happen later.

Both modes use the same graph projection, viewport, node shell, node registry, typed ports, selection, keyboard, minimap, and Inspector foundations. They do not share authority semantics.

This document describes the current frontend contract. It does not authorize a workflow runtime, admission, scheduling, reservations, or remote execution.

## Non-negotiable truth boundary

`GET /overview` returns the composed `fleet.desktop.v2` document. `fleet.desktop.v1` is the managed-control upstream input that the dashboard backend validates before composing v2.

`fleet.desktop.v2` contains independent collections:

- `nodes`: admitted Fleet-managed machines;
- `observed_nodes`: provider observations that are visible but unmanaged.

An observed machine is:

- real provider evidence;
- visible in Fleet;
- **not admitted**;
- **not trusted**;
- **not ready**;
- **not schedulable**;
- **not executable**.

Visibility never grants N5 trust, N6 binding, N7 projection, reservation, scheduling, alias mutation, managed readiness, operations, or execution authority. Those transitions require separate versioned authority contracts.

Topology edges are `[]` until a versioned Fleet relationship contract supplies authoritative relationship evidence. Provider/network grouping is presentation structure only; it is not a relationship edge.

## Live data path

The accepted observation path is:

```text
Provider SaaS API
  -> Nodescale provider adapter
  -> durable provider observations
  -> nodescale.observations.v1 over strict same-UID UDS
  -> Fleet bounded strict consumer
  -> fleet.desktop.v2
  -> Topology Mode graph adapter
```

The graph never reads Nodescale SQLite in normal operation. REST is authoritative. Authenticated WebSocket messages are invalidation signals only, and the 15-second REST fallback remains enabled.

The strict provider-neutral client accepts `fake`, Headscale, and Tailscale observation kinds. Product/runtime acceptance for this milestone uses real Tailscale observations; fake records must not be used for operator-facing acceptance. Device names and inventory count are never hardcoded. A six-device result is runtime acceptance evidence, not a UI contract.

## Shared graph projection

Both modes project their domain records into the same bounded render shape:

```text
Graph {
  nodes: GraphNode[]
  edges: GraphEdge[]
  groups: GraphGroup[]
}

GraphNode {
  id
  nodeType
  label
  status
  detail
  ports
  disabled
  searchText
  x, y, width, height
  group
  source
}
```

`source` preserves the mode-specific record:

- Topology managed node;
- Topology observed node;
- Workflow document node.

The Canvas engine does not infer authority from node shape, icon, position, port, edge, selection, or group membership.

Managed and observed collections are independently bounded to 256 records, for at most 512 combined topology nodes. Layout storage accepts the same combined bound. Layout values are finite and bounded. Duplicate stable topology IDs are rejected from the rendered graph.

## Generic node shell

`FleetCanvasNode` is the reusable presentation shell. It accepts slots/contracts for:

- node type and category;
- icon;
- title and subtitle;
- badges;
- semantic status;
- body summary;
- typed inputs and outputs;
- footer metadata;
- selected, hovered, focused, disabled, unavailable, execution, error, and progress states.

The shell does not contain Machine-specific provider or authority logic. `MachineCanvasNode` and `WorkflowCanvasNode` are adapters that populate the shell.

Observed Machine presentation prioritizes the provider’s clean hostname. Provider FQDN and opaque identifiers belong in technical Inspector details. Managed Machine naming continues to use the authoritative managed display-name and alias contract.

## Node registry

`FLEET_NODE_TYPES` is the built-in immutable descriptor catalog. A descriptor contains:

- stable ID;
- human label;
- category;
- Codicon name;
- semantic accent category;
- availability;
- runtime status;
- input ports;
- output ports;
- configuration schema.

Built-in categories are:

- Machine;
- Trigger;
- Fleet Action;
- Hermes Action;
- Flow Control;
- Condition;
- Data;
- Integration;
- Human Approval.

The catalog includes the planned Machine, trigger, Fleet action, Hermes action, flow, data, human, and HTTP node families. Future action descriptors are deliberately `editor-only` with `runtime: unavailable`. Catalog presence does not claim executable support.

`createFleetNodeRegistry(contributions, options)` accepts validated non-conflicting descriptor contributions. It validates identifiers, categories, labels, typed ports, duplicate port IDs, and bounded object schemas; an optional host icon validator can bind names to the active Codicon catalog. Contributions cannot override built-ins and are normalized to editor-only/unavailable. This is the frontend compatibility seam for future plugin-contributed node descriptors; it is not yet a plugin marketplace or a durable host registration API.

## Port vocabulary

The bounded port kinds are:

- `control`;
- `data`;
- `machine-target`;
- `event`;
- `result`;
- `success`;
- `error`.

A connection is accepted only when both endpoint nodes and port IDs exist, the input handle is not already occupied, and their kinds are compatible. Supported compatibility includes:

- equal kinds;
- event/success/error output into control input;
- result output into data input.

Topology Machine nodes hide ports because current topology has no relationship contract. Workflow editor nodes show typed ports. A visible port or connection is editor data, not execution authority.

## Topology Mode

Topology Mode is the default.

It provides:

- deterministic provider/network regions;
- stable local positions;
- pan, anchored zoom, fit-all, and center-selection;
- search and truthful status filters;
- keyboard node navigation;
- minimap with viewport bounds;
- selection that does not open or resize an overlay;
- an explicit **Inspect selection** action that opens a closable Inspector while the graph remains visible.

Nothing is selected by default. If a selected node disappears after refresh, selection closes rather than moving to another node.

### Observed Machine Inspector

The observed Inspector may show only provider evidence:

- clean hostname;
- provider;
- observed and unmanaged badges;
- provider online fact when explicitly supplied;
- addresses;
- last seen;
- last observed and freshness;
- tags;
- provider classification;
- management: unmanaged;
- readiness: not applicable until managed;
- authority: none;
- collapsed technical provider/network/observation identifiers.

It exposes no alias controls, readiness ladder, reservations, scheduling, operations, or execution actions.

## Workflow Mode editor

Workflow Mode reuses the same Canvas engine and shell. It provides an editor-only graph authoring experience:

- topology/workflow mode switch;
- searchable categorized node palette;
- click-to-add node creation at a deterministic computed position;
- generic descriptor-driven nodes;
- typed ports;
- pointer drag-to-connect with a provisional Bézier edge and typed target feedback;
- keyboard connection authoring from an output port to compatible input ports;
- validated connection document model and directional edge renderer;
- node and connection selection, context actions, and deletion;
- duplicate;
- copy/paste model;
- context-menu actions;
- undo/redo history bounded to 64 states;
- serialization/deserialization;
- shared pan, zoom, fit, node movement, keyboard navigation, minimap, and Inspector treatment.

The workflow document schema is `fleet.workflow-editor.v1`. Clipboard data uses `fleet.workflow-clipboard.v1`. Fleet owns durable definitions in SQLite as immutable numbered revisions. Creates start at version 1; updates require the expected latest version and append a new revision when content changes. Soft deletion hides an active definition while retaining exact historical revisions, and a tombstoned identity cannot be recreated implicitly.

Every node admitted by the current Desktop editor carries `runtime: unavailable`; the production topology-only Machine descriptor cannot be authored in Workflow Mode. The workflow metadata explicitly records that execution is unavailable. The editor and backend define no run, scheduler, reservation, admission, or remote-action endpoint.

The durable Rust V1 contract is a bounded structural document envelope, not the current Desktop block registry. It validates identifiers, counts, references, unique connections, finite positions, bounded arbitrary configuration/target JSON, unavailable runtime metadata, and deterministic canonical hashing. Unknown syntactically valid namespaced block types and typed port/kind identifiers survive persistence without becoming executable. The current Desktop registry separately validates the blocks and port compatibility it can author. Managed and observed exact-machine target evidence remains strictly validated as a separate truth boundary; persisting observed evidence grants no managed authority.

### Connection authoring

Only Workflow Mode accepts operator-authored edges. An output handle starts connection mode. The Canvas draws a provisional directional curve, emphasizes compatible input handles, recedes incompatible handles, and commits only after the production typed-port validator accepts the endpoint pair. Empty drops and Escape cancel without mutation; incompatible drops fail visibly and do not enter history or serialized state.

Keyboard users focus an output handle and press Enter or Space. Compatible input handles become focusable; Tab/Shift+Tab or arrow keys move among them, Enter or Space commits, and Escape cancels. Status changes are announced through the editor's polite live region.

Committed connections are selectable without opening the Inspector. Tab reaches the first or selected connection, arrow keys rove among connections, and accessible names include both endpoint port labels. Pointer selection, keyboard focus, Delete/Backspace, the toolbar, and the host context menu operate on connection entities. Connection create/delete and node deletion with incident edges are single bounded history mutations. Copy/paste and duplicate preserve internal connections only when every endpoint is in the copied selection; they allocate new node and connection IDs and never point an internal copied edge back to an original node.

Connections use static smooth routes with restrained semantic styling. There are no execution pulses or runtime animation. At most 64 connection hints appear in the minimap; larger graphs retain node and viewport clarity without edge spaghetti.

Current truthful limitations:

- the current Desktop exposes a refreshable durable Workflow library, explicit selection/Load, new drafts, name editing through immutable revision updates, and version-fenced soft deletion; it does not yet expose historical-version browsing or restoration;
- durable revisions are authoring state only; no workflow execution engine exists;
- backend conflict rejection requires the operator to reload before retrying and never silently overwrites a newer version;
- multiselect and box-select geometry have pure model foundations but are not yet a complete pointer interaction;
- edge-of-viewport auto-pan during connection drag is deferred; core connection gestures remain bounded and stable without it;
- contributed descriptors are accepted by the frontend registry factory but not yet loaded from a durable host extension point.

## Create Workflow from Topology

The signature transition has a frontend model boundary:

```text
Select a real topology machine
  -> Create workflow from selection
  -> create Exact Machine editor target
  -> switch to Workflow Mode
```

An observed target preserves `authority: observed` plus stable/provider observation references. It does not acquire trust or execution. The resulting node is still editor-only and runtime-unavailable.

## Framework decision

A bounded current-official-doc review evaluated React Flow, Motion, Radix UI, and icon libraries.

### React Flow

`@xyflow/react` is the preferred future graph-engine candidate because it provides mature custom nodes, handles, controlled nodes/edges, pan/zoom, selection, grouping, minimap, accessibility, and performance hooks.

It is not adopted directly in the current runtime plugin because:

- Hermes runtime plugin imports are mapped only for `@hermes/plugin-sdk`, `react`, and `react/jsx-runtime`;
- React Flow is not installed or SDK-exported, and its React/React DOM peer contract cannot resolve through the current runtime import map;
- the official integration requires package CSS with controlled ordering;
- bundling an independent graph/runtime path into `plugin.js` would weaken the host’s single-React and clean-install contract;
- exposing it safely would require a host-owned Desktop package plus an approved loader/import-map capability or SDK re-export, stylesheet ownership, compatibility tests, and a Desktop rebuild, outside this Fleet-only checkpoint.

The provider-neutral graph adapter, registry, and node shell isolate the future migration. A renderer adapter would map canonical `x`/`y` to React Flow `position`, presentation/source records to renderer `data`, and explicit canonical edges to React Flow edges. Selection, viewport, and change callbacks remain renderer-local. This lets a host-provided React Flow capability replace the render/interaction layer without changing topology truth or workflow documents.

### Motion

Desktop already carries Motion, but runtime plugins cannot import `motion/react` directly. Current self-contained hover, selection, node-entry, drawer, and minimap effects use CSS, with `prefers-reduced-motion` disabling animation and transition. A future SDK Motion export may replace only motion internals.

### Accessible primitives and icons

The plugin uses Hermes SDK exports backed by the host’s Radix UI primitives for context menu, search, segmented control, and scrolling. It uses host Codicons rather than shipping another icon package.

## Theme and motion

Fleet semantic tokens derive from host `--ui-*` tokens. They represent:

- selected;
- observed;
- managed;
- inactive;
- attention;
- ready/success;
- error;
- execution-active;
- workflow category accents.

Observed/unmanaged is informational and neutral, never dominant danger red.

Motion is restrained and state-triggered. No constant glow or pulse is used. Reduced-motion mode disables transitions and animations.

## Accessibility

The Canvas exposes a labelled spatial region with button semantics for nodes and connections, deterministic node order, a distinct visible roving-focus state, keyboard directional navigation, Enter/Space selection without automatically opening the Inspector, keyboard-labelled typed connection handles, an explicit **Inspect selection** action, Escape-to-close without clearing selection, and named zoom/fit controls. Inspector drawers are non-modal overlays so the graph remains visible. Host primitives provide keyboard and screen-reader behavior for search, segmented mode switching, scrolling, and context menus.

Pointer targets remain enlarged independently from their restrained visible dots. Connection authoring has a keyboard equivalent and does not rely on animation. Future multiselect gestures must preserve keyboard equivalents before they are considered complete.

## Validation obligations

A release candidate must prove:

1. `node --check desktop/plugin.js`;
2. focused node-registry, shell, graph, workflow-model, authority, accessibility, and Inspector tests;
3. the complete Python suite with `PYTHONPATH=.`;
4. Ruff check and format;
5. diff hygiene;
6. exact-byte atomic deployment and hash equality;
7. live `fleet.desktop.v2` proof against current real Tailscale observations;
8. zero duplicates, zero managed authority leakage, and topology `edges: []`;
9. visual QA in the existing Hermes Desktop with nothing selected by default, selection that leaves the drawer closed, and one explicitly opened observed-only Inspector;
10. live Workflow proof of pointer and keyboard connection creation, invalid-drop rejection, connection selection/deletion, undo/redo, and static theme-compatible rendering.
