# Tools

Every path argument is absolute or relative to the workspace root and must resolve inside it.
Read-only tools never touch a design file. Artifact tools create new files (reports, exports,
renders) and never modify a design file either. Design edits require `KICAD_LAYER_MODE=write`.

Two tiers. `core`, the default, is everything that reads, checks, exports, renders, reviews or
documents: what a design-as-data project needs from the server. `full` (`KICAD_LAYER_TOOLS=full`)
adds the schematic and board edit tools and the frozen routers.

| Tool | Tier | Channel | Kind | What it does |
|---|---|---|---|---|
| `kicad_doctor` | core | cli, ipc | read-only | Which process answers, which kicad-cli was found, whether KiCad's API is reachable and what is open, and what to do about problems |
| `capabilities` | core | builtin | read-only | The capability matrix: covered, planned, gap, gui_only |
| `project_open` | core | file | read-only | Locate a project; root schematic, board, sheets, variables, netclasses, format versions, lock files |
| `run_erc` | core | cli | read-only | Electrical rules check of the whole hierarchy with a verdict and UUID-keyed findings; `summary` for counts and the worst ones |
| `run_drc` | core | cli | read-only | Design rules check counting violations, unconnected items and schematic parity; `summary` for counts per type and rule, the worst violations and the unconnected pairs |
| `sch_netlist` | core | cli | read-only | Resolved nets, nodes, components and sheets from the root sheet, cached by content |
| `sch_trace` | core | cli | read-only | Each pin of one component: its net and everything else on it |
| `export_bom` | core | cli | artifacts | Bill of materials CSV plus parsed rows |
| `export_fab` | core | cli | artifacts | Gerbers, drill, position, optional STEP and PDF, verified on disk with hashes |
| `render_board` | core | cli | artifacts | 3D PNG of the board, returned as an image |
| `sch_render` | core | cli | artifacts | Every sheet as SVG, or one multi-page PDF |
| `pcb_summary` | core | ipc | read-only | The open board: title block, layers, outline size, counts, stackup, netclass rules |
| `pcb_list_items` | core | ipc | read-only | Footprints, pads, tracks, vias, zones, nets or text of the open board, filtered and paged |
| `pcb_net_stats` | core | ipc | read-only | Per-net length, widths, layers, vias, pads, unrouted hints and differential-pair candidates |
| `lib_search` | core | file | read-only | Full-text search over every symbol and footprint library in KiCad's tables |
| `sym_info` | core | file | read-only | One library symbol: pins with types, units, filters, matching footprints, datasheet |
| `fp_info` | core | file | read-only | One library footprint: pads with geometry, mount type, courtyard size, 3D model |
| `lib_index` | core | file | read-only | Build or refresh the library index and report its size and age |
| `lib_fetch` | core | builtin | writes artifacts | Symbol, footprint and 3D model for an LCSC code from EasyEDA's data, into a library next to the project; a draft to check against the datasheet |
| `sch_list_components` | core | file | read-only | Every placed symbol on a sheet with position, rotation, properties, optional pin coordinates |
| `sch_get_symbol` | core | file | read-only | One placed symbol with every pin's sheet position |
| `sch_set_property` | full | file | design write | Change one property of a symbol, byte-lossless elsewhere |
| `sch_add_component` | full | file | design write | Place a library symbol, embed it in the sheet cache, return pin positions |
| `sch_wire` | full | file | design write | Draw a wire through points, adding junctions where its ends meet wires |
| `sch_label` | full | file | design write | Local, global or hierarchical net label at a point |
| `sch_mark` | full | file | design write | Junction dot or no-connect flag at a point |
| `sch_delete` | full | file | design write | Remove one item by uuid |
| `sch_annotate` | full | file | design write | Number every `R?`-style reference on a sheet |
| `pcb_place_footprint` | full | ipc or file | design write | Place a library footprint, live as an undo step or into the file |
| `pcb_move_footprint` | full | ipc or file | design write | Move, rotate or flip a footprint by reference |
| `pcb_add_track` | full | ipc or file | design write | Track segments through points on one layer |
| `pcb_add_via` | full | ipc or file | design write | A through via on a net |
| `pcb_add_zone` | full | ipc or file | design write | A copper pour on a net |
| `pcb_refill_zones` | full | ipc or file | design write | Refill zones live, or with kicad-cli on a closed board; refuses without the board's own `.kicad_pro` unless `allow_default_rules` |
| `pcb_delete_items` | full | ipc or file | design write | Delete items by id, with their kinds checked first |
| `pcb_save` | full | ipc | design write | Ask KiCad to save the open board |
| `pcb_move_footprints` | full | ipc or file | design write | Move, rotate or flip several footprints in one write or one undo step |
| `pcb_set_outline` | full | file | design write | The board outline on Edge.Cuts from a rectangle or polygon, corners rounded with tangent arcs |
| `pcb_add_mounting_holes` | full | file | design write | Mounting holes as board-only footprints, plated on a net or bare NPTH |
| `review_board` | core | cli | read-only | DRC, unrouted, zone fills, off-board parts, fab limits, power track widths, stitching, decoupling |
| `review_schematic` | core | cli | read-only | ERC, footprints, values, annotation, power sources, decoupling, BOM summary, SPICE status |
| `review_project` | core | cli | read-only | Both reviews in one report with one verdict |
| `doc_fetch` | core | builtin | writes artifacts | Download a datasheet or reference document into the indexed documentation library |
| `doc_import` | core | builtin | writes artifacts | Index a document already on disk, e.g. saved from a browser |
| `doc_list` | core | builtin | read-only | List or search the documentation library, including page text |
| `doc_text` | core | builtin | read-only | Text of a document per page, or regex search returning the matches alone (page and one window each) |
| `doc_page` | core | builtin | writes artifacts | Render a page to an image for drawings, pinouts and tables |
| `doc_sections` | core | builtin | read-only | The document's index: bookmarks, contents, headings, table and figure captions, each with its page |
| `doc_facts` | core | builtin | read-only | A part's fact sheet, or one section of it: pins, limits, values, circuit, package, every row with its page |
| `route_check` | core | file | read-only | Every differential pair on a board: lengths, skew against the interface limit, coupled share, class gap and width, layer changes |
| `impedance_calc` | core | builtin | read-only | Closed-form impedance of a trace or pair on a stack-up preset, with the fab's published number when the geometry matches |
| `stackup_info` | core | builtin | read-only | A stack-up preset: layers, permittivity, published impedance geometries |
| `parts_search` | core | builtin | read-only | JLCPCB assembly catalogue: LCSC code, stock, basic part flag, price |
| `route_pairs` | full | file | writes artifacts | Route the differential pairs as coupled pairs (escapes, heading-aware search, crossovers, tuning) into a routes JSON |
| `stitch_planes` | full | file | writes artifacts | A stub and via from every surface-mount pad on a plane net to its plane, only where the plane has copper under the via, into the routes JSON |
| `autoroute` | full | builtin | writes artifacts | FreeRouting for the rest, existing copper protected; session merged into the routes JSON |
| `job_start` | core | builtin | runs the tool it names | Run `autoroute`, `run_drc`, `run_erc`, `pcb_refill_zones`, `render_board` or `review_board` in the background; returns a job id at once |
| `job_status` | core | builtin | read-only | A job's state, elapsed time, output tail and FreeRouting's pass, unrouted and violation counts; `wait_s` blocks up to 50 s |
| `job_result` | core | builtin | read-only | The finished job's normal tool result; a failed job raises the tool's error |

