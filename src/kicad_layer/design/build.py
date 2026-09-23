"""Generate a project and validate it with kicad-cli.

Pipeline: sheets -> project file -> ERC -> netlist -> board -> DRC (zones refilled, schematic
parity) -> render. Any ERC error stops it; DRC's unconnected items are reported (a board before
routing has them), any other DRC error fails the build. A project without a board builder stops
after the netlist. While KiCad holds the project (lock
files), the build goes to ``_staging`` next to the project and ``--promote`` copies it in once
KiCad has closed.

    build.py [--out DIR | --force] [--reopen] [--no-routes] [--sch-only] [--no-render] [--review] [--route-stubs] [--offline]
    build.py --promote [--wait] | --status | --close | --open | --preview [SHEET ...] | --blocks | --seed

``--sch-only`` stops after the netlist and its gates (seconds, no board); ``--no-render`` skips the
board render; ``--preview`` writes one PNG per named sheet (or every sheet) into ``_preview``;
``--review`` compares the netlist with the project's ``Review`` reference pad for pad after the gates,
prints the pins that differ and writes the full report into ``review/``; ``--route-stubs`` routes what
the DRC leaves open on single-ended nets over the copper model (``design/stubs.py``), saves the copper
into ``routing/routes.json`` and builds once more. Placements with ``fit`` or ``near`` are resolved by
the board build itself and recorded in ``routing/placed.json``.

``--offline`` builds without kicad-cli, and the build goes offline by itself when kicad-cli is not
found: the sheets, the lint, the ``.kicad_pro``/``.kicad_dru`` and the board are written, the board
from a netlist synthesized from the sheets' descriptions (``design/offline.py``); ERC, the netlist
gates, DRC, ``--route-stubs``, ``--review`` and the render are skipped and reported UNVERIFIED.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from kicad_layer.cli import netlist as netlist_mod
from kicad_layer.cli import reports, runner
from kicad_layer.cli.discovery import find_kicad_cli
from kicad_layer.config import load_settings, set_settings
from kicad_layer.errors import KICAD_CLI_NOT_FOUND, LayerError

from . import compare as compare_mod
from . import gui, netcompare, offline, parts, verify
from .lint import lint
from .render import Described
from .project import Project, write_project_file
from .root import build_design


def promote(project: Project, wait: bool = False) -> int:
    """Copy a staged build into the project once KiCad has released it."""
    P, STAGING = project.dir, project.staging
    if not STAGING.exists() or not any(STAGING.glob("*.kicad_pcb")):
        print(f"nothing staged in {STAGING}")
        return 1
    locks = gui.lock_files(P)
    if locks and wait:
        print(f"waiting for KiCad to close ({', '.join(p.name for p in locks)}) ...", flush=True)
        deadline = time.time() + 12 * 3600
        while locks and time.time() < deadline:
            time.sleep(15)
            locks = gui.lock_files(P)
    if locks:
        print(f"KiCad still holds {', '.join(p.name for p in locks)}; not promoting")
        return 2
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = P / f"_backup-{stamp}"
    files = [p for pattern in project.design_globs for p in STAGING.glob(pattern)] + list(STAGING.glob("*-top.png")) + [STAGING / "_drc.json"]
    for p in files:
        if not p.exists():
            continue
        cur = P / p.name
        if cur.exists() and p.suffix in (".kicad_sch", ".kicad_pcb", ".kicad_pro") and cur.stat().st_size > 300:
            backup.mkdir(exist_ok=True)
            shutil.copy2(cur, backup / cur.name)
        shutil.copy2(p, cur)
    staged_sheets = {p.name for p in STAGING.glob("*.kicad_sch")}
    stale = [p.name for p in P.glob("*.kicad_sch") if p.name not in staged_sheets]
    if stale:
        print(f"note: sheets in the project that the build no longer produces: {', '.join(stale)}")
    shutil.rmtree(STAGING)
    print(f"promoted {sum(1 for p in files if (P / p.name).exists())} files into {P}" + (f"; previous files in {backup}" if backup.exists() else ""))
    return 0


def _matches_descriptions(project: Project, xml_path: Path) -> bool:
    """Every described sheet must connect its parts as its circuit (or module data) says: the netlist read back, pin group for pin group."""
    ok = True
    for name, builder in project.sheets().items():
        circuit = getattr(builder, "circuit", None)
        module = getattr(builder, "module", None)
        if circuit is not None:
            diffs = verify.compare(verify.circuit_groups(circuit), verify.netlist_groups(xml_path, set(circuit.parts)))
        elif module is not None:
            diffs = verify.compare(verify.module_groups(module), verify.netlist_groups(xml_path, verify.module_refs(module)))
        else:
            continue
        if diffs:
            ok = False
            print(f"description: {name} DIFFERS ({len(diffs)} groups)")
            for line in diffs[:12]:
                print(f"   {line}")
        else:
            print(f"description: {name} matches")
    return ok


def _matches_reference(project: Project, xml_path: Path, symbol_paths: dict[str, tuple[str, str, str]]) -> bool:
    """Every described sheet must group its parts' pins as the reference netlist does, net names aside."""
    ref, new = netcompare.nodes(project.reference_netlist), netcompare.nodes(xml_path)
    ref_sheets = netcompare.sheet_refs(project.reference_netlist)
    ok = True
    for sheet in project.sheets():
        refs = {r for r, (_, path, _) in symbol_paths.items() if path == f"/{sheet}/"} | ref_sheets.get(f"/{sheet}/", set())
        only_ref, only_new = netcompare.diff(ref, new, refs)
        if only_ref or only_new:
            ok = False
            print(f"reference: {sheet} DIFFERS ({len(only_ref)} groups only in the reference, {len(only_new)} only here)")
            for line in only_ref[:12]:
                print(f"   reference only: {line}")
            for line in only_new[:12]:
                print(f"   here only:      {line}")
        else:
            print(f"reference: {sheet} matches ({len(refs)} parts)")
    return ok


