# Changelog

## Unreleased

Field findings from the notch_board (`docs/field-findings.md`): #1 to #19 are fixed below, except BOM
variants (#13) and the CM5 catalogue in `design/catalog.py` (#15); #20 is open.

- `run_drc` and `run_erc` take `summary` (and `top`): counts per type, per rule or net class named in the
  description and per severity, the worst violations by deficit, the unconnected count and pairs, and the
  report path, instead of every finding. The full list is capped at 200 findings (was 400) with a hint.
- `run_drc`, `pcb_refill_zones` and `autoroute` warn when the board's own `<name>.kicad_pro` is missing or its
  custom rules sit under another name (`<other>.kicad_dru`): kicad-cli then uses KiCad's default rules.
  `pcb_refill_zones` refuses with `PROJECT_NOT_FOUND` unless `allow_default_rules` is set.
- `stitch_planes` only places a via where the net's plane has copper under it on another layer: the zone
  fill when the board is filled, else the outline minus other nets' zones and pour keep-outs (with a warning
  to fill first); via keep-outs are respected. New `plane_layers` and `fanout_nets`; `rejected` lists the
  pads refused and why. A plane-less net in `plane_nets` is now refused by the tool unless in `fanout_nets`.
- Background jobs: `job_start` runs `autoroute`, `run_drc`, `run_erc`, `pcb_refill_zones`, `render_board` or
  `review_board` and returns a job id at once; `job_status` gives state, elapsed time, output tail and
  FreeRouting's pass, unrouted and violation counts (`wait_s` to block briefly); `job_result` the tool's
  normal result. New error code `JOB_FAILED`. Each job runs in a detached worker process (`python -m
  kicad_layer.jobs worker`, its own session on POSIX, a hidden-console process group on Windows) that calls
  the tool through a server of the same tier (`tools.run_job_tool`) and writes `status.json`, `log.txt` and
  `result.json` under `<cache>/jobs/<id>/`, so a job survives a restart of the server: `job_status` and
  `job_result` of a new server read it, and the tool's post-processing (autoroute's routes JSON) runs to the
  end. `lost` means the worker ended without a result. The in-server thread is the fallback
  (`KICAD_LAYER_JOBS=thread`, or when spawning fails). A pydantic result (a `run_drc` summary included)
  round-trips through `result.json` (field finding #19).
- FreeRouting's output is streamed (visible in a job) and, at the timeout, the process is terminated and a
  session file it wrote is still used instead of failing outright.
- `autoroute` no longer routes blind through rules the DSN cannot carry: rule areas become per-layer
  `keepout`/`via_keepout`/`place_keepout` entries (footprint keep-outs included), plain two-class clearance
  rules of the `.kicad_dru` become `class_class` rules, and nets named by creepage, physical-clearance,
  disallow or area-conditioned rules, or wide pour nets with a zone, are excluded (`auto_exclude_ruled_nets`,
  default on; `force_nets` overrides) with their copper protected. New `exclude_nets` and `exclude_classes`;
  the report lists `excluded_nets` with reasons, `class_clearances`, `keepouts` and `dropped_segments`.
  FreeRouting headless (2.4.1 included) may route excluded nets despite `-inc`, so its copper on them is
  always dropped; the warning names those nets and no longer blames FreeRouting before 2.4 (field finding #17).
- `pcb_move_footprints`: a batch of moves, rotations and flips in one file write or one KiCad commit, with
  each reference before and after. Moving a footprint through the file (this tool and `pcb_move_footprint`)
  now carries the zones inside it, which KiCad stores in board coordinates; a footprint's own keep-out used
  to stay behind.
- `pcb_set_outline` (rectangle or polygon on Edge.Cuts, corners as KiCad 10 three-point arcs) and
  `pcb_add_mounting_holes` (inline board-only footprints, plated on a net or bare NPTH), file channel only.
- IPC crash handling: `KICAD_LAYER_IPC_LOG=1` logs every request (label, item count, duration, outcome) to a
  rotating `<cache>/logs/ipc.log`; transport failures record KiCad's process ids before and after, and a
  timeout during which KiCad exited is reported as `KICAD_NOT_RUNNING`, not `IPC_BUSY`. Board writes with
  `channel: auto` fall back to the file when KiCad is gone and no living process holds the lock; `force`
  now writes past a lock whose owner is dead (never one a running KiCad holds); the server removes
  orphaned lock files at start. The `IPC_REJECTED` hint for an unhandled request names a closed, busy or
  crashed editor instead of KiCad 11.
- The `.kicad_dru` reader moved from `routers/dru.py` into the core as `kicad_layer.dru` (the routers' module
  re-exports it), with `evaluate` for conditions made of net-class, net-name and item-type tests.
  `design.copper.Rules.load` reads the project's `.kicad_dru`: `disallowed("via"|"track", net)`, custom
  clearances between two nets in `between`, and `track` at least the board's `min_track_width` and any custom
  `track_width` minimum. `design/stubs.py` puts no via or track on a net a `disallow` rule names (a condition
  it cannot evaluate, an area for instance, counts as applying), `inspect clear` and `clear-via` answer NO
  with the rule, and `fit_placements` keeps the custom clearances (field finding #16).
- Design build without kicad-cli: `build.py --offline`, and automatically when kicad-cli is not found, writes
  the sheets, the lint verdict, the `.kicad_pro`/`.kicad_dru` and the board, the board on a netlist
  synthesized from the sheets' descriptions (new `design/offline.py`, `synth_netlist`); ERC, the netlist
  gates, DRC, `--route-stubs`, `--review` and the render are reported UNVERIFIED and skipped, with a warning.
  Projects no longer need their own offline path (field finding #9). The synthesized netlist resolves a module
  sheet's pins as `build_module_sheet` does (`module_sheet.pin_roles`: `"J2.37"` or `ref=`, `gnd_pins` and
  ground names), gives a sheet-to-sheet signal `/NAME` on both sheets, carries a DNP part's `dnp` property,
  and lists the parts of a hand-drawn sheet or a placeholder without nets.
- `build.py --route-stubs` runs on a board that has no `routing/routes.json` yet (it starts from empty copper
  and creates the file) and says so when the rebuilt board carries no saved copper because `Board.routes`
  points elsewhere (field finding #10).
- `build.py --out` copies `sym-lib-table`, `fp-lib-table` and `lib/` only when the project has them (field
  finding #11).
- New `design.build.symbol_paths(root, children, root_file)`: the instance path of every placed symbol; a
  multi-unit symbol takes the path of its lowest unit, as KiCad does, instead of the last unit placed, so the
  board footprint of a dual opamp carries unit 1's path whatever order the units were drawn in (field
  finding #3).
- `design.project.Rules` takes `rule_severities` (DRC check to error, warning or ignore), merged over the
  template's in the `.kicad_pro`. `Rules.template` may be None: the package ships a project-agnostic KiCad 10
  template, `design/templates/default.kicad_pro` (`DEFAULT_TEMPLATE`), so a project no longer builds on
  `tests/fixtures`. `write_project_file` now names the project's own root in `schematic.top_level_sheets` (it
  kept the template's, `pic_programmer.kicad_sch`) (field findings #18 and #15, template part).
- Rule sets: new `design.rules.jlcpcb_2l` (`JLCPCB_2L`, `JLCPCB_2L_CLASSES`): JLCPCB 2 layers, 1.6 mm, 1 oz,
  0.15 mm track and space, 0.6/0.3 mm vias, 0.5 mm hole to hole, 0.3 mm to the edge. `jlcpcb_2l`, `jlcpcb_4l`
  and `aisler_4l` set `silk_overlap` and `silk_over_copper` to warning, take `rule_severities`, and their
  `template` and `assignments` are optional (field findings #13, board part, and #18).
- Circular courtyards count: one courtyard reader, `review.courtyard_points`, serves
  `design.board.courtyard_bbox` and `review.load_board`; a circle is its centre plus and minus the radius (it
  was one point on the rim in the design package and nothing in `load_board`), an arc reaches the extremes it
  sweeps through. The placement check, `fit_placements` and `inspect free`/`spots` now see test points and
  mounting holes (field finding #5).
- `Place(near=..., fit=...)` honours `at` as the starting point, as documented: the search walks a 0.5 mm grid
  through `at`, nearest `at` first, and `near` keeps the part within the radius of the pad (the grid was
  centred on the pad, so `at` had no effect and positions inherited the pad's off-grid offset). `placed.json`
  records the start as `seed`; a recorded place is searched again when `at` changes (field finding #7).
- Board authoring: `Board.planes` takes a `design.board.Plane(layer, net, name, polygon=None, clearance=None,
  priority=0)` next to the `(layer, net, name)` tuple, for a zone over any polygon with its own clearance and
  priority; `Board.Text` takes `layer`, `rot` and `justify` (text on a back layer is mirrored) (field
  finding #13, board part).
- Multi-unit symbols in the design renderer: every unit that carries pins is placed and each pin is drawn
  against its own unit (a dual opamp's pins 5 to 8 were drawn on unit A). `Pin.unit`, `PartInst.units` and
  `unit_pins()`; `Layout.parts` names a unit as `"U4/2"` or `"U4/B"` (`"U4"` is unit 1); the flow places the
  units a layout leaves out, and `flow=None` with a unit left out is refused by name (field finding #1).
- `design.lint` checks a placed unit with its own pins only (field finding #2), and reports two net names
  (labels or power symbols) joined by wires, a junction or a label on a wire's run, not only at one point.
- The flow layout takes a symbol without pins (a mounting hole) in a nominal box (field finding #4).
- PWR_FLAGs are placed after everything else, on a short lead whose run and end touch no other connection
  point (the usual side first, so existing sheets are unchanged); a flag on a connector column no longer lands
  on the next pin and shorts two rails (field finding #6).
- `Circuit.note()` texts are drawn, stacked under the drawing or from the new `Layout.note_at` (field finding #14).
- Do-not-populate: `Circuit.part(..., dnp=True)` and `SchematicBuilder.place(dnp=True)` write `(dnp yes)` on every
  unit (`SchematicBuilder.mark_dnp` for rule-drawn parts); the board footprint gets KiCad's `dnp` attribute from
  the netlist, KiCad's or the offline one (`BoardBuilder.footprint(dnp=)`), and the PCBWay package leaves DNP
  parts out of the BOM and the position file (`export_bom`/`export_fab` take `exclude_dnp`, kicad-cli
  `--exclude-dnp`). BOM variants are not done (field finding #13).
- `Layout.decouple` takes `(anchor, rail)` keys next to anchor keys, so an IC's V+ and V- get their own side,
  offsets and capacitors; `RootLayout.papers` gives a sheet its own paper size (field finding #13).
- The design model no longer assumes a CM5 carrier (field finding #12). `RootLayout.module_sheet` may be
  `None` (its fields now have defaults): a board of consumer sheets only. `Signal` gains `ref` and `to`: a
  module pin is `"37"` or names its connector (`"J2.37"`, or `ref="J2"`), and a number two connectors share
  must name one (it used to label both); a signal between two consumer sheets has `pin=""` and `to=<sheet>`,
  and the root joins its ends by net labels (`by_sheet` gives each end its own view, so `Circuit.check` and
  the label shapes follow). `ModuleSheet.gnd_pins` names ground pins whatever their name, and pins named
  GND*, AGND, *_GND, VSS* or 0V count as ground; `PowerGroup.pins`, `no_connect` and `check_table` take the
  same pin keys (`module_sheet.pin_roles`, `locate`, `is_ground`). Existing projects build byte for byte as
  before.
- `examples/two_layer_basic`: the smallest complete project on the design package (signal table, module
  sheet, a Circuit and a placed Layout with a dual opamp, a DNP part, Board, Project) on `jlcpcb_2l` and the
  package's template, built by `design.build` (field finding #15). `tests/sheet_readback.py` reads a drawing
  back into pin groups without kicad-cli.
- The design API sheet shortens `pathlib._local.Path` (Python 3.13) to `Path` as it does on 3.12, so
  `docs/design-api.md` is the same on both (field finding #8).
- Known issue (field finding #20): several two-pin parts hanging from one label (a signal or a named private
  net) are drawn on the same point; `lint` reports it, and stops the build on a placed sheet only.

## 0.1.0 (2026-09-12)

First public release, built against KiCad 10.0.6.

- MCP server on three channels: kicad-cli (ERC, DRC, netlists, BOM, fabrication exports, renders), KiCad's
  IPC API (the board open in the PCB editor, read and edited as undo steps), lossless file edits (schematics).
  Two tool tiers: `core` (checks, exports, renders, reviews, libraries, documents, board reads) and `full`
  (design edits, routers).
- Design package `kicad_layer.design`: a board as data, the KiCad files as build outputs; sheets as circuits
  with layout plans, a signal table, projects with fab rule sets (JLCPCB, AISLER), placement, blocks, the
  seed of a hand-made board, gates against each sheet's description and a reference netlist.
- Library index and readers (`lib_search`, `sym_info`, `fp_info`, `lib_index`), `lib_fetch` for EasyEDA data
  by LCSC code, `parts_search` on JLCPCB's catalogue.
- Documentation library: fetch, import, list, text, page renders, section index, part fact sheets.
- Reviews with evidence and verdicts, differential pair checks, impedance estimates on fab stack-ups.
- Routers for differential pairs, plane stitching and FreeRouting (frozen: fixes, not features).
- PCBWay fabrication package from a build.

Renamed from kicad-layer on 2026-09-12; the import package stays `kicad_layer`.
