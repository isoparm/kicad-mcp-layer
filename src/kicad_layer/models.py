"""Pydantic models that define every tool's output schema.

These are the contract with the model. Field names are stable; add fields, do not rename.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Verdict = Literal["PASS", "WARN", "FAIL", "UNVERIFIED", "BLOCKED", "EMPTY"]
"""Ladder, strongest first: EMPTY (nothing to check) > BLOCKED (could not run) >
FAIL (errors) > WARN (warnings only) > PASS. UNVERIFIED means the command reported
success but produced no report, so nothing can be claimed."""


class FileArtifact(BaseModel):
    path: str = Field(description="Path relative to the workspace root when possible.")
    size: int
    sha256: str


class FindingItem(BaseModel):
    uuid: str | None = None
    description: str
    x_mm: float | None = None
    y_mm: float | None = None


class Finding(BaseModel):
    id: str = Field(description="Stable id: sha256(type|first item uuid|description)[:12].")
    category: Literal["violation", "unconnected", "parity"]
    type: str = Field(description="KiCad rule key, e.g. clearance or power_pin_not_driven.")
    severity: Literal["error", "warning", "exclusion", "ignore", "info"]
    description: str
    excluded: bool = False
    comment: str | None = None
    sheet: str | None = Field(default=None, description="Schematic sheet path for ERC findings.")
    items: list[FindingItem] = Field(default_factory=list)


class WorstFinding(BaseModel):
    id: str
    type: str
    severity: str
    rule: str | None = Field(default=None, description="'rule:<name>', 'netclass:<name>' or 'board' when the description names the constraint.")
    description: str
    required_mm: float | None = None
    actual_mm: float | None = None
    deficit_mm: float | None = Field(default=None, description="How far the actual value misses the constraint; the sort key.")
    items: list[str] = Field(default_factory=list, description="Item descriptions, e.g. 'Pad 1 [GND] of U3 on F.Cu'.")
    x_mm: float | None = None
    y_mm: float | None = None


class UnconnectedPair(BaseModel):
    a: str
    b: str | None = None
    x_mm: float | None = None
    y_mm: float | None = None


class FindingsSummary(BaseModel):
    by_type: dict[str, int] = Field(default_factory=dict, description="Active findings per KiCad rule key.")
    by_rule: dict[str, int] = Field(default_factory=dict, description="Active findings per constraint named in the description: rule:<name>, netclass:<name>, board.")
    by_severity: dict[str, int] = Field(default_factory=dict)
    worst: list[WorstFinding] = Field(default_factory=list, description="The worst violations: largest deficit first, then the report order.")
    unconnected: int = 0
    unconnected_pairs: list[UnconnectedPair] = Field(default_factory=list)


class VerdictReport(BaseModel):
    verdict: Verdict
    kind: Literal["erc", "drc"]
    source: str
    report_path: str | None = None
    kicad_version: str | None = None
    date: str | None = None
    counts: dict[str, int] = Field(
        default_factory=dict,
        description="errors, warnings, excluded, unconnected, parity, total.",
    )
    findings: list[Finding] = Field(default_factory=list)
    truncated: bool = False
    summary: FindingsSummary | None = Field(default=None, description="Present with summary=true; findings is then empty and the full list is in report_path.")
    command: list[str] = Field(default_factory=list)
    exit_code: int | None = None
    duration_s: float | None = None
    notes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list, description="Conditions that make the result less trustworthy, e.g. default design rules.")


class ExportResult(BaseModel):
    output_dir: str
    files: list[FileArtifact] = Field(default_factory=list)
    commands: list[list[str]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    duration_s: float


class BomRow(BaseModel):
    values: dict[str, str]


class BomResult(BaseModel):
    csv_path: str
    columns: list[str]
    rows: list[BomRow]
    row_count: int
    truncated: bool = False
    command: list[str]


class RenderResult(BaseModel):
    files: list[FileArtifact]
    format: str
    command: list[str]
    note: str


class SheetInfo(BaseModel):
    number: int
    name: str
    tstamps: str


class NetNode(BaseModel):
    ref: str
    pin: str
    pin_function: str | None = None
    pin_type: str | None = None


class Net(BaseModel):
    code: int
    name: str
    netclass: str | None = None
    nodes: list[NetNode]


class Component(BaseModel):
    ref: str
    value: str | None = None
    footprint: str | None = None
    lib: str | None = None
    part: str | None = None
    description: str | None = None
    sheet_name: str | None = None
    sheet_file: str | None = None
    sheet_path: str | None = None
    tstamps: str | None = None
    fields: dict[str, str] = Field(default_factory=dict)
    properties: dict[str, str] = Field(default_factory=dict)
    pins: list[str] = Field(default_factory=list)


class Netlist(BaseModel):
    source: str = Field(description="Root schematic the netlist was exported from.")
    netlist_path: str
    cache_hit: bool
    sheets: list[SheetInfo]
    component_count: int
    net_count: int
    components: list[Component]
    nets: list[Net]
    truncated: bool = False
    command: list[str]


class PinTrace(BaseModel):
    pin: str
    pin_function: str | None = None
    pin_type: str | None = None
    net: str | None = Field(default=None, description="None when the pin is unconnected.")
    netclass: str | None = None
    connected_to: list[NetNode] = Field(default_factory=list)


class TraceResult(BaseModel):
    ref: str
    value: str | None
    footprint: str | None
    sheet_path: str | None
    pins: list[PinTrace]


class ProjectInfo(BaseModel):
    name: str
    directory: str
    project_file: str | None
    root_schematic: str | None
    board: str | None
    schematics: list[str]
    boards: list[str]
    text_variables: dict[str, str] = Field(default_factory=dict)
    netclasses: list[str] = Field(default_factory=list)
    project_file_version: int | None = None
    schematic_format_version: int | None = None
    board_format_version: int | None = None
    lock_files: list[str] = Field(
        default_factory=list,
        description="KiCad editor lock files present. A lock means the GUI has the file open.",
    )
    warnings: list[str] = Field(default_factory=list)


class KicadCliInfo(BaseModel):
    found: bool
    path: str | None = None
    version: str | None = None
    source: str | None = None
    sch_export_verbs: list[str] = Field(default_factory=list)
    pcb_export_verbs: list[str] = Field(default_factory=list)
    error: str | None = None


class IpcInfo(BaseModel):
    address: str
    address_source: str
    reachable: bool
    kicad_version: str | None = None
    open_boards: list[str] = Field(default_factory=list)
    open_schematics: list[str] = Field(default_factory=list)
    pipe_names: list[str] = Field(
        default_factory=list,
        description="Windows named pipes mentioning kicad. Empty while the API server is off.",
    )
    error: str | None = None
    diagnosis: str | None = Field(
        default=None,
        description="not_installed (kicad-python missing), not_running, api_disabled, no_editor_open, reachable, busy, stale_endpoint or unknown.",
    )


class ProcessInfo(BaseModel):
    pid: int
    name: str


class DoctorReport(BaseModel):
    server_version: str
    python: str
    executable: str
    pid: int
    mode: str
    workspace_root: str
    cache_dir: str
    kicad_cli: KicadCliInfo
    ipc: IpcInfo
    kicad_processes: list[ProcessInfo]
    advice: list[str] = Field(default_factory=list)
    writer_formats: dict[str, int] = Field(default_factory=dict, description="The KiCad file format versions this layer's writers emit, by file kind.")


class TitleBlock(BaseModel):
    title: str | None = None
    date: str | None = None
    revision: str | None = None
    company: str | None = None
    comments: list[str] = Field(default_factory=list)


class StackupLayer(BaseModel):
    layer: str
    type: str = Field(description="copper, dielectric, silkscreen, soldermask, solderpaste.")
    thickness_mm: float | None = None
    material: str | None = None
    user_name: str | None = None
    enabled: bool = True


class NetClassInfo(BaseModel):
    name: str
    net_count: int
    clearance_mm: float | None = None
    track_width_mm: float | None = None
    via_diameter_mm: float | None = None
    via_drill_mm: float | None = None
    diff_pair_width_mm: float | None = None
    diff_pair_gap_mm: float | None = None


class BoardSummary(BaseModel):
    board_path: str
    project: str
    title_block: TitleBlock
    copper_layer_count: int
    enabled_layers: list[str]
    size: dict[str, float | None] | None = Field(default=None, description="x_mm, y_mm, width_mm, height_mm of the outline.")
    outline_source: str
    counts: dict[str, int]
    stackup: list[StackupLayer]
    netclasses: list[NetClassInfo]
    notes: list[str] = Field(default_factory=list)


class FootprintItem(BaseModel):
    kind: Literal["footprint"] = "footprint"
    id: str
    ref: str
    value: str
    library: str | None = None
    name: str | None = None
    x_mm: float | None
    y_mm: float | None
    rotation_deg: float
    layer: str
    locked: bool = False
    dnp: bool = False
    exclude_from_bom: bool = False
    pad_count: int = 0


class PadItem(BaseModel):
    kind: Literal["pad"] = "pad"
    id: str
    footprint_ref: str | None = None
    number: str
    x_mm: float | None
    y_mm: float | None
    net: str | None = None
    pad_type: str
    shape: str | None = None
    size_x_mm: float | None = None
    size_y_mm: float | None = None
    drill_mm: float | None = None


class TrackItem(BaseModel):
    kind: Literal["track"] = "track"
    id: str
    x1_mm: float | None
    y1_mm: float | None
    x2_mm: float | None
    y2_mm: float | None
    width_mm: float | None
    layer: str
    net: str | None = None
    length_mm: float | None = None
    arc: bool = False


class ViaItem(BaseModel):
    kind: Literal["via"] = "via"
    id: str
    x_mm: float | None
    y_mm: float | None
    diameter_mm: float | None = None
    drill_mm: float | None = None
    net: str | None = None
    via_type: str
    start_layer: str | None = None
    end_layer: str | None = None


class ZoneItem(BaseModel):
    kind: Literal["zone"] = "zone"
    id: str
    name: str | None = None
    net: str | None = None
    layers: list[str]
    zone_type: str
    filled: bool = False
    priority: int = 0
    rule_area: bool = False


class NetItem(BaseModel):
    kind: Literal["net"] = "net"
    name: str
    netclass: str | None = None


class TextItem(BaseModel):
    kind: Literal["text"] = "text"
    id: str
    value: str
    layer: str
    x_mm: float | None = None
    y_mm: float | None = None
    locked: bool = False


BoardItem = FootprintItem | PadItem | TrackItem | ViaItem | ZoneItem | NetItem | TextItem


class BoardItems(BaseModel):
    board_path: str
    kind: str
    total: int
    offset: int
    returned: int
    truncated: bool
    items: list[BoardItem]


class NetStat(BaseModel):
    net: str
    netclass: str | None = None
    track_count: int
    total_length_mm: float
    widths_mm: list[float]
    layers: list[str]
    via_count: int
    pad_count: int
    routing_hint: str | None = None


class DiffPairCandidate(BaseModel):
    positive: str
    negative: str
    length_p_mm: float
    length_n_mm: float
    length_delta_mm: float


class NetStats(BaseModel):
    board_path: str
    net_count: int
    nets: list[NetStat]
    truncated: bool = False
    diff_pair_candidates: list[DiffPairCandidate] = Field(default_factory=list)


class SymbolHit(BaseModel):
    lib_id: str
    description: str | None = None
    keywords: str | None = None
    pin_count: int
    units: int
    default_footprint: str | None = None
    power: bool = False


class FootprintHit(BaseModel):
    lib_id: str
    description: str | None = None
    tags: str | None = None
    attr: str | None = Field(default=None, description="smd, through_hole, or flags such as exclude_from_bom.")
    pad_count: int
    width_mm: float | None = Field(default=None, description="Courtyard width.")
    height_mm: float | None = None


class LibSearchResult(BaseModel):
    query: str
    symbols: list[SymbolHit]
    footprints: list[FootprintHit]
    symbol_matches: int
    footprint_matches: int
    index_symbols: int
    index_footprints: int


class SymbolPinInfo(BaseModel):
    number: str
    name: str
    type: str
    unit: int
    hidden: bool = False


class SymbolInfo(BaseModel):
    lib_id: str
    library_path: str
    description: str | None = None
    keywords: str | None = None
    datasheet: str | None = None
    default_footprint: str | None = None
    fp_filters: list[str] = Field(default_factory=list)
    power: bool = False
    extends: str | None = None
    units: int
    pin_count: int
    pins: list[SymbolPinInfo]
    matching_footprints: list[str] = Field(default_factory=list, description="Footprints that satisfy the symbol's footprint filters.")


class FootprintPadInfo(BaseModel):
    number: str
    kind: str
    shape: str
    x_mm: float
    y_mm: float
    size_x_mm: float | None = None
    size_y_mm: float | None = None
    drill_mm: float | None = None
    layers: list[str] = Field(default_factory=list)


class FootprintInfo(BaseModel):
    lib_id: str
    path: str
    description: str | None = None
    tags: str | None = None
    attr: str | None = None
    pad_count: int
    smd_pads: int
    tht_pads: int
    width_mm: float | None = None
    height_mm: float | None = None
    model: str | None = None
    pads: list[FootprintPadInfo]


class LibIndexStatus(BaseModel):
    path: str
    symbols: int
    footprints: int
    libraries: int
    built_at: str | None = None
    rebuilt_libraries: int = 0
    seconds: float = 0.0


class ComponentPin(BaseModel):
    number: str
    name: str
    type: str
    x_mm: float
    y_mm: float


class SchematicComponent(BaseModel):
    ref: str
    lib_id: str
    value: str | None = None
    footprint: str | None = None
    uuid: str
    x_mm: float
    y_mm: float
    rotation: int
    mirror: str | None = None
    unit: int
    dnp: bool = False
    properties: dict[str, str] = Field(default_factory=dict)
    pins: list[ComponentPin] = Field(default_factory=list)


class SchematicComponents(BaseModel):
    schematic: str
    sheet_uuid: str
    instance_path: str
    total: int
    components: list[SchematicComponent]
    truncated: bool = False


class NetlistDelta(BaseModel):
    nets_added: list[str] = Field(default_factory=list)
    nets_removed: list[str] = Field(default_factory=list)
    nets_changed: list[str] = Field(default_factory=list, description="Nets whose set of pins changed.")
    components_added: list[str] = Field(default_factory=list)
    components_removed: list[str] = Field(default_factory=list)


class EditResult(BaseModel):
    changed: bool
    dry_run: bool
    file: str
    summary: str
    uuids: list[str] = Field(default_factory=list, description="Items created or affected.")
    pins: list[ComponentPin] = Field(default_factory=list, description="Pin positions of a placed symbol, to wire it.")
    snapshot: str | None = None
    sha256_before: str | None = None
    sha256_after: str | None = None
    bytes_before: int | None = None
    bytes_after: int | None = None
    erc: VerdictReport | None = None
    netlist_delta: NetlistDelta | None = None
    warnings: list[str] = Field(default_factory=list)


class AnnotateResult(BaseModel):
    changed: bool
    dry_run: bool
    file: str
    assignments: dict[str, str]
    snapshot: str | None = None
    warnings: list[str] = Field(default_factory=list)


class FootprintMove(BaseModel):
    """One move of a pcb_move_footprints batch; omitted fields keep their value."""

    ref: str
    x: float | None = Field(default=None, description="New X in mm (give x and y together).")
    y: float | None = None
    rotation: float | None = Field(default=None, description="Absolute rotation in degrees.")
    side: Literal["F.Cu", "B.Cu"] | None = Field(default=None, description="Flip to this side (live channel only).")


class MountingHole(BaseModel):
    x: float
    y: float
    drill: float = Field(gt=0, description="Hole diameter in mm.")
    pad: float = Field(default=0.0, ge=0, description="Copper pad diameter in mm; 0 or not above the drill gives a bare NPTH.")
    net: str | None = Field(default=None, description="Net of a plated hole, e.g. GND.")
    ref: str | None = Field(default=None, description="Reference; default the next free H<n>.")


class BoardEditResult(BaseModel):
    changed: bool
    dry_run: bool
    channel: Literal["ipc", "file"] = Field(description="ipc: live edit in KiCad as an undo step. file: the board file on disk, snapshotted.")
    board: str
    summary: str
    items: list[dict[str, Any]] = Field(default_factory=list, description="Items created or updated, as KiCad reports them after the change.")
    item_ids: list[str] = Field(default_factory=list)
    deleted: list[dict[str, str]] = Field(default_factory=list)
    snapshot: str | None = None
    sha256_before: str | None = None
    sha256_after: str | None = None
    warnings: list[str] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)


class ReviewFinding(BaseModel):
    check: str
    severity: Literal["error", "warning", "info"]
    message: str
    ref: str | None = None
    net: str | None = None
    sheet: str | None = None
    x_mm: float | None = None
    y_mm: float | None = None
    value: float | None = Field(default=None, description="The measured value, in the check's unit.")
    limit: float | None = Field(default=None, description="The limit it was compared against.")
    detail: str | None = None
    count: int = Field(default=1, description="How many identical findings this line stands for; position and detail are the first one's.")


class ReviewCheck(BaseModel):
    id: str
    name: str
    verdict: Literal["PASS", "WARN", "FAIL", "UNVERIFIED", "INFO", "BLOCKED"]
    summary: str
    findings: list[ReviewFinding] = Field(default_factory=list)
    truncated: bool = False
    evidence: str = Field(description="What the check looked at and how.")
    limit_source: str | None = Field(default=None, description="Where the limits come from.")
    data: dict[str, Any] = Field(default_factory=dict)


class ReviewReport(BaseModel):
    target: str
    kind: Literal["board", "schematic", "project"]
    fab: str | None = None
    verdict: Literal["PASS", "WARN", "FAIL"] = Field(description="Worst verdict of the checks that ran. Unverified checks are listed separately and never hidden.")
    counts: dict[str, int]
    checks: list[ReviewCheck]
    unverified: list[str] = Field(default_factory=list, description="Checks that could not run, with their reasons in the check summaries.")
    duration_s: float


class CapabilityRow(BaseModel):
    capability: str
    channel: Literal["cli", "ipc", "file", "builtin"]
    status: Literal["covered", "planned", "gap", "gui_only"]
    tool: str | None = None
    notes: str | None = None


class CapabilityMatrix(BaseModel):
    rows: list[CapabilityRow]
    covered: int
    planned: int
    gap: int
    gui_only: int


class DocInfo(BaseModel):
    id: str = Field(description="Stable id: first 12 hex digits of the file's SHA-256.")
    file: str = Field(description="Path relative to the documentation library.")
    path: str = Field(description="Path relative to the workspace.")
    title: str
    source_url: str | None = None
    fetched: str = Field(description="Date the file entered the library (YYYY-MM-DD).")
    size: int
    sha256: str
    content_type: str
    pages: int | None = None
    tags: list[str] = Field(default_factory=list)
    notes: str = ""


class DocSearchHit(BaseModel):
    doc: DocInfo
    matches: list[dict[str, Any]] = Field(default_factory=list, description="Page number and surrounding text for hits inside the document.")


class DocList(BaseModel):
    library: str = Field(description="The documentation library directory, relative to the workspace.")
    query: str | None = None
    documents: list[DocSearchHit]
    total: int


class DocText(BaseModel):
    doc: DocInfo
    pages: list[int]
    text: dict[int, str] = Field(description="Extracted text per page; empty for image-only pages, which doc_page can render.")
    matches: list[dict[str, Any]] = Field(default_factory=list)
    truncated: bool = False


class DocSection(BaseModel):
    title: str
    page: int
    kind: str = Field(description="bookmark, contents, heading, table or figure: where the entry was found.")


class DocSections(BaseModel):
    doc: DocInfo
    sections: list[DocSection]
    total: int = Field(description="How many the document has before the filter and the cap.")
    find: str | None = None


class DocFacts(BaseModel):
    part: str
    found: bool
    path: str | None = Field(default=None, description="The fact sheet, relative to the workspace.")
    section: str | None = None
    text: str = Field(default="", description="The sheet, or the one section asked for.")
    sections: list[str] = Field(default_factory=list, description="The sheet's headings, for a narrower question next time.")
    advice: str = Field(default="", description="When there is no sheet: how to write one, with the template.")


# --------------------------------------------------------------------------------------
# routing: differential pairs, impedance, stack-ups; parts catalogue
# --------------------------------------------------------------------------------------


class DiffPairReport(BaseModel):
    name: str = Field(description="The pair's name without the _P/_N suffix and without the sheet path.")
    p_net: str
    n_net: str
    netclass: str | None = None
    target_impedance_ohm: float | None = Field(default=None, description="From the interface rules matched on the name.")
    status: Literal["unrouted", "partial", "ok", "warn"]
    p_length_mm: float
    n_length_mm: float
    skew_mm: float
    skew_limit_mm: float | None = None
    p_vias: int = 0
    n_vias: int = 0
    coupled_fraction: float | None = Field(default=None, description="Share of the shorter half that runs parallel to the other at the class gap.")
    gap_target_mm: float | None = None
    gap_deviations: int = 0
    width_target_mm: float | None = None
    width_deviations: int = 0
    layers: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class RouteReport(BaseModel):
    board_path: str
    project_path: str | None = None
    pairs: list[DiffPairReport]
    unpaired: list[str] = Field(default_factory=list, description="Nets that look like half a pair but have no mate.")
    summary: dict[str, int]
    verdict: Literal["PASS", "WARN", "INFO", "EMPTY"]
    notes: list[str] = Field(default_factory=list)


class ImpedanceResult(BaseModel):
    stackup: str
    layer: str
    width_mm: float
    gap_mm: float | None = None
    dielectric_mm: float
    er: float
    er_effective: float
    single_ended_ohm: float
    differential_ohm: float | None = None
    table_match: dict[str, Any] | None = Field(default=None, description="The fab's own table entry when the geometry is one of its published ones.")
    method: str
    uncertainty: str
    source: str


class StackupInfo(BaseModel):
    name: str
    thickness_mm: float
    layers: list[dict[str, Any]]
    table: dict[str, dict[str, float]] = Field(description="Fab-published width (w) and gap (s) per target impedance in ohm; empty when the fab publishes none.")
    source: str
    presets: list[str] = Field(default_factory=list, description="Every preset name impedance_calc and stackup_info accept.")


class PairRouted(BaseModel):
    name: str
    status: Literal["routed", "failed", "skipped"]
    p_length_mm: float = 0.0
    n_length_mm: float = 0.0
    skew_mm: float = 0.0
    vias: int = 0
    layers: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class PairRouteReport(BaseModel):
    board_path: str
    routes_path: str = Field(description="JSON with the routed segments and vias by net, to apply to the board.")
    pairs: list[PairRouted]
    routed: int
    failed: int
    segments: int
    vias: int
    notes: list[str] = Field(default_factory=list)


class StitchReport(BaseModel):
    board_path: str
    routes_path: str
    stitched: int = Field(description="Surface-mount pads on plane nets that got a stub and a via.")
    skipped: list[str] = Field(default_factory=list, description="Pads with no clear spot for a via, with the reason; left to the autorouter.")
    segments: int
    vias: int
    rejected: list[str] = Field(default_factory=list, description="Pads (also in skipped) refused because no clear via spot lands on their plane: a cut-out, another net's zone, or no plane at all.")
    warnings: list[str] = Field(default_factory=list, description="E.g. unfilled zones (checked against outlines) or default design rules.")


class ExcludedNet(BaseModel):
    net: str
    reason: str


class AutorouteReport(BaseModel):
    board_path: str
    dsn_path: str
    ses_path: str
    routes_path: str
    seconds: float
    returncode: int
    session_segments: int
    session_vias: int
    session_nets: int
    segments: int = Field(description="Segments in the merged routes JSON.")
    vias: int
    nets: int
    log_tail: str = ""
    warnings: list[str] = Field(default_factory=list, description="Conditions that make the result less trustworthy, e.g. default design rules.")
    excluded_nets: list[ExcludedNet] = Field(default_factory=list, description="Nets left unrouted on purpose, with the reason; their copper went to the router protected.")
    ignored_classes: list[str] = Field(default_factory=list, description="DSN classes passed to FreeRouting's ignore list (-inc).")
    class_clearances: list[str] = Field(default_factory=list, description="class_class clearance rules taken from the .kicad_dru.")
    keepouts: list[str] = Field(default_factory=list, description="Keep-out entries emitted from rule areas and area rules.")
    dropped_segments: int = Field(0, description="Segments of excluded nets the router drew anyway (it ignored -inc) and that were dropped.")


class PartHit(BaseModel):
    lcsc: str
    mpn: str
    manufacturer: str
    package: str
    description: str
    stock: int
    basic: bool = Field(description="A JLCPCB basic part: no extended-part handling fee.")
    price_usd: float | None = None
    min_qty: int | None = None


class PartsSearch(BaseModel):
    keyword: str
    total: int
    hits: list[PartHit]
    source: str


class LibFetch(BaseModel):
    """What lib_fetch wrote for one LCSC code: names, ids, files, and the caveats that come with user-drawn data."""

    lcsc: str
    library: str  # the library base name and nickname: <library>.kicad_sym, <library>.pretty, <library>.3dshapes
    library_dir: str
    symbol: str | None
    symbol_id: str | None  # library:symbol, as a schematic references it
    footprint: str | None
    footprint_id: str | None
    model_step: str | None
    model_wrl: str | None
    pads: int | None  # pads in the footprint file; 0 is a warning
    pins: int | None
    files: list[str]  # written or updated, relative to library_dir
    warnings: list[str]
    source: str


JobState = Literal["queued", "running", "done", "failed", "lost", "unknown"]


class JobStatus(BaseModel):
    id: str
    tool: str | None = None
    state: JobState = Field(description="done: job_result has the result. lost: the process that ran it ended without a result. unknown: no such job.")
    elapsed_s: float | None = None
    progress: dict[str, Any] = Field(default_factory=dict, description="FreeRouting: pass, phase, unrouted, violations from its log.")
    log_tail: list[str] = Field(default_factory=list, description="The last lines of the job's output.")
    error: str | None = None
    hint: str | None = None


class JobResult(BaseModel):
    id: str
    tool: str | None = None
    state: JobState
    elapsed_s: float | None = None
    result: Any = Field(default=None, description="The tool's normal result once done: its structured output, or {'text': [...]} for a tool without one.")
    hint: str | None = None
