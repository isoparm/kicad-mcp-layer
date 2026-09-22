# Changelog

## Unreleased

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
  `review_board` in a thread of the server and returns a job id at once; `job_status` gives state, elapsed
  time, output tail and FreeRouting's pass, unrouted and violation counts (`wait_s` to block briefly);
  `job_result` the tool's normal result. State is kept in memory and in `<cache>/jobs`, so a restarted
  server reports a running job as `lost`. New error code `JOB_FAILED`.
- FreeRouting's output is streamed (visible in a job) and, at the timeout, the process is terminated and a
  session file it wrote is still used instead of failing outright.
- `autoroute` no longer routes blind through rules the DSN cannot carry: rule areas become per-layer
  `keepout`/`via_keepout`/`place_keepout` entries (footprint keep-outs included), plain two-class clearance
  rules of the `.kicad_dru` become `class_class` rules, and nets named by creepage, physical-clearance,
  disallow or area-conditioned rules, or wide pour nets with a zone, are excluded (`auto_exclude_ruled_nets`,
  default on; `force_nets` overrides) with their copper protected. New `exclude_nets` and `exclude_classes`;
  the report lists `excluded_nets` with reasons, `class_clearances`, `keepouts` and `dropped_segments`
  (FreeRouting before 2.4 ignores `-inc` when headless, so its copper on excluded nets is dropped).
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
