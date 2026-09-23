"""The capability matrix: what this server can do, through which channel, and what it cannot.

Honesty is the point. KiCad 10's API covers the PCB editor only, cannot plot, and cannot
open projects; those rows say so instead of hiding behind a tool that half-works.
"""

from __future__ import annotations

from kicad_layer.models import CapabilityMatrix, CapabilityRow

R = CapabilityRow

ROWS: list[CapabilityRow] = [
    R(capability="Diagnose environment, processes, endpoints", channel="builtin", status="covered", tool="kicad_doctor"),
    R(capability="List capabilities", channel="builtin", status="covered", tool="capabilities"),
    R(capability="Open a project and list its files, variables, netclasses", channel="file", status="covered", tool="project_open"),
    R(capability="Electrical rules check with machine-readable findings", channel="cli", status="covered", tool="run_erc", notes="Positions corrected for the KiCad 10.0.x ERC JSON unit bug."),
    R(capability="Design rules check incl. unconnected items and schematic parity", channel="cli", status="covered", tool="run_drc"),
    R(capability="Resolved netlist (nets, nodes, pin types) for the whole hierarchy", channel="cli", status="covered", tool="sch_netlist", notes="kicadxml export on the root sheet, cached by content fingerprint."),
    R(capability="Trace what a component pin connects to", channel="cli", status="covered", tool="sch_trace"),
    R(capability="Bill of materials as CSV and rows", channel="cli", status="covered", tool="export_bom"),
    R(capability="Fabrication outputs: Gerbers, drill, position, STEP, PDF", channel="cli", status="covered", tool="export_fab"),
    R(capability="3D render of the board as PNG", channel="cli", status="covered", tool="render_board"),
    R(capability="Schematic sheets as SVG or PDF", channel="cli", status="covered", tool="sch_render"),
    R(capability="Summary of the board open in the PCB editor", channel="ipc", status="covered", tool="pcb_summary", notes="Design rules are not readable through the API in KiCad 10."),
    R(capability="List footprints, tracks, vias, zones, pads, nets of the open board", channel="ipc", status="covered", tool="pcb_list_items"),
    R(capability="Per-net length, width, layer and via statistics", channel="ipc", status="covered", tool="pcb_net_stats"),
    R(capability="Place a library footprint on the board", channel="ipc", status="covered", tool="pcb_place_footprint", notes="Live through KiCad's paste path; file channel when the board is closed."),
    R(capability="Move, rotate or flip a footprint as one undo step", channel="ipc", status="covered", tool="pcb_move_footprint", notes="Flip needs the live channel."),
    R(capability="Add track segments", channel="ipc", status="covered", tool="pcb_add_track"),
    R(capability="Add a via", channel="ipc", status="covered", tool="pcb_add_via"),
    R(capability="Add a copper zone", channel="ipc", status="covered", tool="pcb_add_zone"),
    R(capability="Move, rotate or flip many footprints in one write or one undo step", channel="ipc", status="covered", tool="pcb_move_footprints", notes="Flip needs the live channel."),
    R(capability="Board outline on Edge.Cuts with rounded corners", channel="file", status="covered", tool="pcb_set_outline", notes="File channel only."),
    R(capability="Mounting holes, plated on a net or NPTH", channel="file", status="covered", tool="pcb_add_mounting_holes", notes="File channel only."),
    R(capability="Refill zones", channel="ipc", status="covered", tool="pcb_refill_zones", notes="Live in KiCad, or kicad-cli on a closed board."),
    R(capability="Delete board items by id with type check", channel="ipc", status="covered", tool="pcb_delete_items"),
    R(capability="Save the open board", channel="ipc", status="covered", tool="pcb_save"),
    R(capability="List schematic components with pins and positions", channel="file", status="covered", tool="sch_list_components"),
    R(capability="Read one symbol in full", channel="file", status="covered", tool="sch_get_symbol"),
    R(capability="Change a symbol property", channel="file", status="covered", tool="sch_set_property", notes="Lossless edit: untouched bytes stay identical."),
    R(capability="Place a library symbol on a sheet", channel="file", status="covered", tool="sch_add_component", notes="Embeds the symbol in the sheet cache, returns pin positions."),
    R(capability="Draw wires, with junctions where they meet", channel="file", status="covered", tool="sch_wire"),
    R(capability="Net labels: local, global, hierarchical", channel="file", status="covered", tool="sch_label"),
    R(capability="Junction dots and no-connect flags", channel="file", status="covered", tool="sch_mark"),
    R(capability="Delete any schematic item by uuid", channel="file", status="covered", tool="sch_delete"),
    R(capability="Annotate unnumbered references", channel="file", status="covered", tool="sch_annotate"),
    R(capability="Instantiate a verified circuit block", channel="file", status="planned", tool="sch_add_block", notes="Needs the block library (milestone 7)."),
    R(capability="Search symbol and footprint libraries", channel="file", status="covered", tool="lib_search", notes="Full-text index over every library in KiCad's tables, built on first use."),
    R(capability="Read one library symbol: pins, types, footprint filters, matching footprints", channel="file", status="covered", tool="sym_info"),
    R(capability="Read one library footprint: pads, geometry, mount type, 3D model", channel="file", status="covered", tool="fp_info"),
    R(capability="Build or refresh the library index", channel="file", status="covered", tool="lib_index"),
    R(capability="Symbol, footprint and 3D model for an LCSC code from EasyEDA's component data", channel="builtin", status="covered", tool="lib_fetch",
      notes="easyeda2kicad as a subprocess into the project's lib/; user-drawn models, checked pad for pad against the datasheet before use."),
    R(capability="Board review: DRC, unrouted, zone fills, off-board parts, fab DFM limits, power track widths, stitching, decoupling distance", channel="cli", status="covered", tool="review_board", notes="Limits from JLCPCB's published capabilities, cited in the report."),
    R(capability="Schematic review: ERC, footprints, values, annotation, power sources, decoupling, BOM summary", channel="cli", status="covered", tool="review_schematic"),
    R(capability="Whole-project review in one report", channel="cli", status="covered", tool="review_project"),
    R(capability="SPICE simulation", channel="cli", status="gap", notes="KiCad ships only the ngspice library, no executable; the review reports the netlist's model coverage and marks simulation UNVERIFIED."),
    R(capability="Visual diff of renders before and after a change", channel="cli", status="planned", notes="Needs an image library; renders exist, the comparison does not yet."),
    R(capability="Open, close or switch projects and boards in KiCad", channel="ipc", status="gui_only", notes="No API for it in KiCad 10; launch kicad.exe or use the GUI."),
    R(capability="Plot or export through the live API", channel="ipc", status="gui_only", notes="Added in KiCad 11. kicad-cli covers it headlessly today."),
    R(capability="Edit the schematic through the live API", channel="ipc", status="gap", notes="KiCad 10 exposes no schematic API; the file channel is the only route."),
    R(capability="Update PCB from schematic (netlist import)", channel="ipc", status="gap", notes="No API in KiCad 10; done in the GUI."),
    R(capability="Autorouting", channel="cli", status="gap", notes="Planned through Freerouting once the board tools exist."),
    R(capability="Fetch a datasheet or reference document into an indexed library with provenance", channel="builtin", status="covered", tool="doc_fetch",
      notes="Browser-grade headers, redirect and viewer-page following; classified failures point to the browser fallback."),
    R(capability="Import a document saved by hand into the library", channel="builtin", status="covered", tool="doc_import"),
    R(capability="List and search the documentation library, including document text", channel="builtin", status="covered", tool="doc_list"),
    R(capability="Read a document's text per page with regex search", channel="builtin", status="covered", tool="doc_text"),
    R(capability="Render a document page to an image for drawings and tables", channel="builtin", status="covered", tool="doc_page"),
    R(capability="Index a document's sections, tables and figures with their pages", channel="builtin", status="covered", tool="doc_sections"),
    R(capability="Answer from a part's fact sheet: pins, limits, values, circuit, package, with pages", channel="builtin", status="covered", tool="doc_facts"),
    R(capability="Differential pair lengths, skew, coupling and class geometry from the board file", channel="file", status="covered", tool="route_check"),
    R(capability="Impedance estimate for a trace or pair on a stack-up preset, with the fab's table", channel="builtin", status="covered", tool="impedance_calc"),
    R(capability="Differential pair routing: escapes, heading-aware search, crossovers, tuning bumps, into a routes JSON", channel="file", status="covered", tool="route_pairs"),
    R(capability="Plane stitching: stub and via from every surface-mount pad on a plane net", channel="file", status="covered", tool="stitch_planes"),
    R(capability="Autorouting of the remaining nets with FreeRouting, existing copper protected", channel="builtin", status="covered", tool="autoroute", notes="Rule areas become DSN keep-outs, plain two-class .kicad_dru clearances class_class rules; nets under rules the DSN cannot carry are excluded and listed."),
    R(capability="Stack-up presets with published impedance geometries", channel="builtin", status="covered", tool="stackup_info"),
    R(capability="JLCPCB assembly catalogue search: LCSC code, stock, basic part, price", channel="builtin", status="covered", tool="parts_search"),
    R(capability="Run a long tool (autoroute, DRC, ERC, zone refill, render, board review) in the background past the client's request timeout", channel="builtin", status="covered", tool="job_start",
      notes="A detached worker process per job, state and result in <cache>/jobs/<id>; a server restart does not stop or lose it."),
    R(capability="State, elapsed time, output tail and FreeRouting pass/unrouted/violations of a background job", channel="builtin", status="covered", tool="job_status"),
    R(capability="The normal result of a finished background job", channel="builtin", status="covered", tool="job_result"),
    R(capability="Search the internet for documentation", channel="builtin", status="gap", notes="Discovery uses the client's web search; the layer takes over from the URL."),
]


def matrix(query: str | None = None, status: str | None = None) -> CapabilityMatrix:
    rows = ROWS
    if query:
        q = query.lower()
        rows = [r for r in rows if q in r.capability.lower() or (r.tool and q in r.tool.lower()) or (r.notes and q in r.notes.lower())]
    if status:
        rows = [r for r in rows if r.status == status]
    return CapabilityMatrix(
        rows=rows,
        covered=sum(1 for r in ROWS if r.status == "covered"),
        planned=sum(1 for r in ROWS if r.status == "planned"),
        gap=sum(1 for r in ROWS if r.status == "gap"),
        gui_only=sum(1 for r in ROWS if r.status == "gui_only"),
    )
