"""Generate the Hello World project and validate it with kicad-cli.

Pipeline: schematic -> ERC -> netlist (kicad-cli) -> board from the netlist -> DRC with
schematic parity and zone refill -> 3D render. Every step's verdict is printed; the
pipeline stops at the first failure so nothing half-broken is left behind unnoticed.

Usage: python examples/hello_world/build.py <project_dir> [project_name]
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from design import assign_refs, build_board, build_schematic, letters  # noqa: E402

from kicad_layer.cli import netlist as netlist_mod  # noqa: E402
from kicad_layer.cli import reports, runner  # noqa: E402
from kicad_layer.cli.discovery import find_kicad_cli  # noqa: E402
from kicad_layer.config import load_settings, set_settings  # noqa: E402

TEMPLATE_PRO = HERE.parent.parent / "tests" / "fixtures" / "pic_programmer" / "pic_programmer.kicad_pro"
TEMPLATE_PCB = HERE.parent.parent / "tests" / "fixtures" / "pic_programmer" / "pic_programmer.kicad_pcb"


def write_project_file(path: Path, name: str, root_uuid: str) -> None:
    pro = json.loads(TEMPLATE_PRO.read_text(encoding="utf-8"))
    pro["meta"] = {"filename": f"{name}.kicad_pro", "version": 3}
    pro["sheets"] = [[root_uuid, "Root"]]
    pro["text_variables"] = {}
    pro["libraries"] = {"pinned_footprint_libs": [], "pinned_symbol_libs": []}
    pro["legacy"] = {"version": 3}
    pro["cvpcb"] = {"equivalence_files": []}
    rules = pro["board"]["design_settings"]["rules"]
    rules.update({
        "min_clearance": 0.2, "min_track_width": 0.2, "min_via_diameter": 0.6,
        "min_via_annular_width": 0.13, "min_through_hole_diameter": 0.3,
        "min_copper_edge_clearance": 0.3, "min_hole_to_hole": 0.25, "min_silk_clearance": 0.0,
    })
    cls = pro["net_settings"]["classes"][0]
    cls.update({"clearance": 0.2, "track_width": 0.25, "via_diameter": 0.8, "via_drill": 0.3,
                "diff_pair_width": 0.2, "diff_pair_gap": 0.2, "diff_pair_via_gap": 0.25})
    pro["net_settings"]["classes"] = [cls]
    pro.get("schematic", {})["drawing"] = pro.get("schematic", {}).get("drawing", {})
    path.write_text(json.dumps(pro, indent=2), encoding="utf-8")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    project_dir = Path(argv[1]).resolve()
    name = argv[2] if len(argv) > 2 else project_dir.name
    project_dir.mkdir(parents=True, exist_ok=True)
    set_settings(load_settings({"KICAD_LAYER_WORKSPACE": str(project_dir.parent)}))
    cli = find_kicad_cli()
    print(f"kicad-cli {cli.version} at {cli.path}")

    sch_path = project_dir / f"{name}.kicad_sch"
    pcb_path = project_dir / f"{name}.kicad_pcb"
    pro_path = project_dir / f"{name}.kicad_pro"

    # keep whatever was there
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = project_dir / f"_backup-{stamp}"
    for p in (sch_path, pcb_path, pro_path):
        if p.exists() and p.stat().st_size > 0:
            backup.mkdir(exist_ok=True)
            shutil.copy2(p, backup / p.name)
    if backup.exists():
        print(f"previous files backed up to {backup}")

    ls = letters()
    assign_refs(ls)
    t0 = time.time()
    uuids = build_schematic(sch_path, name, ls)
    # the schematic builder made the root uuid; read it back for the project file
    root_uuid = sch_path.read_text(encoding="utf-8").split('(uuid "', 1)[1].split('"', 1)[0]
    write_project_file(pro_path, name, root_uuid)
    print(f"schematic written: {sch_path.name} ({len(uuids)} symbols) in {time.time() - t0:.1f}s")

    erc = reports.run_erc(sch_path, severity="all")
    print(f"ERC: {erc.verdict} {erc.counts}")
    for f in erc.findings[:25]:
        print(f"   {f.severity:7s} {f.type:28s} {f.description}  |  " + "; ".join(i.description for i in f.items[:2]))
    if erc.verdict in ("FAIL", "BLOCKED", "UNVERIFIED"):
        print("stopping: ERC did not pass")
        return 1

    xml_path, _, _ = netlist_mod.export_netlist(sch_path, refresh=True)
    net = netlist_mod.load_netlist(sch_path, refresh=False, include_components=True, max_nets=5000)
    print(f"netlist: {net.component_count} components, {net.net_count} nets")

    t1 = time.time()
    stats = build_board(pcb_path, sch_path.name, ls, net, uuids, TEMPLATE_PCB)
    print(f"board written: {pcb_path.name} ({stats}) in {time.time() - t1:.1f}s")

    # let KiCad fill the zones and save them into the file, then judge
    fill = runner.run([cli.path, "pcb", "drc", "--refill-zones", "--save-board", "--format", "json", "--severity-all",
                       "--schematic-parity", "-o", str(project_dir / "_drc.json"), str(pcb_path)], timeout_s=600, cwd=project_dir)
    print(f"zone refill + DRC exit {fill.returncode}")
    drc = reports.run_drc(pcb_path, severity="all", schematic_parity=True)
    print(f"DRC: {drc.verdict} {drc.counts}")
    by_type: dict[str, int] = {}
    for f in drc.findings:
        by_type[f"{f.severity}:{f.type}"] = by_type.get(f"{f.severity}:{f.type}", 0) + 1
    for k, v in sorted(by_type.items(), key=lambda kv: -kv[1]):
        print(f"   {v:4d}  {k}")
    for f in [x for x in drc.findings if x.severity == "error"][:30]:
        print(f"   ERROR {f.type}: {f.description} | " + " ; ".join(f"{i.description} @({i.x_mm},{i.y_mm})" for i in f.items[:2]))

    render = project_dir / f"{name}-top.png"
    r = runner.run([cli.path, "pcb", "render", "-o", str(render), "-w", "2400", "--height", "900", "--side", "top", "--quality", "high", str(pcb_path)], timeout_s=600, cwd=project_dir)
    print(f"render exit {r.returncode}: {render if render.exists() else r.tail()}")
    return 0 if drc.verdict in ("PASS", "WARN") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
