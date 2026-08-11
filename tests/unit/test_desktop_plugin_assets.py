import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "desktop" / "plugin.js"
LEGACY_ENTRY = ROOT / "dashboard" / "dist" / "index.js"
IMPORT_SPECIFIER = re.compile(r"(from\s*|import\s*\(\s*|import\s+)(['\"])([^'\"]+)\2")


def test_desktop_plugin_is_runtime_loadable_and_registers_d1_surfaces() -> None:
    source = PLUGIN.read_text(encoding="utf-8")
    completed = subprocess.run(
        ["node", "--check", str(PLUGIN)],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert {match.group(3) for match in IMPORT_SPECIFIER.finditer(source)} == {
        "@hermes/plugin-sdk",
        "react",
        "react/jsx-runtime",
    }
    assert "id: 'hermes-fleet'" in source
    assert "ROUTES_AREA" in source
    assert "SIDEBAR_NAV_AREA" in source
    assert "path: '/fleet'" in source
    assert "data: { path: section.path }" in source
    assert "render: () => jsx(FleetRoute, { ctx, sectionId: section.id })" in source
    assert "codicon: 'server-process'" in source
    assert "ctx.rest('/overview')" in source
    assert "refetchInterval: 15_000" in source
    assert "ctx.storage.get(LAYOUT_STORAGE_KEY" in source
    assert "ctx.storage.set(LAYOUT_STORAGE_KEY" in source
    assert "function NodeInspector" in source
    assert "function ObservedNodeInspector" in source
    assert "Observed · unmanaged" in source
    assert "Provider observation only." in source
    assert "node.kind === 'observed'" in source
    assert "observed-node-${observation.observed_id.slice(7)}" in source
    assert "aria-label': 'Readiness ladder'" in source
    assert "method: 'PUT'" in source
    assert "method: 'DELETE'" in source
    assert "Clear local alias" in source
    assert (
        "['fleet.desktop.v1', 'fleet.desktop.v2'].includes(overview.schema)" in source
    )
    assert "Resetting name" not in source
    assert "Name reset was rejected" not in source
    assert "Technical details" in source
    assert "Copy stable identity" in source
    assert "title: 'Identity',\n            children: jsxs('div'" in source
    assert "fleet.desktop-events.v1" in source
    assert "ctx.socket('/events'" in source
    assert "queryClient.invalidateQueries({ queryKey: QUERY_KEY })" in source
    assert "focus-visible:ring-2" in source
    assert "flex min-w-0 flex-wrap" in source
    assert ".fleet-node-enter" in source
    assert "@media (prefers-reduced-motion: reduce)" in source
    assert (
        "section.id === 'overview' ? 'fleet.open' : `fleet.open.${section.id}`"
        in source
    )
    assert "STATUSBAR_AREAS.right" in source
    assert "role: 'region'" in source
    assert "role: 'button'" in source
    assert "const [rovingId, setRovingId] = useState(null)" in source
    assert "'data-focused': focused" in source
    assert "node.source.kind === 'workflow'" in source
    assert "? 'Observed identity'" in source
    assert ": 'Stable identity'" in source
    assert "ResizeObserver" in source
    assert "function GraphEdges" in source
    assert "edges: []" in source
    assert "onPointerCancel" in source
    assert "Loader" in source
    assert "emptyMessage = 'No machines match this view.'" in source
    assert "ErrorState" in source
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|\brgb\(", source)
    assert "var(--color-" not in source
    assert "var(--ui-bg-editor)" in source
    assert "var(--ui-text-primary)" in source
    assert not re.search(r"<[/A-Za-z]", source)


def test_graph_first_canvas_opens_evidence_in_an_explicit_overlay_drawer() -> None:
    source = PLUGIN.read_text(encoding="utf-8")

    assert "const [selectedId, setSelectedId] = useState(null)" in source
    assert "function FleetInspectorDrawer" in source
    assert "'aria-label': 'Fleet inspector drawer'" in source
    assert (
        "const closeInspector = useCallback(() => setInspectorOpen(false), [])"
        in source
    )
    assert "onClose: closeInspector" in source
    assert "className: 'h-full min-h-0 w-full shrink-0 overflow-auto" in source
    assert "canvasNodes.find(node => node.stable_id === selectedId) ?? null" in source
    assert "selectedNode && inspectorOpen" in source
    assert source.count("children: 'Inspect selection'") == 2
    select_callbacks = re.findall(
        r"const selectNode = useCallback\(id => \{(.*?)\n  \}, \[\]\)",
        source,
        re.DOTALL,
    )
    assert len(select_callbacks) == 2
    assert all(
        "setInspectorOpen(true)" not in callback for callback in select_callbacks
    )
    assert all(
        "if (!id) setInspectorOpen(false)" in callback for callback in select_callbacks
    )
    assert "Workflow node limit reached (256)." in source
    assert "title: 'Topology unavailable'" in source
    assert "fleet-workflow-surface" in source
    assert "const CONTRIBUTION_PORT_LIMIT = 16" in source
    assert "appendTopologyTargetsToWorkflow(current.present" in source
    assert "title: 'Your Fleet is empty'" not in source
    assert (
        "canvasNodes.find(node => node.stable_id === selectedId) ?? canvasNodes[0]"
        not in source
    )


def test_graph_first_canvas_renders_groups_minimap_and_compact_status() -> None:
    source = PLUGIN.read_text(encoding="utf-8")

    assert "function GraphGroups" in source
    assert "jsx(MemoGraphGroups, { groups: graph.groups })" in source
    assert "function FleetMiniMap" in source
    assert "'aria-label': 'Fleet minimap'" in source
    assert "children: '0 relationship edges'" in source
    assert (
        "Edges are hidden because Fleet does not yet expose relationship evidence."
        not in source
    )
    assert "Drag nodes or use Shift+arrow to save local layout." not in source


def test_premium_canvas_exposes_reusable_node_system_contract() -> None:
    source = PLUGIN.read_text(encoding="utf-8")

    assert "Codicon" in source.split("from '@hermes/plugin-sdk'", 1)[0]
    assert "export const FLEET_NODE_TYPE_CATEGORIES" in source
    assert "export const FLEET_NODE_TYPES" in source
    assert "export function getFleetNodeType" in source
    assert "function FleetCanvasNode" in source
    assert "jsx('style', { children: FLEET_CANVAS_STYLES })" in source
    assert "function MachineCanvasNode" in source
    assert "function NodePort" in source
    assert "const FLEET_PORT_KINDS" in source
    assert "jsx(FleetCanvasNode" in source
    assert "jsx(Codicon" in source
    assert "@media (prefers-reduced-motion: reduce)" in source
    assert ".fleet-node-shell" in source
    assert ".fleet-group-region" in source
    assert ".fleet-minimap" in source
    assert ".fleet-inspector-drawer" in source
    assert ".fleet-minimap[data-inspector-open='true']" in source
    assert ".fleet-node-port[data-port-kind='machine-target']" in source
    assert ".fleet-node-port[data-port-kind='error']" in source
    assert ".fleet-canvas-root *::before" in source
    assert "width: min(25rem, 56%)" in source
    assert "strokeDasharray: group.kind === 'observed'" not in source


def test_machine_node_uses_truthful_clean_presentation_name_and_neutral_state() -> None:
    script = r"""
import fs from 'node:fs'
const dataUrl = source =>
  `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const sdkUrl = dataUrl(`
  export const ROUTES_AREA = 'app.routes'
  export const SIDEBAR_NAV_AREA = 'app.sidebar.nav'
  export const Button = 'Button'
  export const Codicon = 'Codicon'
  export const ContextMenu = 'ContextMenu'
  export const ContextMenuContent = 'ContextMenuContent'
  export const ContextMenuItem = 'ContextMenuItem'
  export const ContextMenuSeparator = 'ContextMenuSeparator'
  export const ContextMenuTrigger = 'ContextMenuTrigger'
  export const ScrollArea = 'ScrollArea'
  export const SearchField = 'SearchField'
  export const SegmentedControl = 'SegmentedControl'
  export const EmptyState = 'EmptyState'
  export const ErrorState = 'ErrorState'
  export const Loader = 'Loader'
  export const StatusDot = 'StatusDot'
  export const PALETTE_AREA = 'palette'
  export const STATUSBAR_AREAS = { right: 'status:right' }
  export const host = { navigate: () => undefined, notify: () => undefined }
  export const queryClient = { invalidateQueries: () => Promise.resolve() }
  export const useQuery = () => { throw new Error('render was not expected') }
`)
const reactUrl = dataUrl(`
  export const memo = value => value
  export const useCallback = value => value
  export const useEffect = () => undefined
  export const useMemo = factory => factory()
  export const useRef = value => ({ current: value })
  export const useState = value => [
    typeof value === 'function' ? value() : value,
    () => {}
  ]
`)
const jsxUrl = dataUrl(`
  export const jsx = (type, props, key) => ({ type, props, key })
  export const jsxs = jsx
`)
let source = fs.readFileSync(process.argv[1], 'utf8')
source = source.replaceAll("'@hermes/plugin-sdk'", `'${sdkUrl}'`)
source = source.replaceAll("'react/jsx-runtime'", `'${jsxUrl}'`)
source = source.replaceAll("'react'", `'${reactUrl}'`)
const mod = await import(dataUrl(source))
const observed = {
  observed_id: 'sha256:' + 'a'.repeat(64),
  network_id: 'network-1',
  provider_kind: 'tailscale',
  provider_instance_id: 'instance-1',
  provider_node_id: 'node-1',
  hostname: 'compute-a',
  given_name: 'compute-a.example.invalid',
  addresses: ['provider-address-1'],
  tags: ['tag:machine'],
  online: null,
  expired: false,
  classification: 'discovered_unmanaged',
  first_observed_at: '2026-08-10T00:00:00+00:00',
  last_observed_at: '2026-08-10T00:00:01+00:00',
  snapshot_at: '2026-08-10T00:00:01+00:00'
}
const node = mod.buildFleetCanvasNodes({ nodes: [], observed_nodes: [observed] })[0]
const graphNode = mod.buildFleetGraph([node]).nodes[0]
console.log(JSON.stringify({
  categories: mod.FLEET_NODE_TYPE_CATEGORIES,
  registryKeys: Object.keys(mod.FLEET_NODE_TYPES),
  machineType: mod.getFleetNodeType('machine'),
  unknownType: mod.getFleetNodeType('unknown'),
  title: node.naming.display_name,
  technicalName: node.naming.technical_name,
  nodeType: node.node_type,
  status: graphNode.status,
  ports: graphNode.ports
}))
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(PLUGIN)],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    loaded = json.loads(completed.stdout)
    assert loaded["categories"] == [
        "machine",
        "trigger",
        "fleet-action",
        "hermes-action",
        "flow-control",
        "condition",
        "data",
        "integration",
        "human-approval",
    ]
    assert loaded["registryKeys"][0] == "machine"
    assert len(loaded["registryKeys"]) == 47
    assert loaded["machineType"]["icon"] == "server-process"
    assert loaded["unknownType"] is None
    assert loaded["title"] == "compute-a"
    assert loaded["technicalName"] == "compute-a.example.invalid"
    assert loaded["nodeType"] == "machine"
    assert loaded["status"] == {
        "key": "observed",
        "label": "OBSERVED · UNMANAGED",
        "tone": "info",
    }
    assert loaded["ports"] == []


def test_workflow_editor_foundation_is_complete_serializable_and_non_executing() -> (
    None
):
    source = PLUGIN.read_text(encoding="utf-8")
    for symbol in (
        "function WorkflowPalette",
        "function WorkflowCanvasNode",
        "function WorkflowModePanel",
        "export function createEmptyWorkflow",
        "export function addWorkflowNode",
        "export function connectWorkflowNodes",
        "export function deleteWorkflowSelection",
        "export function duplicateWorkflowSelection",
        "export function copyWorkflowSelection",
        "export function pasteWorkflowClipboard",
        "export function serializeWorkflow",
        "export function deserializeWorkflow",
        "export function deserializeWorkflowRevision",
        "export function normalizeWorkflowListing",
        "export function workflowPersistenceCanReplaceHistory",
        "export function commitFleetWorkflowEditorMutation",
        "export function commitTopologySelectionToWorkflow",
        "export async function loadWorkflowPersistenceControl",
        "export async function saveWorkflowPersistenceControl",
        "export async function deleteWorkflowPersistenceControl",
        "export function createWorkflowDraftAfterDeletion",
        "export function createWorkflowFromTopology",
        "export function createWorkflowHistory",
        "export function applyWorkflowEdit",
        "export function undoWorkflow",
        "export function redoWorkflow",
        "export function updateFleetSelection",
        "export function nodesInsideSelection",
    ):
        assert symbol in source
    assert "Editor only · execution unavailable" in source
    assert "surface === 'workflows'" in source
    assert "ctx.rest('/workflows')" in source
    assert "expectedVersion: persistedVersion" in source
    assert "Load durable" in source
    assert "Save durable" in source
    assert "Workflow library" in source
    assert "Refresh workflows" in source
    assert "New workflow" in source
    assert "Delete durable" in source
    assert "Workflow name" in source
    assert "method: 'DELETE'" in source
    assert "'aria-busy': persistenceBusy" in source
    assert "pointer-events-none opacity-70" in source
    assert "Workflow save was rejected. Reload durable state before retrying." in source
    assert "loadWorkflow({ quiet: true })" not in source
    assert (
        "Discard unsaved workflow edits and load the latest durable revision?" in source
    )

    for interaction_contract in (
        "function WorkflowPortHandle",
        "function WorkflowNodePorts",
        "function ProvisionalWorkflowEdge",
        "function workflowPortAriaLabel",
        "data-connection-state",
        "data-connection-compatible",
        "selectedEdgeId",
        "onConnectionCommit",
        "function focusConnectionTarget",
        "function applyConnectionMove",
        "connectionMoveFrameRef",
        "canvasMoveFrameRef",
        "function cancelActiveCanvasGesture",
        "onLostPointerCapture: cancelPointer",
        "active.captureElement.releasePointerCapture",
        "data-fleet-edge",
        "sourcePortLabel",
        "interactiveTarget",
        "event.type === 'lostpointercapture'",
        "Connection cancelled.",
        "Delete selected connection",
        "fleet-workflow-edge",
        "fleet-provisional-edge",
        "markerEnd",
        "Connect ${node.label} ${port.label}",
        "export function createWorkflowConnectionIndex",
        "function workflowConnectionCompatibilityFromIndex",
        "connectionCompatibilityMap",
        "connectionInvokerRef",
        "rovingPortKey",
        "aria-atomic",
        "const FleetCanvasStaticScene = memo",
        "sceneHandlersRef",
    ):
        assert interaction_contract in source

    graph_node_block = source[
        source.index("function GraphNode") : source.index("function WorkflowNodePorts")
    ]
    assert "WorkflowPortHandle" not in graph_node_block
    assert "onContextMenu" in graph_node_block
    assert (
        "event.stopPropagation()\n      event.currentTarget.focus" in graph_node_block
    )

    begin_node_drag_block = source[
        source.index("function beginNodeDrag") : source.index(
            "function applyCanvasMove"
        )
    ]
    assert "event.isPrimary === false || pointerRef.current" in begin_node_drag_block
    assert "setSelectedEdgeId?.(null)" in begin_node_drag_block

    begin_pan_block = source[
        source.index("function beginPan") : source.index("function beginNodeDrag")
    ]
    assert "event.isPrimary === false || pointerRef.current" in begin_pan_block

    live_region_block = source[
        source.index("role: 'status'") - 240 : source.index("role: 'status'") + 240
    ]
    assert "editorNotice ?? ''" in live_region_block

    script = r"""
import fs from 'node:fs'
const dataUrl = source =>
  `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const sdkUrl = dataUrl(`
  export const ROUTES_AREA = 'app.routes'
  export const SIDEBAR_NAV_AREA = 'app.sidebar.nav'
  export const Button = 'Button'
  export const Codicon = 'Codicon'
  export const ContextMenu = 'ContextMenu'
  export const ContextMenuContent = 'ContextMenuContent'
  export const ContextMenuItem = 'ContextMenuItem'
  export const ContextMenuSeparator = 'ContextMenuSeparator'
  export const ContextMenuTrigger = 'ContextMenuTrigger'
  export const ScrollArea = 'ScrollArea'
  export const SearchField = 'SearchField'
  export const SegmentedControl = 'SegmentedControl'
  export const EmptyState = 'EmptyState'
  export const ErrorState = 'ErrorState'
  export const Loader = 'Loader'
  export const StatusDot = 'StatusDot'
  export const PALETTE_AREA = 'palette'
  export const STATUSBAR_AREAS = { right: 'status:right' }
  export const host = { navigate: () => undefined, notify: () => undefined }
  export const queryClient = { invalidateQueries: () => Promise.resolve() }
  export const useQuery = () => { throw new Error('render was not expected') }
`)
const reactUrl = dataUrl(`
  export const memo = value => value
  export const useCallback = value => value
  export const useEffect = () => undefined
  export const useMemo = factory => factory()
  export const useRef = value => ({ current: value })
  export const useState = value => [
    typeof value === 'function' ? value() : value,
    () => {}
  ]
`)
const jsxUrl = dataUrl(`
  export const jsx = (type, props, key) => ({ type, props, key })
  export const jsxs = jsx
`)
let source = fs.readFileSync(process.argv[1], 'utf8')
source = source.replaceAll("'@hermes/plugin-sdk'", `'${sdkUrl}'`)
source = source.replaceAll("'react/jsx-runtime'", `'${jsxUrl}'`)
source = source.replaceAll("'react'", `'${reactUrl}'`)
const mod = await import(dataUrl(source))
const ids = Object.keys(mod.FLEET_NODE_TYPES)
const rejects = operation => {
  try { operation(); return false } catch { return true }
}
let workflow = mod.createEmptyWorkflow('workflow-1')
workflow = mod.addWorkflowNode(workflow, {
  id: 'trigger-1', type: 'manual-trigger', position: { x: 10, y: 20 }
})
workflow = mod.addWorkflowNode(workflow, {
  id: 'delay-1', type: 'delay', position: { x: 310, y: 20 }
})
const workflowWithoutConnection = workflow
const compatibility = mod.workflowConnectionCompatibility(workflow, {
  source: 'trigger-1', sourcePort: 'control',
  target: 'delay-1', targetPort: 'control'
})
const malformedCompatibility = mod.workflowConnectionCompatibility(
  { nodes: {}, connections: [] }, {}
)
const malformedMemberCompatibility = mod.workflowConnectionCompatibility(
  { nodes: [null], connections: [null] }, {}
)
workflow = mod.connectWorkflowNodes(workflow, {
  id: 'connection-1', source: 'trigger-1', sourcePort: 'control',
  target: 'delay-1', targetPort: 'control'
})
const duplicateCompatibility = mod.workflowConnectionCompatibility(workflow, {
  source: 'trigger-1', sourcePort: 'control',
  target: 'delay-1', targetPort: 'control'
})
const workflowWithSecondSource = mod.addWorkflowNode(workflow, {
  id: 'trigger-2', type: 'manual-trigger', position: { x: 0, y: 220 }
})
const occupiedCompatibility = mod.workflowConnectionCompatibility(
  workflowWithSecondSource,
  {
    source: 'trigger-2', sourcePort: 'control',
    target: 'delay-1', targetPort: 'control'
  }
)
const occupiedConnectionRejected = rejects(() => mod.connectWorkflowNodes(
  workflowWithSecondSource,
  {
    id: 'connection-occupied', source: 'trigger-2', sourcePort: 'control',
    target: 'delay-1', targetPort: 'control'
  }
))
const duplicateConnectionRejected = rejects(() => mod.connectWorkflowNodes(workflow, {
  id: 'connection-2', source: 'trigger-1', sourcePort: 'control',
  target: 'delay-1', targetPort: 'control'
}))
const invalidConnectionRejected = rejects(() => mod.connectWorkflowNodes(
  workflowWithoutConnection,
  {
    id: 'connection-invalid', source: 'trigger-1', sourcePort: 'control',
    target: 'delay-1', targetPort: 'missing'
  }
))
const connectionDeleted = mod.deleteWorkflowConnection(workflow, 'connection-1')
let connectionHistory = mod.createWorkflowHistory(workflowWithoutConnection)
connectionHistory = mod.applyWorkflowEdit(connectionHistory, workflow)
const connectionUndone = mod.undoWorkflow(connectionHistory)
const connectionRedone = mod.redoWorkflow(connectionUndone)
const deletionHistory = mod.applyWorkflowEdit(connectionRedone, connectionDeleted)
const deletionUndone = mod.undoWorkflow(deletionHistory)
const draftStarted = mod.workflowConnectionDraftReducer(null, {
  type: 'start', source: 'trigger-1', sourcePort: 'control',
  point: { x: 230, y: 78 }, keyboard: false
})
const draftMoved = mod.workflowConnectionDraftReducer(draftStarted, {
  type: 'move', point: { x: 300, y: 78 },
  target: { nodeId: 'delay-1', portId: 'control', state: 'valid' }
})
const draftCancelled = mod.workflowConnectionDraftReducer(
  draftMoved,
  { type: 'cancel' }
)
const selection = mod.updateFleetSelection([], 'trigger-1', { toggle: false })
const copied = mod.copyWorkflowSelection(workflow, selection)
const pasted = mod.pasteWorkflowClipboard(workflow, copied, {
  idPrefix: 'paste', offset: { x: 40, y: 40 }
})
const duplicated = mod.duplicateWorkflowSelection(workflow, selection, {
  idPrefix: 'duplicate', offset: { x: 24, y: 24 }
})
const deleted = mod.deleteWorkflowSelection(workflow, selection)
const parsed = mod.deserializeWorkflow(mod.serializeWorkflow(pasted))
const duplicateEdgePacket = JSON.parse(mod.serializeWorkflow(workflow))
duplicateEdgePacket.connections.push({
  ...duplicateEdgePacket.connections[0],
  id: 'connection-duplicate'
})
const duplicateSerializedEdgeRejected = rejects(() =>
  mod.deserializeWorkflow(JSON.stringify(duplicateEdgePacket))
)
let history = mod.createWorkflowHistory(workflow)
history = mod.applyWorkflowEdit(history, duplicated)
history = mod.undoWorkflow(history)
history = mod.redoWorkflow(history)
const observedSource = {
  stable_id: 'observed-node-' + 'a'.repeat(64),
  kind: 'observed', node_type: 'machine',
  naming: {
    display_name: 'compute-observed',
    technical_name: 'compute-observed.example.invalid'
  },
  provider: {
    kind: 'tailscale', label: 'Tailscale', node_id: 'node-1',
    network_id: 'network-1', instance_id: 'instance-1'
  },
  observation: {
    observed_id: 'sha256:' + 'a'.repeat(64),
    addresses: ['provider-address-1']
  }
}
const topology = mod.createWorkflowFromTopology('from-topology', [observedSource])
let connectedTarget = mod.addWorkflowNode(topology, {
  id: 'send-message-1', type: 'send-message', position: { x: 320, y: 20 }
})
connectedTarget = mod.connectWorkflowNodes(connectedTarget, {
  id: 'machine-connection-1',
  source: topology.nodes[0].id, sourcePort: 'machine',
  target: 'send-message-1', targetPort: 'machine'
})
const connectedTargetRoundTrip = mod.deserializeWorkflow(
  mod.serializeWorkflow(connectedTarget)
)
const appended = mod.appendTopologyTargetsToWorkflow(workflow, [observedSource])
const appendedAgain = mod.appendTopologyTargetsToWorkflow(appended, [observedSource])
let appendHistory = mod.createWorkflowHistory(workflow)
appendHistory = mod.applyWorkflowEdit(appendHistory, appended)
appendHistory = mod.undoWorkflow(appendHistory)
const duplicateMemberJson = mod.serializeWorkflow(workflow).replace(
  '"schema":"fleet.workflow-editor.v1"',
  '"schema":"fleet.workflow-editor.v1","schema":"fleet.workflow-editor.v1"'
)
const contradictoryTarget = JSON.parse(mod.serializeWorkflow(topology))
contradictoryTarget.nodes[0].target.stable_id = 'observed-node-' + 'b'.repeat(64)
let httpA = mod.createEmptyWorkflow('http-canonical')
httpA = mod.addWorkflowNode(httpA, {
  id: 'http-1', type: 'http', position: { x: 0, y: 0 },
  configuration: { url: 'https://example.invalid', method: 'POST' }
})
let httpB = mod.createEmptyWorkflow('http-canonical')
httpB = mod.addWorkflowNode(httpB, {
  id: 'http-1', type: 'http', position: { x: 0, y: 0 },
  configuration: { method: 'POST', url: 'https://example.invalid' }
})
const hiddenPayload = JSON.parse(mod.serializeWorkflow(workflow))
hiddenPayload.nodes[0].configuration = { execution: { run: 'x' } }
hiddenPayload.nodes[0].target = {
  authority: 'observed', execution: { reservation: 'x' }
}
const malformedClipboardRejected = rejects(() =>
  mod.pasteWorkflowClipboard(workflow, {
    schema: 'fleet.workflow-clipboard.v1', nodes: null, connections: []
  })
)
const longPrefixPaste = mod.pasteWorkflowClipboard(
  workflow,
  mod.copyWorkflowSelection(workflow, ['trigger-1', 'delay-1']),
  { idPrefix: 'p'.repeat(128), offset: { x: 1, y: 1 } }
)
const invalidContribution = {
  id: 'bad-contribution', label: 'Bad', category: 'data', icon: 'json',
  inputs: [{ id: 42, direction: 'output', kind: 'data', label: null }],
  outputs: [], configurationSchema: 'not-a-schema'
}
const contributionBase = {
  id: 'plugin-data', label: 'Plugin Data', category: 'data', icon: 'json',
  inputs: [], outputs: [],
  configurationSchema: {
    type: 'object', properties: {}, additionalProperties: false
  }
}
const contributionWithExtra = { ...contributionBase, extra: true }
const contributionWithInvertedBounds = {
  ...contributionBase,
  id: 'plugin-bounds',
  configurationSchema: {
    type: 'object',
    properties: { amount: { type: 'number', minimum: 10, maximum: 0 } },
    additionalProperties: false
  }
}
const oversizedPorts = {
  ...contributionBase,
  id: 'oversized-ports',
  inputs: Array.from({ length: 17 }, (_, index) => ({
    id: `input-${index}`,
    direction: 'input',
    kind: 'data',
    label: `Input ${index}`
  }))
}
const boxed = mod.nodesInsideSelection([
  { id: 'a', x: 0, y: 0, width: 100, height: 80 },
  { id: 'b', x: 300, y: 300, width: 100, height: 80 }
], { x: -5, y: -5, width: 120, height: 100 })
const nonFiniteBoxed = mod.nodesInsideSelection([
  { id: 'near', x: 0, y: 0, width: 1, height: 1 },
  { id: 'far', x: 999999, y: 0, width: 1, height: 1 }
], { x: 0, y: 0, width: Infinity, height: 1 })
const compatibilityIndex = mod.createWorkflowConnectionIndex(workflowWithSecondSource)
const indexedDuplicate = mod.workflowConnectionCompatibilityFromIndex(
  compatibilityIndex,
  {
    source: 'trigger-1', sourcePort: 'control',
    target: 'delay-1', targetPort: 'control'
  }
)
const indexedOccupied = mod.workflowConnectionCompatibilityFromIndex(
  compatibilityIndex,
  {
    source: 'trigger-2', sourcePort: 'control',
    target: 'delay-1', targetPort: 'control'
  }
)
mod.commitFleetWorkflowPersistedVersion(7)
const persistedWorkflowVersion = mod.getFleetWorkflowPersistedVersion()
const invalidPersistedWorkflowVersionRejected = rejects(() =>
  mod.commitFleetWorkflowPersistedVersion(0)
)
const workflowListing = mod.normalizeWorkflowListing({
  executionAvailable: false,
  workflows: [
    { workflowId: 'alpha', latestVersion: 2, createdAtMs: 10, updatedAtMs: 20 },
    { workflowId: 'beta', latestVersion: 1, createdAtMs: 30, updatedAtMs: 30 }
  ]
})
const executableWorkflowListingRejected = rejects(() =>
  mod.normalizeWorkflowListing({ executionAvailable: true, workflows: [] })
)
const malformedWorkflowListingRejected = rejects(() =>
  mod.normalizeWorkflowListing({
    executionAvailable: false,
    workflows: [{
      workflowId: 'alpha', latestVersion: 0, createdAtMs: 10, updatedAtMs: 5
    }]
  })
)
const matchingWorkflowGenerationAccepted = mod.workflowPersistenceCanReplaceHistory(
  4,
  4
)
const staleWorkflowGenerationRejected = mod.workflowPersistenceCanReplaceHistory(4, 5)
const invalidWorkflowGenerationRejected = rejects(() =>
  mod.workflowPersistenceCanReplaceHistory(-1, 0)
)
const rejectsAsync = async operation => {
  try { await operation(); return false } catch { return true }
}
const deferred = () => {
  let resolve
  let reject
  const promise = new Promise((accept, decline) => {
    resolve = accept
    reject = decline
  })
  return { promise, resolve, reject }
}
const revision = (version, document = workflow) => ({
  workflowId: document.id,
  version,
  contentHash: 'a'.repeat(64),
  document: JSON.parse(mod.serializeWorkflow(document)),
  createdAtMs: version
})
const listing = {
  executionAvailable: false,
  workflows: [{
    workflowId: workflow.id,
    latestVersion: 2,
    createdAtMs: 1,
    updatedAtMs: 2
  }]
}

let mutationHistory = mod.createWorkflowHistory(workflow)
const generationStart = mod.getFleetWorkflowEditorGeneration()
mutationHistory = mod.commitFleetWorkflowEditorMutation(
  mutationHistory,
  current => mod.applyWorkflowEdit(current, duplicated)
)
const generationAfterApply = mod.getFleetWorkflowEditorGeneration()
mutationHistory = mod.commitFleetWorkflowEditorMutation(
  mutationHistory,
  current => mod.undoWorkflow(current)
)
const generationAfterUndo = mod.getFleetWorkflowEditorGeneration()
mutationHistory = mod.commitFleetWorkflowEditorMutation(
  mutationHistory,
  current => mod.redoWorkflow(current)
)
const generationAfterRedo = mod.getFleetWorkflowEditorGeneration()
mutationHistory = mod.commitFleetWorkflowEditorMutation(
  mutationHistory,
  mod.createWorkflowHistory(mod.createEmptyWorkflow('new-draft'))
)
const generationAfterNew = mod.getFleetWorkflowEditorGeneration()

const loadDeferred = deferred()
let staleLoadHistory = mod.createWorkflowHistory(workflow)
const staleLoadPromise = mod.loadWorkflowPersistenceControl({
  targetId: workflow.id,
  request: path => path === '/workflows'
    ? Promise.resolve(listing)
    : loadDeferred.promise
})
await Promise.resolve()
await Promise.resolve()
const newerLoadDocument = { ...workflow, name: 'newer local load edit' }
staleLoadHistory = mod.applyWorkflowEdit(staleLoadHistory, newerLoadDocument)
mod.markFleetWorkflowEditorMutation()
loadDeferred.resolve({
  executionAvailable: false,
  revision: revision(2)
})
const staleLoadResult = await staleLoadPromise
const staleLoadUnlocked = !mod.isFleetWorkflowPersistenceLocked()

const crossRouteLoadDeferred = deferred()
const crossRouteLoadPromise = mod.loadWorkflowPersistenceControl({
  targetId: workflow.id,
  request: path => path === '/workflows'
    ? Promise.resolve(listing)
    : crossRouteLoadDeferred.promise
})
await Promise.resolve()
await Promise.resolve()
const crossRouteGenerationBefore = mod.getFleetWorkflowEditorGeneration()
const crossRouteMutationBlocked = rejects(() =>
  mod.commitTopologySelectionToWorkflow(
    mod.createWorkflowHistory(workflow),
    observedSource
  )
)
const crossRouteGenerationAfter = mod.getFleetWorkflowEditorGeneration()
crossRouteLoadDeferred.resolve({
  executionAvailable: false,
  revision: revision(2)
})
const crossRouteLoadResult = await crossRouteLoadPromise

const saveDeferred = deferred()
const staleSavePromise = mod.saveWorkflowPersistenceControl({
  request: () => saveDeferred.promise,
  workflow,
  persistedVersion: 2
})
const newerSaveDocument = { ...workflow, name: 'newer local save edit' }
mod.markFleetWorkflowEditorMutation()
saveDeferred.resolve({
  outcome: 'version_created',
  revision: revision(3)
})
const staleSaveResult = await staleSavePromise
let nextSaveExpectedVersion = null
const nextSaveResult = await mod.saveWorkflowPersistenceControl({
  request: (_path, options) => {
    nextSaveExpectedVersion = options.body.expectedVersion
    return Promise.resolve({
      outcome: 'version_created',
      revision: revision(4, newerSaveDocument)
    })
  },
  workflow: newerSaveDocument,
  persistedVersion: staleSaveResult.revision.version
})

let deleteRequestCount = 0
let dirtyDeletePrompt = null
const cancelledDelete = await mod.deleteWorkflowPersistenceControl({
  request: () => { deleteRequestCount += 1 },
  workflowId: workflow.id,
  persistedVersion: 4,
  hasUnsavedEdits: true,
  confirmDelete: message => {
    dirtyDeletePrompt = message
    return false
  }
})
const deleteDeferred = deferred()
const staleDeletePromise = mod.deleteWorkflowPersistenceControl({
  request: () => deleteDeferred.promise,
  workflowId: workflow.id,
  persistedVersion: 4,
  hasUnsavedEdits: true,
  confirmDelete: () => true
})
const newerDeleteDocument = { ...workflow, name: 'preserved after delete' }
const newerDeleteHistory = mod.applyWorkflowEdit(
  mod.createWorkflowHistory(workflow),
  newerDeleteDocument
)
mod.markFleetWorkflowEditorMutation()
deleteDeferred.resolve({ outcome: 'deleted' })
const staleDeleteResult = await staleDeletePromise
const preservedDeleteDraft = mod.createWorkflowDraftAfterDeletion(
  newerDeleteHistory,
  ['other-workflow'],
  'workflow-after-delete'
)
const tombstonedIdReserved = mod.createWorkflowDraftAfterDeletion(
  newerDeleteHistory,
  [],
  workflow.id
)

const failedLoadRejected = await rejectsAsync(() =>
  mod.loadWorkflowPersistenceControl({
    targetId: workflow.id,
    request: async () => { throw new Error('load failed') }
  })
)
const loadFailureUnlocked = !mod.isFleetWorkflowPersistenceLocked()
const failedSaveRejected = await rejectsAsync(() =>
  mod.saveWorkflowPersistenceControl({
    request: async () => { throw new Error('save failed') },
    workflow,
    persistedVersion: 4
  })
)
const saveFailureUnlocked = !mod.isFleetWorkflowPersistenceLocked()
const failedDeleteRejected = await rejectsAsync(() =>
  mod.deleteWorkflowPersistenceControl({
    request: async () => { throw new Error('delete failed') },
    workflowId: workflow.id,
    persistedVersion: 4,
    hasUnsavedEdits: false,
    confirmDelete: () => true
  })
)
const deleteFailureUnlocked = !mod.isFleetWorkflowPersistenceLocked()
console.log(JSON.stringify({
  ids,
  descriptor: mod.FLEET_NODE_TYPES['manual-trigger'],
  connection: workflow.connections[0],
  compatibility,
  malformedCompatibility,
  malformedMemberCompatibility,
  duplicateCompatibility,
  occupiedCompatibility,
  occupiedConnectionRejected,
  duplicateConnectionRejected,
  invalidConnectionRejected,
  connectionDeletedCount: connectionDeleted.connections.length,
  connectionUndoRedoCounts: [
    connectionUndone.present.connections.length,
    connectionRedone.present.connections.length,
    deletionUndone.present.connections.length
  ],
  draftLifecycle: [draftStarted.status, draftMoved.status, draftCancelled],
  selection,
  pastedCounts: [pasted.nodes.length, pasted.connections.length],
  duplicatedCounts: [duplicated.nodes.length, duplicated.connections.length],
  deletedCounts: [deleted.nodes.length, deleted.connections.length],
  parsedSchema: parsed.schema,
  duplicateSerializedEdgeRejected,
  historyPresent: history.present.nodes.length,
  historyPast: history.past.length,
  topology,
  connectedTargetAuthority: connectedTargetRoundTrip.nodes[0].target.authority,
  connectedTargetExecution: connectedTargetRoundTrip.metadata.executionAvailable,
  providerInstance: topology.nodes[0].target.provider_instance_id,
  machineWorkflowRejected: rejects(() => mod.addWorkflowNode(
    mod.createEmptyWorkflow('bad-machine'),
    { id: 'machine-1', type: 'machine', position: { x: 0, y: 0 } }
  )),
  hiddenPayloadRejected: rejects(() => mod.deserializeWorkflow(hiddenPayload)),
  malformedClipboardRejected,
  longPrefixUnique:
    new Set(longPrefixPaste.nodes.map(node => node.id)).size ===
      longPrefixPaste.nodes.length,
  longPrefixConnectionCount: longPrefixPaste.connections.length,
  longPrefixConnectionRemapped: longPrefixPaste.connections.slice(1).every(edge =>
    !['trigger-1', 'delay-1'].includes(edge.source) &&
    !['trigger-1', 'delay-1'].includes(edge.target)
  ),
  invalidContributionRejected:
    !mod.createFleetNodeRegistry([invalidContribution]).has('bad-contribution'),
  extraContributionRejected:
    !mod.createFleetNodeRegistry([contributionWithExtra]).has('plugin-data'),
  invertedBoundsRejected:
    !mod.createFleetNodeRegistry([contributionWithInvertedBounds]).has('plugin-bounds'),
  nonArrayContributionsRejected: rejects(() => mod.createFleetNodeRegistry({})),
  oversizedPortsRejected:
    !mod.createFleetNodeRegistry([oversizedPorts]).has('oversized-ports'),
  appendedCounts: [appended.nodes.length, appended.connections.length],
  appendDedupeCount: appendedAgain.nodes.length,
  duplicateSelectionCount:
    mod.appendTopologyTargetsToWorkflow(workflow, [observedSource, observedSource])
      .nodes.length,
  appendUndoCount: appendHistory.present.nodes.length,
  duplicateMemberRejected: rejects(() => mod.deserializeWorkflow(duplicateMemberJson)),
  contradictoryTargetRejected: rejects(() =>
    mod.deserializeWorkflow(contradictoryTarget)
  ),
  configurationCanonical:
    mod.serializeWorkflow(httpA) === mod.serializeWorkflow(httpB),
  invalidHistoryRejected: rejects(() =>
    mod.createWorkflowHistory({ ...workflow, execution: { run: 'x' } })
  ),
  persistedWorkflowVersion,
  invalidPersistedWorkflowVersionRejected,
  workflowListing,
  executableWorkflowListingRejected,
  malformedWorkflowListingRejected,
  matchingWorkflowGenerationAccepted,
  staleWorkflowGenerationRejected,
  invalidWorkflowGenerationRejected,
  mutationGenerationDeltas: [
    generationAfterApply - generationStart,
    generationAfterUndo - generationAfterApply,
    generationAfterRedo - generationAfterUndo,
    generationAfterNew - generationAfterRedo
  ],
  staleLoadKind: staleLoadResult.kind,
  staleLoadName: staleLoadHistory.present.name,
  staleLoadUnlocked,
  crossRouteMutationBlocked,
  crossRouteGenerationUnchanged:
    crossRouteGenerationAfter === crossRouteGenerationBefore,
  crossRouteLoadKind: crossRouteLoadResult.kind,
  staleSaveKind: staleSaveResult.kind,
  staleSaveVersion: staleSaveResult.revision.version,
  nextSaveKind: nextSaveResult.kind,
  nextSaveExpectedVersion,
  cancelledDeleteKind: cancelledDelete.kind,
  dirtyDeletePrompt,
  deleteRequestCount,
  staleDeleteKind: staleDeleteResult.kind,
  preservedDeleteDraft: {
    id: preservedDeleteDraft.id,
    name: preservedDeleteDraft.name,
    nodeCount: preservedDeleteDraft.nodes.length
  },
  tombstonedIdReserved: tombstonedIdReserved.id !== workflow.id,
  failedRequests: [
    failedLoadRejected, loadFailureUnlocked,
    failedSaveRejected, saveFailureUnlocked,
    failedDeleteRejected, deleteFailureUnlocked
  ],
  boxed,
  nonFiniteBoxed,
  indexedDuplicate,
  indexedOccupied
}))
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(PLUGIN)],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    loaded = json.loads(completed.stdout)
    assert loaded["ids"] == [
        "machine",
        "exact-machine",
        "machine-group",
        "manual-trigger",
        "schedule",
        "fleet-event",
        "node-online",
        "node-offline",
        "node-ready",
        "run-completed",
        "webhook",
        "file-event",
        "find-ready-machine",
        "find-ready-gpu-machine",
        "send-message",
        "broadcast",
        "reserve-capacity",
        "release-capacity",
        "wait-for-machine",
        "get-machine-status",
        "start-agent",
        "continue-run",
        "run-profile",
        "delegate",
        "tool-call",
        "extract-result",
        "stop-run",
        "wait-for-agent",
        "if",
        "switch",
        "fan-out",
        "join",
        "loop",
        "retry",
        "delay",
        "timeout",
        "error-handler",
        "json",
        "transform",
        "filter",
        "merge",
        "extract-field",
        "template",
        "approval",
        "prompt-operator",
        "wait-for-input",
        "http",
    ]
    assert loaded["descriptor"]["availability"] == "editor-only"
    assert loaded["descriptor"]["runtime"] == "unavailable"
    assert loaded["descriptor"]["outputs"][0]["kind"] == "control"
    assert loaded["connection"]["kind"] == "control"
    assert loaded["compatibility"] == {"valid": True, "kind": "control"}
    assert loaded["malformedCompatibility"] == {
        "valid": False,
        "reason": "invalid connection request",
    }
    assert loaded["malformedMemberCompatibility"] == {
        "valid": False,
        "reason": "invalid connection endpoints",
    }
    assert loaded["duplicateCompatibility"] == {
        "valid": False,
        "reason": "duplicate workflow connection",
    }
    assert loaded["occupiedCompatibility"] == {
        "valid": False,
        "reason": "workflow input already connected",
    }
    assert loaded["occupiedConnectionRejected"] is True
    assert loaded["duplicateConnectionRejected"] is True
    assert loaded["invalidConnectionRejected"] is True
    assert loaded["connectionDeletedCount"] == 0
    assert loaded["connectionUndoRedoCounts"] == [0, 1, 1]
    assert loaded["draftLifecycle"] == ["pending", "valid", None]
    assert loaded["selection"] == ["trigger-1"]
    assert loaded["pastedCounts"] == [3, 1]
    assert loaded["duplicatedCounts"] == [3, 1]
    assert loaded["deletedCounts"] == [1, 0]
    assert loaded["parsedSchema"] == "fleet.workflow-editor.v1"
    assert loaded["duplicateSerializedEdgeRejected"] is True
    assert loaded["historyPresent"] == 3
    assert loaded["historyPast"] == 1
    assert loaded["topology"]["nodes"][0]["type"] == "exact-machine"
    assert loaded["topology"]["nodes"][0]["target"]["authority"] == "observed"
    assert loaded["connectedTargetAuthority"] == "observed"
    assert loaded["connectedTargetExecution"] is False
    assert loaded["providerInstance"] == "instance-1"
    assert loaded["machineWorkflowRejected"] is True
    assert loaded["hiddenPayloadRejected"] is True
    assert loaded["malformedClipboardRejected"] is True
    assert loaded["longPrefixUnique"] is True
    assert loaded["longPrefixConnectionCount"] == 2
    assert loaded["longPrefixConnectionRemapped"] is True
    assert loaded["invalidContributionRejected"] is True
    assert loaded["extraContributionRejected"] is True
    assert loaded["invertedBoundsRejected"] is True
    assert loaded["nonArrayContributionsRejected"] is True
    assert loaded["oversizedPortsRejected"] is True
    assert loaded["appendedCounts"] == [3, 1]
    assert loaded["appendDedupeCount"] == 3
    assert loaded["duplicateSelectionCount"] == 3
    assert loaded["appendUndoCount"] == 2
    assert loaded["duplicateMemberRejected"] is True
    assert loaded["contradictoryTargetRejected"] is True
    assert loaded["configurationCanonical"] is True
    assert loaded["invalidHistoryRejected"] is True
    assert loaded["persistedWorkflowVersion"] == 7
    assert loaded["invalidPersistedWorkflowVersionRejected"] is True
    assert [item["workflowId"] for item in loaded["workflowListing"]] == [
        "alpha",
        "beta",
    ]
    assert loaded["executableWorkflowListingRejected"] is True
    assert loaded["malformedWorkflowListingRejected"] is True
    assert loaded["matchingWorkflowGenerationAccepted"] is True
    assert loaded["staleWorkflowGenerationRejected"] is False
    assert loaded["invalidWorkflowGenerationRejected"] is True
    assert loaded["mutationGenerationDeltas"] == [1, 1, 1, 1]
    assert loaded["staleLoadKind"] == "stale"
    assert loaded["staleLoadName"] == "newer local load edit"
    assert loaded["staleLoadUnlocked"] is True
    assert loaded["crossRouteMutationBlocked"] is True
    assert loaded["crossRouteGenerationUnchanged"] is True
    assert loaded["crossRouteLoadKind"] == "loaded"
    assert loaded["staleSaveKind"] == "saved_stale"
    assert loaded["staleSaveVersion"] == 3
    assert loaded["nextSaveKind"] == "saved"
    assert loaded["nextSaveExpectedVersion"] == 3
    assert loaded["cancelledDeleteKind"] == "cancelled"
    assert "Unsaved local edits will be discarded" in loaded["dirtyDeletePrompt"]
    assert loaded["deleteRequestCount"] == 0
    assert loaded["staleDeleteKind"] == "deleted_stale"
    assert loaded["preservedDeleteDraft"] == {
        "id": "workflow-after-delete",
        "name": "preserved after delete",
        "nodeCount": 2,
    }
    assert loaded["tombstonedIdReserved"] is True
    assert loaded["failedRequests"] == [True, True, True, True, True, True]
    assert "execution" not in loaded["topology"]
    assert loaded["boxed"] == ["a"]
    assert loaded["nonFiniteBoxed"] == []
    assert loaded["indexedDuplicate"] == {
        "valid": False,
        "reason": "duplicate workflow connection",
    }
    assert loaded["indexedOccupied"] == {
        "valid": False,
        "reason": "workflow input already connected",
    }


def test_desktop_plugin_evaluates_and_registers_current_sdk_contributions() -> None:
    script = r"""
import fs from 'node:fs'

const dataUrl = source =>
  `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const sdkUrl = dataUrl(`
  export const ROUTES_AREA = 'app.routes'
  export const SIDEBAR_NAV_AREA = 'app.sidebar.nav'
  export const Button = 'Button'
  export const Codicon = 'Codicon'
  export const ContextMenu = 'ContextMenu'
  export const ContextMenuContent = 'ContextMenuContent'
  export const ContextMenuItem = 'ContextMenuItem'
  export const ContextMenuSeparator = 'ContextMenuSeparator'
  export const ContextMenuTrigger = 'ContextMenuTrigger'
  export const ScrollArea = 'ScrollArea'
  export const SearchField = 'SearchField'
  export const SegmentedControl = 'SegmentedControl'
  export const EmptyState = 'EmptyState'
  export const ErrorState = 'ErrorState'
  export const Loader = 'Loader'
  export const StatusDot = 'StatusDot'
  export const PALETTE_AREA = 'palette'
  export const STATUSBAR_AREAS = { right: 'status:right' }
  export const host = { navigate: () => undefined, notify: () => undefined }
  export const queryClient = {
    getQueryData: () => undefined,
    setQueryData: () => undefined,
    invalidateQueries: () => Promise.resolve()
  }
  export const useQuery = () => { throw new Error('render was not expected') }
`)
const reactUrl = dataUrl(`
  export const memo = value => value
  export const useCallback = value => value
  export const useEffect = () => undefined
  export const useMemo = factory => factory()
  export const useRef = value => ({ current: value })
  export const useState = value => [
    typeof value === 'function' ? value() : value,
    () => {}
  ]
`)
const jsxUrl = dataUrl(`
  export const jsx = (type, props, key) => ({ type, props, key })
  export const jsxs = jsx
`)

let source = fs.readFileSync(process.argv[1], 'utf8')
source = source.replaceAll("'@hermes/plugin-sdk'", `'${sdkUrl}'`)
source = source.replaceAll("'react/jsx-runtime'", `'${jsxUrl}'`)
source = source.replaceAll("'react'", `'${reactUrl}'`)
const plugin = (await import(dataUrl(source))).default
const contributions = []
plugin.register({ register: contribution => contributions.push(contribution) })
const serializable = contributions.map(({ render, ...contribution }) => ({
  ...contribution,
  hasRender: typeof render === 'function'
}))
console.log(JSON.stringify({ id: plugin.id, contributions: serializable }))
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(PLUGIN)],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    loaded = json.loads(completed.stdout)
    assert loaded["id"] == "hermes-fleet"
    contributions = loaded["contributions"]

    sections = [
        ("overview", "/fleet"),
        ("network", "/fleet/network"),
        ("members", "/fleet/members"),
        ("invitations", "/fleet/invitations"),
        ("profiles", "/fleet/profiles"),
        ("workflows", "/fleet/workflows"),
        ("activity", "/fleet/activity"),
        ("settings", "/fleet/settings"),
    ]
    assert contributions[:8] == [
        {
            "id": f"page:{section_id}",
            "area": "app.routes",
            "data": {"path": path},
            "hasRender": True,
        }
        for section_id, path in sections
    ]
    assert contributions[8] == {
        "id": "nav",
        "area": "app.sidebar.nav",
        "order": 55,
        "data": {
            "codicon": "server-process",
            "label": "Fleet",
            "path": "/fleet",
        },
        "hasRender": False,
    }
    assert contributions[9] == {
        "id": "status",
        "area": "status:right",
        "order": 55,
        "hasRender": True,
    }

    open_commands = contributions[10:18]
    assert [entry["id"] for entry in open_commands] == [
        f"open-{section_id}-command" for section_id, _path in sections
    ]
    assert [entry["data"]["id"] for entry in open_commands] == [
        "fleet.open",
        "fleet.open.network",
        "fleet.open.members",
        "fleet.open.invitations",
        "fleet.open.profiles",
        "fleet.open.workflows",
        "fleet.open.activity",
        "fleet.open.settings",
    ]
    assert [entry["data"]["label"] for entry in open_commands] == [
        "Fleet: Open Overview",
        "Fleet: Open Network",
        "Fleet: Open Members",
        "Fleet: Open Invitations",
        "Fleet: Open Profiles",
        "Fleet: Open Workflows",
        "Fleet: Open Activity",
        "Fleet: Open Settings",
    ]
    assert all(entry["area"] == "palette" for entry in open_commands)
    assert all(entry["hasRender"] is False for entry in open_commands)
    assert all(entry["data"]["keywords"][0] == "fleet" for entry in open_commands)

    assert contributions[18] == {
        "id": "refresh-command",
        "area": "palette",
        "data": {
            "id": "fleet.refresh",
            "label": "Fleet: Refresh Overview",
            "keywords": ["fleet", "refresh", "reconnect"],
        },
        "hasRender": False,
    }
    assert len(contributions) == 19


def test_hidden_legacy_dashboard_entry_registers_without_visible_ui() -> None:
    script = r"""
import fs from 'node:fs'
const registrations = []
globalThis.window = {
  __HERMES_PLUGINS__: {
    register: (name, component) => registrations.push({ name, result: component() })
  }
}
const source = fs.readFileSync(process.argv[1], 'utf8')
const url = `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
await import(url)
console.log(JSON.stringify(registrations))
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(LEGACY_ENTRY)],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == [{"name": "hermes-fleet", "result": None}]


def test_d2_canvas_engine_is_deterministic_bounded_and_truthful() -> None:
    script = r"""
import fs from 'node:fs'
const dataUrl = source =>
  `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const sdkUrl = dataUrl(`
  export const ROUTES_AREA = 'app.routes'
  export const SIDEBAR_NAV_AREA = 'app.sidebar.nav'
  export const Button = 'Button'
  export const Codicon = 'Codicon'
  export const ContextMenu = 'ContextMenu'
  export const ContextMenuContent = 'ContextMenuContent'
  export const ContextMenuItem = 'ContextMenuItem'
  export const ContextMenuSeparator = 'ContextMenuSeparator'
  export const ContextMenuTrigger = 'ContextMenuTrigger'
  export const ScrollArea = 'ScrollArea'
  export const SearchField = 'SearchField'
  export const SegmentedControl = 'SegmentedControl'
  export const EmptyState = 'EmptyState'
  export const ErrorState = 'ErrorState'
  export const Loader = 'Loader'
  export const StatusDot = 'StatusDot'
  export const PALETTE_AREA = 'palette'
  export const STATUSBAR_AREAS = { right: 'status:right' }
  export const host = { navigate: () => undefined, notify: () => undefined }
  export const queryClient = {
    getQueryData: () => undefined,
    setQueryData: () => undefined,
    invalidateQueries: () => Promise.resolve()
  }
  export const useQuery = () => { throw new Error('render was not expected') }
`)
const reactUrl = dataUrl(`
  export const memo = value => value
  export const useCallback = value => value
  export const useEffect = () => undefined
  export const useMemo = factory => factory()
  export const useRef = value => ({ current: value })
  export const useState = value => [
    typeof value === 'function' ? value() : value,
    () => {}
  ]
`)
const jsxUrl = dataUrl(`
  export const jsx = (type, props, key) => ({ type, props, key })
  export const jsxs = jsx
`)
let source = fs.readFileSync(process.argv[1], 'utf8')
source = source.replaceAll("'@hermes/plugin-sdk'", `'${sdkUrl}'`)
source = source.replaceAll("'react/jsx-runtime'", `'${jsxUrl}'`)
source = source.replaceAll("'react'", `'${reactUrl}'`)
const mod = await import(dataUrl(source))

const rejects = operation => {
  try { operation(); return false } catch { return true }
}
const node = (id, options = {}) => ({
  stable_id: id,
  identity: { source: 'nodescale', network_id: 'network-1', device_id: id },
  naming: {
    display_name: options.name ?? id,
    provider_name: null,
    alias: options.alias ?? null,
    has_alias: options.alias != null
  },
  managed: { active: options.active ?? true },
  readiness: {
    alive: options.alive ?? true,
    scheduler_ready: options.ready ?? false,
    capacity: null
  },
  operations: []
})
const nodes = [
  node('node-c', { active: false, alive: false }),
  node('node-a', { name: 'compute-a', ready: true }),
  node('node-b', { alias: 'compute-b' })
]
const graph = mod.buildFleetGraph(nodes, { 'node-b': { x: 900, y: 700 } })
const filtered = mod.filterFleetGraph(graph, 'compute-b', 'all')
const attention = mod.filterFleetGraph(graph, '', 'attention')
const fit = mod.fitFleetGraph(graph.nodes.slice(0, 1), 400, 300, 40)
const zoomed = mod.zoomFleetViewport({ x: 10, y: 20, scale: 1 }, 2, 100, 80)
const panned = mod.panFleetViewport({ x: 10, y: 20, scale: 2 }, 40, -20)
const moved = mod.moveFleetPosition(
  { 'node-a': { x: 0, y: 0 }, 'node-b': { x: 20, y: 30 } },
  'node-a',
  24,
  -24
)
const fullLayout = Object.fromEntries(
  Array.from(
    { length: 256 },
    (_, index) => [
      `node-${String(index).padStart(3, '0')}`,
      { x: index, y: index }
    ]
  )
)
const movedIntoFullLayout = mod.moveFleetPosition(fullLayout, 'node-new', 24, -24)
const minimumZoom = mod.zoomFleetViewport({ x: 0, y: 0, scale: 1 }, 0.01, 0, 0)
const many = mod.buildFleetGraph(
  Array.from(
    { length: 256 },
    (_, index) => node(`node-${String(index).padStart(3, '0')}`)
  ),
  {}
)
const duplicateGraph = mod.buildFleetGraph([
  node('node-duplicate', { name: 'first' }),
  node('node-unique'),
  node('node-duplicate', { name: 'second' })
])
const observed = {
  observed_id: 'sha256:' + 'a'.repeat(64),
  network_id: 'network-1',
  provider_kind: 'headscale',
  provider_instance_id: 'instance-1',
  provider_node_id: 'provider-node-1',
  hostname: 'compute-observed',
  given_name: 'compute-observed.example.invalid',
  addresses: ['provider-address-1'],
  tags: ['tag:worker'],
  online: true,
  expired: false,
  classification: 'discovered_unmanaged',
  first_observed_at: '2026-08-10T00:00:00+00:00',
  last_observed_at: '2026-08-10T00:00:01+00:00',
  snapshot_at: '2026-08-10T00:00:01+00:00'
}
observed.managed = { active: true }
observed.readiness = { scheduler_ready: true }
observed.operations = ['fleet.hermes.run']
observed.alias = 'compute-observed'
observed.reservation = { id: 'reservation-1' }
observed.binding = { id: 'binding-1' }
observed.execution = { run: 'run-1' }
const combinedNodes = mod.buildFleetCanvasNodes({
  nodes: [node('managed-node', { ready: true })],
  observed_nodes: [observed]
})
const combinedGraph = mod.buildFleetGraph(combinedNodes)
const oversizedPositions = mod.sanitizeFleetPositions(Object.fromEntries(
  Array.from({ length: 513 }, (_, index) => [`node-${index}`, { x: index, y: index }])
))
const observedBatch = Array.from({ length: 256 }, (_, index) => ({
  ...observed,
  observed_id: `sha256:${index.toString(16).padStart(64, '0')}`,
  provider_node_id: `provider-node-${index}`,
  managed: undefined,
  readiness: undefined,
  operations: undefined,
  alias: undefined,
  reservation: undefined,
  binding: undefined,
  execution: undefined
}))
const combined512 = mod.buildFleetGraph(mod.buildFleetCanvasNodes({
  nodes: Array.from({ length: 256 }, (_, index) => node(`managed-${index}`)),
  observed_nodes: observedBatch
}))
console.log(JSON.stringify({
  ids: graph.nodes.map(item => item.id),
  positions: graph.nodes.map(item => [item.id, item.x, item.y]),
  edges: graph.edges,
  filtered: filtered.nodes.map(item => item.id),
  attention: attention.nodes.map(item => item.id),
  fit: {
    x: Math.round(fit.x * 1000) / 1000,
    y: Math.round(fit.y * 1000) / 1000,
    scale: Math.round(fit.scale * 1000) / 1000
  },
  zoomed,
  panned,
  moved,
  movedIntoFullLayout: {
    count: Object.keys(movedIntoFullLayout).length,
    newPosition: movedIntoFullLayout['node-new'],
    retainedFirst: movedIntoFullLayout['node-000'],
    retainedLast: movedIntoFullLayout['node-255'] ?? null
  },
  minimumZoom,
  manyCount: many.nodes.length,
  manyUnique: new Set(many.nodes.map(item => `${item.x}:${item.y}`)).size,
  manyFinite: many.nodes.every(
    item => Number.isFinite(item.x) && Number.isFinite(item.y)
  ),
  duplicateIds: duplicateGraph.nodes.map(item => item.id),
  combined: combinedGraph.nodes.map(item => ({
    id: item.id,
    label: item.label,
    kind: item.source.kind,
    status: item.status.label,
    detail: item.detail,
    hasReadiness: 'readiness' in item.source,
    hasOperations: 'operations' in item.source,
    hasManaged: 'managed' in item.source
  })),
  combinedEdges: combinedGraph.edges,
  observedEvidenceKeys: Object.keys(combinedNodes[1].observation).sort(),
  combined512Count: combined512.nodes.length,
  overLimitManagedRejected: rejects(() => mod.buildFleetCanvasNodes({
    nodes: Array.from({ length: 257 }, (_, index) => node(`managed-over-${index}`)),
    observed_nodes: []
  })),
  overLimitObservedRejected: rejects(() => mod.buildFleetCanvasNodes({
    nodes: [], observed_nodes: [...observedBatch, observedBatch[0]]
  })),
  combinedGroups: combinedGraph.groups.map(item => ({
    id: item.id,
    label: item.label,
    kind: item.kind,
    nodeIds: item.nodeIds
  })),
  oversizedPositionCount: Object.keys(oversizedPositions).length
}))
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(PLUGIN)],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "ids": ["node-a", "node-b", "node-c"],
        "positions": [
            ["node-a", 0, 0],
            ["node-b", 900, 700],
            ["node-c", 0, 176],
        ],
        "edges": [],
        "filtered": ["node-b"],
        "attention": ["node-b"],
        "fit": {"x": 62.5, "y": 77.5, "scale": 1.25},
        "zoomed": {"x": -80, "y": -40, "scale": 2},
        "panned": {"x": 50, "y": 0, "scale": 2},
        "moved": {
            "node-a": {"x": 24, "y": -24},
            "node-b": {"x": 20, "y": 30},
        },
        "movedIntoFullLayout": {
            "count": 257,
            "newPosition": {"x": 24, "y": -24},
            "retainedFirst": {"x": 0, "y": 0},
            "retainedLast": {"x": 255, "y": 255},
        },
        "minimumZoom": {"x": 0, "y": 0, "scale": 0.5},
        "manyCount": 256,
        "manyUnique": 256,
        "manyFinite": True,
        "duplicateIds": ["node-unique"],
        "combined": [
            {
                "id": "managed-node",
                "label": "managed-node",
                "kind": "managed",
                "status": "READY",
                "detail": "No worker capacity",
                "hasReadiness": True,
                "hasOperations": True,
                "hasManaged": True,
            },
            {
                "id": "observed-node-" + "a" * 64,
                "label": "compute-observed",
                "kind": "observed",
                "status": "OBSERVED · UNMANAGED",
                "detail": "Headscale",
                "hasReadiness": False,
                "hasOperations": False,
                "hasManaged": False,
            },
        ],
        "combinedEdges": [],
        "observedEvidenceKeys": [
            "addresses",
            "classification",
            "expired",
            "expires_at",
            "first_observed_at",
            "given_name",
            "hostname",
            "last_observed_at",
            "last_seen_at",
            "network_id",
            "observed_id",
            "online",
            "provider_instance_id",
            "provider_kind",
            "provider_node_id",
            "registered_at",
            "snapshot_at",
            "tags",
        ],
        "combined512Count": 512,
        "overLimitManagedRejected": True,
        "overLimitObservedRejected": True,
        "combinedGroups": [
            {
                "id": "managed:nodescale:network-1",
                "label": "Managed · nodescale · network-1",
                "kind": "managed",
                "nodeIds": ["managed-node"],
            },
            {
                "id": "observed:headscale:network-1:instance-1",
                "label": "Headscale network",
                "kind": "observed",
                "nodeIds": ["observed-node-" + "a" * 64],
            },
        ],
        "oversizedPositionCount": 0,
    }


def test_d3_inspector_contract_is_provider_neutral_and_evidence_driven() -> None:
    script = r"""
import fs from 'node:fs'
const dataUrl = source =>
  `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const sdkUrl = dataUrl(`
  export const ROUTES_AREA = 'app.routes'
  export const SIDEBAR_NAV_AREA = 'app.sidebar.nav'
  export const Button = 'Button'
  export const Codicon = 'Codicon'
  export const ContextMenu = 'ContextMenu'
  export const ContextMenuContent = 'ContextMenuContent'
  export const ContextMenuItem = 'ContextMenuItem'
  export const ContextMenuSeparator = 'ContextMenuSeparator'
  export const ContextMenuTrigger = 'ContextMenuTrigger'
  export const ScrollArea = 'ScrollArea'
  export const SearchField = 'SearchField'
  export const SegmentedControl = 'SegmentedControl'
  export const EmptyState = 'EmptyState'
  export const ErrorState = 'ErrorState'
  export const Loader = 'Loader'
  export const StatusDot = 'StatusDot'
  export const PALETTE_AREA = 'palette'
  export const STATUSBAR_AREAS = { right: 'status:right' }
  export const host = { navigate: () => undefined, notify: () => undefined }
  export const queryClient = {
    getQueryData: () => undefined,
    setQueryData: () => undefined,
    invalidateQueries: () => Promise.resolve()
  }
  export const useQuery = () => { throw new Error('render was not expected') }
`)
const reactUrl = dataUrl(`
  export const memo = value => value
  export const useCallback = value => value
  export const useEffect = () => undefined
  export const useMemo = factory => factory()
  export const useRef = value => ({ current: value })
  export const useState = value => [
    typeof value === 'function' ? value() : value,
    () => {}
  ]
`)
const jsxUrl = dataUrl(`
  export const jsx = (type, props, key) => ({ type, props, key })
  export const jsxs = jsx
`)
let source = fs.readFileSync(process.argv[1], 'utf8')
source = source.replaceAll("'@hermes/plugin-sdk'", `'${sdkUrl}'`)
source = source.replaceAll("'react/jsx-runtime'", `'${jsxUrl}'`)
source = source.replaceAll("'react'", `'${reactUrl}'`)
const mod = await import(dataUrl(source))
const base = {
  stable_id: 'fleet-node-' + '1'.repeat(64),
  identity: { source: 'nodescale', network_id: 'network-1', device_id: 'device-1' },
  naming: {
    display_name: 'device-1', provider_name: null, alias: null, has_alias: false
  },
  managed: {
    state: 'active',
    active: true,
    projection_generation: '9',
    membership_generation: '8',
    binding_generation: '7'
  },
  readiness: {
    managed_state: 'active',
    admission_generation: 7,
    alive: true,
    fresh: true,
    scheduler_ready: true,
    observation_age_ms: 2500,
    reasons: [],
    last_observation: {
      admission_generation: 7,
      observed_at_ms: 100,
      received_at_ms: 101,
      network: 'reachable',
      keryx: 'available',
      hermes: 'available',
      worker: 'available'
    },
    capacity: { active_workers: 1, max_workers: 3, available_worker_slots: 2 },
    resources: {
      cpu: { logical_cores: 8, load_basis_points: 2575 },
      ram: { total_bytes: 17179869184, available_bytes: 8589934592 },
      swap: { total_bytes: 0, available_bytes: 0 },
      disk: { total_bytes: 107374182400, available_bytes: 53687091200 },
      gpu: {
        present: true,
        vram: { total_bytes: 8589934592, available_bytes: 4294967296 }
      }
    }
  },
  operations: ['fleet.health', 'fleet.inventory']
}
const stale = structuredClone(base)
stale.readiness.alive = false
stale.readiness.fresh = false
stale.readiness.scheduler_ready = false
stale.readiness.reasons = ['observation_stale']
const missing = structuredClone(base)
missing.readiness.alive = false
missing.readiness.fresh = false
missing.readiness.scheduler_ready = false
missing.readiness.observation_age_ms = null
missing.readiness.reasons = ['observation_missing']
missing.readiness.last_observation = null
missing.readiness.capacity = null
missing.readiness.resources = null
console.log(JSON.stringify({
  ready: mod.buildReadinessLadder(base).map(item => [item.key, item.state]),
  stale: mod.buildReadinessLadder(stale).map(item => [item.key, item.state]),
  missing: mod.buildReadinessLadder(missing).map(item => [item.key, item.state]),
  reason: mod.describeReadinessReason('keryx_unavailable'),
  unknownReason: mod.describeReadinessReason('future_reason'),
  age: mod.formatFleetAge(2500),
  bytes: mod.formatFleetBytes(17179869184),
  mutation: mod.aliasMutationBody(base, 'compute-a'),
  activity: mod
    .diffFleetOverview({ nodes: [stale] }, { nodes: [base] }, 3)
    .map(item => [item.kind, item.node_id]),
  staleResource: mod.buildResourceRows(stale.readiness)[0].value,
  canvasCapacity: mod.formatCanvasCapacity(base.readiness),
  staleCanvasCapacity: mod.formatCanvasCapacity(stale.readiness),
  missingCanvasCapacity: mod.formatCanvasCapacity(missing.readiness),
  resources: mod.buildResourceRows(base.readiness).map(item => [item.key, item.value])
}))
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(PLUGIN)],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "ready": [
            ["managed", "ready"],
            ["fresh", "ready"],
            ["network", "ready"],
            ["keryx", "ready"],
            ["hermes", "ready"],
            ["worker", "ready"],
            ["capacity", "ready"],
        ],
        "stale": [
            ["managed", "ready"],
            ["fresh", "blocked"],
            ["network", "unknown"],
            ["keryx", "unknown"],
            ["hermes", "unknown"],
            ["worker", "unknown"],
            ["capacity", "unknown"],
        ],
        "missing": [
            ["managed", "ready"],
            ["fresh", "unknown"],
            ["network", "unknown"],
            ["keryx", "unknown"],
            ["hermes", "unknown"],
            ["worker", "unknown"],
            ["capacity", "unknown"],
        ],
        "reason": "Keryx is unavailable.",
        "unknownReason": "Unknown readiness reason: future_reason",
        "age": "2.5s ago",
        "bytes": "16.0 GiB",
        "mutation": {
            "source": "nodescale",
            "network_id": "network-1",
            "device_id": "device-1",
            "binding_generation": "7",
            "alias": "compute-a",
        },
        "activity": [["recovered", "fleet-node-" + "1" * 64]],
        "staleResource": "Last observed 2.5s ago: 1 / 3 active · 2 free",
        "canvasCapacity": "Workers 1 / 3",
        "staleCanvasCapacity": "Last observed 2.5s ago: Workers 1 / 3",
        "missingCanvasCapacity": "No worker capacity",
        "resources": [
            ["workers", "1 / 3 active · 2 free"],
            ["cpu", "8 logical · 25.75% load"],
            ["ram", "8.0 GiB free / 16.0 GiB"],
            ["swap", "0 B free / 0 B"],
            ["disk", "50.0 GiB free / 100.0 GiB"],
            ["gpu", "Present"],
            ["vram", "4.0 GiB free / 8.0 GiB"],
        ],
    }


