# Review checks

What each check looks at, the limit it applies, and where that limit comes from. Checks read
the design files directly; ERC, DRC and the netlist come from kicad-cli.

## Board

| Check | Verdicts | What it measures | Limit and source |
|---|---|---|---|
| `board` | INFO | Size, copper layers, footprints, tracks, vias, zones, plated holes | none |
| `drc` | PASS/WARN/FAIL | KiCad's design rules check with schematic parity when a schematic exists | project design rules |
| `unrouted` | PASS/FAIL | DRC's unconnected items, listed on their own | any unrouted connection fails |
| `zone_fills` | PASS/WARN | Zones with fill requested but no filled polygons in the file | a file DRC on unfilled zones is not the real board |
| `off_board` | PASS/WARN/FAIL/UNVERIFIED | Footprint origins outside the Edge.Cuts bounding box (FAIL), courtyards crossing it (WARN) | none; UNVERIFIED without an outline |
| `dfm` | PASS/WARN/FAIL | Track width, project clearance vs fab spacing, via drill and diameter, via and PTH annular rings, via hole spacing, copper to edge (bounding-box estimate), board size, silkscreen text height and stroke, layer count | JLCPCB published capabilities, 2- and 4-layer 1 oz, checked 2026-09-04 |
| `power_tracks` | PASS/WARN | Narrowest track on nets whose names look like power rails | 0.25 mm; IPC-2152 1 oz external 10 C rise, about 0.5 A at 0.25 mm |
| `stitching` | PASS/WARN/INFO | Vias on each net that has a zone | a zone net with no vias is only reachable on its own layer |
| `decoupling` | PASS/WARN/INFO/UNVERIFIED | For every IC power_in pin (from the netlist), distance to the nearest capacitor on that net | kicad-happy EMC DC-001: over 8 mm warning, over 5 mm note |

Power-net name pattern: `+5V`, `3V3`, `VCC`, `VDD`, `VBUS`, `VIN`, `VOUT`, `GND`, `VBAT`,
`VSYS`, `PWR`, `VREF` and variants.

## Schematic

| Check | Verdicts | What it measures | Limit and source |
|---|---|---|---|
| `erc` | PASS/WARN/FAIL | KiCad's electrical rules check on the whole hierarchy | project ERC settings |
| `footprints` | PASS/FAIL | Components without a footprint | none allowed |
| `values` | PASS/WARN | Components still carrying the library default value such as `R` or `C` | connectors, holes, test points and switches exempt |
| `annotation` | PASS/FAIL | References containing `?` | none allowed |
| `power_sources` | PASS/FAIL/UNVERIFIED | Nets with power_in pins and no power_out pin or PWR_FLAG, taken from ERC's `power_pin_not_driven` rule; the summary names the nets that rely on a PWR_FLAG alone. UNVERIFIED when ERC did not run or the project ignores the rule | KiCad ERC; the exported netlist cannot see PWR_FLAGs, so it only names nets |
| `decoupling_sch` | PASS/WARN | IC power_in nets with no capacitor on them at all | netlist |
| `bom` | INFO | Component count by prefix, distinct value and footprint pairs, net count | none |
| `spice` | UNVERIFIED | SPICE netlist exported; elements without a model counted; no engine available | KiCad ships the ngspice library only |

## Reading a report

The report verdict is the worst of the checks that ran. `unverified` lists the checks that
could not run and their reasons; a review with unverified checks is not a clean bill of
health. Findings carry `value` and `limit` so the size of a miss is visible, and a location
where one applies. Identical findings are collapsed into one line whose `count` says how many
there were; the position and detail belong to the first. Errors come first, then the most
frequent. `counts` on the report sums the real numbers, not the lines. A check shows at most
50 lines and says `truncated` when it had more.

Copper layers are counted from the board's layer table, including inner layers of type
`power` or `mixed`; JLCPCB limits exist for 2 and 4 layers, and a board with more gets the
4-layer limits plus a warning saying so.
