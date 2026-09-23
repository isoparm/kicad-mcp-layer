# Field findings: notch_board

Date: 2026-09-22. Source: the Audio_Tester `notch_board` project (design-as-code on `kicad_layer.design`,
built on Windows with KiCad 10 and FreeRouting 2.4.1). Each finding was checked against the code at
commit `bd75b11`. Line numbers refer to that commit.

**All 19 findings fixed on 2026-09-22 except:** BOM variants (#13), the CM5 catalogue still in `design/catalog.py`
(#15) and a settable grid step for `near`/`fit` (#7: the search is fixed at 0.5 mm, through `at`). The fixes were
checked without kicad-cli: #3's effect on schematic parity, and ERC and DRC on the fixed code, wait for a KiCad
machine. Rebuilt offline on the merged package (its own `build.py --offline` and `design.build.main --offline`),
notch_board gives its committed sheets and board byte for byte; its `.kicad_pro` and `.kicad_dru` differ only by the
`top_level_sheets` fix (#15) and by its own template and rules edited after its last build. #20 was found while
merging the fixes and is open.

Every confirmed finding that can be reproduced without kicad-cli has a test in
`tests/test_field_findings.py`, marked `xfail(strict=True, reason="docs/field-findings.md #N")`. The suite
stays green while the defect is there. The day a fix lands, the test XPASSes and fails the run, so the fix
removes the mark and updates the entry here. Numbers are the original report's; sections are ordered by
severity.

| # | Severity | Status | Test (`tests/test_field_findings.py::`) |
|---|---|---|---|
| 1 | blocking | FIXED in `dcc7982` | `test_1_a_dual_opamp_is_drawn_with_every_unit` |
| 6 | blocking | FIXED in `dcc7982` (render), `42f884f` (lint) | `test_6_a_pwr_flag_lead_does_not_short_the_next_connector_pin` |
| 4 | blocking | FIXED in `dcc7982` | `test_4_the_flow_layout_takes_a_symbol_without_pins` |
| 18 | blocking | FIXED in `26dffd1` | `test_18_rules_set_drc_severities` |
| 16 | blocking | FIXED in `322049a` | `test_16_the_stub_router_keeps_a_disallow_via_rule` |
| 5 | high | FIXED in `7a3a17b` | `test_5_courtyard_bbox_measures_a_circle`, `test_5_load_board_reads_a_circular_courtyard` |
| 2 | high | FIXED in `42f884f` | `test_2_lint_reads_only_the_pins_of_the_placed_unit` |
| 11 | high | FIXED in `293e253` | `test_11_out_builds_a_project_without_its_own_libraries` |
| 9 | high | FIXED in `293e253` | `test_9_the_schematic_is_written_without_kicad_cli` |
| 10 | high | FIXED in `293e253` | `test_10_route_stubs_runs_on_a_board_without_routes_json` |
| 3 | high | FIXED in `293e253` | `test_3_a_multi_unit_symbol_takes_the_path_of_its_first_unit`, `tests/test_design_build.py::test_offline_a_dual_opamp_footprint_takes_the_lowest_unit_symbol_path` |
| 19 | medium | FIXED in `9b42e37` | `tests/test_jobs.py::test_a_worker_job_survives_a_server_restart` (and the other worker tests there) |
| 8 | medium | FIXED in `9608af6` | `test_8_the_api_sheet_shortens_pathlib_on_every_supported_python` |
| 12 | medium | FIXED in `c59917c` | `test_12_a_signal_names_one_connector_of_a_two_connector_module`, `test_12_a_project_without_a_module_sheet` and the other `test_12_*` |
| 7 | medium | FIXED in `2fed97f`; OPEN: a settable grid step | `tests/test_design_placement.py::test_near_starts_from_at_on_a_grid_through_at` |
| 17 | low | FIXED in `916230f` | `test_17_the_dropped_copper_warning_does_not_blame_old_freerouting` |
| 13 | low | FIXED in `dcc7982`, `a50a529` (schematic: DNP, per-rail Decouple, per-sheet paper), `a8383d8` (board: Plane, Text, jlcpcb_2l); OPEN: BOM variants | `test_13_a_part_can_be_placed_do_not_populate`, `test_13_a_board_text_has_a_layer_and_a_rotation` |
| 14 | low | FIXED in `dcc7982` | `test_14_a_circuit_note_is_drawn` |
| 15 | low | FIXED in `26dffd1` (package template), `d35268f` and `32d87e1` (example); OPEN: catalogue | `tests/test_example_two_layer.py` |
| 20 | medium | CONFIRMED (found on the merge of the a1 branch) | `test_20_two_pin_parts_hanging_from_one_label_are_drawn_apart` |

Nothing here was NOT REPRODUCED. With the libraries this check had (a subset of KiCad 10's), the fast
suite went from 218 passed, 6 failed, 11 skipped to 218 passed, 6 failed, 11 skipped, 18 xfailed. The 6
failures were library gaps plus #8. After the fixes and their merge (2026-09-22) it is 287 passed, 5 failed
(library gaps), 11 skipped, 1 xfailed (#20). The fixes were checked without kicad-cli; ERC, DRC and schematic
parity on a KiCad machine are still to run.

---

## #1 The renderer places unit 1 of every symbol (blocking)

**Status:** FIXED in `dcc7982`: `Pin.unit` and `PartInst.units`; the renderer places every unit that carries pins
(`Layout.parts["U4/2"]` or `["U4/B"]`, the flow places the rest) and draws each pin against its own unit.
**Test:** `test_1_a_dual_opamp_is_drawn_with_every_unit`, and `tests/test_design_units.py` (drawings read back
against their circuits).

**Evidence:** every `place_part` call in `design/render.py` omits `unit`: lines 176 (`At`), 185 (`Beside`),
195 and 206 (`flow`). `SchematicBuilder.place` supports `unit=` (`sch_writer.py:142`) and `place_part` forwards
it. `PartInst` still takes the pins of every unit (`circuit.py:43`), and `anchor_pins` draws each pin at
`pl.pin(number)`, which is computed from the one placed unit:

```python
self.placed[ref] = place_part(self.sch, ref, inst.part, (hint.x, hint.y), value_text=inst.value_text, **kw)   # render.py:176
self.pins = [Pin(ref, p.number, p.name, int(p.rotation)) for p in sym.pins]                                   # circuit.py:43
```

**Impact:** with an OPA1678, units B and C (power) never appear. Their pins are drawn at unit A's coordinates:
pin 5 lands on pin 3, pin 6 on pin 2 and pin 7 on pin 1. Stubs and labels of different nets then meet at one
point, and pins 4 and 8 run to empty space. notch_board had to replace the renderer with its own
`design/unit_sheet.py`.

**Fix:** make an anchor a (ref, unit) pair. Group `PartInst.pins` by `Pin.unit` (unit 0 counts for every unit),
place each unit (for example with `Layout.parts["U1/2"]` or a unit-aware flow), and draw each pin against its own
unit's `Placed`.

## #6 A PWR_FLAG lead lands on the next connector pin (blocking)

**Status:** FIXED in `dcc7982`: flags are placed last, on a lead whose run and end touch no other connection
point; `42f884f`: `lint` groups points joined by wires and reports two net names in one group.
**Test:** `test_6_a_pwr_flag_lead_does_not_short_the_next_connector_pin`.

**Evidence:** `render.py:489`. The flag is set `2 * GRID` = 2.54 mm off the wire, square to it. That is exactly
a connector's pin pitch, so on a pin column the flag lands on the neighbour pin's stub end:

```python
side = (g(at[0] + d[1] * 2 * GRID), g(at[1] - d[0] * 2 * GRID))
self.sch.wire(at, side)
self.sch.pwr_flag(side)
```

Reproduced with `Conn_01x03`: pin 1 on `+12V` (flagged), pin 2 on GND and pin 3 on a signal. The PWR_FLAG and the
GND symbol both sit at (43.18, 50.8), so +12V and GND are one net. `lint` does not see it: it compares names at
one point only, and it skips PWR_FLAG (`lint.py:108-111`).

**Impact:** a shorted rail in the netlist. The build's description check (`verify.compare`, after kicad-cli)
stops it, but no layout hint moves the flag.

**Fix:** place the flag on the far side of the label, or along the wire's own direction, or pick a side whose
2.54 mm point is not a terminal of another net. Also have `lint` join points along wires and report two power
names in one group.

## #4 `render.flow` crashes on a symbol with no pins (blocking)

**Status:** FIXED in `dcc7982`: the flow gives a pinless part a nominal 5.08 mm box.
**Test:** `test_4_the_flow_layout_takes_a_symbol_without_pins`.

**Evidence:** `render.py:196-199`. `Mechanical:MountingHole` has no pins, and `two_pin` is `len(pins) == 2`, so
the part goes to the flow:

```python
pts = [trial.pin(p.number) for p in trial.symbol.pins]
...
x0, x1 = min(p[0] for p in pts), max(p[0] for p in pts)   # ValueError: min() iterable argument is empty
```

**Impact:** a plain sheet (`Layout()` with no `parts`) cannot hold a mounting hole, fiducial or logo.
Workaround: give the part an `At`.

**Fix:** in `flow`, give a pinless part a nominal box (for example 5.08 mm square), or place it after the anchors
without measuring pins.

## #18 Rules has no DRC severities, and the template makes silkscreen an error (blocking)

**Status:** FIXED in `26dffd1`: `Rules.rule_severities` is merged over the template's; the rule sets keep `silk_overlap`
and `silk_over_copper` at warning, and the package ships its own template, `design/templates/default.kicad_pro`. Originally CONFIRMED. **Test:** `test_18_rules_set_drc_severities`.

**Evidence:** `design/project.py:22-33`. `Rules` has no `rule_severities`, and `write_project_file`
(`project.py:105-136`) keeps the template's `board.design_settings.rule_severities` unchanged. The only
KiCad-authored template around, `tests/fixtures/pic_programmer/pic_programmer.kicad_pro`, has:

```json
"silk_over_copper": "error", "silk_overlap": "error"
```

`build.py:297` treats every DRC error except `unconnected_items` as blocking.

**Impact:** every board generated on that template fails DRC on silkscreen. notch_board ships its own
`design/template.kicad_pro` (see `project_def.py:69-70`).

**Fix:** add `rule_severities: dict[str, str]` to `Rules` and merge it in `write_project_file`. Have the rule
sets (`jlcpcb_4l`, `aisler_4l`) default the silk checks to `warning`.

## #16 The stub router places vias that a `.kicad_dru` rule forbids (blocking)

**Status:** FIXED in `322049a`: the reader moved into the core (`kicad_layer.dru`, re-exported by `routers/dru.py`);
`copper.Rules.load` reads the `.kicad_dru` (disallowed vias and tracks, custom clearances, track minimums) and
`stubs.route_stubs` puts no via or track where a rule forbids it. Originally CONFIRMED. **Test:** `test_16_the_stub_router_keeps_a_disallow_via_rule`.

**Evidence:** `copper.Rules.load` (`design/copper.py:120-136`) reads only the `.kicad_pro`: net classes,
patterns and the board minimums. No code in `design/` reads the `.kicad_dru`. `stubs._via_spots` and
`route_stubs` (`stubs.py:124`, `382-406`) add a via wherever a clearance check passes:

```python
for x, y in stub.vias:
    out.vias.append(RouteVia(o.net, x, y, size, drill))   # stubs.py:404-405
```

A parser already exists in `routers/dru.py`, but `design` may not import `routers` (`tests/test_layers.py`).

**Impact:** `build.py --route-stubs` put vias on the notch's HiZ nets, which have `(constraint disallow via)`, and
KiCad DRC reported `items_not_allowed`. The same model sits behind `fit_placements` (`board.py:198`) and
`inspect clear-via`, so they ignore the custom rules too.

**Fix:** move the `.kicad_dru` reader (or the part that handles `disallow` and class-to-class clearance) into the
core, load it in `copper.Rules.load` next to the project, and refuse vias (and tracks) that a rule disallows for
the net.

## #5 Circular courtyards are a point or nothing (high)

**Status:** FIXED in `7a3a17b`: one reader, `review.courtyard_points`, serves `board.courtyard_bbox` and
`review.load_board`: a circle is its centre plus and minus the radius, an arc reaches the extremes it sweeps. Originally CONFIRMED. **Tests:** `test_5_courtyard_bbox_measures_a_circle`,
`test_5_load_board_reads_a_circular_courtyard`.

**Evidence:** `board.courtyard_bbox` (`design/board.py:98-106`) accepts `fp_circle` but reads only `start`,
`end` and `mid`. A circle has `center` and `end`, so the box collapses to the one point on the rim.
`MountingHole_3.2mm_M3` gives `(3.45, 0.0, 3.45, 0.0)`. `review.load_board` (`review.py:245`) leaves `fp_circle`
out entirely, so the courtyard is `None`:

```python
for k in ("start", "end", "mid"):                                   # board.py:102
if t in ("fp_line", "fp_rect", "fp_arc", "fp_poly") and ...:        # review.py:245
```

**Impact:** `check_placement` and `fit_placements` (which reads `load_board`, `board.py:200`) do not see overlaps
of test points and mounting holes (KiCad's are circles). notch_board rechecks placement with its own
`design/placement_check.py`.

**Fix:** for `fp_circle`, add centre ± radius on both axes, in both places. Better, share one courtyard reader
between `board.py` and `review.py`.

## #2 `lint` counts the pins of every unit (high)

**Status:** FIXED in `42f884f`: the three loops read only the placed unit's pins (and unit 0).
**Test:** `test_2_lint_reads_only_the_pins_of_the_placed_unit`.

**Evidence:** `design/lint.py:63` (`body_box`), `114` (terminals) and `128` (pin-on-wire check) all iterate
`pl.symbol.pins` without filtering on `pl.unit`:

```python
for p in pl.symbol.pins:
    terminals[_key(pl.pin(p.number))] = terminals.get(_key(pl.pin(p.number)), 0) + 1   # lint.py:114-115
```

**Impact:** phantom pins widen the body box (spurious "label runs over" warnings) and add phantom terminals. On an
OPA1678, unit B's pin 5 sits where unit A's pin 3 is. A wire running through pin 3 with no junction then counts
two terminals and is not reported, even though KiCad will not connect it. This applies today to any multi-unit
symbol on a placed sheet.

**Fix:** skip pins where `p.unit not in (0, pl.unit)` in all three loops, as `sch_writer.place` already does
(`sch_writer.py:193`).

## #11 `--out` copies library tables that may not exist (high)

**Status:** FIXED in `293e253`: each table and `lib/` is copied only when the project has it. Originally CONFIRMED. **Test:** `test_11_out_builds_a_project_without_its_own_libraries`.

**Evidence:** `design/build.py:175-180`:

```python
if out != P:
    for f in ("sym-lib-table", "fp-lib-table"):
        shutil.copy2(P / f, out / f)
    ...
    shutil.copytree(project.lib_dir, out / "lib")
```

**Impact:** `FileNotFoundError` for a project that uses only KiCad's libraries. That breaks the check CLAUDE.md
prescribes after every design-package change (`build.py --sch-only --out <scratch>`).

**Fix:** copy each table and `lib/` only when it exists.

## #9 The build needs kicad-cli before it writes anything (high)

**Status:** FIXED in `293e253`: `build.py --offline`, and automatically without kicad-cli, writes the sheets, the project
and the board on a netlist synthesized from the descriptions (`design/offline.py`); ERC, the gates and DRC are
reported UNVERIFIED. Since `079e5d5` that netlist resolves module pins as the module sheet does (`pin_roles`: pin
keys by connector, ground pins by number or name) and carries DNP. Originally CONFIRMED. **Tests:**
`test_9_the_schematic_is_written_without_kicad_cli` (the fix goes offline by itself when kicad-cli is missing, so
the plain `--sch-only` run writes the sheets) and the offline tests in `tests/test_design_build.py`.

**Evidence:** `design/build.py:170` calls `find_kicad_cli()` before lint, `build_design` and the writes
(`191-211`):

```python
cli = find_kicad_cli()                 # build.py:170, raises without kicad-cli
...
root.write(str(out / f"{project.name}.kicad_sch"))   # build.py:208
```

**Impact:** without KiCad (CI, a Linux sandbox, a second machine) the build produces nothing: no sheets, no
`.kicad_pro`, not even the lint verdict. notch_board carries an `--offline` path (`design/build.py:35-75`) with a
netlist synthesized from the descriptions (`design/netlist_local.py`).

**Fix:** look for kicad-cli only when ERC is due. Without it, write the sheets and the project, report ERC,
netlist and DRC as UNVERIFIED, and stop (or offer the synthesized-netlist board as an explicit offline mode).

## #10 `--route-stubs` does nothing on a fresh board (high)

**Status:** FIXED in `293e253`: `--route-stubs` starts from empty copper when `routing/routes.json` is absent and creates
it. Originally CONFIRMED. **Test:** `test_10_route_stubs_runs_on_a_board_without_routes_json`. A control run with
an empty `routing/routes.json` in place does reach the router.

**Evidence:** `design/build.py:300`:

```python
if attempt == 0 and "--route-stubs" in argv and drc.counts.get("unconnected") and routes_path.is_file():
```

**Impact:** on a board that was never routed there is no `routing/routes.json`, so the flag is ignored, with no
message. Yet the flag is meant to route what DRC leaves open, and a fresh board has the most open.

**Fix:** drop the `routes_path.is_file()` condition and start from `routes_mod.Routes()` when the file is absent
(`routes_mod.save` creates it). The board must be built with `Board.routes` pointing there for the second pass
to apply it.

## #3 `symbol_paths` keeps the last placed unit's uuid (high)

**Status:** FIXED in `293e253`: `build.symbol_paths` keeps the path of the lowest unit, whatever the order the units were
placed in. Originally CONFIRMED in the code; the effect on DRC parity was not verified here (no kicad-cli). **Tests:**
`test_3_a_multi_unit_symbol_takes_the_path_of_its_first_unit`, and
`tests/test_design_build.py::test_offline_a_dual_opamp_footprint_takes_the_lowest_unit_symbol_path` (a dual opamp
drawn by the renderer, unit 1 not last: the board footprint carries unit 1's path).

**Evidence:** `design/build.py:225-231`. The map is keyed by reference, so each unit overwrites the previous:

```python
for p in cb.placed:
    symbol_paths[p.ref] = (f"{cb.path}/{p.uuid}", f"/{sheet.name}/", sheet.file)
```

**Impact:** the footprint's `path` points at whichever unit was placed last. This is reachable today with any
hand-drawn multi-unit sheet. notch_board places units in the order 3, 2, 1 (`design/notch.py:155,166`) so that
unit 1 ends up last; its board carries unit 1's uuid for U4 and U5. If the fix to #1 places units in natural
order, the path becomes unit 3's.

**Fix:** keep the first entry per reference, or the one with the lowest `unit`. Project scripts copy this loop
(notch_board's offline build does), so a shared helper would help.

## #19 A server restart loses running jobs even when the subprocess finishes (medium)

**Status:** FIXED in `9b42e37`: each job runs in a detached worker process (`python -m kicad_layer.jobs worker`)
that writes `status.json`, `log.txt` and `result.json` under `<cache>/jobs/<id>/`, read by any later server.
**Test:** `tests/test_jobs.py::test_a_worker_job_survives_a_server_restart` (plus a failing, a dying and an
unspawnable worker). Originally CONFIRMED (documented behaviour, `jobs.py:10-12`).

**Evidence:** `jobs.py:153` starts the subprocess with its output piped into a thread of the server.
`JobRunner.get` (`jobs.py:245-247`) turns every job that was not finished into `lost`:

```python
if old.state in ("queued", "running"):
    old.state = "lost"  # the process that ran it is gone
```

The post-processing also runs in the server: for `autoroute`, that is SES parsing, dropping excluded nets and
merging into `routes.json` (`routing_tools.py:82-90`). So even when FreeRouting finishes and writes its `.ses`,
`routes.json` is never produced.

**Impact:** when Claude Desktop reconnects (and restarts the server), long FreeRouting or DRC runs are lost and
must run again.

**Fix:** run the job's whole callable in a detached worker process (`python -m kicad_layer.jobs run <id>`) that
writes its log and result to `<cache>/jobs/<id>.json`, and record its PID. `get` then reports `running` while the
PID is alive and reads the result when it is done.

## #8 The API sheet is wrong on Python 3.13 (medium)

**Status:** FIXED in `9608af6`: the pattern takes `pathlib._local`, so the sheet is the same on 3.12 and 3.13. Originally CONFIRMED on Python 3.13.13, where `tests/test_design_api.py::test_api_sheet_is_current` failed.
**Test:** `test_8_the_api_sheet_shortens_pathlib_on_every_supported_python`.

**Evidence:** `design/api.py:111`. On 3.13, `inspect.signature` prints `pathlib._local.Path`, and the pattern
turns that into `_local.Path`:

```python
s = re.sub(r"(?:kicad_layer\.[\w.]+|typing|collections\.abc|pathlib)\.(\w+)", r"\1", s)
```

`pyproject.toml:7` allows `>=3.12`.

**Impact:** on 3.13 the generated `docs/design-api.md` differs from the committed one, so the staleness test
fails on a clean tree. Regenerating there would write `_local.Path` into the sheet.

**Fix:** use `pathlib(?:\._local)?` in the pattern, or `[\w.]+` after `pathlib`.

## #12 The model assumes a CM5 carrier (medium)

**Status:** FIXED in `c59917c`: `RootLayout.module_sheet` may be `None`, `Signal.pin` names its connector
(`"J2.37"` or `ref=`), `Signal.to` makes a sheet-to-sheet signal, `ModuleSheet.gnd_pins` and common ground names.
**Test:** `test_12_a_signal_names_one_connector_of_a_two_connector_module` (rewritten on the new API; a shared bare
number is refused) and the other `test_12_*` (a two-sheet project without a module). Originally CONFIRMED.

**Evidence:**

- `RootLayout.module_sheet` is required (`project.py:40`). `build_root` always draws a module sheet, and every
  signal is one wire from a consumer sheet to it (`root.py:47-62`). Two consumer sheets cannot share a signal.
- `Signal.pin` is "CM5 pin number" (`signals.py:10`), and the label shape is `cm5_shape` (`signals.py:16`).
- `build_module_sheet` keys signals by pin number only (`module_sheet.py:59`), so pin 1 of every module
  connector gets the label. Ground is recognised by name prefix (`module_sheet.py:68`, `draw.py:56`).

```python
by_pin = {s.pin: s for s in m.signals}      # module_sheet.py:59
```

**Impact:** a board without a module (notch_board: header, power, notch) needs a stand-in module sheet. A module
with two connectors that share pin numbers, like the CM5's two 100-pin connectors, is labelled wrongly. A
ground pin named `VSS` or `0V` is refused.

**Fix:** add `Signal.connector` (the reference) and key `by_pin` on (ref, pin). Allow `RootLayout.module_sheet =
None`, and add sheet-to-sheet signals (a `Signal.sheets` pair). Let `ModuleSheet` name its ground pins.

## #7 `near` ignores `at`; the search grid follows the pad (medium)

**Status:** FIXED in `2fed97f`: the search grid runs through `at` and is ranked by distance to `at`; `near` bounds it to
the radius around the pad (regression test in `tests/test_design_placement.py`). Originally PARTLY: the 0.5 mm grid is documented (`fit_placements` docstring, `board.py:172-174`); what
contradicted the docs is that `at` was ignored when `near` is given. OPEN: the grid step stays 0.5 mm (not
settable from `Place`). **Test:** `tests/test_design_placement.py::test_near_starts_from_at_on_a_grid_through_at`.

**Evidence:** the `Place` docstring (`board.py:28-30`, reproduced in `docs/design-api.md:327`) says "`at` is only
the starting point". With `near`, `fit_placements` replaces the centre with the pad's position (`board.py:203-209`).
`_fit_search` then walks a grid of `step=0.5` (not settable from `Place`) centred on that point
(`board.py:230-239`):

```python
centre, anchor = (pad.x, pad.y), f"{p.near[0]}.{p.near[1]}"                                 # board.py:209
cands = [(round(centre[0] + i * step, 3), round(centre[1] + j * step, 3)) for i in ... ]    # board.py:237
```

**Impact:** the first candidate is the footprint origin on top of the pad, and positions inherit the pad's
off-grid offset (for example x = 101.27 + k·0.5). The author's `at` (which side of the pad) has no effect.

**Fix:** with `near`, search around `at` and rank by distance to the pad (or state that `at` is ignored). Snap
candidates to a board grid, and expose `step` on `Place`.

## #20 Two-pin parts hanging from one label are drawn on one point (medium)

**Status:** CONFIRMED on the merged code (after `d6786b5`); found while checking the a1 branch's renderer.
**Test:** `test_20_two_pin_parts_hanging_from_one_label_are_drawn_apart`.

**Evidence:** a connector pin on a sheet signal `S` with R1, C1 and R2 each from `S` to ground. In a plain
`Layout()` (the flow) the renderer hangs every one of them from the same tap on the pin's stub, so all three
symbols are placed at one point, (58.42, 95.25) here, and their ground symbols on one point too. The same
happens on a named private net (a local label) and on a placed layout (`Layout.parts` with the connector at
an `At`); parts on a rail are spaced correctly (the decoupling rule steps them by `Decouple.pitch`).
`lint` reports it: `C1 sits on R1 at (...)`, `R2 sits on R1 at (...)`, `#PWR002 sits on #PWR001 at (...)`.

**Impact:** on a placed sheet the build lints the geometry and stops ("the drawing has geometry KiCad would
misread"), with no layout hint that spreads the parts except an `At` for each. A plain sheet is not linted by
the build, so it is written as is: the netlist is right (the stacked parts share both pins), but the drawing
is unreadable and hides that there are three parts.

**Fix:** step each further part hanging from one tap along the stub (or down a short vertical bus from the tap)
by one part pitch, as the rail rule already does for decoupling capacitors; until then, give each such part a
place in `Layout.parts`.

## #17 The autoroute warning blames FreeRouting before 2.4 (low)

**Status:** FIXED in `916230f`: the warning names the excluded nets FreeRouting routed anyway and says the copper
was dropped, no version. The code already passed `-inc` and dropped that copper whatever the version.
**Test:** `test_17_the_dropped_copper_warning_does_not_blame_old_freerouting`. Originally CONFIRMED.

**Evidence:** `routers/routing_tools.py:84` (comment) and `88`:

```python
warnings.append(f"FreeRouting routed excluded nets anyway ({dropped} segments and vias dropped); versions before 2.4 ignore -inc when headless.")
```

The same claim is in `docs/tools.md:313` and `CHANGELOG.md:28`. On the notch_board run, FreeRouting 2.4.1
headless also routed the excluded nets (88 segments and vias dropped).

**Impact:** the message points the user at an upgrade that does not help.

**Fix:** state the fact without a version ("FreeRouting headless, 2.4.1 included, does not honour -inc; the copper
was dropped"), in the code, the docs and the changelog.

## #13 Missing authoring API (low)

**Status:** schematic part FIXED in `dcc7982` and `a50a529`: `Circuit.part(..., dnp=True)` writes `(dnp yes)`, the
footprint gets the `dnp` attribute and the PCBWay package passes `--exclude-dnp`; `Layout.decouple` takes
`(anchor, rail)` keys; `RootLayout.papers` sets a sheet's paper. Board part FIXED in `a8383d8`: `Board.planes`
takes a `Plane` with `polygon`, `clearance` and `priority`; `Board.Text` has `layer`, `rot` and `justify`;
`design/rules.py` has `jlcpcb_2l` (`JLCPCB_2L`). Since `079e5d5` the offline build's netlist carries the DNP flag
too, so an offline board gets the attribute. OPEN: BOM variants are not done. **Tests:** `test_13_a_part_can_be_placed_do_not_populate`,
`test_13_a_board_text_has_a_layer_and_a_rotation`. The other items are API design and have no test.

**Evidence:**

- DNP and variants: `sch_writer.py:180` always writes `(dnp no)`; neither `place` nor `Part` has a flag.
  notch_board keeps its BOM variants in `design/bom_variants.py`.
- No 2-layer rule set: `design/rules.py` offers `jlcpcb_4l` (line 50) and `aisler_4l` (line 112) only.
- `Board.planes` is `(layer, net, name)` poured over the whole board less `plane_inset` (`board.py:79`,
  `302-305`). `pcb_writer.zone` already takes any polygon, `priority` and `clearance` (`pcb_writer.py:367`).
- `Board.Text` has no layer or rotation (`board.py:62-68`); `pcb_writer.text` has both (`pcb_writer.py:403`).
- `Layout.decouple` is per anchor (`render.py:112`, `417`). An IC with two rails (V+ and V-) gets one side,
  first offset and pitch for both.
- One paper size for every described sheet: `root.py:66,70` pass `L.paper`; placeholders are fixed at A4
  (`root.py:72`).
- `copper.Rules` ignores `.kicad_dru` (see #16).

**Fix:** a `dnp` flag on `place`, `PartInst` and `Part`, then variants; `jlcpcb_2l`; `Plane(layer, net, polygon=None,
clearance, priority)`; `Text(layer, rot, justify)`; `Decouple` keyed by (ref, rail); `RootLayout.papers: dict`.

## #14 `Circuit.note()` is never drawn (low)

**Status:** FIXED in `dcc7982`: circuit notes are stacked under the drawing, or from `Layout.note_at`.
**Test:** `test_14_a_circuit_note_is_drawn`.

**Evidence:** `design/circuit.py:119-121` stores the text in `self.notes`. No code reads `Circuit.notes`: `render`
draws only `Layout.notes` (`render.py:144-145`). The API sheet says "the renderer places it"
(`docs/design-api.md:66`).

```python
def note(self, text: str, size: float = 1.5) -> None:
    """A free text on the sheet; the renderer places it."""
    self.notes.append((text, size))
```

**Impact:** notes silently vanish from the sheet.

**Fix:** have `render` place circuit notes (for example stacked under the flow, or above the first anchor), or
drop the method.

## #15 The examples do not show the design package (low)

**Status:** FIXED except the catalogue. Template part in `26dffd1`: the package ships a project-agnostic KiCad 10 template,
`design/templates/default.kicad_pro`, used when `Rules.template` is None. Example in `d35268f`:
`examples/two_layer_basic` is a complete `Project` (module sheet, a `Circuit` and a placed `Layout`, `Board`,
`Rules`), checked by `tests/test_example_two_layer.py`; since 32d87e1 it builds on `jlcpcb_2l` and the package
template, with no test fixture behind it. OPEN: the catalogue (`catalog.py`) is still another board's.

**Evidence:**

- `examples/hello_world/design.py:14-17` builds on `SchematicBuilder` and `BoardBuilder` directly. There is no
  `Circuit`, `Layout`, `Board` or `Project`, and its own `build.py` repeats the pipeline.
- No example builds a `Project`; `docs/design-api.md` lists the classes only.
- `design/catalog.py` is a CM5 carrier board's catalogue: `CM5IO` symbols, M.2 socket, FH12 (lines 17-25). Its
  table fills part of `docs/design-api.md` (for example line 145).
- The `.kicad_pro` template a `Rules` needs exists only as a test fixture:
  `examples/hello_world/build.py:29` reads `tests/fixtures/pic_programmer/pic_programmer.kicad_pro` (see also
  #18).
- In this checkout, `examples/` is untracked (`git status`), while `README.md:53,243` links to it.

**Fix:** a small `examples/` project on `Circuit` + `Layout` + `Board` + `Project` (notch_board's header and power
sheets would do). Ship a KiCad-authored 2- and 4-layer template under `src/kicad_layer/design/templates/`, and
move the CM5 catalogue into that board's repository.