## Review reports

Each review is a list of checks. A check reports `PASS`, `WARN`, `FAIL`, `INFO` or
`UNVERIFIED`, its findings with locations and the measured value against the limit, the
evidence it used, and where its limits come from. The report's verdict is the worst of the
checks that ran; checks that could not run are listed under `unverified` with their reason
and are never dropped. The checks and their limits are documented in
[review-checks.md](review-checks.md).

## Board writes

`pcb_*` write tools pick a channel per call. With the board open in KiCad's PCB Editor
(`channel: auto` or `ipc`) each operation is one commit in KiCad: it appears in the undo
history, and the result is read back from KiCad rather than echoed from the request. The
change lives in KiCad's memory until `pcb_save`; kicad-cli tools read the file on disk.
With the board closed (`channel: auto` or `file`) the board file is edited losslessly with
the same snapshot, lock-file and conflict rules as schematics, and zones stay unfilled
until `pcb_refill_zones` runs kicad-cli. A board this process has seen live over the API is
never edited on disk, because KiCad may still hold unsaved changes to it.

`pcb_move_footprints` takes a list of moves (`ref`, and any of `x`/`y`, `rotation`, `side`) and applies
them together: one file write with one snapshot, or one KiCad commit with a single undo step. Every
reference is checked before anything changes. Moving a footprint through the file keeps the absolute
angles of its pads and texts and carries along the zones inside it (keep-outs, antenna clearances), which
KiCad stores in board coordinates; `pcb_move_footprint` does the same. Flipping needs the live channel.

