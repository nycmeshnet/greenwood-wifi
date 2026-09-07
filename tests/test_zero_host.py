"""Regression tests for the zero-host path (Actions + viewer + tunable CLI).

Catches the class of bug that broke run 34071179945: workflow ARGS
construction with literal single-quotes around ${{ inputs.* }} values,
which argparse then received as "'2000'" instead of "2000".

Stdlib only — no extra deps so CI needs no requirements change.
"""

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN_YML = ROOT / ".github" / "workflows" / "run.yml"
VIEWER = ROOT / "viewer" / "index.html"

# Workflow inputs that intentionally have no direct --<name> CLI flag.
NON_CLI_INPUTS = {"publish", "refresh-cache"}

# Workflow input name -> CLI flag (most are 1:1; heights use -m suffix).
INPUT_TO_FLAG = {
    "mount-height": "--mount-height-m",
    "client-height": "--client-height-m",
}


def _read_run_yml() -> str:
    assert RUN_YML.exists(), f"missing {RUN_YML}"
    return RUN_YML.read_text()


def _parser():
    from main import build_parser
    return build_parser()


def test_run_yml_exists_and_has_dispatch():
    text = _read_run_yml()
    assert "workflow_dispatch" in text
    assert "inputs:" in text


def test_no_quoted_inputs_bug():
    """ARGS must not wrap ${{ inputs.* }} in single quotes.

    Bug pattern: ARGS="--range-2g '${{ inputs.range-2g }}'"
    expands to literal '2000' (with quotes) after word-splitting,
    failing float() parsing. Correct: no inner single quotes.
    """
    text = _read_run_yml()
    bad = re.findall(r"'?\$\{\{\s*inputs\.[^}]+\}\}'?", text)
    quoted = [m for m in bad if m.startswith("'") or m.endswith("'")]
    assert not quoted, f"quoted inputs would break argparse: {quoted}"


def test_run_step_omits_empty_range_6g():
    text = _read_run_yml()
    assert 'inputs.range-6g' in text
    # Must guard the optional 6 GHz flag so empty input doesn't pass "".
    assert re.search(r'if\s*\[\s*-n\s*"\$\{\{\s*inputs\.range-6g\s*\}\}"', text), (
        "expected a non-empty guard for the optional range-6g input"
    )


def test_workflow_inputs_match_cli_flags():
    """Every workflow input (except publish) must map to a main.py flag."""
    text = _read_run_yml()
    parser = _parser()
    cli_opts = set(parser._option_string_actions.keys())
    defined = re.findall(r"^ {6}([a-z0-9][a-z0-9-]*):\s*$", text, re.M)
    assert defined, "no inputs found in run.yml"
    for name in defined:
        if name in NON_CLI_INPUTS:
            continue
        flag = INPUT_TO_FLAG.get(name, f"--{name}")
        assert flag in cli_opts, (
            f"workflow input '{name}' has no {flag} flag in main.build_parser()"
        )


def test_workflow_referenced_inputs_are_defined():
    """Every ${{ inputs.* }} used in the shell must be a defined input."""
    text = _read_run_yml()
    defined = set(re.findall(r"^ {6}([a-z0-9][a-z0-9-]*):\s*$", text, re.M))
    used = set(re.findall(r"\$\{\{\s*inputs\.([a-z0-9-]+)\s*\}\}", text))
    assert used <= defined, f"undefined inputs referenced: {used - defined}"


def test_baseline_cache_committed():
    """data/cache/ baseline must be tracked so Actions runs offline."""
    from subprocess import run, PIPE
    r = run(["git", "ls-files", "data/cache"], capture_output=True, text=True, cwd=ROOT)
    tracked = [l for l in r.stdout.splitlines() if l.strip()]
    assert len(tracked) >= 5, (
        f"expected baseline cache committed (boundary/trees/buildings/paths/elevation/solar), got {tracked}. "
        "Run: git add -f data/cache && commit, else Actions fails when Overpass is down."
    )