def symbol_paths(root, children: dict, root_file: str) -> dict[str, tuple[str, str, str]]:
    """Instance path, sheet name and sheet file for every placed symbol, as the board needs them.

    A symbol with several units takes the path of its lowest unit, as KiCad does when it updates the
    board from the schematic; ``root`` and ``children`` are what ``build_design`` returns.
    """
    found: dict[str, tuple[int, tuple[str, str, str]]] = {}

    def keep(ref: str, unit: int, entry: tuple[str, str, str]) -> None:
        if ref not in found or unit < found[ref][0]:
            found[ref] = (unit, entry)

    for sheet in root.sheets:
        cb = children[sheet.name]
        for p in cb.placed:
            keep(p.ref, p.unit, (f"{cb.path}/{p.uuid}", f"/{sheet.name}/", sheet.file))
    for p in root.placed:
        keep(p.ref, p.unit, (f"{root.path}/{p.uuid}", "/", root_file))
    return {ref: entry for ref, (_, entry) in found.items()}


def main(project: Project, argv: list[str]) -> int:
    """The build pipeline for ``project`` with the flags in this module's docstring; returns the exit code."""
    P = project.dir
    if "--status" in argv:
        return gui.status(P)
    if "--promote" in argv:
        return promote(project, wait="--wait" in argv)
    if "--close" in argv:
        return 0 if gui.close_kicad(P) else 1
    if "--open" in argv:
        return 0 if gui.open_kicad(project.gui_task) else 1
    if "--blocks" in argv:
        from . import blocks as blocks_mod

        locks = [p for p in gui.lock_files(P) if p.name.endswith(".kicad_pcb.lck")]  # only the board matters here
        if locks:
            print(f"KiCad holds {', '.join(p.name for p in locks)}: close the PCB editor, then run --blocks")
            return 1
        board = P / f"{project.name}.kicad_pcb"
        if not project.blocks:
            print("no blocks declared on this project")
            return 0
        for line in blocks_mod.apply(board, project.blocks):
            print(line)
        return 0
    if "--preview" in argv:
        from .preview import preview

        names = [a for a in argv[argv.index("--preview") + 1:] if not a.startswith("--")]
        for png in preview(project, names or None):
            print(png)
        return 0
    sch_only = "--sch-only" in argv
    no_render = "--no-render" in argv or sch_only
    reopen = "--reopen" in argv
    if reopen and not gui.close_kicad(P):
        return 1
    out = P
    staged = False
    if "--out" in argv:
        out = Path(argv[argv.index("--out") + 1]).resolve()
        out.mkdir(parents=True, exist_ok=True)
    else:
        locks = gui.lock_files(P)
        if locks and "--force" not in argv:
            out = project.staging
            out.mkdir(parents=True, exist_ok=True)
            staged = True
            print(f"KiCad holds {', '.join(p.name for p in locks)}: building into {out}")
    workspace = P.parent if str(out).startswith(str(P.parent)) else out.parent
    set_settings(load_settings({"KICAD_LAYER_WORKSPACE": str(workspace)}))
    cli = None
    offline_why = "--offline" if "--offline" in argv else ""
    if not offline_why:
        try:
            cli = find_kicad_cli()
            print(f"kicad-cli {cli.version} at {cli.path}")
        except LayerError as ex:
            if ex.code != KICAD_CLI_NOT_FOUND:
                raise
            offline_why = f"kicad-cli not found ({ex})"
    if offline_why:
        print(f"WARNING offline build ({offline_why}): ERC, KiCad's netlist and its gates, DRC and the render are SKIPPED (UNVERIFIED); "
              "the board, if any, is built on a netlist synthesized from the descriptions")
    if project.check_signals is not None:
        project.check_signals()
    project.register_libraries()  # regenerates the project's own library first, so an --out copy below is current
    if out != P:  # the project's own library tables and library, where it has them
        for f in ("sym-lib-table", "fp-lib-table"):
            if (P / f).is_file():
                shutil.copy2(P / f, out / f)
        if project.lib_dir.is_dir():
            if (out / "lib").exists():
                shutil.rmtree(out / "lib")
            shutil.copytree(project.lib_dir, out / "lib")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = out / f"_backup-{stamp}"
    for p in list(out.glob("*.kicad_sch")) + list(out.glob("*.kicad_pcb")) + list(out.glob("*.kicad_pro")):
        if p.stat().st_size > 300:
            backup.mkdir(exist_ok=True)
            shutil.copy2(p, backup / p.name)
    if backup.exists():
        print(f"previous files backed up to {backup}")

    t0 = time.time()
    root, children = build_design(project)
    builders = project.sheets()
    geometry: list[str] = []
    for name, cb in children.items():
        b = builders.get(name)
        if isinstance(b, Described) and not b.layout.placed:
            continue  # a plain sheet: rows of anchors, a stub and a label per pin, nothing KiCad could misread
        errors, warnings = lint(cb)
        geometry += [f"{name}: {e}" for e in errors]
        for w in warnings:
            print(f"   note {name}: {w}")
    if geometry:
        print("geometry:\n   " + "\n   ".join(geometry))
        print("stopping: the drawing has geometry KiCad would misread")
        return 1
    no_part = parts.verify_all([root, *children.values()])  # every BOM symbol placed from the catalogue carries its part
    print("parts: every BOM symbol carries Manufacturer/MPN" + (f"; NO PART NUMBER for {', '.join(no_part)}" if no_part else ""))
    root.write(str(out / f"{project.name}.kicad_sch"))
    for sheet in root.sheets:
        children[sheet.name].write(str(out / sheet.file))
    write_project_file(project, out / f"{project.name}.kicad_pro", root.sheet_list(children))
    n_sym = sum(len(b.placed) for b in [root, *children.values()])
    print(f"schematic written: root + {len(root.sheets)} sheets, {n_sym} symbols, in {time.time() - t0:.1f}s")

    root_sch = out / f"{project.name}.kicad_sch"
    if offline_why:
        return _offline_board(project, argv, out, root, children, root_sch)
    erc = reports.run_erc(root_sch, severity="all")
    print(f"ERC: {erc.verdict} {erc.counts}")
    for f in erc.findings[:40]:
        print(f"   {f.severity:7s} {f.type:28s} {f.description}  |  " + "; ".join(i.description for i in f.items[:2]) + f"  @ {f.sheet}")
    erc_ok = erc.verdict not in ("FAIL", "BLOCKED", "UNVERIFIED")
    xml_path, _, _ = netlist_mod.export_netlist(root_sch, refresh=True)  # exported even after an ERC failure: the reference gate reports either way
    net = netlist_mod.load_netlist(root_sch, refresh=False, include_components=True, max_nets=5000)
    print(f"netlist: {net.component_count} components, {net.net_count} nets")
    paths = symbol_paths(root, children, root_sch.name)

    described_ok = _matches_descriptions(project, xml_path)
    reference_ok = project.reference_netlist is None or _matches_reference(project, xml_path, paths)
    if not erc_ok:
        print("stopping: ERC did not pass")
        return 1
    if not described_ok:
        print("stopping: a sheet's drawing does not connect its parts as its description says")
        return 1
    if not reference_ok:
        print("stopping: a described sheet does not connect its parts as the reference does")
        return 1
    if "--review" in argv:
        if project.review is None:
            print("review: the project has no Review reference")
            return 1
        rv = project.review
        ref_xml = rv.reference if rv.reference.suffix.lower() == ".xml" else netlist_mod.export_netlist(rv.reference, refresh=False)[0]
        ours_d, ref_d = compare_mod.load(xml_path), compare_mod.load(ref_xml)
        report = compare_mod.compare(ours_d, ref_d, rv.module, rv.reference_module, ignore=rv.ignore, ignore_reference=rv.ignore_reference)
        out_md = rv.report or (P / "review" / f"{rv.reference.stem}-pins.md")
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(report.markdown(project.name, rv.reference.stem, ours_d, ref_d), encoding="utf-8", newline='\n')
        for line in report.summary():
            print("review: " + line)
        print(f"review: report written to {out_md}")
    if sch_only:
        print("schematic only: stopping after the netlist")
        return 0
    if project.board_builder is None:
        if "--seed" in argv:
            from . import seed as seed_mod

            pcb_path = P / f"{project.name}.kicad_pcb"
            if any(p.name.endswith(".kicad_pcb.lck") for p in gui.lock_files(P)):
                print("KiCad holds the board: close the PCB editor, then run --seed")
                return 1
            try:
                for line in seed_mod.seed_board(project, pcb_path, root_sch.name, net, paths):
                    print(line)
            except ValueError as ex:
                print(f"seed refused: {ex}")
                return 1
            return 0
        print("no board builder: schematic-only build (the board is hand-made; --seed writes its first import)")
        return 0
    pcb_path = out / f"{project.name}.kicad_pcb"
    routes_path = P / "routing" / "routes.json"
    stubs_routed = 0
    for attempt in (0, 1):
        t1 = time.time()
        stats = project.board_builder(pcb_path, root_sch.name, net, paths, project.setup_template, with_routes="--no-routes" not in argv)
        fitted = stats.pop("fitted", [])
        print(f"board written: {pcb_path.name} {stats} in {time.time() - t1:.1f}s")
        for line in fitted:
            print(f"   placed: {line}")
        fill = runner.run([cli.path, "pcb", "drc", "--refill-zones", "--save-board", "--format", "json", "--severity-all", "--schematic-parity",
                           "-o", str(out / "_drc.json"), str(pcb_path)], timeout_s=600, cwd=out)
        print(f"zone refill + DRC exit {fill.returncode}")
        drc = reports.run_drc(pcb_path, severity="all", schematic_parity=True)
        by_type: dict[str, int] = {}
        for f in drc.findings:
            by_type[f"{f.severity}:{f.type}"] = by_type.get(f"{f.severity}:{f.type}", 0) + 1
        print(f"DRC: {drc.verdict} {drc.counts}")
        for k, v in sorted(by_type.items(), key=lambda kv: -kv[1]):
            print(f"   {v:4d}  {k}")
        blocking = [f for f in drc.findings if f.severity == "error" and f.type != "unconnected_items"]
        for f in blocking[:30]:
            print(f"   ERROR {f.type}: {f.description} | " + " ; ".join(f"{i.description} @({i.x_mm},{i.y_mm})" for i in f.items[:2]))
        if attempt == 1 and stubs_routed and not stats.get("routed_segments") and not stats.get("routed_vias"):
            print(f"stubs: the rebuilt board carries no saved copper; point the project's Board.routes at {routes_path} to apply it")
        if attempt == 0 and "--route-stubs" in argv and drc.counts.get("unconnected"):
            from kicad_layer import routes as routes_mod
            from kicad_layer.review import load_board

            from . import copper, stubs
            opens = stubs.open_connections(json.loads((out / "_drc.json").read_text(encoding="utf-8")))
            saved = routes_mod.load(routes_path) if routes_path.is_file() else routes_mod.Routes()  # a fresh board has no routes.json yet
            res = stubs.route_stubs(load_board(pcb_path), copper.Rules.load(out / f"{project.name}.kicad_pro"), opens, pcb_path, routes=saved)
            for line in res.lines:
                print(f"   stubs: {line}")
            if res.routed:
                routes_path.parent.mkdir(parents=True, exist_ok=True)
                routes_mod.save(res.routes, routes_path)
                stubs_routed = res.routed
                print(f"stubs: {res.routed} routed, {res.failed} failed, {res.skipped} skipped; routes.json updated, building again")
                continue
            print(f"stubs: nothing routed ({res.failed} failed, {res.skipped} skipped)")
        break
    # kicad-cli's --save-board writes CRLF on Windows; the repositories keep LF
    raw = pcb_path.read_bytes()
    if b"\r\n" in raw:
        pcb_path.write_bytes(raw.replace(b"\r\n", b"\n"))
    if not no_render:
        render = out / f"{project.name}-top.png"
        r = runner.run([cli.path, "pcb", "render", "-o", str(render), "-w", "1800", "--height", "1200", "--side", "top", "--quality", "high", str(pcb_path)],
                       timeout_s=600, cwd=out)
        print(f"render exit {r.returncode}: {render if render.exists() else r.tail()}")
    if staged:
        print(f"STAGED: the build is in {out}. Run build.py --promote when KiCad is closed, or --promote --wait to have it copied in as soon as KiCad closes.")
    elif reopen and not blocking:
        gui.open_kicad(project.gui_task)
    return 0 if not blocking else 1


