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
    assert "role: 'tree'" in source
    assert "role: 'treeitem'" in source
    assert "Stable identity ${node.id}" in source
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


def test_desktop_plugin_evaluates_and_registers_current_sdk_contributions() -> None:
    script = r"""
import fs from 'node:fs'

const dataUrl = source =>
  `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const sdkUrl = dataUrl(`
  export const ROUTES_AREA = 'app.routes'
  export const SIDEBAR_NAV_AREA = 'app.sidebar.nav'
  export const Button = 'Button'
  export const EmptyState = 'EmptyState'
  export const ErrorState = 'ErrorState'
  export const Loader = 'Loader'
  export const StatusDot = 'StatusDot'
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
  export const EmptyState = 'EmptyState'
  export const ErrorState = 'ErrorState'
  export const Loader = 'Loader'
  export const StatusDot = 'StatusDot'
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
const oversizedPositions = mod.sanitizeFleetPositions(Object.fromEntries(
  Array.from({ length: 257 }, (_, index) => [`node-${index}`, { x: index, y: index }])
))
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
    evictedLast: movedIntoFullLayout['node-255'] ?? null
  },
  minimumZoom,
  manyCount: many.nodes.length,
  manyUnique: new Set(many.nodes.map(item => `${item.x}:${item.y}`)).size,
  manyFinite: many.nodes.every(
    item => Number.isFinite(item.x) && Number.isFinite(item.y)
  ),
  duplicateIds: duplicateGraph.nodes.map(item => item.id),
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
            ["node-c", 0, 150],
        ],
        "edges": [],
        "filtered": ["node-b"],
        "attention": ["node-b"],
        "fit": {"x": 40, "y": 68.222, "scale": 1.778},
        "zoomed": {"x": -80, "y": -40, "scale": 2},
        "panned": {"x": 50, "y": 0, "scale": 2},
        "moved": {
            "node-a": {"x": 24, "y": -24},
            "node-b": {"x": 20, "y": 30},
        },
        "movedIntoFullLayout": {
            "count": 256,
            "newPosition": {"x": 24, "y": -24},
            "retainedFirst": {"x": 0, "y": 0},
            "evictedLast": None,
        },
        "minimumZoom": {"x": 0, "y": 0, "scale": 0.5},
        "manyCount": 256,
        "manyUnique": 256,
        "manyFinite": True,
        "duplicateIds": ["node-unique"],
        "oversizedPositionCount": 0,
    }
