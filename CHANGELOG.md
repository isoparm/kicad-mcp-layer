# Changelog

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
