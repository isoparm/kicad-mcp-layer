"""Every MCP tool, in one place.

Implementation lives in the channel modules and is testable without MCP. This module only
declares the model-facing signatures, descriptions and annotations, one registrar per group.
``GROUPS`` is the registry the drift test checks against ``docs/tools.md``; ``KICAD_LAYER_TOOLS``
picks the tier: ``core`` (checks, exports, renders, reviews, libraries, documents, board reads) or
``full`` (plus the design-edit tools and the frozen routers).
"""

from __future__ import annotations

import re

from pathlib import Path
from typing import Annotated, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Image
from mcp.types import ToolAnnotations
from pydantic import Field

from kicad_layer import capabilities as caps
from kicad_layer.config import TIERS
from kicad_layer import docs as docs_mod
from kicad_layer import easyeda as easyeda_mod
from kicad_layer import doctor as doctor_mod
from kicad_layer import jlcpcb as jlcpcb_mod
from kicad_layer import project as project_mod
from kicad_layer import libindex
from kicad_layer import pcb_tools
from kicad_layer import review as review_mod
from kicad_layer import routing as routing_mod
from kicad_layer.routers import routing_tools
from kicad_layer import sch_tools
from kicad_layer.cli import exports, netlist, reports
from kicad_layer.ipc import board_read
from kicad_layer.models import (
    LibFetch,
    AutorouteReport,
    PairRouteReport,
    StitchReport,
    AnnotateResult,
    BoardEditResult,
    EditResult,
    ReviewReport,
    SchematicComponent,
    SchematicComponents,
    BoardItems,
    BoardSummary,
    BomResult,
    CapabilityMatrix,
    DocInfo,
    DocList,
    DocSearchHit,
    DocFacts,
    DocSection,
    DocSections,
    DocText,
    DoctorReport,
    ExportResult,
    FootprintHit,
    FootprintInfo,
    FootprintPadInfo,
    LibIndexStatus,
    LibSearchResult,
    Netlist,
    NetStats,
    ProjectInfo,
    RenderResult,
    SymbolHit,
    SymbolInfo,
    SymbolPinInfo,
    TraceResult,
    VerdictReport,
    ImpedanceResult,
    PartsSearch,
    RouteReport,
    StackupInfo,
)
from kicad_layer.paths import BOARD, display, resolve_in_workspace, root_schematic_for


Fab = Annotated[Literal["jlcpcb", "jlcpcb-2l", "jlcpcb-4l"], Field(description="Whose manufacturing limits to check against. jlcpcb picks 2- or 4-layer limits from the board.")]

Channel = Annotated[Literal["auto", "ipc", "file"], Field(description="auto: live if the board is open in KiCad, else the file. A board seen live is never edited on disk.")]
EditBoardPath = Annotated[str | None, Field(description="The .kicad_pcb to edit. Omit to use the board open in KiCad.")]

DESIGN_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)
DryRun = Annotated[bool, Field(description="Report what would change without writing the file.")]
Force = Annotated[bool, Field(description="Write even if KiCad holds the sheet's lock file. Only when nothing is unsaved in the editor.")]

RoutesIn = Annotated[str | None, Field(description="Routes JSON from an earlier routing step; its copper becomes an obstacle and is kept in the output.")]
RoutesOut = Annotated[str | None, Field(description="Where to write the routes JSON; default <board>-routes.json next to the board.")]
Keepouts = Annotated[list[list[float]] | None, Field(description="Rectangles [x0, y0, x1, y1] in mm no copper may enter, e.g. a slot.")]

ProjectPath = Annotated[
    str | None,
    Field(description="A project directory or any file in it; the project's own libraries (its sym-lib-table and fp-lib-table) are included and shadow global ones."),
]


def _project_dir(project_path: str | None):
    if not project_path:
        return None
    p = resolve_in_workspace(project_path, must_exist=True)
    return p if p.is_dir() else p.parent


OpenBoardPath = Annotated[
    str | None,
    Field(description="Which open board, as a .kicad_pcb path. Omit to use the one board open in the PCB Editor."),
]


def _open_board_path(board_path: str | None):
    return resolve_in_workspace(board_path, suffixes=(BOARD,)) if board_path else None

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
WRITES_ARTIFACTS = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

SchematicPath = Annotated[
    str,
    Field(description="A .kicad_sch file, absolute or relative to the workspace. Any sheet of the project works; the root sheet is used."),
]
BoardPath = Annotated[str, Field(description="A .kicad_pcb file, absolute or relative to the workspace.")]


# Tool groups. The core tier is everything that reads, checks, exports, renders, reviews or documents;
# the full tier adds the design-edit tools and the frozen routers. KICAD_LAYER_TOOLS selects the tier.
GROUPS: dict[str, tuple[str, ...]] = {
    "diagnostics": ('kicad_doctor', 'capabilities', 'project_open'),
    "checks": ('run_erc', 'run_drc', 'sch_netlist', 'sch_trace'),
    "exports": ('export_bom', 'export_fab', 'render_board', 'sch_render'),
    "board_read": ('pcb_summary', 'pcb_list_items', 'pcb_net_stats'),
    "libraries": ('lib_search', 'sym_info', 'fp_info', 'lib_index', 'lib_fetch'),
    "sch_read": ('sch_list_components', 'sch_get_symbol'),
    "sch_edit": ('sch_set_property', 'sch_add_component', 'sch_wire', 'sch_label', 'sch_mark', 'sch_delete', 'sch_annotate'),
    "pcb_edit": ('pcb_place_footprint', 'pcb_move_footprint', 'pcb_add_track', 'pcb_add_via', 'pcb_add_zone', 'pcb_refill_zones', 'pcb_delete_items', 'pcb_save'),
    "review": ('review_board', 'review_schematic', 'review_project'),
    "signal_integrity": ('route_check', 'impedance_calc', 'stackup_info', 'parts_search'),
    "routers": ('route_pairs', 'stitch_planes', 'autoroute'),
    "docs": ('doc_fetch', 'doc_import', 'doc_list', 'doc_text', 'doc_page', 'doc_sections', 'doc_facts'),
}
FULL_ONLY = ('sch_edit', 'pcb_edit', 'routers')
TOOL_NAMES: tuple[str, ...] = tuple(n for names in GROUPS.values() for n in names)


def tool_names(tier: str = "core") -> tuple[str, ...]:
    """The tools ``tier`` registers, in registration order."""
    return tuple(n for g, names in GROUPS.items() if tier == "full" or g not in FULL_ONLY for n in names)