`pcb_set_outline` and `pcb_add_mounting_holes` work on the file only (the board closed in KiCad).
The outline is a rectangle `[x0, y0, x1, y1]` or a polygon; with `corner_radius_mm` every corner becomes a
tangent arc written the KiCad 10 way, `(gr_arc (start) (mid) (end))`, and the lines are shortened to meet
it, so the outline stays one closed chain (a radius that does not fit an edge is refused). `replace`
(default) removes the board's own Edge.Cuts drawings first. A mounting hole is an inline footprint
`MountingHole:MountingHole_<drill>mm[_Pad]` with `(attr board_only exclude_from_pos_files exclude_from_bom)`:
plated, with pad number 1 on `net`, when `pad` exceeds the drill, otherwise a bare NPTH. References default
to the next free `H<n>`.

KiCad finds a board's rules by the board's name only: net classes and constraints in
`<board>.kicad_pro`, custom rules in `<board>.kicad_dru`. A board copied or renamed without them is
checked and filled against KiCad's defaults without a word. So `run_drc` and `autoroute` (when
`project_path` is left to its default) add a warning (`no project file next to the board: KiCad used
default design rules`), and so does a `.kicad_dru` in the board's folder under another name (the
copy-with-rename mistake: its rules exist and are not applied). `pcb_refill_zones` refuses with
`PROJECT_NOT_FOUND` in both cases, since a fill against the wrong clearances is saved into the board;
`allow_default_rules: true` fills anyway and keeps the warning.

## Design writes

The `sch_*` write tools need `KICAD_LAYER_MODE=write`; in read-only mode they refuse with
`READ_ONLY_MODE`. Each one edits the sheet's concrete syntax tree and writes back only the
nodes it changed; a file saved without changes is byte-identical. Before writing, the
previous file is copied to `.kicad-layer/snapshots/` next to the project and the write is
atomic. While KiCad holds the sheet's lock file the write is refused with
`SCHEMATIC_LOCKED` unless `force` is given, and if the file changed on disk since it was
read the write is refused with `EDIT_CONFLICT`. After a successful write the tool runs ERC
on the project's root sheet and compares the netlist with the state before the edit, so
the result says exactly which nets and components changed. Every `dry_run` reports the
same summary without touching the file.

## The library index

`lib_search` reads KiCad's global and project library tables, parses every `.kicad_sym` and
`.kicad_mod` they point at, and stores names, descriptions, keywords, pins and pad geometry
in a SQLite database with full-text search under the cache directory. The first build takes
about half a minute for the stock libraries; afterwards only libraries whose files changed
are re-parsed. Derived symbols (`extends`) are indexed with their parent's pins.

Pass `project_path` to any library tool to include that project's own libraries, the ones
its `sym-lib-table` and `fp-lib-table` declare next to the `.kicad_pro`. They are indexed
under the project as a separate scope and shadow global libraries with the same name,
exactly as KiCad resolves them. KiCad's stock libraries have no Compute Module 5 symbol
and no M.2 socket, for example; a project that carries the CM5 IO library provides them.

`lib_fetch` brings a part the stock libraries lack from EasyEDA, JLCPCB's own design tool, where
nearly every part in the assembly catalogue has a symbol, a footprint and a 3D model. It runs
`easyeda2kicad` (the `parts` extra) as a subprocess for one LCSC code into `<project>/lib/<lib_name>.kicad_sym`,
`.pretty` and `.3dshapes`, and reports the names, the ids (`lib_name:footprint`), the pad and pin counts
and the files written; the project registers `lib_name` in its two library tables. The footprint comes
out of easyeda2kicad in KiCad's old `(module ...)` form, so `lib_fetch` runs `kicad-cli fp upgrade` on it
(pads and 3D path checked unchanged); without kicad-cli it stays old-style and the result says so. The models are
drawn by users and JLCPCB staff, so the result is a draft: `fp_info` against the datasheet drawing
(`doc_page`) before a board relies on it. Without the package, or for a code that has no EasyEDA
model, `LIB_FETCH_FAILED` says so.