def test_cli_defaults_parse_cleanly():
    """Bare parse_args([]) must succeed and give numeric defaults."""
    parser = _parser()
    args = parser.parse_args([])
    for attr in ("range_2g", "range_5g", "panel_w", "load_w", "derating"):
        assert isinstance(getattr(args, attr), float), attr
    assert args.ap_height == 2.0 and args.rx_height == 2.0
    # Deprecated aliases still work.
    aliased = parser.parse_args(["--ap-height", "1.5", "--rx-height", "1.0"])
    assert aliased.ap_height == 1.5 and aliased.rx_height == 1.0
    # Quoted strings like "'2000'" must fail — guards the original bug shape.
    with_args = ["--range-2g", "'2000'"]
    try:
        parser.parse_args(with_args)
    except SystemExit:
        pass  # argparse exits(2) on invalid float — expected
    else:
        raise AssertionError("parser accepted quoted float \"'2000'\"; it must reject it")


def test_shade_radius_auto():
    """shade-radius 0 means auto: max(30, 3x tree height)."""
    from main import apply_overrides
    import constants
    base = constants.SHADE_RADIUS_M
    args = _parser().parse_args(["--shade-radius", "0"])
    try:
        eff = apply_overrides(args)
        assert eff["shade_radius_m"] == max(30.0, 3.0 * 15.0)
    finally:
        constants.SHADE_RADIUS_M = base


def test_apply_overrides_patches_and_restores():
    """apply_overrides must propagate CLI values into consumer modules."""
    import constants
    import data.osm as osm_module
    import models.power as power_module
    import models.terrain as terrain_module
    import optimize.placer as placer_module
    from main import apply_overrides

    saved = {
        "constants_panel": constants.PANEL_RATED_W,
        "osm_rel": osm_module.CEMETERY_RELATION_ID,
        "power_panel": power_module.PANEL_RATED_W,
        "terrain_ap": terrain_module.AP_HEIGHT_M,
        "placer_pen": placer_module.MARGINAL_PENALTY,
    }
    args = argparse.Namespace(
        relation_id=999, panel_w=120.0, load_w=8.0, derating=0.9, tilt=35.0,
        freeze_thresh_f=32.0, candidate_spacing=25.0, ilp_spacing=25.0,
        test_spacing=12.0, ap_height=2.0, rx_height=1.0, shade_radius=60.0,
        marginal_penalty=2.0,
    )
    try:
        eff = apply_overrides(args)
        assert eff["panel_w"] == 120.0
        assert constants.PANEL_RATED_W == 120.0
        assert osm_module.CEMETERY_RELATION_ID == 999
        assert power_module.PANEL_RATED_W == 120.0
        assert power_module.DEMAND_WH_PER_DAY == 8.0 * 24.0
        assert terrain_module.AP_HEIGHT_M == 2.0
        assert placer_module.MARGINAL_PENALTY == 2.0
    finally:
        constants.PANEL_RATED_W = saved["constants_panel"]
        constants.DEMAND_WH_PER_DAY = 10.0 * 24.0
        osm_module.CEMETERY_RELATION_ID = saved["osm_rel"]
        power_module.PANEL_RATED_W = saved["power_panel"]
        power_module.DEMAND_WH_PER_DAY = 10.0 * 24.0
        terrain_module.AP_HEIGHT_M = saved["terrain_ap"]
        placer_module.MARGINAL_PENALTY = saved["placer_pen"]


def test_viewer_exists_and_loads_json():
    assert VIEWER.exists(), f"missing {VIEWER}"
    html = VIEWER.read_text()
    assert "leaflet" in html.lower()
    assert "ap_placement.json" in html
    assert "earth.google.com" in html  # documents the Earth drag-drop path


def test_stale_cache_triggers_refresh_and_autocommit():
    """Cache older than 90 days must flip --no-cache on and push back.

    Lazy refresh: no cron, no PR. The next dispatch after staleness
    re-fetches live and commits data/cache/ to the same branch.
    """
    text = _read_run_yml()
    assert 'id: cache-age' in text
    assert '90' in text  # staleness threshold in days
    assert 'steps.cache-age.outputs.stale' in text
    assert 'Commit refreshed cache' in text
    assert 'git push origin HEAD:${{ github.ref_name }}' in text