def _offline_board(project: Project, argv: list[str], out: Path, root, children: dict, root_sch: Path) -> int:
    """The rest of an offline build: the descriptions' netlist and, with a board builder, the board; nothing checked."""
    print("ERC: UNVERIFIED (offline)")
    paths = symbol_paths(root, children, root_sch.name)
    net, notes = offline.synth_netlist(project.sheets(), children, root=root, source=str(root_sch))
    print(f"netlist: {net.component_count} components, {net.net_count} nets, SYNTHESIZED from the descriptions (not KiCad's; the gates are UNVERIFIED)")
    for line in notes:
        print(f"   note: {line}")
    for flag in ("--review", "--route-stubs"):
        if flag in argv:
            print(f"{flag}: skipped offline (it needs kicad-cli)")
    if "--sch-only" in argv:
        print("schematic only: stopping after the netlist")
        return 0
    if project.board_builder is None:
        if "--seed" in argv:
            print("--seed refused offline: the first import of a hand-made board takes KiCad's netlist")
            return 1
        print("no board builder: schematic-only build")
        return 0
    pcb_path = out / f"{project.name}.kicad_pcb"
    t1 = time.time()
    stats = project.board_builder(pcb_path, root_sch.name, net, paths, project.setup_template, with_routes="--no-routes" not in argv)
    fitted = stats.pop("fitted", [])
    print(f"board written: {pcb_path.name} {stats} in {time.time() - t1:.1f}s")
    for line in fitted:
        print(f"   placed: {line}")
    print("DRC: UNVERIFIED (offline; zones not filled). Build again with kicad-cli before trusting or ordering this board.")
    return 0