## The IPC channel

`pcb_*` tools talk to the board open in KiCad's PCB Editor through KiCad's official API.
They need KiCad running, the API enabled (Preferences, Plugins, "Enable KiCad API") and the
PCB Editor window open. Every failure is classified: `KICAD_NOT_RUNNING` and
`KICAD_API_DISABLED` mean the request never reached KiCad; `BOARD_NOT_OPEN`, `IPC_BUSY` and
`IPC_REJECTED` mean KiCad answered and said no. `IPC_BUSY` is retryable after the user closes
a dialog or finishes an interactive tool.

A crash shows up as a request that never gets its answer. Every transport failure records the KiCad
process ids before the call (as of the connection, or per call with logging on) and after it, in the
server log and in the error; a timeout during which KiCad's process disappeared comes back as
`KICAD_NOT_RUNNING` ("KiCad exited while the request was pending") instead of `IPC_BUSY`. With
`KICAD_LAYER_IPC_LOG=1` every request (label, item count, duration, outcome, pids) goes to a rotating
`<cache>/logs/ipc.log`, the file to attach to a KiCad bug report.

When a write with `channel: auto` cannot go live because KiCad is gone (unreachable and no KiCad
process runs), or a live write fails that way mid-call, the edit falls back to the file channel, with a
warning that whatever KiCad had not saved is lost. It never does while a running KiCad holds the board's
lock file. On the file channel a lock whose owner is gone (a pid that no longer runs, or this host with
no KiCad running; a lock from another host always counts as held) is skipped with `force`; without it
the write is refused with `EDIT_CONFLICT` and that hint. At start the server removes such orphaned
`~*.lck` files under the workspace (four levels deep) and logs each one.

## The documentation library

Datasheets, application notes and reference designs live in one library, `research/references`
under the workspace by default (`KICAD_LAYER_DOCS_DIR` overrides it), with `index.json` recording
each document's source URL, fetch date, size, SHA-256, page count, tags and notes. The workflow:

1. Find the URL with web search; manufacturer sites and distributor mirrors usually both host the
   PDF.
2. `doc_fetch` downloads it with browser-grade headers, follows redirects and a single PDF link on a
   viewer page, and indexes it. When the site answers with a scripted download portal or refuses the
   plain client, the browser tier takes over (`browser="auto"`, the default): headless Chromium
   through Playwright loads the page, waits for scripts, and takes the PDF the page offers, whether
   it is the response itself, a download that starts on load, or the best "download"/".pdf" link or
   button. This is deterministic code, not an agent driving a browser, so it costs no model tokens.
   Install it with `pip install "kicad-mcp-layer[browser]"` and `python -m playwright install chromium`.
   A site that still refuses (login wall, bot check) raises `DOC_FETCH_FAILED` or `DOC_NOT_PDF` with
   the last fallback spelled out: open the URL in a browser, save the file, `doc_import` it.
   Provenance is kept either way; browser fetches say so in the entry's notes.
3. When both tiers fail, escalate before asking a person: (a) the document is often mirrored on a
   host that does not object (a distributor's datasheet store, the manufacturer's product portal
   rather than its marketing site: Raspberry Pi's `pip.raspberrypi.com` serves what
   `raspberrypi.com` refuses); (b) an HTML page that returns 403 to scripts can still be read
   through the client's own web tools to find the file's real URL; (c) as a last resort the agent
   drives a real browser session to the page, takes the download, and `doc_import`s the file from
   the browser's download folder. Only when all of that fails is the user asked to save the file.
4. `doc_text` returns the text per page and can search it with a regular expression; `doc_page`
   renders a page to an image for pinouts, package drawings and tables, which text extraction
   cannot carry. `doc_list` searches titles, tags, notes and the text of every document.
