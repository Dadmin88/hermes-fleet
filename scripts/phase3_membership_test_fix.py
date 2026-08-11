from pathlib import Path

TESTS = Path(__file__).resolve().parents[1] / "tests" / "unit" / "test_desktop_plugin_assets.py"


def replace_once(old: str, new: str) -> None:
    text = TESTS.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise RuntimeError(f"expected exactly one test fixture anchor: {old[:72]!r}")
    TESTS.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "  attention: mod.filterFleetMembershipRows(model.rows, '', 'attention').map(row => row.label),",
    """  attention: mod.filterFleetMembershipRows(
    model.rows, '', 'attention'
  ).map(row => row.label),""",
)
replace_once(
    "  inactive: mod.filterFleetMembershipRows(model.rows, '', 'not_active').map(row => row.label),",
    """  inactive: mod.filterFleetMembershipRows(
    model.rows, '', 'not_active'
  ).map(row => row.label),""",
)
replace_once(
    "  observed: mod.filterFleetMembershipRows(model.rows, '', 'observed').map(row => row.label),",
    """  observed: mod.filterFleetMembershipRows(
    model.rows, '', 'observed'
  ).map(row => row.label),""",
)
replace_once(
    "  search: mod.filterFleetMembershipRows(model.rows, 'compute-b', 'all').map(row => row.label),",
    """  search: mod.filterFleetMembershipRows(
    model.rows, 'compute-b', 'all'
  ).map(row => row.label),""",
)