def test_fleet_app_shell_has_stable_internal_routes_without_sidebar_sprawl() -> None:
    source = PLUGIN.read_text(encoding="utf-8")

    assert "export const FLEET_SECTIONS" in source
    assert "export function getFleetSection" in source
    assert "function FleetAppShell" in source
    assert "function FleetSectionNavigation" in source
    assert "'aria-label': 'Fleet sections'" in source
    assert "'aria-current': active ? 'page' : undefined" in source
    assert "for (const section of FLEET_SECTIONS)" in source
    assert "id: `page:${section.id}`" in source
    assert "data: { path: section.path }" in source
    assert source.count("area: SIDEBAR_NAV_AREA") == 1
    assert (
        "data: { codicon: 'server-process', label: 'Fleet', path: '/fleet' }" in source
    )

    for path in (
        "/fleet",
        "/fleet/network",
        "/fleet/members",
        "/fleet/invitations",
        "/fleet/profiles",
        "/fleet/workflows",
        "/fleet/activity",
        "/fleet/settings",
    ):
        assert f"path: '{path}'" in source

    assert "surface: 'network'" in source
    assert "surface: 'workflows'" in source
    assert "host.navigate('/fleet/workflows')" in source
    assert "function useFleetWorkflowSession" in source
    assert "SegmentedControl" not in source
    assert (
        "Provider visibility, Nodescale trust, Keryx identity, Fleet authorization"
        in source
    )
    assert "No invitation secret is read, cached, or simulated by this shell." in source
    assert "This shell does not mutate backend configuration." in source