5. Answering a question costs the rows, not the pages. `doc_facts` is asked first: a part's fact
   sheet in `research/parts/<MPN>.md` (`KICAD_LAYER_PARTS_DIR` overrides the folder) holds its pins,
   limits, values, recommended circuit and package, every row with its page, written once from the
   datasheet and checked against the rendered pages; one section of it is a hundred tokens. Without a
   sheet, `doc_sections` gives the document's index (bookmarks, the contents page, headings, table and
   figure captions, each with its page), `doc_text` with those pages reads them, and `doc_page` shows a
   table or drawing; a subagent writes the sheet from those and the answer comes from the sheet from
   then on. `python -m kicad_layer.docs` offers the same as a command line for scripts and subagents.

Extracted text and rendered pages are cached in `.text/` and `.pages/` inside the library and keyed
by content hash, so re-fetching an unchanged document costs nothing.

## Routing checks

`route_check` finds differential pairs by name (`X_P`/`X_N`, `X_DP`/`X_DN`, `X+`/`X-`), keeping
the sheet path on the net and stripping it from the pair's name. For each pair it sums the track
segments of both halves, adds 1.6 mm per via (the board thickness, as KiCad does), and reports the
skew against the interface's limit. The default limits come from the Compute Module 5 datasheet and
are ordinary for these interfaces: Ethernet and MIPI 100 ohm within 0.15 mm, PCIe and USB 3.0
90 ohm within 0.1 mm, USB 2.0 90 ohm within 0.15 mm; `skew_limit_mm` overrides them. Segments of
the two halves that run parallel on one layer are compared with the net class's pair gap and
width from the `.kicad_pro`, and the share of the shorter half that runs coupled is reported,
because an uncoupled stretch is where the impedance drifts. A pair whose halves change layers a
different number of times is flagged. `review_board` includes the same check as `diff_pairs`.

`impedance_calc` estimates an outer-layer trace or pair on a stack-up preset with Hammerstad and
Jensen's microstrip formulas and the usual coupled-line correction. Closed forms run about ten
percent above a field solver for tightly coupled pairs, so when the geometry matches an entry of
the fab's published table the result names that entry, and that is the number to design to.
`stackup_info` lists the presets (`presets` in the result): JLCPCB's 4-layer JLC04161H-7628 with
its 50, 90 and 100 ohm geometries (the default); PCBWay's standard 4-layer 1.6 mm board
(`pcbway-4l-1.6mm`: 7628 prepreg 0.1855 mm after lamination, Dk 4.74, 1 oz copper), for which PCBWay
publishes no width table, so every number there is a closed-form estimate; and AISLER's 4-layer
1.6 mm board (`aisler-4l-1.6mm`, alias `aisler`: two 1080 prepregs of 0.138 mm together, Dk 4.3,
35 um copper) with AISLER's published 50, 90 and 100 ohm geometries. The closed form runs about
twelve percent under AISLER's table, which is why the table wins when the geometry matches.

`parts_search` asks JLCPCB's assembly catalogue for a part number or a value and package and
returns LCSC codes, stock, the basic-part flag and prices. It uses the endpoint behind
jlcpcb.com/parts, which is undocumented; `PARTS_FETCH_FAILED` means the endpoint changed or the
network is down, never that the part does not exist.

## Routing

Routing writes copper as a **routes JSON** (segments and vias by net) next to the board, never into
the board file: a design-as-code project re-applies the JSON when it generates the board, and the
copper of nets whose pads moved since is dropped there. Three tools run in order.

