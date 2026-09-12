# kicad-mcp-layer

An AI layer for KiCad 10: a Model Context Protocol server that lets Claude Code, or any MCP
client, work on KiCad designs.

It drives KiCad through three channels and says which one it is using:

| Channel | Mechanism | Used for |
|---|---|---|
| `cli` | `kicad-cli` subprocess | ERC, DRC, netlists, BOM, renders, fabrication exports. Works with KiCad open or closed. |
| `ipc` | KiCad's official IPC API via `kicad-python` | The board open in the PCB editor. Edits land as undo steps. |
| `file` | Lossless edits of `.kicad_sch` and `.kicad_pro` | Schematics, which KiCad 10 exposes no API for. |

The legacy SWIG `pcbnew` module is never used; a test enforces it.

This is the library half of **[kicad-ai-stack](https://github.com/ShamanAndrey/kicad-ai-stack)**, the
workspace it was built in. The stack adds the one-command setup (`bootstrap.py`, which clones this
repository, builds its environment and registers the server with Claude Code) and the working rules for
a session. Start there if you want the whole thing set up; start here if you want the server or the
design package on their own.

## What this is

Two layers on one core.

* **`kicad_layer.design` is the product.** A board is Python data: parts from a catalogue, a
  signal table, circuits as pin-to-net statements, placement, saved copper. The KiCad files are
  build outputs, and KiCad's own ERC and DRC are the tests. Two boards live on it.
* **The MCP server is its check-and-view layer** for Claude Code: checks, exports, renders,
  reviews, documentation and library search. That is the `core` tool tier, registered by
  default. The `full` tier adds the schematic and board edit tools and the routers.

A project holds only its data: sheets as circuits, placement, a `Project`, a routing plan for the
pipeline in `kicad_layer.routers`, and the fabrication package from `kicad_layer.design.fab`. The whole
authoring API is on one generated sheet, [docs/design-api.md](docs/design-api.md), so a session reads
that instead of the source; `python -m kicad_layer.design.inspect` answers questions about a built board.

The routers (`kicad_layer.routers`) are frozen: fixes, not features. The intended loop routes by
hand in KiCad and keeps the copper as data. `tests/test_layers.py` enforces the layering.

## Status

Pre-alpha, built in the open. Done so far:

* Rung 0: environment diagnostics, the capability matrix, project discovery, workspace
  confinement, read-only default mode.
* Rung 1: every kicad-cli tool (ERC, DRC, netlist, trace, BOM, fabrication export, board
  render, schematic render).
* Rung 2: reading the board open in KiCad's PCB Editor over the IPC API (summary, items,
  per-net statistics), with every failure classified as unreachable or rejected.
* Writers: a lossless S-expression engine, a library loader, and schematic and board
  writers that generated a complete 89-LED board which passed ERC and DRC with zero
  findings (`examples/hello_world`).
* Milestone 1: a full-text index of every library KiCad can
  see, including a project's own, with `lib_search`, `sym_info`, `fp_info` and `lib_index`.
* Milestone 2: editing existing schematics losslessly. A concrete syntax tree keeps every
  untouched byte; edits are atomic, snapshotted, refused while KiCad holds the lock, and
  validated by ERC and a netlist comparison. Tools: `sch_list_components`, `sch_get_symbol`,
  `sch_set_property`, `sch_add_component`, `sch_wire`, `sch_label`, `sch_mark`, `sch_delete`,
  `sch_annotate`.

* Milestone 3: editing boards. Live through KiCad's API as one undo step per operation,
  read back from KiCad; or losslessly in the file when the board is closed. A board seen
  live is never edited on disk. Tools: `pcb_place_footprint`, `pcb_move_footprint`,
  `pcb_add_track`, `pcb_add_via`, `pcb_add_zone`, `pcb_refill_zones`, `pcb_delete_items`,
  `pcb_save`. KiCad 10's own "create items from text" call is a stub, so live placement
  builds the footprint from the library file pad by pad.

* Milestone 4: design review beyond DRC. `review_board`, `review_schematic` and
  `review_project` produce one report where every check says PASS, WARN, FAIL, INFO or
  UNVERIFIED with its evidence and the source of its limits: DRC and unrouted connections,
  zone fill state, parts outside the outline, manufacturability against JLCPCB's published
  limits, track widths on power nets, zone stitching, decoupling distance, footprints and
  values and annotation, power-net sources, and the honest state of SPICE. The first run
  on the Hello World found its vias below the fab's minimum annular ring, which is why the
  layer's default via is now 0.8 mm. The first run on a real project then caught the review
  itself: PWR_FLAGs never appear in the exported netlist, so the power-source check now
  follows KiCad's ERC rule instead. Checks are documented in
  [docs/review-checks.md](docs/review-checks.md).