def _register_diagnostics(mcp: MCPServer) -> None:
    @mcp.tool(annotations=READ_ONLY)
    def kicad_doctor() -> DoctorReport:
        """Diagnose this server and its environment: which process is answering, which kicad-cli was found,
        whether KiCad's API is reachable and which documents are open, and what to do about any problem.
        Call this first whenever another tool fails unexpectedly."""
        return doctor_mod.diagnose()

    @mcp.tool(annotations=READ_ONLY)
    def capabilities(
        query: Annotated[str | None, Field(description="Filter rows by substring.")] = None,
        status: Annotated[Literal["covered", "planned", "gap", "gui_only"] | None, Field(description="Filter by status.")] = None,
    ) -> CapabilityMatrix:
        """What this server can do, through which channel (cli, ipc, file), and what KiCad 10 makes
        impossible. Consult it before promising the user something."""
        return caps.matrix(query, status)

    @mcp.tool(annotations=READ_ONLY)
    def project_open(
        path: Annotated[str, Field(description="A project directory, or any .kicad_pro, .kicad_sch or .kicad_pcb inside it.")],
    ) -> ProjectInfo:
        """Locate a KiCad project and describe it: root schematic, board, all sheets, text variables,
        netclasses, file format versions, and any editor lock files that mean KiCad has a file open."""
        return project_mod.open_project(path)


def _register_checks(mcp: MCPServer) -> None:
    @mcp.tool(annotations=READ_ONLY)
    def run_erc(
        schematic_path: SchematicPath,
        severity: Annotated[
            Literal["default", "all", "error", "warning"],
            Field(description="all (default) includes excluded violations flagged as excluded; default is errors and warnings only."),
        ] = "all",
    ) -> VerdictReport:
        """Run KiCad's Electrical Rules Check on the whole schematic hierarchy with kicad-cli and return
        a verdict (PASS, WARN, FAIL, or UNVERIFIED when no report was produced) with every finding,
        keyed by stable ids and item UUIDs. Works whether or not KiCad is open."""
        root = root_schematic_for(schematic_path)
        return reports.run_erc(root, severity=severity)

    @mcp.tool(annotations=READ_ONLY)
    def run_drc(
        board_path: BoardPath,
        severity: Literal["default", "all", "error", "warning"] = "all",
        schematic_parity: Annotated[bool, Field(description="Also compare the board against the schematic next to it.")] = True,
        all_track_errors: Annotated[bool, Field(description="Report every track error instead of the first per track.")] = False,
    ) -> VerdictReport:
        """Run KiCad's Design Rules Check on a board with kicad-cli. The verdict counts clearance and
        other violations, unconnected items (unrouted nets), and schematic parity problems; a board with
        unrouted nets is never PASS. Works whether or not KiCad is open."""
        board = resolve_in_workspace(board_path, suffixes=(BOARD,))
        return reports.run_drc(board, severity=severity, schematic_parity=schematic_parity, all_track_errors=all_track_errors)

    @mcp.tool(annotations=READ_ONLY)
    def sch_netlist(
        schematic_path: SchematicPath,
        refresh: Annotated[bool, Field(description="Ignore the cache and export again.")] = False,
        include_components: Annotated[bool, Field(description="Include the component list (value, footprint, sheet, pins).")] = True,
        max_nets: Annotated[int, Field(ge=1, le=5000)] = 500,
    ) -> Netlist:
        """The resolved connectivity of the whole schematic hierarchy: every net with its nodes (ref, pin,
        pin function, pin type), every component, every sheet. Exported by kicad-cli from the root sheet and
        cached until any schematic file changes. This is the source of truth for 'what connects to what'."""
        root = root_schematic_for(schematic_path)
        return netlist.load_netlist(root, refresh=refresh, include_components=include_components, max_nets=max_nets)

    @mcp.tool(annotations=READ_ONLY)
    def sch_trace(
        schematic_path: SchematicPath,
        ref: Annotated[str, Field(description="Reference designator, e.g. U1 or R12.")],
        pin: Annotated[str | None, Field(description="Pin number to restrict to; all pins when omitted.")] = None,
    ) -> TraceResult:
        """For one component, list each pin's net and everything else on that net. Unconnected pins have
        net null. Uses the cached netlist."""
        root = root_schematic_for(schematic_path)
        return netlist.trace(root, ref, pin)


def _register_exports(mcp: MCPServer) -> None:
    @mcp.tool(annotations=WRITES_ARTIFACTS)
    def export_bom(
        schematic_path: SchematicPath,
        fields: Annotated[list[str] | None, Field(description="Columns; default Reference, Value, Footprint, ${QUANTITY}, ${DNP}.")] = None,
        group_by: Annotated[list[str] | None, Field(description="Group rows by these fields; default Value and Footprint. Pass [] for no grouping.")] = None,
        output_path: Annotated[str | None, Field(description="CSV path; default <name>-bom.csv next to the schematic.")] = None,
        max_rows: Annotated[int, Field(ge=1, le=5000)] = 500,
    ) -> BomResult:
        """Export a bill of materials as CSV with kicad-cli and return the parsed rows."""
        root = root_schematic_for(schematic_path)
        return exports.export_bom(root, fields=fields, group_by=group_by, output_path=output_path, max_rows=max_rows)

    @mcp.tool(annotations=WRITES_ARTIFACTS)
    def export_fab(
        board_path: BoardPath,
        output_dir: Annotated[str | None, Field(description="Output directory; default 'fab' next to the board.")] = None,
        gerbers: bool = True,
        drill: bool = True,
        position: Annotated[bool, Field(description="Pick-and-place CSV in mm, both sides.")] = True,
        step: Annotated[bool, Field(description="STEP 3D model; slow, needs resolvable 3D models.")] = False,
        pdf: Annotated[bool, Field(description="Multi-page PDF of copper, silkscreen and edge layers.")] = False,
        layers: Annotated[list[str] | None, Field(description="Gerber layers, e.g. F.Cu,B.Cu,Edge.Cuts; default is the board's plot settings.")] = None,
    ) -> ExportResult:
        """Produce fabrication files with kicad-cli and list exactly what was written, with sizes and
        hashes. Reads the board file on disk: save in KiCad first if it has unsaved changes."""
        board = resolve_in_workspace(board_path, suffixes=(BOARD,))
        return exports.export_fab(board, output_dir=output_dir, gerbers=gerbers, drill=drill, position=position, step=step, pdf=pdf, layers=layers)

    @mcp.tool(annotations=WRITES_ARTIFACTS, structured_output=False)
    def render_board(
        board_path: BoardPath,
        side: Literal["top", "bottom", "left", "right", "front", "back"] = "top",
        width: Annotated[int, Field(ge=64, le=4096)] = 1600,
        height: Annotated[int, Field(ge=64, le=4096)] = 900,
        quality: Literal["basic", "high", "user", "job_settings"] = "basic",
        output_path: Annotated[str | None, Field(description="PNG path; default renders/<name>-<side>.png next to the board.")] = None,
    ) -> list:
        """Render the board in 3D to a PNG with kicad-cli and return the image so you can look at it.
        Reads the board file on disk."""
        board = resolve_in_workspace(board_path, suffixes=(BOARD,))
        png, command = exports.render_board(board, side=side, width=width, height=height, quality=quality, output_path=output_path)
        return [f"Rendered {display(board)} ({side}) to {display(png)} with: {' '.join(command)}", Image(path=png)]

    @mcp.tool(annotations=WRITES_ARTIFACTS)
    def sch_render(
        schematic_path: SchematicPath,
        format: Literal["svg", "pdf"] = "svg",
        output_dir: Annotated[str | None, Field(description="Output directory; default 'renders' next to the schematic.")] = None,
    ) -> RenderResult:
        """Draw every sheet of the schematic to SVG (one file per sheet) or one multi-page PDF with
        kicad-cli, and return the file paths so the client can read and display them."""
        root = root_schematic_for(schematic_path)
        return exports.render_schematic(root, fmt=format, output_dir=output_dir)