`route_pairs` routes the differential pairs the net classes describe. Every pair's escape is planned
first: a straight stub along each pad's long axis on the side whose corridor clears the footprint's
other pads (a stub may squeeze past a staggered row when both tracks fit), converging to the class
pitch; all escapes are reserved as obstacles before anything is routed. The centreline is then found
by A* on a 0.2 mm grid with the heading in the state (45 degrees per step, two straight cells between
turns, a small cost per turn, first step along the escape, last step along the far end's approach),
starting and ending on the pad layers so no via pair is needed at the escapes; a layer change costs
about 5 mm and needs room for a via pair. The path is string-pulled without sharp turns and offset
into P and N with mitred corners. When P would arrive on the wrong side of N the pair keeps its
polarity by crossing over: P goes under N on the other layer through two vias on the longest straight
run with room for them. Tuning bumps on the shorter half's straight runs, leaning away from the
partner and checked against the grid, bring the skew inside the interface limit where room allows;
the report gives the residual. Pairs whose halves interleave with another pair's in a through-hole
field (a MagJack) are better left to the autorouter with `exclude`.

`stitch_planes` puts a stub and a via from every surface-mount pad on a plane net down to its plane,
trying just past the pad end along the pad's long axis first and then across it; connectors keep
their vias inside, between the pin rows, so the signal escapes stay free, small parts put them
outside. Every candidate is checked against the other pads (an exposed pad's own paste windows
excepted), the routes handed in, the vias placed so far, keep-outs, plated holes and the board edge.
A candidate must also land on its plane: on a layer other than the pad's, the net's plane has to cover
the via's disc grown by the clearance. With the zones filled in the file the fill is the test (a cut-out,
a thermal gap or another net's island in the plane layer is seen as KiCad filled it); unfilled, the zone
outline is, minus the outlines of other nets' zones of the same or higher priority on that layer and minus
keep-outs that forbid pours, and the result warns `zones are unfilled; fill first for exact results`.
Keep-outs that forbid vias refuse a via on any layer. `plane_layers` (layer to net, as for `autoroute`)
limits a net's plane to those layers; a plane layer with no zone of its net is taken as solid, with a
warning. A net of `plane_nets` without a plane anywhere is refused unless it is listed in `fanout_nets`,
which gives its pads a fan-out via for the autorouter. Pads refused for want of plane copper are listed
in `rejected` (and in `skipped`) with the reason.

`autoroute` exports the board plus the routes JSON as a Specctra DSN in the dialect KiCad writes
(micrometres, protected wiring, planes on the plane layers, keep-outs for holes), runs FreeRouting
headless (`tools/freerouting*.jar` with the Java in `tools/jre`, or `KICAD_LAYER_FREEROUTING` and
`KICAD_LAYER_JAVA`), parses the session file and merges it over the routes JSON. FreeRouting routes
pairs as single nets, so run `route_pairs` first.

A DSN carries net classes (one width, one clearance each), class-to-class clearances and keep-outs, and
nothing else; FreeRouting routes blind through every other rule. So the export sorts the board's rules
first and the result says what it decided:

* Rule areas (keep-out zones, on the board or inside a footprint) become per-layer DSN keep-outs: tracks
  not allowed gives a `keepout` (which also stops vias), vias not allowed a `via_keepout`, footprints not
  allowed a `place_keepout`. A `.kicad_dru` rule that disallows vias or tracks inside a named area
  (`insideArea`, `enclosedByArea`, `intersectsArea`) with no net in its condition does the same for that
  area. `keepouts` lists them.
* Plain two-class rules (`A.NetClass == 'X' && B.NetClass == 'Y'`, `... && B.NetClass != 'X'` for X against
  every other class, `A.NetClass == 'X'` alone, `hasNetclass` as a synonym) with a clearance, physical
  clearance or creepage minimum become `class_class` clearances (creepage and physical clearance as a
  straight clearance, which is stricter). Anything else (an OR, a net name, an item type, an area) is
  skipped with a warning. `class_clearances` lists what was emitted.
* `exclude_nets` (names or wildcards) and `exclude_classes` are not routed. With `auto_exclude_ruled_nets`
  (default on) neither are the nets a rule names by class or name when the rule has a constraint the DSN
  cannot carry (`disallow`, `creepage`, `physical_clearance`, `physical_hole_clearance`) or a condition on
  an area, footprint or courtyard, nor nets whose class or `track_width` rule is wider than 2 mm and that
  have a zone (a pour, not a track). `force_nets` routes a net anyway. An excluded net stays in the DSN
  with its pins and copper, protected, in a class `<class>_excluded` that goes to FreeRouting's ignore
  list (`-inc`), so class clearances still apply to it; the filled pour of an excluded net is a keep-out,
  an unfilled one only an outline (refill first). FreeRouting headless (2.4.1 included) may route an
  excluded net anyway; whatever it draws on one is always dropped from the session (`dropped_segments`,
  and a warning naming the nets).
  `excluded_nets` gives each net with its reason: route those by hand and check with `run_drc`.

## Background jobs

An MCP client gives a tool call about a minute. FreeRouting on a real board, DRC or a zone refill on a
couple of hundred parts, and a high-quality render take longer: the client gives up while the work goes
on unseen. `job_start` runs one of those tools (`autoroute`, `run_drc`, `run_erc`, `pcb_refill_zones`,
`render_board`, `review_board`) in a worker process of its own with the arguments a direct call takes, checked
the same way, and answers at once with a job id. The tool must be in this server's tier (`autoroute` and
`pcb_refill_zones` need `full`) and keeps its own rules (`pcb_refill_zones` still needs write mode).
`job_status` gives the state (`queued`, `running`, `done`, `failed`), the seconds so far, the last lines of
output (the kicad-cli command, FreeRouting's log) and, for `autoroute`, `progress` with FreeRouting's
`pass`, `phase`, `unrouted` and `violations` read from its log; `wait_s` (up to 50 s) waits for the end
before answering, which saves polls. `job_result` returns the tool's normal result (its structured output;
for `render_board` the text with the PNG path, to be read as an image) or, for a failed job, raises the
tool's error with its code (`JOB_FAILED` when it had none).