def test_phase1_overview_command_center_derives_truthful_operator_state() -> None:
    source = PLUGIN.read_text(encoding="utf-8")
    for contract in (
        "export function buildFleetOverviewCommandCenterModel",
        "export function formatFleetObservationAge",
        "Fleet command-center summary",
        "Needs attention",
        "Quick actions",
        "Readiness evidence",
        "Authority and availability",
        "Invite someone",
        "Search Fleet",
        (
            "Invitation creation unavailable until an authenticated "
            "operator contract exists."
        ),
        (
            "Task and run counts are unavailable in the current "
            "authoritative Desktop contract."
        ),
    ):
        assert contract in source
    assert "pending invitations" not in source.lower()
    assert "pending members" not in source.lower()

    script = r"""
import fs from 'node:fs'
const dataUrl = source =>
  `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const sdkUrl = dataUrl(`
  export const ROUTES_AREA = 'app.routes'
  export const SIDEBAR_NAV_AREA = 'app.sidebar.nav'
  export const Button = 'Button'
  export const Codicon = 'Codicon'
  export const ContextMenu = 'ContextMenu'
  export const ContextMenuContent = 'ContextMenuContent'
  export const ContextMenuItem = 'ContextMenuItem'
  export const ContextMenuSeparator = 'ContextMenuSeparator'
  export const ContextMenuTrigger = 'ContextMenuTrigger'
  export const ScrollArea = 'ScrollArea'
  export const SearchField = 'SearchField'
  export const EmptyState = 'EmptyState'
  export const ErrorState = 'ErrorState'
  export const Loader = 'Loader'
  export const StatusDot = 'StatusDot'
  export const PALETTE_AREA = 'palette'
  export const STATUSBAR_AREAS = { right: 'status:right' }
  export const host = { navigate: () => undefined, notify: () => undefined }
  export const queryClient = {
    getQueryData: () => undefined,
    setQueryData: () => undefined,
    invalidateQueries: () => Promise.resolve()
  }
  export const useQuery = () => { throw new Error('render was not expected') }
`)
const reactUrl = dataUrl(`
  export const memo = value => value
  export const useCallback = value => value
  export const useEffect = () => undefined
  export const useMemo = factory => factory()
  export const useRef = value => ({ current: value })
  export const useState = value => [
    typeof value === 'function' ? value() : value,
    () => {}
  ]
`)
const jsxUrl = dataUrl(`
  export const jsx = (type, props, key) => ({ type, props, key })
  export const jsxs = jsx
`)
let source = fs.readFileSync(process.argv[1], 'utf8')
source = source.replaceAll("'@hermes/plugin-sdk'", `'${sdkUrl}'`)
source = source.replaceAll("'react/jsx-runtime'", `'${jsxUrl}'`)
source = source.replaceAll("'react'", `'${reactUrl}'`)
const mod = await import(dataUrl(source))
const gib = 1024 * 1024 * 1024
const node = (hex, name, options) => ({
  stable_id: 'fleet-node-' + hex.repeat(64),
  naming: { display_name: name },
  managed: { state: options.active ? 'active' : 'disabled', active: options.active },
  readiness: {
    scheduler_ready: options.ready,
    observation_age_ms: options.age,
    reasons: options.reasons,
    capacity: options.capacity,
    profiles: options.profiles,
    resources: { ram: options.ram }
  }
})
const overview = {
  summary: {
    managed: 3,
    active: 2,
    alive: 2,
    ready: 1,
    not_ready: 1,
    observed_unmanaged: 2
  },
  nodes: [
    node('a', 'Compute Alpha', {
      active: true,
      ready: true,
      age: 3000,
      reasons: [],
      capacity: { active_workers: 1, max_workers: 2, available_worker_slots: 1 },
      profiles: [{ name: 'general', version: '1' }],
      ram: { total_bytes: 8 * gib, available_bytes: 4 * gib }
    }),
    node('b', 'Compute Beta', {
      active: true,
      ready: false,
      age: 12000,
      reasons: ['network_unreachable'],
      capacity: { active_workers: 2, max_workers: 4, available_worker_slots: 2 },
      profiles: [
        { name: 'general', version: '1' },
        { name: 'research', version: '1' }
      ],
      ram: { total_bytes: 4 * gib, available_bytes: 2 * gib }
    }),
    node('c', 'Compute Reserve', {
      active: false,
      ready: false,
      age: null,
      reasons: ['node_not_active'],
      capacity: null,
      profiles: null,
      ram: null
    })
  ],
  observed_nodes: [{}, {}],
  observations: { state: 'available', truncated: false }
}
const model = mod.buildFleetOverviewCommandCenterModel(overview)
console.log(JSON.stringify({
  metrics: model.metrics,
  attention: model.attention,
  capacity: model.capacity,
  memory: model.memory,
  profileCount: model.profileCount,
  freshness: model.freshness,
  observationStatus: model.observationStatus,
  age: mod.formatFleetObservationAge(12000)
}))
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(PLUGIN)],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    loaded = json.loads(completed.stdout)
    assert [metric["id"] for metric in loaded["metrics"]] == [
        "managed",
        "active",
        "alive",
        "ready",
        "observed",
        "attention",
    ]
    assert [metric["value"] for metric in loaded["metrics"]] == [3, 2, 2, 1, 2, 1]
    assert len(loaded["attention"]) == 1
    assert loaded["attention"][0]["label"] == "Compute Beta"
    assert loaded["attention"][0]["reason"] == "network_unreachable"
    assert loaded["attention"][0]["reasonLabel"] == "Network unreachable"
    assert loaded["attention"][0]["ageLabel"] == "12s ago"
    assert loaded["capacity"] == {
        "reportingNodes": 2,
        "activeWorkers": 3,
        "maxWorkers": 6,
        "availableWorkerSlots": 3,
    }
    assert loaded["memory"] == {
        "reportingNodes": 2,
        "totalBytes": 12 * 1024 * 1024 * 1024,
        "availableBytes": 6 * 1024 * 1024 * 1024,
        "usedBytes": 6 * 1024 * 1024 * 1024,
        "usedPercent": 50,
    }
    assert loaded["profileCount"] == 2
    assert loaded["freshness"] == "Oldest managed sample 12s ago"
    assert loaded["observationStatus"] == "Provider observations live"
    assert loaded["age"] == "12s ago"


def test_phase2_network_workspace_models_truthful_filters_and_presentation_state() -> (
    None
):
    source = PLUGIN.read_text(encoding="utf-8")
    for contract in (
        "export const NETWORK_FILTERS",
        "export function buildFleetNetworkWorkspaceModel",
        "export function normalizeFleetNetworkPresentationState",
        "export function setFleetNetworkPresentationState",
        "function NetworkSummaryStrip",
        "function NetworkFacetSelect",
        "Source/provider",
        "No machines match the current Network filters.",
        "data-authority",
        "border-style: dashed",
        "NETWORK_METRIC_FILTERS",
        "openFleetNetwork({",
    ):
        assert contract in source

    script = r"""