def _register_board_read(mcp: MCPServer) -> None:
    @mcp.tool(annotations=READ_ONLY)
    def pcb_summary(board_path: OpenBoardPath = None) -> BoardSummary:
        """Describe the board open in KiCad's PCB Editor through the live API: title block, copper
        layer count, enabled layers, outline size, item counts, stackup and netclass rules. Needs KiCad
        running with the API enabled and the board open; kicad_doctor explains if it is not."""
        return board_read.summary(_open_board_path(board_path))

    @mcp.tool(annotations=READ_ONLY)
    def pcb_list_items(
        kind: Literal["footprint", "pad", "track", "via", "zone", "net", "text"],
        board_path: OpenBoardPath = None,
        net: Annotated[str | None, Field(description="Only items on this net name.")] = None,
        layer: Annotated[str | None, Field(description="Only items on this layer, canonical name such as F.Cu or B.SilkS.")] = None,
        ref: Annotated[str | None, Field(description="Only footprints or pads whose reference starts with this, e.g. R or U1.")] = None,
        limit: Annotated[int, Field(ge=1, le=2000)] = 200,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> BoardItems:
        """List items of one kind from the board open in KiCad, in millimetres and degrees with KiCad
        layer names and item ids. Page with limit and offset on large boards."""
        return board_read.list_items(kind, board_path=_open_board_path(board_path), net=net, layer=layer, ref=ref, limit=limit, offset=offset)

    @mcp.tool(annotations=READ_ONLY)
    def pcb_net_stats(
        board_path: OpenBoardPath = None,
        net: Annotated[str | None, Field(description="One net name; all nets when omitted.")] = None,
        limit: Annotated[int, Field(ge=1, le=2000)] = 200,
    ) -> NetStats:
        """Per-net routing statistics from the open board: track count and total length, widths,
        layers, vias, pads, a hint when a net has pads but no tracks, and differential-pair candidates
        with their length mismatch."""
        return board_read.net_stats(board_path=_open_board_path(board_path), net=net, limit=limit)


def _register_libraries(mcp: MCPServer) -> None:
    @mcp.tool(annotations=WRITES_ARTIFACTS)
    def lib_fetch(
        lcsc: Annotated[str, Field(description="LCSC code such as C520543, as parts_search or jlcpcb.com/parts give it.")],
        project_path: Annotated[str | None, Field(description="The project (.kicad_pro or its folder) whose lib/ receives the files. Register <lib_name> in its sym-lib-table and fp-lib-table afterwards.")] = None,
        lib_dir: Annotated[str | None, Field(description="Folder for the library files instead of <project>/lib; must be inside the workspace.")] = None,
        lib_name: Annotated[str, Field(description="Library base name: <lib_name>.kicad_sym, <lib_name>.pretty and <lib_name>.3dshapes; several parts share one library.")] = "jlc",
        parts: Literal["full", "symbol", "footprint", "model"] = "full",
        overwrite: Annotated[bool, Field(description="Replace a symbol, footprint or model of the same name already in the library.")] = False,
    ) -> LibFetch:
        """Symbol, footprint and 3D model (STEP and WRL) for one LCSC code, converted from EasyEDA's
        component data (EasyEDA is JLCPCB's own design tool; nearly every part in the assembly catalogue
        has a model there) by easyeda2kicad into a library next to the project. A part swap becomes:
        parts_search for the code and stock, lib_fetch for the files, then fp_info and the datasheet
        drawing (doc_page) for the pad-for-pad check. The models are drawn by users and JLCPCB staff:
        treat them as drafts, never as verified."""
        target = easyeda_mod.target_dir(resolve_in_workspace(lib_dir, must_exist=False) if lib_dir else None, _project_dir(project_path))
        return easyeda_mod.fetch(lcsc, target, lib_name, parts=parts, overwrite=overwrite)

    @mcp.tool(annotations=READ_ONLY)
    def lib_search(
        query: Annotated[str, Field(description="Words from the part's name, description or keywords, e.g. 'attiny1614', '0603 resistor', 'usb c receptacle 16 pin'.")],
        kind: Literal["symbol", "footprint", "both"] = "both",
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        project_path: ProjectPath = None,
    ) -> LibSearchResult:
        """Search every symbol and footprint library KiCad can see (about 22,000 symbols and 15,000
        footprints in the stock libraries) by name, description and keywords. Exact and prefix name
        matches come first. The index is built on first use, which takes about half a minute, then
        refreshes only for libraries whose files changed."""
        r = libindex.search(query, kind, limit, _project_dir(project_path))
        st = libindex.status()
        return LibSearchResult(
            query=query,
            symbols=[SymbolHit(lib_id=s[0], description=s[1], keywords=s[2], pin_count=s[3], units=s[4], default_footprint=s[5] or None, power=bool(s[6])) for s in r["symbols"]],
            footprints=[FootprintHit(lib_id=f[0], description=f[1], tags=f[2], attr=f[3], pad_count=f[4], width_mm=f[5], height_mm=f[6]) for f in r["footprints"]],
            symbol_matches=r["symbol_total"],
            footprint_matches=r["footprint_total"],
            index_symbols=st.symbols,
            index_footprints=st.footprints,
        )

    @mcp.tool(annotations=READ_ONLY)
    def sym_info(
        lib_id: Annotated[str, Field(description="Library symbol id such as Device:R or MCU_Microchip_ATtiny:ATtiny1614-SS.")],
        project_path: ProjectPath = None,
    ) -> SymbolInfo:
        """Everything about one library symbol: description, datasheet, default footprint, footprint
        filters and the footprints that satisfy them, units, and every pin with its number, name and
        electrical type. Derived symbols are shown flattened, the way KiCad places them."""
        rec = libindex.symbol_info(lib_id, _project_dir(project_path))
        return SymbolInfo(
            lib_id=rec["lib_id"],
            library_path=rec["library_path"],
            description=rec["description"] or None,
            keywords=rec["keywords"] or None,
            datasheet=rec["datasheet"] or None,
            default_footprint=rec["footprint"] or None,
            fp_filters=(rec["fp_filters"] or "").split(),
            power=bool(rec["power"]),
            extends=rec["extends"] or None,
            units=rec["units"],
            pin_count=rec["pin_count"],
            pins=[SymbolPinInfo(number=p[0], name=p[1], type=p[2], unit=p[3], hidden=bool(p[4])) for p in rec["pins"]],
            matching_footprints=rec["matching_footprints"],
        )

    @mcp.tool(annotations=READ_ONLY)
    def fp_info(
        lib_id: Annotated[str, Field(description="Library footprint id such as Resistor_SMD:R_0603_1608Metric.")],
        project_path: ProjectPath = None,
    ) -> FootprintInfo:
        """Everything about one library footprint: description, tags, mount type, courtyard size, 3D
        model, and every pad with number, kind, shape, position, size, drill and layers, exactly as
        KiCad will place it."""
        rec = libindex.footprint_info(lib_id, _project_dir(project_path))
        return FootprintInfo(
            lib_id=rec["lib_id"],
            path=rec["path"],
            description=rec["description"] or None,
            tags=rec["tags"] or None,
            attr=rec["attr"] or None,
            pad_count=rec["pad_count"],
            smd_pads=rec["smd_pads"],
            tht_pads=rec["tht_pads"],
            width_mm=rec["width"],
            height_mm=rec["height"],
            model=rec["model"] or None,
            pads=[FootprintPadInfo(**p) for p in rec["pads"]],
        )

    @mcp.tool(annotations=READ_ONLY)
    def lib_index(
        rebuild: Annotated[bool, Field(description="Re-parse every library even if unchanged.")] = False,
        project_path: ProjectPath = None,
    ) -> LibIndexStatus:
        """Build or refresh the library index and report its size and age. Normally unnecessary:
        lib_search builds it on first use and refreshes changed libraries automatically."""
        rep = libindex.build(rebuild=rebuild, project_dir=_project_dir(project_path))
        result = LibIndexStatus(
            path=str(libindex.index_path()),
            symbols=rep.symbols,
            footprints=rep.footprints,
            libraries=rep.libraries,
            built_at=rep.built_at or None,
            rebuilt_libraries=rep.rebuilt_libraries,
            seconds=rep.seconds,
        )
        return result

    # ---- schematic editing (file channel) -------------------------------------------


def _register_sch_read(mcp: MCPServer) -> None:
    @mcp.tool(annotations=READ_ONLY)
    def sch_list_components(
        schematic_path: Annotated[str, Field(description="The .kicad_sch sheet to list; each sheet is listed on its own.")],
        ref_prefix: Annotated[str | None, Field(description="Only references starting with this, e.g. R or U1.")] = None,
        include_pins: Annotated[bool, Field(description="Include every pin with its sheet coordinates.")] = False,
        include_power: Annotated[bool, Field(description="Include power symbols (#PWR, #FLG).")] = False,
        limit: Annotated[int, Field(ge=1, le=5000)] = 500,
    ) -> SchematicComponents:
        """Every placed symbol on one sheet, read from the file: reference, library id, value, footprint,
        position, rotation, unit, properties and optionally pin coordinates. Works with KiCad closed."""
        return sch_tools.list_components(schematic_path, ref_prefix=ref_prefix, include_pins=include_pins, include_power=include_power, limit=limit)

    @mcp.tool(annotations=READ_ONLY)
    def sch_get_symbol(
        schematic_path: Annotated[str, Field(description="The .kicad_sch sheet the symbol is on.")],
        ref: Annotated[str, Field(description="Reference designator, e.g. U1.")],
    ) -> SchematicComponent:
        """One placed symbol in full, with every pin's number, name, electrical type and sheet position,
        which is what you need to wire to it."""
        return sch_tools.get_component(schematic_path, ref)


def _register_sch_edit(mcp: MCPServer) -> None:
    @mcp.tool(annotations=DESIGN_WRITE)
    def sch_set_property(
        schematic_path: Annotated[str, Field(description="The .kicad_sch sheet the symbol is on.")],
        ref: str,
        name: Annotated[str, Field(description="Property name: Value, Footprint, Datasheet, Reference or any custom field.")],
        value: str,
        dry_run: DryRun = False,
        force: Force = False,
    ) -> EditResult:
        """Change one property of a placed symbol, leaving every other byte of the file as it was.
        Requires write mode. Saves atomically after a snapshot, then runs ERC when the change can
        affect connectivity."""
        return sch_tools.set_property(schematic_path, ref, name, value, dry_run=dry_run, force=force)

    @mcp.tool(annotations=DESIGN_WRITE)
    def sch_add_component(
        schematic_path: Annotated[str, Field(description="The .kicad_sch sheet to place on.")],
        lib_id: Annotated[str, Field(description="Library symbol id from lib_search, e.g. Device:R or CM5IO:ComputeModule5-CM5_HSS.")],
        ref: Annotated[str, Field(description="Reference to assign, e.g. R12, or a prefix with ? such as R? to annotate later.")],
        x_mm: float,
        y_mm: float,
        rotation: Literal[0, 90, 180, 270] = 0,
        mirror: Literal["x", "y"] | None = None,
        value: Annotated[str | None, Field(description="Value text; default is the library value.")] = None,
        footprint: Annotated[str | None, Field(description="Footprint lib_id; default is the library default.")] = None,
        unit: Annotated[int, Field(ge=1)] = 1,
        dry_run: DryRun = False,
        force: Force = False,
    ) -> EditResult:
        """Place a library symbol on a sheet: embeds the symbol in the sheet's cache, writes the
        instance with its reference, and returns every pin's sheet position so you can wire it.
        Positions snap to the 1.27 mm grid. Requires write mode. Validated by ERC and a netlist
        comparison after saving."""
        return sch_tools.add_component(schematic_path, lib_id, ref, x_mm, y_mm, rotation=rotation, mirror=mirror, value=value, footprint=footprint, unit=unit, dry_run=dry_run, force=force)

    @mcp.tool(annotations=DESIGN_WRITE)
    def sch_wire(
        schematic_path: Annotated[str, Field(description="The .kicad_sch sheet to draw on.")],
        points: Annotated[list[list[float]], Field(description="Two or more [x_mm, y_mm] points; consecutive points become wire segments.")],
        add_junctions: Annotated[bool, Field(description="Add junction dots where the wire's ends meet existing wires.")] = True,
        dry_run: DryRun = False,
        force: Force = False,
    ) -> EditResult:
        """Draw a wire through the given points, snapped to the 1.27 mm grid. Ends must land exactly on
        pin tips, wire ends or labels to connect. Requires write mode. Validated by ERC and a netlist
        comparison so the result shows which nets changed."""
        return sch_tools.wire(schematic_path, points, add_junctions=add_junctions, dry_run=dry_run, force=force)

    @mcp.tool(annotations=DESIGN_WRITE)
    def sch_label(
        schematic_path: Annotated[str, Field(description="The .kicad_sch sheet to label.")],
        text: Annotated[str, Field(description="Net name.")],
        x_mm: float,
        y_mm: float,
        rotation: Literal[0, 90, 180, 270] = 0,
        kind: Literal["local", "global", "hierarchical"] = "local",
        shape: Literal["input", "output", "bidirectional", "tri_state", "passive"] = "input",
        dry_run: DryRun = False,
        force: Force = False,
    ) -> EditResult:
        """Put a net label at a point, which must coincide with a wire end or pin tip to name that net.
        Local labels connect within the sheet, global across sheets, hierarchical to the parent's sheet
        pin. Requires write mode."""
        return sch_tools.label(schematic_path, text, x_mm, y_mm, rotation=rotation, kind=kind, shape=shape, dry_run=dry_run, force=force)

    @mcp.tool(annotations=DESIGN_WRITE)
    def sch_mark(
        schematic_path: Annotated[str, Field(description="The .kicad_sch sheet.")],
        kind: Literal["junction", "no_connect"],
        x_mm: float,
        y_mm: float,
        dry_run: DryRun = False,
        force: Force = False,
    ) -> EditResult:
        """Add a junction dot or a no-connect flag at a point. Put no-connect flags on unused pins so
        ERC stops reporting them. Requires write mode."""
        return sch_tools.mark(schematic_path, kind, x_mm, y_mm, dry_run=dry_run, force=force)

    @mcp.tool(annotations=DESIGN_WRITE)
    def sch_delete(
        schematic_path: Annotated[str, Field(description="The .kicad_sch sheet.")],
        uuid: Annotated[str, Field(description="The item's uuid, as reported by the list, get and edit tools.")],
        dry_run: DryRun = False,
        force: Force = False,
    ) -> EditResult:
        """Remove one item (symbol, wire, label, junction, no-connect) by uuid. Requires write mode.
        Validated by ERC and a netlist comparison."""
        return sch_tools.delete(schematic_path, uuid, dry_run=dry_run, force=force)

    @mcp.tool(annotations=DESIGN_WRITE)
    def sch_annotate(
        schematic_path: Annotated[str, Field(description="The .kicad_sch sheet to annotate.")],
        dry_run: DryRun = False,
        force: Force = False,
    ) -> AnnotateResult:
        """Give every unannotated reference such as R? the next free number for its prefix on that sheet.
        Requires write mode."""
        return sch_tools.annotate(schematic_path, dry_run=dry_run, force=force)

    # ---- board editing (ipc or file channel) -------------------------------------------


def _register_pcb_edit(mcp: MCPServer) -> None:
    @mcp.tool(annotations=DESIGN_WRITE)
    def pcb_place_footprint(
        lib_id: Annotated[str, Field(description="Footprint lib_id from lib_search, e.g. Resistor_SMD:R_0603_1608Metric.")],
        ref: Annotated[str, Field(description="Reference designator to give the footprint.")],
        x_mm: float,
        y_mm: float,
        rotation: float = 0,
        value: Annotated[str, Field(description="Value text shown on the fabrication layer.")] = "",
        layer: Literal["F.Cu", "B.Cu"] = "F.Cu",
        board_path: EditBoardPath = None,
        channel: Channel = "auto",
        dry_run: DryRun = False,
        force: Force = False,
    ) -> BoardEditResult:
        """Place a library footprint on the board. Live: KiCad pastes it as one undo step. File: it is
        embedded in the board file. A footprint placed this way has no schematic symbol behind it, so
        DRC's parity check will report it until the schematic catches up. Requires write mode."""
        return pcb_tools.place_footprint(board_path, lib_id, ref, x_mm, y_mm, rotation=rotation, value=value, layer=layer, channel=channel, dry_run=dry_run, force=force)

    @mcp.tool(annotations=DESIGN_WRITE)
    def pcb_move_footprint(
        ref: str,
        x_mm: float | None = None,
        y_mm: float | None = None,
        rotation: Annotated[float | None, Field(description="Absolute rotation in degrees.")] = None,
        layer: Annotated[Literal["F.Cu", "B.Cu"] | None, Field(description="Flip to this side (live channel only).")] = None,
        board_path: EditBoardPath = None,
        channel: Channel = "auto",
        dry_run: DryRun = False,
        force: Force = False,
    ) -> BoardEditResult:
        """Move, rotate or flip a footprint by reference. Live edits are one undo step and are read back
        from KiCad. Pads keep their nets; tracks attached to moved pads are not moved. Requires write mode."""
        return pcb_tools.move_footprint(board_path, ref, x_mm=x_mm, y_mm=y_mm, rotation=rotation, layer=layer, channel=channel, dry_run=dry_run, force=force)

    @mcp.tool(annotations=DESIGN_WRITE)
    def pcb_add_track(
        points: Annotated[list[list[float]], Field(description="Two or more [x_mm, y_mm] points; consecutive points become segments.")],
        net: Annotated[str, Field(description="Net name the track belongs to, e.g. GND or /LED_H.")],
        width: Annotated[float, Field(gt=0, description="Track width in mm.")] = 0.25,
        layer: str = "F.Cu",
        board_path: EditBoardPath = None,
        channel: Channel = "auto",
        dry_run: DryRun = False,
        force: Force = False,
    ) -> BoardEditResult:
        """Add track segments through the given points on one copper layer. Ends must land on pads,
        vias or other tracks of the same net to connect; DRC tells you if they do not. Requires write mode."""
        return pcb_tools.add_track(board_path, points, width=width, layer=layer, net=net, channel=channel, dry_run=dry_run, force=force)

    @mcp.tool(annotations=DESIGN_WRITE)
    def pcb_add_via(
        x_mm: float,
        y_mm: float,
        net: str,
        size: Annotated[float, Field(gt=0, description="Via diameter in mm.")] = 0.8,
        drill: Annotated[float, Field(gt=0, description="Drill diameter in mm.")] = 0.3,
        board_path: EditBoardPath = None,
        channel: Channel = "auto",
        dry_run: DryRun = False,
        force: Force = False,
    ) -> BoardEditResult:
        """Add a through via on a net. Requires write mode."""
        return pcb_tools.add_via(board_path, x_mm, y_mm, net=net, size=size, drill=drill, channel=channel, dry_run=dry_run, force=force)

    @mcp.tool(annotations=DESIGN_WRITE)
    def pcb_add_zone(
        polygon: Annotated[list[list[float]], Field(description="Three or more [x_mm, y_mm] outline points.")],
        net: str,
        layer: str = "F.Cu",
        name: str = "",
        board_path: EditBoardPath = None,
        channel: Channel = "auto",
        dry_run: DryRun = False,
        force: Force = False,
    ) -> BoardEditResult:
        """Add a copper pour on a net with thermal-relief pad connections. Fill it afterwards with
        pcb_refill_zones. Requires write mode."""
        return pcb_tools.add_zone(board_path, polygon, net=net, layer=layer, name=name, channel=channel, dry_run=dry_run, force=force)

    @mcp.tool(annotations=DESIGN_WRITE)
    def pcb_refill_zones(
        board_path: EditBoardPath = None,
        channel: Channel = "auto",
    ) -> BoardEditResult:
        """Refill every copper zone. Live: KiCad fills in place. File: kicad-cli fills and saves the
        board. Run this after any copper edit and before DRC. Requires write mode."""
        return pcb_tools.refill_zones(board_path, channel=channel)

    @mcp.tool(annotations=DESIGN_WRITE)
    def pcb_delete_items(
        ids: Annotated[list[str], Field(description="Item ids (uuids) from pcb_list_items or an edit result.")],
        board_path: EditBoardPath = None,
        channel: Channel = "auto",
        dry_run: DryRun = False,
        force: Force = False,
    ) -> BoardEditResult:
        """Delete board items by id. Each item's kind is checked and reported before removal, so a
        track id is never mistaken for a footprint. Requires write mode."""
        return pcb_tools.delete_items(board_path, ids, channel=channel, dry_run=dry_run, force=force)

    @mcp.tool(annotations=DESIGN_WRITE)
    def pcb_save(
        board_path: EditBoardPath = None,
    ) -> BoardEditResult:
        """Ask KiCad to save the open board to disk, so kicad-cli tools such as run_drc and export_fab
        see the live edits. Requires write mode and the board open in the PCB Editor."""
        return pcb_tools.save_board(board_path)

    # ---- design review ------------------------------------------------------------------


def _register_review(mcp: MCPServer) -> None:
    @mcp.tool(annotations=READ_ONLY)
    def review_board(
        board_path: BoardPath,
        fab: Fab = "jlcpcb",
        schematic_parity: Annotated[bool, Field(description="Include DRC's schematic parity check when a schematic exists.")] = True,
    ) -> ReviewReport:
        """Review a board the way a fab and a layout reviewer would: DRC and unrouted connections,
        zone fill state, footprints outside the outline, manufacturability against the fab's
        published limits (tracks, vias, annular rings, hole spacing, edge clearance, silkscreen),
        track widths on power nets, zone stitching, and decoupling capacitor distance. Every check
        reports PASS, WARN, FAIL or UNVERIFIED with its evidence and the source of its limits."""
        board = resolve_in_workspace(board_path, suffixes=(BOARD,))
        return review_mod.review_board(board, fab=fab, parity=schematic_parity)

    @mcp.tool(annotations=READ_ONLY)
    def review_schematic(
        schematic_path: SchematicPath,
    ) -> ReviewReport:
        """Review a schematic: ERC, footprints assigned, values set, annotation, power nets driven,
        decoupling present, bill-of-materials summary, and the honest state of SPICE simulation.
        Every check reports PASS, WARN, FAIL, INFO or UNVERIFIED with its evidence."""
        root = root_schematic_for(schematic_path)
        return review_mod.review_schematic(root)

    @mcp.tool(annotations=READ_ONLY)
    def review_project(
        path: Annotated[str, Field(description="A project directory or any file in it.")],
        fab: Fab = "jlcpcb",
    ) -> ReviewReport:
        """The full review of a project: every schematic check followed by every board check, in
        one report with one verdict and the list of checks that could not run."""
        p = resolve_in_workspace(path)
        return review_mod.review_project(p, fab=fab)

    # ---- routing ------------------------------------------------------------------------


def _register_signal_integrity(mcp: MCPServer) -> None:
    @mcp.tool(annotations=READ_ONLY)
    def route_check(
        board_path: BoardPath,
        project_path: Annotated[str | None, Field(description="The .kicad_pro with the net classes; default: next to the board.")] = None,
        skew_limit_mm: Annotated[float | None, Field(description="Override the per-interface intra-pair skew limit for every pair.")] = None,
        via_length_mm: Annotated[float | None, Field(description="Length added per via when measuring a half; default 1.6 mm, the board thickness.")] = None,
    ) -> RouteReport:
        """Measure every differential pair on a board: the routed length of each half, the skew
        between them against the interface's limit (Ethernet and MIPI 0.15 mm, PCIe and USB 3.0
        0.1 mm, USB 2.0 0.15 mm by default, from the Compute Module 5 datasheet), the share of the
        pair that runs coupled at the net class's gap, width and gap deviations, and layer changes.
        Pairs are found by name (X_P/X_N, X_DP/X_DN, X+/X-); unrouted pairs are listed as such."""
        board = resolve_in_workspace(board_path, suffixes=(BOARD,))
        project = resolve_in_workspace(project_path) if project_path else None
        return routing_mod.route_check(board, project, skew_limit_mm=skew_limit_mm, via_length_mm=via_length_mm)

    @mcp.tool(annotations=READ_ONLY)
    def impedance_calc(
        width_mm: Annotated[float, Field(description="Trace width in mm.")],
        gap_mm: Annotated[float | None, Field(description="Edge-to-edge gap of a differential pair; omit for a single-ended line.")] = None,
        stackup: Annotated[str, Field(description="Stack-up preset, see stackup_info: jlc04161h-7628 (JLCPCB 4-layer), pcbway-4l-1.6mm (PCBWay standard 4-layer) or aisler-4l-1.6mm (AISLER 4-layer). Default: JLCPCB's JLC04161H-7628.")] = "jlc04161h-7628",
    ) -> ImpedanceResult:
        """Estimate the impedance of an outer-layer trace or pair on a stack-up preset with
        closed-form microstrip formulas, and report the fab's own published number when the
        geometry matches one of its table entries. Closed forms are about ten percent optimistic
        for tightly coupled pairs; the table entry is the one to design to."""
        return routing_mod.impedance(width_mm, gap_mm, stackup)

    @mcp.tool(annotations=READ_ONLY)
    def stackup_info(
        stackup: Annotated[str, Field(description="Preset name: jlc04161h-7628, pcbway-4l-1.6mm or aisler-4l-1.6mm (aliases jlcpcb, pcbway, aisler); default JLCPCB's 4-layer JLC04161H-7628. The result lists every preset.")] = "jlc04161h-7628",
    ) -> StackupInfo:
        """A stack-up preset: layers with thickness and permittivity, and the fab's published
        trace geometries per target impedance, with the source they were read from."""
        return routing_mod.stackup_info(stackup)

    # ---- parts --------------------------------------------------------------------------

    @mcp.tool(annotations=READ_ONLY)
    def parts_search(
        keyword: Annotated[str, Field(description="Manufacturer part number, or value and package such as '10uF 0805' or '10k 0603'.")],
        limit: Annotated[int, Field(description="Hits to return, at most 50.", ge=1, le=50)] = 8,
        in_stock_only: bool = True,
    ) -> PartsSearch:
        """Search JLCPCB's assembly parts catalogue: LCSC code, manufacturer part number, package,
        stock, whether it is a basic part, and unit price. Uses the same undocumented endpoint as
        jlcpcb.com/parts, so a failure means the endpoint changed, not that the part is missing."""
        return jlcpcb_mod.search(keyword, limit, in_stock_only=in_stock_only)

    # ---- routing ------------------------------------------------------------------------


def _register_routers(mcp: MCPServer) -> None:
    @mcp.tool(annotations=WRITES_ARTIFACTS)
    def route_pairs(
        board_path: BoardPath,
        project_path: Annotated[str | None, Field(description="The .kicad_pro with the net classes (pair width, gap, via, clearance); default: next to the board.")] = None,
        routes_in: RoutesIn = None,
        routes_out: RoutesOut = None,
        only: Annotated[list[str] | None, Field(description="Pair names to route, e.g. ['DSI_D0', 'USB_A_D']; default all.")] = None,
        exclude: Annotated[list[str] | None, Field(description="Pair names to leave to the autorouter.")] = None,
        order: Annotated[list[str] | None, Field(description="Routing order by pair name; unlisted pairs follow. Within a connector column, the pair nearest its turn should go first.")] = None,
        keepouts: Keepouts = None,
    ) -> PairRouteReport:
        """Route the board's differential pairs (found by name, geometry from the net classes) as coupled
        pairs on the outer layers and write them as a routes JSON. Escapes are planned for every pair
        first and reserved, the centreline is searched with a heading-aware A* (45 degrees per step, no
        folding back), P and N are offset with mitred corners, P crosses under N through two vias when it
        would land on the wrong side, and tuning bumps trim the skew. Failed pairs come back with the
        reason. The board file is not changed: apply the JSON with routes_apply or from the design code."""
        board = resolve_in_workspace(board_path, suffixes=(BOARD,))
        project = resolve_in_workspace(project_path) if project_path else None
        return routing_tools.route_pairs(board, project, routes_in=resolve_in_workspace(routes_in) if routes_in else None,
                                         routes_out=resolve_in_workspace(routes_out, must_exist=False) if routes_out else None,
                                         only=only, exclude=exclude, order=order, keepouts=[tuple(k) for k in keepouts] if keepouts else None)

    @mcp.tool(annotations=WRITES_ARTIFACTS)
    def stitch_planes(
        board_path: BoardPath,
        project_path: Annotated[str | None, Field(description="The .kicad_pro with the net classes; default: next to the board.")] = None,
        routes_in: RoutesIn = None,
        routes_out: RoutesOut = None,
        plane_nets: Annotated[list[str] | None, Field(description="Nets with a plane; default: the nets of the board's zones.")] = None,
        keepouts: Keepouts = None,
    ) -> StitchReport:
        """Give every surface-mount pad on a plane net a short stub and a via to its plane, checked
        against the other pads, the routes handed in, keep-outs and the board edge. Connectors get their
        vias inside, in the channel between the pin rows; small parts outside. Pads with no clear spot
        are listed and left for the autorouter. Writes a routes JSON (the input routes plus the stitches)."""
        board = resolve_in_workspace(board_path, suffixes=(BOARD,))
        project = resolve_in_workspace(project_path) if project_path else None
        return routing_tools.stitch_planes(board, project, routes_in=resolve_in_workspace(routes_in) if routes_in else None,
                                           routes_out=resolve_in_workspace(routes_out, must_exist=False) if routes_out else None,
                                           plane_nets=plane_nets, keepouts=[tuple(k) for k in keepouts] if keepouts else None)

    @mcp.tool(annotations=WRITES_ARTIFACTS)
    def autoroute(
        board_path: BoardPath,
        project_path: Annotated[str | None, Field(description="The .kicad_pro with the net classes; default: next to the board.")] = None,
        routes_in: RoutesIn = None,
        routes_out: RoutesOut = None,
        plane_layers: Annotated[dict[str, str] | None, Field(description="Layer -> net for the plane layers, e.g. {'In1.Cu': 'GND', 'In2.Cu': '+5V'}; those layers are not routed.")] = None,
        routable_layers: Annotated[list[str] | None, Field(description="Layers the autorouter may use; default every copper layer that is not a plane.")] = None,
        passes: Annotated[int, Field(description="FreeRouting optimisation passes.", ge=1, le=200)] = 40,
        timeout_s: Annotated[float, Field(description="Give up after this long.", ge=60, le=7200)] = 3000.0,
    ) -> AutorouteReport:
        """Route what is still unrouted with FreeRouting: the board and the routes JSON handed in go out as
        a Specctra DSN with the existing copper protected, the headless router runs (tools/freerouting*.jar
        with the Java in tools/jre or KICAD_LAYER_FREEROUTING / KICAD_LAYER_JAVA), and the session file
        comes back merged into the routes JSON. Differential pairs should be routed first with route_pairs;
        FreeRouting routes them as single nets."""
        board = resolve_in_workspace(board_path, suffixes=(BOARD,))
        project = resolve_in_workspace(project_path) if project_path else None
        return routing_tools.autoroute(board, project, routes_in=resolve_in_workspace(routes_in) if routes_in else None,
                                       routes_out=resolve_in_workspace(routes_out, must_exist=False) if routes_out else None,
                                       plane_layers=plane_layers, routable_layers=routable_layers, passes=passes, timeout_s=timeout_s)

    # ---- documentation ------------------------------------------------------------------


def _register_docs(mcp: MCPServer) -> None:
    def _doc_info(entry) -> DocInfo:
        return DocInfo(id=entry.id, file=entry.file, path=display(docs_mod.docs_dir() / entry.file), title=entry.title, source_url=entry.source_url, fetched=entry.fetched,
                       size=entry.size, sha256=entry.sha256, content_type=entry.content_type, pages=entry.pages, tags=entry.tags, notes=entry.notes)

    def _doc(doc: str) -> tuple[DocInfo, "Path"]:
        entry, path = docs_mod.resolve(doc)
        if entry is None:
            entry = docs_mod.import_file(path, subdir="", source_url=None) if path.parent == docs_mod.docs_dir() else None
        if entry is None:
            # a file elsewhere in the workspace: describe it without indexing it
            info = DocInfo(id="", file=display(path), path=display(path), title=path.stem, source_url=None, fetched="", size=path.stat().st_size, sha256="", content_type="application/pdf" if path.suffix.lower() == ".pdf" else "application/octet-stream")
            return info, path
        return _doc_info(entry), path

    @mcp.tool(annotations=WRITES_ARTIFACTS)
    def doc_fetch(
        url: Annotated[str, Field(description="Direct URL of the document, usually a PDF datasheet, application note or reference-design archive.")],
        subdir: Annotated[str, Field(description="Folder inside the documentation library, e.g. datasheets, reference-designs, standards.")] = "datasheets",
        filename: Annotated[str | None, Field(description="File name to store under; default derived from the URL.")] = None,
        title: Annotated[str | None, Field(description="Human title, e.g. 'Silvertel Ag5400 PoE module datasheet'.")] = None,
        tags: Annotated[list[str] | None, Field(description="Keywords for later lookup, e.g. ['poe', 'silvertel', 'ag5405'].")] = None,
        notes: str = "",
        expect: Literal["pdf", "any"] = "pdf",
        browser: Annotated[Literal["auto", "never", "always"], Field(description="auto: try a plain fetch, then headless Chromium when the site answers with a page or refuses; always: start in the browser; never: plain fetch only.")] = "auto",
    ) -> DocInfo:
        """Download a technical document into the project's documentation library (research/references
        by default) and index it with its source URL, date, size and hash. A plain fetch with
        browser-grade headers comes first; it follows redirects and a single PDF link on a viewer page.
        When a site answers with a scripted download portal or refuses the plain client, headless
        Chromium loads the page and takes the download it offers. If that fails too (login walls, bot
        checks), the error says so: open the URL in a browser, save the file, and use doc_import. Find
        URLs with web search first; manufacturer sites and distributor mirrors usually both work."""
        return _doc_info(docs_mod.fetch(url, subdir=subdir, filename=filename, title=title, tags=tags, notes=notes, expect=expect, browser=browser))

    @mcp.tool(annotations=WRITES_ARTIFACTS)
    def doc_import(
        path: Annotated[str, Field(description="A file already on disk, e.g. one saved from a browser.")],
        subdir: str = "datasheets",
        source_url: Annotated[str | None, Field(description="Where it came from, for provenance.")] = None,
        title: str | None = None,
        tags: list[str] | None = None,
        notes: str = "",
        move: Annotated[bool, Field(description="Move instead of copy.")] = False,
    ) -> DocInfo:
        """Bring a document that is already on disk into the documentation library and index it."""
        p = Path(path)
        if not p.is_absolute():
            p = resolve_in_workspace(path, must_exist=True)
        return _doc_info(docs_mod.import_file(p, subdir=subdir, source_url=source_url, title=title, tags=tags, notes=notes, move=move))

    @mcp.tool(annotations=READ_ONLY)
    def doc_list(
        query: Annotated[str | None, Field(description="Words to look for in titles, file names, tags, notes and the documents' text; omit to list everything.")] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> DocList:
        """List or search the documentation library. Text hits include the page number, so a following
        doc_text or doc_page call can go straight to the right place."""
        if query:
            found = docs_mod.search(query, limit=limit)
        else:
            found = [(e, []) for e in docs_mod.load_index()[:limit]]
        return DocList(library=display(docs_mod.docs_dir()), query=query, documents=[DocSearchHit(doc=_doc_info(e), matches=h) for e, h in found], total=len(docs_mod.load_index()))

    @mcp.tool(annotations=READ_ONLY)
    def doc_text(
        doc: Annotated[str, Field(description="Document id, library file name, title, or a path inside the workspace.")],
        pages: Annotated[str | None, Field(description="Pages to return, e.g. '3-5,12'. Without find: default all, capped to keep the answer readable. With find: default none, only the matches come back.")] = None,
        find: Annotated[str | None, Field(description="Regular expression to locate in the text; matches come back with page numbers and one context window each (overlapping windows merged), and no page text unless pages is given.")] = None,
        max_chars: Annotated[int, Field(ge=1000, le=200000)] = 40000,
    ) -> DocText:
        """Extract the text of a PDF (or read a text document), per page, or search it. With find, the
        answer is the matches alone: a page number and a short window per hit, so a lookup costs a few
        hundred tokens; add pages to read a page around a hit. Pages that are drawings or scanned images
        come back empty; use doc_page to look at those."""
        info, path = _doc(doc)
        all_pages = docs_mod.extract_text(path)
        wanted = [] if (find and not pages) else docs_mod.parse_pages(pages, len(all_pages))
        matches = docs_mod.find_in_text(all_pages, find) if find else []
        text: dict[int, str] = {}
        used = 0
        truncated = False
        for n in wanted:
            t = all_pages[n - 1]
            if used + len(t) > max_chars:
                room = max_chars - used
                if room > 200:
                    text[n] = t[:room] + " ..."
                truncated = True
                break
            text[n] = t
            used += len(t)
        return DocText(doc=info, pages=wanted, text=text, matches=matches, truncated=truncated)

    @mcp.tool(annotations=WRITES_ARTIFACTS)
    def doc_page(
        doc: Annotated[str, Field(description="Document id, library file name, title, or a path inside the workspace.")],
        page: Annotated[int, Field(ge=1)],
        scale: Annotated[float, Field(ge=0.5, le=4.0, description="Render scale; 2 is about 144 dpi.")] = 2.0,
    ) -> list:
        """Render one page of a PDF to an image and return it, for pinout drawings, package dimensions,
        tables and anything else text extraction cannot carry."""
        info, path = _doc(doc)
        png = docs_mod.render_page(path, page, scale=scale)
        return [f"{info.title}, page {page} of {info.pages or '?'} rendered to {display(png)}", Image(path=png)]

    @mcp.tool(annotations=READ_ONLY)
    def doc_sections(
        doc: Annotated[str, Field(description="Document id, library file name, title, or a path inside the workspace.")],
        find: Annotated[str | None, Field(description="Regular expression on the titles, e.g. 'pin|package'; omit for the whole index.")] = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 120,
    ) -> DocSections:
        """The document's index: its bookmarks, its contents page, the headings found in the text and every
        table and figure caption, each with its page. Ask this first, then doc_text with those pages or
        doc_page for the table: a datasheet lookup then costs the rows you need, not the pages around them."""
        info, path = _doc(doc)
        found = docs_mod.sections(path)
        if find:
            rx = re.compile(find, re.IGNORECASE)
            picked = [s for s in found if rx.search(s["title"])]
        else:
            picked = found
        return DocSections(doc=info, sections=[DocSection(**s) for s in picked[:limit]], total=len(found), find=find)

    @mcp.tool(annotations=READ_ONLY)
    def doc_facts(
        part: Annotated[str, Field(description="Manufacturer part number or a name that starts its fact sheet's file name, e.g. MAX98357A.")],
        section: Annotated[str | None, Field(description="One heading of the sheet, by prefix: Pins, Limits, Values, Recommended circuit, Package, Notes.")] = None,
        find: Annotated[str | None, Field(description="Regular expression: only the sheet's lines that match come back, each with its section, e.g. 'trip|B0'.")] = None,
    ) -> DocFacts:
        """A part's fact sheet: pins, limits, the values a design is built on, the recommended circuit and the
        package, every row with its datasheet page, written once from the datasheet and checked against the
        rendered pages. The cheapest answer to a datasheet question. Without a sheet the answer says how to
        write one (a subagent with doc_sections, doc_text and doc_page, into research/parts/)."""
        path, text, headings = docs_mod.fact_sheet(part, section, find)
        if path is None:
            from kicad_layer.errors import LayerError

            try:
                entry, _ = docs_mod.resolve(part)
            except LayerError:
                entry = None
            return DocFacts(part=part, found=False, advice=docs_mod.fact_sheet_brief(part, entry))
        if (section or find) and not text:
            return DocFacts(part=part, found=True, path=display(path), section=section, sections=headings,
                            advice=f"nothing in the sheet for section={section!r} find={find!r}; the sheet has: {', '.join(headings)}")
        return DocFacts(part=part, found=True, path=display(path), section=section, text=text, sections=headings)


REGISTRARS = {"diagnostics": _register_diagnostics, "checks": _register_checks, "exports": _register_exports, "board_read": _register_board_read, "libraries": _register_libraries, "sch_read": _register_sch_read, "sch_edit": _register_sch_edit, "pcb_edit": _register_pcb_edit, "review": _register_review, "signal_integrity": _register_signal_integrity, "routers": _register_routers, "docs": _register_docs}


def register_tools(mcp: MCPServer, tier: str = "core") -> None:
    """Register the tools of ``tier``: ``core`` (default) or ``full``."""
    if tier not in TIERS:
        raise ValueError(f"tier must be one of {TIERS}, got {tier!r}")
    for group in GROUPS:
        if tier == "full" or group not in FULL_ONLY:
            REGISTRARS[group](mcp)