A call returning never stops the work, and a subprocess is never killed for it. The worker is detached
(`python -m kicad_layer.jobs worker <dir>`: its own session on POSIX; on Windows a new process group with a
hidden console, out of the host's job object when the host allows it), so an MCP host that reconnects and
restarts the server does not stop the job, and it still runs the tool's post-processing (for `autoroute`,
reading the session and writing the routes JSON). Everything about a job is in `<cache>/jobs/<id>/`:
`status.json` (state, worker pid, heartbeat, progress, error), `log.txt` and `result.json`. Any server
process reads those, so `job_status` and `job_result` answer for a job an earlier server started. A job
whose worker ended without a result is `lost` (the error quotes the worker's stderr), an id never seen is
`unknown`. When the worker cannot be spawned, or with `KICAD_LAYER_JOBS=thread`, the job runs in a thread
of the server as before and is `lost` if the server goes away. FreeRouting has no save-and-stop command
when headless: at `autoroute`'s `timeout_s` the process is asked to terminate, killed after fifteen
seconds, and a session file it wrote before that is still parsed and merged (the log tail says so);
without one the call fails with `ROUTER_FAILED` as before.

## Verdicts

`run_erc` and `run_drc` return one of:

| Verdict | Meaning |
|---|---|
| `PASS` | The check ran and found nothing |
| `WARN` | Warnings only |
| `FAIL` | At least one error, unconnected item or parity problem |
| `UNVERIFIED` | kicad-cli reported success but wrote no report. Nothing can be claimed. |
| `BLOCKED` | kicad-cli could not run the check (bad file, bad arguments) |
| `EMPTY` | Nothing to check |

The findings list is capped at 200 (`truncated: true`, the full list in `report_path`). A big board's
DRC runs to hundreds of kilobytes, more than an MCP client takes, so ask it with `summary: true` first:
`findings` is then empty and `summary` holds the active findings per KiCad type (`by_type`), per constraint
named in the description (`by_rule`: `rule:<name>` for a custom rule, `netclass:<name>`, `board` for the
board setup), per severity, the `top` (default 20) worst violations sorted by deficit (required minus actual
for a minimum, actual minus required for a maximum; findings without a measure follow in report order), the
unconnected count and up to `top` unconnected pairs. `report_path` is the kicad-cli JSON to read for the rest.

## Errors

Anticipated failures come back as `is_error` results whose text starts with a stable code:
`WORKSPACE_VIOLATION`, `FILE_NOT_FOUND`, `WRONG_FILE_TYPE`, `READ_ONLY_MODE`,
`KICAD_CLI_NOT_FOUND`, `KICAD_CLI_FAILED`, `KICAD_CLI_TIMEOUT`, `KICAD_NOT_RUNNING`,
`KICAD_API_DISABLED`, `BOARD_NOT_OPEN`, `PROJECT_NOT_FOUND`, `NOT_FOUND_IN_DESIGN`,
`INVALID_ARGUMENT`, `IPC_BUSY`, `IPC_REJECTED`, `DOC_FETCH_FAILED`, `DOC_NOT_PDF`, `PARTS_FETCH_FAILED`,
`LIB_FETCH_FAILED`, `JOB_FAILED`. Each carries
a hint saying what to do next.