import fs from 'node:fs'
const dataUrl = source =>
  `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const sdkUrl = dataUrl(`
  export const ROUTES_AREA = 'app.routes'
  export const SIDEBAR_NAV_AREA = 'app.sidebar.nav'
  export const Button = 'Button'
  export const Codicon = 'Codicon'
  export const ContextMenu = 'ContextMenu'
  export const ContextMenuContent = 'ContextMenuContent'
  export const ContextMenuItem = 'ContextMenuItem'
  export const ContextMenuSeparator = 'ContextMenuSeparator'
  export const ContextMenuTrigger = 'ContextMenuTrigger'
  export const ScrollArea = 'ScrollArea'
  export const SearchField = 'SearchField'
  export const EmptyState = 'EmptyState'
  export const ErrorState = 'ErrorState'
  export const Loader = 'Loader'
  export const StatusDot = 'StatusDot'
  export const PALETTE_AREA = 'palette'
  export const STATUSBAR_AREAS = { right: 'status:right' }
  export const host = { navigate: () => undefined, notify: () => undefined }
  export const queryClient = { invalidateQueries: () => Promise.resolve() }
  export const useQuery = () => { throw new Error('render was not expected') }
`)
const reactUrl = dataUrl(`
  export const memo = value => value
  export const useCallback = value => value
  export const useEffect = () => undefined
  export const useMemo = factory => factory()
  export const useRef = value => ({ current: value })
  export const useState = value => [
    typeof value === 'function' ? value() : value,
    () => {}
  ]
