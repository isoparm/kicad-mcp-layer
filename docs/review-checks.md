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
| `dfm` | PASS/WARN/FAIL | Track width, project clearance vs fab spacing, via drill and diameter, via ring (the fab's via rule: diameter minus hole), PTH annular ring, via hole spacing, copper to edge (exact distance to the Edge.Cuts segments; an edge connector's overhang is info), board size, silkscreen text height and stroke (one aggregated line per problem), layer count | JLCPCB published capabilities, 2- and 4-layer 1 oz, checked 2026-09-04: via ring 0.05 mm absolute, 0.075 mm preferred |
| `decoupling` | PASS/WARN/INFO/UNVERIFIED | For every IC power_in pin (from the netlist; if the symbol marks its supply pins passive, the pins on supply-named nets), pad-to-pad distance to the nearest capacitor on that net, and that capacitor's ground: a ground via within 1.5 mm or ground pour under the pad | kicad-happy EMC DC-001: over 8 mm warning, over 5 mm note |
| `diff_pairs` | PASS/WARN/INFO | Pairs by suffix (`_P/_N`, `+/-`, `_DP/_DM`, `D+/D-`): length skew, layer changes, spacing. Low-speed pairs (SPK, LED, motor, audio) are skipped; USB full speed gets an info note instead of a skew limit | pair rules of the interface; full-speed USB tolerates centimetres |

### Layout rules (`layout=True`, the default)

These read the zone fills KiCad wrote into the board, so fill zones first (`pcb_refill_zones`); a check that
needs fills and finds none is UNVERIFIED. `review_board` takes `currents` (`{"VBUS": 2.0}`, in A) and
`fast_nets` (names or globs added to the built-in fast-net pattern).

| Check | Verdicts | What it measures | Limit and source |
|---|---|---|---|
| `via_in_pad` | PASS/WARN | Vias drilled into a surface-mount pad's real shape, except the exposed pad of its own net (thermal vias) | IPC-7095 and assembly guidance: no open vias in SMD pads unless planned and plugged |
| `test_points` | PASS/WARN/INFO | Supply rails, ground, programming nets (SWD, JTAG, reset, BOOTn) and bus nets with no test point footprint or connector pin on them | design-for-test practice |
| `thermal_pads` | PASS/WARN | Exposed pads: vias of the pad's net inside the pad; fill of that net on the other side under the pad | exposed-pad layout guides (TI SLMA002, SLUA271) |
| `fast_edge` | PASS/WARN/UNVERIFIED | Distance from fast-net tracks (clocks, USB, I2S, SPI, SWCLK, switch nodes and `fast_nets`) to the board edge | 4 to 5 times the height above the reference plane, from the stack-up; UNVERIFIED without one |
| `stitching` | PASS/WARN/INFO | Vias on each zone net; plane pairs stitched along the edge; a ground via near each fast-net layer change | edge stitching every 15 mm (lambda/20); return via within 2 mm |
| `switcher_loop` | PASS/WARN/INFO | Switch node by name or IC + inductor + diode; input capacitor on the IC's supply with the smallest loop, copper to copper; its ground to the diode or IC ground | capacitor within 3 mm of the pin, return within 5 mm, both on one layer (TI SNVA021, ADI AN-136) |
| `power_tracks` | PASS/WARN | Narrowest track on power-named nets (not ADC, SENS, DIV, FB, REF, DET nets) and on nets given in `currents`: width against the current, vias at layer changes | 0.25 mm minimum; IPC-2221 fit I = k ΔT^0.44 A^0.725, k 0.048 outer and 0.024 inner, 1 oz, 10 C rise; vias as 25 um barrels |
| `antenna` | PASS/WARN/INFO | Antenna footprints (modules by name, `ANT` references): copper, pads, vias and pours inside the keep-out (from the footprint's rule area or the board's) | radio module datasheets |
| `plane_reference` | PASS/WARN/UNVERIFIED | Fast-net tracks sampled every 0.25 mm against the ground or supply fill on the adjacent layer; bare stretches longer than 1 mm | a signal's return path needs a continuous plane under its whole length |

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
| `decoupling_sch` | PASS/WARN/UNVERIFIED | IC power_in nets (or, when the symbols mark supply pins passive, supply-named nets) with no capacitor on them at all; UNVERIFIED when neither finds a supply pin | netlist |
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
