"""Write AP placement JSON, KML (Google Earth), and summary report (US units)."""

import json
import os
from datetime import datetime

import numpy as np
import simplekml

from constants import FT_PER_M


_STATUS_COLOR = {
    "viable":   "ff00aa00",   # green  (AABBGGRR in KML)
    "marginal": "ff00aaff",   # yellow
    "unviable": "ff0000ff",   # red
}


def write_kml(aps: list[dict], path: str = "output/ap_placement.kml") -> None:
    """
    Write a KML file suitable for Google Earth.

    Each AP is a labelled Placemark with a colour-coded pin:
        green  — solar viable
        yellow — solar marginal (flagged)
        red    — solar unviable (should not appear; filtered before optimisation)

    The description balloon shows solar status, harvest, shade, and band info.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    kml = simplekml.Kml(name="Green-Wood Cemetery WiFi APs")

    for ap in aps:
        pnt = kml.newpoint(name=ap["name"], coords=[(ap["lon"], ap["lat"])])

        status = ap.get("solar_status", "viable")
        color = _STATUS_COLOR.get(status, "ff00aa00")
        pnt.style.iconstyle.color = color
        pnt.style.iconstyle.scale = 1.2
        pnt.style.labelstyle.scale = 0.9

        bands_str = ", ".join(ap.get("bands", []))
        facing = ap.get("panel_facing", "south")
        azimuth = ap.get("panel_azimuth_deg", 180)
        tilt = ap.get("panel_tilt_deg", 40)
        pnt.description = (
            f"<![CDATA["
            f"<b>{ap['name']}</b><br/>"
            f"Bands: {bands_str}<br/>"
            f"Solar: {status}<br/>"
            f"Daily harvest: {ap.get('harvest_wh', '?'):.0f} Wh "
            f"(demand 240 Wh)<br/>"
            f"Shade: {ap.get('shade_pct', '?'):.0f}%<br/>"
            f"Panel: {facing} ({azimuth}°), tilt {tilt}°<br/>"
            f"Coverage efficiency: {ap.get('coverage_efficiency_pct', '?'):.0f}%<br/>"
            f"Overlap with neighbours: {ap.get('overlap_pct', '?'):.0f}%"
            f"]]>"
        )

    kml.save(path)
    print(f"Written: {path}  ({len(aps)} placemarks)")


def write_placement_json(aps: list[dict], path: str = "output/ap_placement.json") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(aps, f, indent=2)
    print(f"Written: {path}  ({len(aps)} APs)")


def write_summary(
    aps: list[dict],
    coverage_matrix: np.ndarray,
    n_test_points: int,
    rated_range_ft: float,
    path: str = "output/summary.md",
    shoulder_months_list: list[str] | None = None,
) -> None:
    """
    Write a Markdown summary.

    aps             : list of AP dicts produced by main.py (already US-unit annotated)
    coverage_matrix : bool[n_selected, n_test_points]
    n_test_points   : total test points (covered + uncovered)
    rated_range_ft  : primary (2.4 GHz) rated range in feet, for area comparisons
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)

    total_covered = int(coverage_matrix.any(axis=0).sum()) if coverage_matrix.size else 0
    uncovered = n_test_points - total_covered
    coverage_pct = 100.0 * total_covered / n_test_points if n_test_points else 0.0

    marginal = [ap for ap in aps if ap.get("solar_status") == "marginal"]

    # APs that cover a small fraction of their theoretical rated-range circle —
    # heavy tree attenuation means a cheaper short-range AP would do the same job.
    underutilized = [ap for ap in aps if ap.get("coverage_efficiency_pct", 100) < 30]

    # APs where most of their covered area is also covered by a neighbour.
    high_overlap = [ap for ap in aps if ap.get("overlap_pct", 0) > 50]

    shoulder_months_list = shoulder_months_list or []

    lines = [
        "# AP Placement Summary — Green-Wood Cemetery",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
    ]

    if shoulder_months_list:
        lines += [
            "## ⚠ Shoulder Month Notice",
            f"**{', '.join(shoulder_months_list)}** have insufficient sunlight for 24/7 "
            "operation even at fully-exposed locations (100 W panel, 10 W load). "
            "The system will operate during daylight and early evening only in these "
            "months. All other above-freezing months are fully viable.",
            "",
        ]

    # Deduplicate panel orientation across all APs (usually identical for every AP)
    facings = sorted({ap.get("panel_facing", "south") for ap in aps})
    azimuths = sorted({ap.get("panel_azimuth_deg", 180) for ap in aps})
    tilts = sorted({ap.get("panel_tilt_deg", 40) for ap in aps})

    lines += [
        "## Panel Orientation",
        f"- **Facing:** {', '.join(str(f) for f in facings)}",
        f"- **Azimuth:** {', '.join(str(a) + '°' for a in azimuths)} "
        "(0° = north, 90° = east, 180° = south, 270° = west)",
        f"- **Tilt:** {', '.join(str(t) + '°' for t in tilts)} from horizontal",
        "",
        "## Coverage",
        f"- **Total APs placed:** {len(aps)}",
        f"- **Test points covered:** {total_covered:,} / {n_test_points:,} "
        f"({coverage_pct:.1f} %)",
        f"- **Uncovered points:** {uncovered:,}",
        "",
        "## Solar-Marginal APs",
    ]

    if marginal:
        lines.append(
            "The design-month solar harvest at these locations is within 20 % of the "
            "240 Wh/day demand (10 W × 24 h). The optimiser already preferred other "
            "candidates where possible; these were selected because no better-sited AP "
            "could cover their zone. Consider relocating to a less shaded spot."
        )
        lines.append("")
        for ap in marginal:
            shade = ap.get("shade_pct", "?")
            harvest = ap.get("harvest_wh", "?")
            lines.append(
                f"- **{ap['name']}**  —  harvest {harvest:.0f} Wh/day, "
                f"shade {shade:.0f} %, lat {ap['lat']}, lon {ap['lon']}"
            )
    else:
        lines.append("_None — all APs have adequate solar exposure._")

    lines += ["", "## Potentially Downgrade-able APs"]
    if underutilized:
        lines.append(
            "These APs cover less than 30 % of the area their rated range implies. "
            "Heavy tree attenuation is the usual cause. A cheaper AP with a shorter "
            "rated range would deliver the same real-world coverage at lower cost."
        )
        lines.append("")
        for ap in underutilized:
            lines.append(
                f"- **{ap['name']}**  —  covers only "
                f"{ap.get('coverage_efficiency_pct', '?'):.0f} % of rated-range area"
            )
    else:
        lines.append("_None — all APs are utilising their rated range effectively._")

    lines += ["", "## High-Overlap APs"]
    if high_overlap:
        lines.append(
            "More than 50 % of these APs' coverage area is also served by an adjacent AP. "
            "If cost reduction matters more than redundancy, these are the first candidates "
            "to remove and re-run the optimiser."
        )
        lines.append("")
        for ap in high_overlap:
            lines.append(
                f"- **{ap['name']}**  —  {ap.get('overlap_pct', '?'):.0f} % overlap "
                f"with neighbours"
            )
    else:
        lines.append("_None — coverage is well distributed with minimal overlap._")

    lines += ["", "## AP List"]
    lines.append(
        "| Name | Lat | Lon | Solar | Harvest (Wh/day) | Shade % | Efficiency % | Overlap % |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    for ap in aps:
        lines.append(
            f"| {ap['name']} "
            f"| {ap['lat']} "
            f"| {ap['lon']} "
            f"| {ap.get('solar_status', '?')} "
            f"| {ap.get('harvest_wh', '?'):.0f} "
            f"| {ap.get('shade_pct', '?'):.0f} "
            f"| {ap.get('coverage_efficiency_pct', '?'):.0f} "
            f"| {ap.get('overlap_pct', '?'):.0f} |"
        )

    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")

    print(f"Written: {path}")
