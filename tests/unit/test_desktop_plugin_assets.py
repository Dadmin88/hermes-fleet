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
    assert "data: { path: '/fleet' }" in source
    assert "render: () => jsx(FleetPage, { ctx })" in source
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
    assert "id: 'fleet.open'" in source
    assert "STATUSBAR_AREAS.right" in source
    assert "role: 'tree'" in source
    assert "role: 'treeitem'" in source
    assert "node.source.kind === 'workflow'" in source
    assert "? 'Observed identity'" in source
    assert ": 'Stable identity'" in source
    assert "ResizeObserver" in source
    assert "function GraphEdges" in source
    assert "edges: []" in source
    assert "onPointerCancel" in source
    assert "Loader" in source
    assert "EmptyState" in source
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
    assert "const closeInspector = useCallback(() => setSelectedId(null), [])" in source
    assert "onClose: closeInspector" in source
    assert "className: 'h-full min-h-0 w-full shrink-0 overflow-auto" in source
    assert "canvasNodes.find(node => node.stable_id === selectedId) ?? null" in source
    assert (
        "canvasNodes.find(node => node.stable_id === selectedId) ?? canvasNodes[0]"
        not in source
    )


def test_graph_first_canvas_renders_groups_minimap_and_compact_status() -> None:
    source = PLUGIN.read_text(encoding="utf-8")

    assert "function GraphGroups" in source
    assert "jsx(GraphGroups, { groups: graph.groups })" in source
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
  hostname: 'katana',
  given_name: 'katana.example.ts.net',
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
    assert loaded["title"] == "katana"
    assert loaded["technicalName"] == "katana.example.ts.net"
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
        "export function createWorkflowFromTopology",
        "export function createWorkflowHistory",
        "export function applyWorkflowEdit",
        "export function undoWorkflow",
        "export function redoWorkflow",
        "export function updateFleetSelection",
        "export function nodesInsideSelection",
    ):
        assert symbol in source
    assert "Editor foundation · execution unavailable" in source
    assert "mode === 'workflow'" in source

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
workflow = mod.connectWorkflowNodes(workflow, {
  id: 'connection-1', source: 'trigger-1', sourcePort: 'control',
  target: 'delay-1', targetPort: 'control'
})
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
let history = mod.createWorkflowHistory(workflow)
history = mod.applyWorkflowEdit(history, duplicated)
history = mod.undoWorkflow(history)
history = mod.redoWorkflow(history)
const topology = mod.createWorkflowFromTopology('from-topology', [{
  stable_id: 'observed-node-a', kind: 'observed', node_type: 'machine',
  naming: { display_name: 'katana', technical_name: 'katana.example.ts.net' },
  provider: {
    kind: 'tailscale', label: 'Tailscale', node_id: 'node-1',
    network_id: 'network-1', instance_id: 'instance-1'
  },
  observation: {
    observed_id: 'sha256:' + 'a'.repeat(64),
    addresses: ['provider-address-1']
  }
}])
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
const boxed = mod.nodesInsideSelection([
  { id: 'a', x: 0, y: 0, width: 100, height: 80 },
  { id: 'b', x: 300, y: 300, width: 100, height: 80 }
], { x: -5, y: -5, width: 120, height: 100 })
console.log(JSON.stringify({
  ids,
  descriptor: mod.FLEET_NODE_TYPES['manual-trigger'],
  connection: workflow.connections[0],
  selection,
  pastedCounts: [pasted.nodes.length, pasted.connections.length],
  duplicatedCounts: [duplicated.nodes.length, duplicated.connections.length],
  deletedCounts: [deleted.nodes.length, deleted.connections.length],
  parsedSchema: parsed.schema,
  historyPresent: history.present.nodes.length,
  historyPast: history.past.length,
  topology,
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
  invalidContributionRejected:
    !mod.createFleetNodeRegistry([invalidContribution]).has('bad-contribution'),
  boxed
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
    assert loaded["selection"] == ["trigger-1"]
    assert loaded["pastedCounts"] == [3, 1]
    assert loaded["duplicatedCounts"] == [3, 1]
    assert loaded["deletedCounts"] == [1, 0]
    assert loaded["parsedSchema"] == "fleet.workflow-editor.v1"
    assert loaded["historyPresent"] == 3
    assert loaded["historyPast"] == 1
    assert loaded["topology"]["nodes"][0]["type"] == "exact-machine"
    assert loaded["topology"]["nodes"][0]["target"]["authority"] == "observed"
    assert loaded["providerInstance"] == "instance-1"
    assert loaded["machineWorkflowRejected"] is True
    assert loaded["hiddenPayloadRejected"] is True
    assert loaded["malformedClipboardRejected"] is True
    assert loaded["longPrefixUnique"] is True
    assert loaded["invalidContributionRejected"] is True
    assert "execution" not in loaded["topology"]
    assert loaded["boxed"] == ["a"]


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
    assert loaded == {
        "id": "hermes-fleet",
        "contributions": [
            {
                "id": "page",
                "area": "app.routes",
                "data": {"path": "/fleet"},
                "hasRender": True,
            },
            {
                "id": "nav",
                "area": "app.sidebar.nav",
                "order": 55,
                "data": {
                    "codicon": "server-process",
                    "label": "Fleet",
                    "path": "/fleet",
                },
                "hasRender": False,
            },
            {
                "id": "status",
                "area": "status:right",
                "order": 55,
                "hasRender": True,
            },
            {
                "id": "open-command",
                "area": "palette",
                "data": {
                    "id": "fleet.open",
                    "label": "Fleet: Open Canvas",
                    "keywords": ["fleet", "nodes", "readiness", "canvas"],
                },
                "hasRender": False,
            },
            {
                "id": "refresh-command",
                "area": "palette",
                "data": {
                    "id": "fleet.refresh",
                    "label": "Fleet: Refresh Overview",
                    "keywords": ["fleet", "refresh", "reconnect"],
                },
                "hasRender": False,
            },
        ],
    }


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
  node('node-a', { name: 'Worker One', ready: true }),
  node('node-b', { alias: 'Upstairs Workstation' })
]
const graph = mod.buildFleetGraph(nodes, { 'node-b': { x: 900, y: 700 } })
const filtered = mod.filterFleetGraph(graph, 'upstairs', 'all')
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
  hostname: 'observed-host',
  given_name: 'Observed Worker',
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
observed.alias = 'Injected alias'
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
                "label": "observed-host",
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
  mutation: mod.aliasMutationBody(base, 'Workstation'),
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
            "alias": "Workstation",
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
