import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "desktop" / "plugin.js"
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
    assert "Loader" in source
    assert "EmptyState" in source
    assert "ErrorState" in source
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|\brgb\(", source)
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
  export const jsx = (type, props, key) => ({ type, props, key })
  export const jsxs = jsx
`)

let source = fs.readFileSync(process.argv[1], 'utf8')
source = source.replaceAll("'@hermes/plugin-sdk'", `'${sdkUrl}'`)
source = source.replaceAll("'react/jsx-runtime'", `'${reactUrl}'`)
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