`)
const jsxUrl = dataUrl(`
  export const jsx = (type, props, key) => ({ type, props, key })
  export const jsxs = jsx
`)
let source = fs.readFileSync(process.argv[1], 'utf8')
source = source.replaceAll("'@hermes/plugin-sdk'", `'${sdkUrl}'`)
source = source.replaceAll("'react/jsx-runtime'", `'${jsxUrl}'`)
source = source.replaceAll("'react'", `'${reactUrl}'`)
const mod = await import(dataUrl(source))

const managed = (id, network, options = {}) => ({
  stable_id: id,
  identity: { source: 'nodescale', network_id: network, device_id: id },
  naming: {
    display_name: options.name ?? id,
    provider_name: null,
    alias: null,
    has_alias: false
  },
  managed: {
    active: options.active ?? true,
    state: options.active === false ? 'disabled' : 'active',
    binding_generation: 1
  },
  readiness: {
    alive: options.alive ?? true,
    scheduler_ready: options.ready ?? false,
    fresh: options.fresh ?? true,
    observation_age_ms: 12000,
    reasons: options.ready ? [] : ['no_worker_capacity'],
    last_observation: options.observed === false ? null : {
      network: 'reachable',
      keryx: 'available',
      hermes: 'available',
      worker: 'available'
    },
    capacity: {
      active_workers: options.ready ? 1 : 2,
      max_workers: 2,
      available_worker_slots: options.ready ? 1 : 0
    },
    resources: null,
    profiles: []
  },
  operations: ['fleet.health']
})
const observed = {
  observed_id: 'sha256:' + 'b'.repeat(64),
  network_id: 'network-b',
  provider_kind: 'headscale',
  provider_instance_id: 'instance-b',
  provider_node_id: 'provider-node-b',
  hostname: 'compute-observed',
  given_name: 'compute-observed.example.invalid',
  addresses: ['provider-address-b'],
  tags: ['tag:worker'],
  registered_at: null,
  last_seen_at: null,
  expires_at: null,
  online: true,
  expired: false,
  classification: 'discovered_unmanaged',
  first_observed_at: '2026-08-10T00:00:00+00:00',
  last_observed_at: '2026-08-10T00:00:01+00:00',
  snapshot_at: '2026-08-10T00:00:01+00:00'
}
const canvasNodes = mod.buildFleetCanvasNodes({
  nodes: [
    managed('node-a', 'network-a', { name: 'compute-a', ready: true }),
    managed('node-b', 'network-a', { name: 'compute-b' }),
    managed('node-c', 'network-c', { active: false, alive: false })
  ],
  observed_nodes: [observed]
})
const graph = mod.buildFleetGraph(canvasNodes)
const model = mod.buildFleetNetworkWorkspaceModel(graph)
const project = result => result.nodes.map(node => node.id)
console.log(JSON.stringify({
  filters: mod.NETWORK_FILTERS,
  model,
  managed: project(mod.filterFleetGraph(graph, '', 'managed')),
  active: project(mod.filterFleetGraph(graph, '', 'active')),
  alive: project(mod.filterFleetGraph(graph, '', 'alive')),
  ready: project(mod.filterFleetGraph(graph, '', 'ready')),
  attention: project(mod.filterFleetGraph(graph, '', 'attention')),
  observed: project(mod.filterFleetGraph(graph, '', 'observed')),
  provider: project(mod.filterFleetGraph(graph, '', 'all', 'provider:headscale')),
  network: project(mod.filterFleetGraph(graph, '', 'all', 'all', 'network-c')),
  query: project(mod.filterFleetGraph(graph, 'compute-b', 'all')),
  normalized: mod.normalizeFleetNetworkPresentationState({
    query: 'compute-a',
    status: 'ready',
    source: 'managed:nodescale',
    network: 'network-a',
    selectedId: 'node-a'
  }),
  invalid: mod.normalizeFleetNetworkPresentationState({
    query: 'x'.repeat(300),
    status: 'unsupported',
    source: 'x'.repeat(300),
    network: 'x'.repeat(300),
    selectedId: 'x'.repeat(300)
  })
}))
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(PLUGIN)],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    loaded = json.loads(completed.stdout)
    assert [row[0] for row in loaded["filters"]] == [
        "all",
        "managed",
        "active",
        "alive",
        "ready",
        "attention",
        "observed",
        "awaiting",
        "inactive",
    ]
    assert loaded["model"]["visible"] == 4
    assert loaded["model"]["managed"] == 3
    assert loaded["model"]["active"] == 2
    assert loaded["model"]["alive"] == 2
    assert loaded["model"]["ready"] == 1
    assert loaded["model"]["attention"] == 1
    assert loaded["model"]["inactive"] == 1
    assert loaded["model"]["observed"] == 1
    assert loaded["model"]["relationshipCount"] == 0
    assert loaded["managed"] == ["node-a", "node-b", "node-c"]
    assert loaded["active"] == ["node-a", "node-b"]
    assert loaded["alive"] == ["node-a", "node-b"]
    assert loaded["ready"] == ["node-a"]
    assert loaded["attention"] == ["node-b"]
    assert loaded["observed"] == ["observed-node-" + "b" * 64]
    assert loaded["provider"] == ["observed-node-" + "b" * 64]
    assert loaded["network"] == ["node-c"]
    assert loaded["query"] == ["node-b"]
    assert loaded["normalized"] == {
        "query": "compute-a",
        "status": "ready",
        "source": "managed:nodescale",
        "network": "network-a",
        "selectedId": "node-a",
    }
    assert loaded["invalid"] == {
        "query": "",
        "status": "all",
        "source": "all",
        "network": "all",
        "selectedId": None,
    }


def test_phase3_membership_center_preserves_authority_boundaries() -> None:
    source = PLUGIN.read_text(encoding="utf-8")
    for contract in (
        "export const MEMBERSHIP_FILTERS",
        "export function buildFleetMembershipCenterModel",
        "export function filterFleetMembershipRows",
        "export function buildMembershipAuthorityStages",
        "function FleetMembershipCenter",
        "function MembershipAuthorityLadder",
        "function ManagedMembershipDetail",
        "function ObservedMembershipDetail",
        "'aria-label': 'Membership authority ladder'",
        "Membership and binding generations are version evidence",
        "does not expose a live trusted flag or authenticated Keryx peer ID",
        "sectionId === 'members'",
        "['overview', 'network', 'members', 'workflows']",
    ):
        assert contract in source
    assert "Membership surface reserved" not in source
    assert "Membership controls await an authenticated operator contract." not in source
    assert "id: 'members', label: 'Members'" in source
    assert "availability: 'available'" in source
    assert (
        "Managed admission, generation evidence, readiness, and provider observations."
        in source
    )
    assert "fleet-membership-root" in source
    assert "overflow-y-auto lg:overflow-hidden" in source
    assert "visibleRows.find(row => row.id === selectedId)" in source
    assert "['not_active', 'Disabled / removed', summary.notActive]" in source
    membership_surface = source[
        source.index("function FleetMembershipCenter") : source.index(
            "const FLEET_PLACEHOLDER_COPY"
        )
    ]
    for forbidden_mutation in (
        "ctx.rest",
        "aliasMutationBody",
        "fleet.hermes.run",
        "Trust device",
        "Revoke trust",
    ):
        assert forbidden_mutation not in membership_surface
    observed_detail = source[
        source.index("function ObservedMembershipDetail") : source.index(
            "function MembershipDetail"
        )
    ]
    assert "label: 'Expired'" in observed_detail
    assert "label: 'Expires'" in observed_detail

    desktop_docs = (ROOT / "docs" / "desktop.md").read_text(encoding="utf-8")
    assert "| Section | Route | Current behavior |" in desktop_docs
    assert "| Members | `/fleet/members` | Read-only Membership Center" in desktop_docs
    assert "- **Membership:** read-only managed admission" in desktop_docs
    assert "Membership mutation controls" in desktop_docs

    script = r"""
import fs from 'node:fs'
const dataUrl = source =>
  `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const sdkUrl = dataUrl(`
  export const ROUTES_AREA = 'app.routes'
  export const SIDEBAR_NAV_AREA = 'app.sidebar.nav'
  export const Button = 'Button'
  export const Codicon = 'Codicon'
  export const ContextMenu = 'ContextMenu'
  export const ContextMenuContent = 'ContextMenuContent'
  export const ContextMenuItem = 'ContextMenuItem'
  export const ContextMenuSeparator = 'ContextMenuSeparator'
  export const ContextMenuTrigger = 'ContextMenuTrigger'
  export const ScrollArea = 'ScrollArea'
  export const SearchField = 'SearchField'
  export const SegmentedControl = 'SegmentedControl'
  export const EmptyState = 'EmptyState'
  export const ErrorState = 'ErrorState'
  export const Loader = 'Loader'
  export const StatusDot = 'StatusDot'
  export const PALETTE_AREA = 'palette'
  export const STATUSBAR_AREAS = { right: 'status:right' }
  export const host = { navigate: () => undefined, notify: () => undefined }
  export const queryClient = {
    getQueryData: () => undefined,
    setQueryData: () => undefined,
    invalidateQueries: () => Promise.resolve()
  }
  export const useQuery = () => { throw new Error('render was not expected') }
`)
const reactUrl = dataUrl(`
  export const memo = value => value
  export const useCallback = value => value
  export const useEffect = () => undefined
  export const useMemo = factory => factory()
  export const useRef = value => ({ current: value })
  export const useState = value => [
    typeof value === 'function' ? value() : value,
    () => {}
  ]
`)
const jsxUrl = dataUrl(`
  export const jsx = (type, props, key) => ({ type, props, key })
  export const jsxs = jsx
`)
let source = fs.readFileSync(process.argv[1], 'utf8')
source = source.replaceAll("'@hermes/plugin-sdk'", `'${sdkUrl}'`)
source = source.replaceAll("'react/jsx-runtime'", `'${jsxUrl}'`)
source = source.replaceAll("'react'", `'${reactUrl}'`)
const mod = await import(dataUrl(source))

