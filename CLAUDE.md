# kicad-mcp-layer: working notes for Claude Code

An MCP server (official `mcp` 2.x SDK, Python 3.12) that drives KiCad 10 through
`kicad-cli`, KiCad's IPC API (`kicad-python`) and direct file edits.

## Ground rules

* Never import the SWIG `pcbnew` module. `tests/test_swig_guard.py` fails the build if you do.
* Never print to stdout. The server runs on stdio; log to stderr with `logging`.
* Every path a tool accepts goes through `kicad_layer.paths.resolve_in_workspace`.
* Anticipated failures are `LayerError(code, message, hint=...)`. Codes are listed in
  `errors.py` and `docs/tools.md`. Never signal failure with a `{"success": false}` dict.
* A check that produced no report is `UNVERIFIED`, never `PASS`.
* Design edits require `require_write_mode()` and, for schematics, refuse while a
  `~name.kicad_sch.lck` file exists.
* Connectivity comes from `kicad-cli sch export netlist`, never from wire tracing.
* The KiCad file formats the writers emit live in `formats.py` only. The layer is built against KiCad 10.0.6;
  a KiCad upgrade is deliberate: full suite after, doctor's advice read. `kicad-python` is the `ipc` extra.

## Layers

* `kicad_layer.design` is the product: a board as data, the KiCad files as build outputs. It may import
  only the core (`sexpr`, `ids`, `kicad_libs`, `libtables`, `paths`, `config`, `errors`, `models`, `cli/`),
  the two writers and `routes`. `tests/test_layers.py` enforces it.
* The MCP server is the check-and-view layer. `KICAD_LAYER_TOOLS` picks the tier: `core` (default: checks,
  exports, renders, reviews, libraries, documents, board reads) or `full` (plus design edits and the routers).
* `kicad_layer.routers` is frozen: fixes, not features, and only `tools.py` imports it. The intended loop
  routes by hand in KiCad and captures the copper as data (`routes.py`).

## Layout

* `src/kicad_layer/tools.py` declares every MCP tool, one registrar per group; implementation lives in
  `cli/`, `ipc/`, `routers/`, `project.py`, `doctor.py`, `capabilities.py` and is testable without MCP.
  The design package is `design/`; the copper data model is `routes.py`.
* `models.py` holds every output schema. Add fields; do not rename them.
* `GROUPS` in `tools.py` (one registrar per group), the table in `docs/tools.md` with its tier column and
  the `covered` rows in `capabilities.py` must agree; `tests/test_registry.py` checks.

## Context: what a session reads

Measured over the first two boards (shares of about 680k tokens of tool results):
reading library source and project sheets 14%, scratch Python (board and schematic questions, PDFs, memory
patches) 20%, images 14%, builds, tests and routing runs 15%, web search and fetch 8%, logs 4%, the MCP tools
under 2%. Run the script after a board and compare. So:

* To write or change a sheet, a board or a project, read `docs/design-api.md` (generated, about 7k tokens),
  never the source. Open a source file only to change the library. `python -m kicad_layer.design.api`
  regenerates the sheet; `tests/test_design_api.py` fails when it is stale.
* Ask a built board through `python -m kicad_layer.design.inspect <build> parts|part|net|pin|classes|drc|unrouted|bbox`,
  not through a one-liner. Add a question there when one recurs. Placing a part or adding copper is a question
  too: `region` (what is there), `free` and `spots` (does or where does a part fit), `clear` and `clear-via`
  (does a candidate track or via keep every clearance, and what it hits if not). Never dump coordinates and
  do the arithmetic in the session.
* Datasheets and reference designs: fetch once into the document library (`doc_fetch`). Ask `doc_facts` first:
  a part's sheet in `research/parts/` answers pins, limits, values, circuit and package with pages at about a
  hundred tokens a section. No sheet yet: have a subagent write it (`doc_sections` for the pages, `doc_text`
  with those pages, `doc_page` to check every table against the picture; the CLI `python -m kicad_layer.docs`
  gives it the same without the server) and keep only the sheet. Never page through a PDF in the main session;
  `doc_text` with `find` returns merged match windows only, `pages` reads one page around a hit.
* Renders and plots cost about a thousand tokens each; look once, at the crop you need.

## Design package (`src/kicad_layer/design`)

* A project is data on it: sheets, placement, a `Project`, a `RoutingPlan` for `routers.pipeline` and the PCBWay
  package from `design/fab.py`. A part added to a sheet needs no coordinates: `Place(ref, at, rot, near=(REF, PAD))`
  lets the build find the nearest place that fits (recorded in `routing/placed.json`), and `build.py --route-stubs`
  routes what the DRC leaves open on single-ended nets over the copper model, then builds again. Boards build on it;
  after a change run `tests/test_design*.py`, then a board's `build.py --sch-only --out <scratch>` and compare with
  the previous build byte for byte.
* Long patch scripts go through a file, not an inline heredoc (the shell drops commands over ~8 KB).

## Tests

```
.venv\Scripts\python -m pytest                 # the fast loop: parallel, no slow, no live tests (about 30 s)
.venv\Scripts\python -m pytest -m "not gui"   # plus the slow tests; run this before a commit
.venv\Scripts\python -m pytest -m ""          # everything; needs KiCad open with the API on
```

A test that takes more than ten seconds is marked `slow`. Keep the default run short.

`tests/test_cli_live.py` runs real `kicad-cli` on the fixture corpus in `tests/fixtures/`
(KiCad demo projects upgraded to the 10.0 formats) and self-skips without kicad-cli.
Fixtures must stay KiCad-authored; never hand-write one.

## Decisions

The architecture decision record is `../research/notes/00-design-inputs.md` in the author's workspace (not
published). Read it before changing how a channel works, when you have it.