* Writers, second round, for the first real product (a Compute Module 5 carrier): hierarchical
  schematics (sheet symbols with pins, hierarchical labels, per-sheet instance paths, multi-unit
  symbols), boards with any even number of copper layers, and project-local symbol and
  footprint libraries. A seven-sheet, four-layer project generated this way passes ERC with
  no findings; KiCad's own ERC and DRC are the tests.

* Documentation layer: `doc_fetch`, `doc_import`, `doc_list`, `doc_text` and `doc_page` keep
  datasheets and reference designs in an indexed library under `research/references` with
  their source URL, date and hash, extract text per page, and render pages to images so
  pinouts, package drawings and tables can be read. When a site answers with a scripted
  download portal or refuses the plain client, an optional headless-Chromium tier
  (`pip install "kicad-mcp-layer[browser]"`) loads the page and takes the download it offers;
  what still fails gets a classified error that says to save the file in a browser and import it.

* Routing checks and parts: `route_check` measures every differential pair on a board (lengths,
  skew against the interface's limit, coupled share, class gap and width, layer changes) and the
  review carries it as `diff_pairs`; `impedance_calc` and `stackup_info` give closed-form estimates
  next to the fab's published geometries for JLCPCB's 4-layer stack-up; `parts_search` checks
  JLCPCB's assembly catalogue for LCSC codes and stock. The first real board's 28 pairs and 30
  special parts went through them before routing started.

* Routing: `route_pairs` routes differential pairs as coupled pairs (escapes planned and reserved
  for every pair first, a heading-aware search that never folds a pair back on itself, mitred
  offsets, a two-via crossover where P would land on the wrong side, tuning bumps for skew);
  `stitch_planes` drops a stub and via from every surface-mount pad on a plane net; `autoroute`
  hands the rest to FreeRouting through a Specctra DSN with the earlier copper protected and
  merges the session back. All three write a routes JSON next to the board rather than touching
  the board, so a design-as-code project re-applies it on every build. The first real board's 21
  routed pairs came out with zero clearance errors; the residual skew sits on the pairs that
  needed a crossover.

* Manufacturing package (milestone 5, PCBWay): `kicad_layer.design.fab` writes the Gerber and drill
  archive, the position file and a BOM in PCBWay's assembly layout from a finished build, and refuses
  a BOM line without a part number. The first real product ordered from it.

* The design package became the product: `kicad_layer.design` holds a board as data (sheets described
  as circuits with a layout plan, a signal table that drives the module sheet and the root, a project
  with its rule set, placements, blocks) and the KiCad files are build outputs. A build writes the
  schematic, the project file with its constraints and net classes, the design rules, runs ERC, exports
  the netlist and checks every sheet against its own description and against a reference netlist, pin
  group for pin group. Fab rule sets for JLCPCB and AISLER (constraints, classes on the fab's published
  pair geometries, its own `.kicad_dru` rules), and `review_board` takes a fab name for its limits.

* Hand layout on a generated schematic: for a board placed and routed by a person in KiCad, the build
  never writes the board. `build.py --seed` writes its first import instead of KiCad's F8, every footprint
  in rows and linked to its symbol; blocks (an anchor footprint, members with offsets, one KiCad group)
  keep parts that belong together, applied by `--blocks`. The routers are frozen: fixes, not features.

* Parts and documents: `doc_sections` and `doc_facts` read a document's index and a part's fact sheet
  (written once from the datasheet, every number with its page); `lib_fetch` brings a symbol, footprint
  and 3D model for an LCSC code from EasyEDA's data, upgraded to KiCad's current format, for the pad-for-pad
  check against the drawing that follows.

Next: bring-up (milestone 6) and per-fab export profiles, driven by the first real product. See
[docs/tools.md](docs/tools.md) for the tool list.

## Requirements

* KiCad 10.0.x (built against 10.0.6, Windows 11). `kicad-cli` ships with it. Upgrade KiCad
  deliberately: the file formats and the API move with each major, `kicad_doctor` reports the
  drift, and the full test suite is the check.
* Python 3.12 or newer.
* `kicad-python` for the IPC channel (the board open in KiCad), as the `ipc` extra. Without it the
  doctor says so and every kicad-cli and file tool still works. Other extras: `parts` (easyeda2kicad
  behind `lib_fetch`), `preview` (PyMuPDF for sheet previews), `browser` (headless Chromium for
  datasheet portals), `dev` (pytest).
* Developed and tested on Windows 11; kicad-cli discovery also knows the macOS bundle and the Linux
  paths. One convenience is Windows-only: `build.py --open` starts KiCad through a scheduled task.

## Install

The quick way is the stack's bootstrap: clone [kicad-ai-stack](https://github.com/ShamanAndrey/kicad-ai-stack)
and run `python bootstrap.py`; it clones this repository next to it, creates the environment, installs the
extras and writes the Claude Code registration. On its own:

With [uv](https://docs.astral.sh/uv/):

```bash
uv sync --extra dev --extra ipc
uv run pytest
```

With pip:

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev,ipc]"
.venv\Scripts\python -m pytest
```

The default run is the fast loop: in parallel, without the slow tests (a full library index, a
whole review, a headless browser) and without the live ones. `pytest -m "not gui"` adds the slow
tests; `pytest -m ""` runs everything and needs KiCad open with its API on. Tests that need
`kicad-cli` skip themselves when it is not installed.

## Register with Claude Code

Put a `.mcp.json` in the directory that holds your KiCad projects:

```json
{
  "mcpServers": {
    "kicad-mcp-layer": {
      "command": "C:\\path\\to\\kicad-mcp-layer\\.venv\\Scripts\\python.exe",
      "args": ["-m", "kicad_layer"],
      "env": {
        "KICAD_LAYER_WORKSPACE": "C:\\path\\to\\your\\kicad\\projects",
        "KICAD_LAYER_MODE": "readonly"
      }
    }
  }
}
```

Register it in one place only. If Claude Code runs inside the Claude desktop app, quit and
relaunch the app after changing MCP configuration; a new session is not enough.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `KICAD_LAYER_WORKSPACE` | current directory | Every path must resolve under it |
| `KICAD_LAYER_MODE` | `readonly` | `write` allows design edits |
| `KICAD_CLI` | auto-detected | Explicit path to `kicad-cli`; if set and wrong, that is an error |
| `KICAD_API_SOCKET` | `ipc://%TEMP%\kicad\api.sock` | IPC address, honoured by `kicad-python` |
| `KICAD_LAYER_CACHE_DIR` | `%LOCALAPPDATA%\kicad-mcp-layer\cache` | Netlist and report cache |
| `KICAD_LAYER_LOG` | `INFO` | stderr log level |
| `KICAD_LAYER_TOOLS` | `core` | `core` registers checks, exports, renders, reviews, libraries, documents and board reads; `full` adds the design-edit tools and the routers |

To use the board tools, enable the API in KiCad: Preferences, Plugins, "Enable KiCad API". It
takes effect immediately.

## Design

The architecture decisions and the research behind them live in a companion `research/`
folder (not part of this package): a survey of every existing KiCad MCP server, KiCad 10's
file formats and API surface, and the tests that showed why no existing schematic parser
could be reused. Short version:

* Connectivity comes from `kicad-cli sch export netlist`, never from home-grown wire tracing.
* Every IPC failure is classified as unreachable or rejected; file edits are allowed only when
  unreachable, and never for a board this process has seen live.
* Schematic writes refuse while KiCad holds the editor lock file.
* Verdicts can say `UNVERIFIED`. A missing report is never a pass.
* Two tool tiers: `core` reads, checks, exports, renders, reviews and documents; `full` adds
  design edits and the frozen routers.

## Related

* [kicad-ai-stack](https://github.com/ShamanAndrey/kicad-ai-stack): the workspace around this library, with
  the setup script and the working rules for a Claude Code session.
* [examples/hello_world](examples/hello_world): a complete board as data on this library, the first one it built.
* [docs/tools.md](docs/tools.md) the tool reference, [docs/design-api.md](docs/design-api.md) the authoring API.

## Acknowledgements

Ideas and, where MIT-licensed, code from
[kicad-mcp-pro](https://github.com/oaslananka/kicad-mcp-pro),
[kicad-happy](https://github.com/aklofas/kicad-happy),
[lamaalrajih/kicad-mcp](https://github.com/lamaalrajih/kicad-mcp) and
[KiCAD-MCP-Server](https://github.com/mixelpixx/KiCAD-MCP-Server); lessons from
[Konnect](https://github.com/mixelpixx/Konnect). Test fixtures are KiCad's own demo projects.

## License

MIT. See [LICENSE](LICENSE).
