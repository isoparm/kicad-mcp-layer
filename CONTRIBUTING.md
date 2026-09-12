# Contributing

Thank you for looking. A few rules keep the layer honest; the long form is in [CLAUDE.md](CLAUDE.md),
which a Claude Code session reads too.

- **Tests first, fast.** `.venv\Scripts\python -m pytest` is the loop (about 30 s, parallel, no slow or
  live tests). Before a pull request: `pytest -m "not gui"`. Anything over ten seconds is marked `slow`.
- **Layering.** `kicad_layer.design` imports only the core, the writers and `routes`; `tests/test_layers.py`
  enforces it. The MCP server is the check-and-view layer; implementation lives outside `tools.py` and is
  testable without MCP.
- **Never the SWIG `pcbnew` module**; a test fails the build if it appears. Never print to stdout in the
  server (it runs on stdio). Every path a tool takes goes through `resolve_in_workspace`.
- **Failures are `LayerError(code, message, hint=...)`** with codes listed in `errors.py` and
  `docs/tools.md`. A check that produced no report is `UNVERIFIED`, never `PASS`.
- **Registries agree.** A new tool goes into `GROUPS` in `tools.py`, the table in `docs/tools.md` and the
  `covered` rows in `capabilities.py`; `tests/test_registry.py` checks. `python -m kicad_layer.design.api`
  regenerates the authoring sheet after a design-package change.
- **Fixtures stay KiCad-authored** (`tests/fixtures/NOTICE.md`); never hand-write one.
- **KiCad 10.0.6** is the version the layer is built against. An upgrade is a deliberate change: full suite
  after, the doctor's advice read.

Issues and pull requests on GitHub: [ShamanAndrey/kicad-mcp-layer](https://github.com/ShamanAndrey/kicad-mcp-layer).
The workspace around the library, with its setup script, is
[ShamanAndrey/kicad-ai-stack](https://github.com/ShamanAndrey/kicad-ai-stack).