const managed = (id, options = {}) => ({
  stable_id: `fleet-node-${id.repeat(64).slice(0, 64)}`,
  identity: {
    source: 'nodescale',
    network_id: 'network-a',
    device_id: `device-${id}`
  },
  naming: {
    display_name: options.name ?? `compute-${id}`,
    provider_name: null,
    alias: null,
    has_alias: false
  },
  managed: {
    state: options.state ?? 'active',
    active: (options.state ?? 'active') === 'active',
    projection_generation: options.projection ?? '10',
    membership_generation: options.membership ?? '20',
    binding_generation: options.binding ?? '30'
  },
  readiness: {
    managed_state: options.state ?? 'active',
    alive: options.alive ?? true,
    scheduler_ready: options.ready ?? false,
    fresh: options.fresh ?? true,
    observation_age_ms: options.age ?? 12000,
    reasons: options.reasons ?? (options.ready ? [] : ['no_worker_capacity']),
    last_observation: options.hasObservation === false ? null : {
      network: 'reachable',
      keryx: 'available',
      hermes: 'available',
      worker: 'available'
    },
    capacity: options.capacity ?? {
      active_workers: 1,
      max_workers: 2,
      available_worker_slots: options.ready ? 1 : 0
    },
    resources: null,
    profiles: []
  },
  operations: ['fleet.health', 'fleet.inventory']
})

const observed = {
  observed_id: 'sha256:' + 'a'.repeat(64),
  network_id: 'network-a',
  provider_kind: 'headscale',
  provider_instance_id: 'provider-instance-a',
  provider_node_id: 'provider-node-a',
  hostname: 'compute-a',
  given_name: 'compute-observed.example.invalid',
  addresses: ['192.0.2.10'],
  tags: ['tag:worker'],
  registered_at: null,
  last_seen_at: '2026-08-10T00:00:00+00:00',
  expires_at: null,
  online: true,
  expired: false,
  classification: 'discovered_unmanaged',
  first_observed_at: '2026-08-10T00:00:00+00:00',
  last_observed_at: '2026-08-10T00:00:10+00:00',
  snapshot_at: '2026-08-10T00:00:10+00:00',
  managed: { state: 'active', active: true },
  readiness: { scheduler_ready: true },
  operations: ['fleet.hermes.run'],
  trust: true,
  keryx_peer_id: 'peer-a'
}

const overview = {
  schema: 'fleet.desktop.v2',
  summary: {
    managed: 3,
    active: 2,
    alive: 2,
    ready: 1,
    not_ready: 1,
    observed_unmanaged: 1
  },
  nodes: [
    managed('a', { name: 'compute-a', ready: true, membership: '21', binding: '31' }),
    managed('b', { name: 'compute-b', ready: false, membership: '22', binding: '32' }),
    managed('c', {
      name: 'compute-c',
      state: 'disabled',
      alive: false,
      fresh: false,
      hasObservation: false,
      reasons: ['node_not_active'],
      capacity: null,
      membership: '23',
      binding: '33'
    })
  ],
  observed_nodes: [observed],
  observations: {
    state: 'available',
    network_id: 'network-a',
    reconciliation: {},
    truncated: false
  }
}
const model = mod.buildFleetMembershipCenterModel(overview)
const managedRow = model.rows.find(row => row.label === 'compute-a')
const observedRow = model.rows.find(row => row.kind === 'observed')
const stages = mod.buildMembershipAuthorityStages(managedRow)
const observedStages = mod.buildMembershipAuthorityStages(observedRow)
const forbiddenObservedKeys = [
  'managed', 'readiness', 'operations', 'trust', 'keryx_peer_id', 'scheduler_ready'
]
console.log(JSON.stringify({
  summary: model.summary,
  filters: mod.MEMBERSHIP_FILTERS,
  attention: mod.filterFleetMembershipRows(
    model.rows, '', 'attention'
  ).map(row => row.label),
  inactive: mod.filterFleetMembershipRows(
    model.rows, '', 'not_active'
  ).map(row => row.label),
  observed: mod.filterFleetMembershipRows(
    model.rows, '', 'observed'
  ).map(row => row.label),
  search: mod.filterFleetMembershipRows(
    model.rows, 'compute-b', 'all'
  ).map(row => row.label),
  managedStages: stages.map(stage => [stage.key, stage.state]),
  managedMembershipDetail: stages.find(stage => stage.key === 'membership').detail,
  managedBindingDetail: stages.find(stage => stage.key === 'binding').detail,
  observedStages: observedStages.map(stage => [stage.key, stage.state]),
  observedProjectionDetail: observedStages
    .find(stage => stage.key === 'projection').detail,
  observedForbiddenKeys: forbiddenObservedKeys.filter(key =>
    key in observedRow.node || key in observedRow.node.observation
  ),
  sameNameKinds: model.rows
    .filter(row => row.label === 'compute-a')
    .map(row => row.kind)
}))
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(PLUGIN)],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    loaded = json.loads(completed.stdout)
    assert loaded["summary"] == {
        "visible": 4,
        "managed": 3,
        "active": 2,
        "notActive": 1,
        "disabled": 1,
        "removed": 0,
        "ready": 1,
        "attention": 1,
        "observed": 1,
    }
    assert [item[0] for item in loaded["filters"]] == [
        "all",
        "managed",
        "active",
        "not_active",
        "disabled",
        "removed",
        "ready",
        "attention",
        "observed",
    ]
    assert loaded["attention"] == ["compute-b"]
    assert loaded["inactive"] == ["compute-c"]
    assert loaded["observed"] == ["compute-a"]
    assert loaded["search"] == ["compute-b"]
    assert loaded["managedStages"] == [
        ["provider", "not joined"],
        ["projection", "accepted"],
        ["membership", "generation 21"],
        ["binding", "generation 31"],
        ["admission", "active"],
        ["readiness", "ready"],
    ]
    assert "not a live current trust check" in loaded["managedMembershipDetail"]
    assert (
        "not proof of a live authenticated Keryx peer binding"
        in loaded["managedBindingDetail"]
    )
    assert loaded["observedStages"] == [
        ["provider", "evidence"],
        ["projection", "not exposed"],
        ["membership", "unavailable"],
        ["binding", "unavailable"],
        ["admission", "not proven"],
        ["readiness", "not proven"],
    ]
    assert "does not correlate" in loaded["observedProjectionDetail"]
    assert loaded["observedForbiddenKeys"] == []
    assert loaded["sameNameKinds"] == ["managed", "observed"]
