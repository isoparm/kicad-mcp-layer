"""The smallest complete project on the design package: a two-layer line buffer.

A signal header J1 stands in for the module (``ModuleSheet``): IN on pin 1, OUT on pin 3, ground on 2
and 4. The Buffer sheet is a ``Circuit`` and a ``Layout``: a dual opamp (OPA1678) whose unit A buffers
IN to OUT, whose unit B is unused (a follower on ground) and whose power unit C is decoupled on each
rail; the supply comes in on J2 (+12V / GND / -12V, flagged for ERC); two mounting holes go to the
flow. C3 is optional bulk, marked do-not-populate. ``Board`` places the parts on a 50 x 30 mm board
with a ground pour on the bottom; ``rules`` is JLCPCB's two-layer set (``design.rules.jlcpcb_2l``) on the
package's own KiCad 10 template, and ``make_project`` ties it together as a ``Project``.

Read it with ``docs/design-api.md``; build it with ``build.py`` next to it.
"""
from __future__ import annotations

from pathlib import Path

from kicad_layer.design.board import Board, Place, Text
from kicad_layer.design.board import build_board as _build_board
from kicad_layer.design.circuit import Circuit
from kicad_layer.design.module_sheet import DescribedModule, ModuleSheet, PowerGroup
from kicad_layer.design.parts import Part
from kicad_layer.design.project import Project, RootLayout, Rules
from kicad_layer.design.render import At, Decouple, Described, Flow, Layout
from kicad_layer.design.rules import jlcpcb_2l
from kicad_layer.design.signals import Signal, by_sheet, check_table

NAME = "two_layer_basic"

# -- parts: one description each, stamped on the symbol and carried to the BOM ------------------------

OPAMP = Part("OPAMP", ("Amplifier_Operational", "OPA1678"), "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", "OPA1678", "Texas Instruments", "OPA1678IDR")
R_100K = Part("R_100K", ("Device", "R"), "Resistor_SMD:R_0805_2012Metric", "100k", "Yageo", "RC0805FR-07100KL")
C_100N = Part("C_100N", ("Device", "C"), "Capacitor_SMD:C_0805_2012Metric", "100nF", "Samsung Electro-Mechanics", "CL21B104KBCNNNC")
C_10U = Part("C_10U", ("Device", "C"), "Capacitor_SMD:C_1206_3216Metric", "10uF 25V", "Samsung Electro-Mechanics", "CL31A106KAHNNNE")
HDR4 = Part("HDR4", ("Connector_Generic", "Conn_01x04"), "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical", "IN / GND / OUT / GND",
            "Wurth Elektronik", "61300411121")
HDR3 = Part("HDR3", ("Connector_Generic", "Conn_01x03"), "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical", "+12V / GND / -12V",
            "Wurth Elektronik", "61300311121")
HOLE = Part("HOLE", ("Mechanical", "MountingHole"), "MountingHole:MountingHole_3.2mm_M3", "M3", "", "")

# -- the signal table: what crosses from the header (the "module") to the Buffer sheet ---------------

SIGNALS = [Signal("IN", "1", "out", "Buffer", "line in, into unit A"), Signal("OUT", "3", "in", "Buffer", "unit A output")]
J1_GND = ["2", "4"]


def check_signals() -> None:
    check_table(SIGNALS, {}, J1_GND)


HEADER = ModuleSheet(parts=(("J1", HDR4, (127.0, 88.9), (127.0, 78.74), (127.0, 99.06)),), signals=SIGNALS, no_connect=[],
                     power=(PowerGroup("GND", J1_GND, offset=5.08, reach=7.62, from_end="bottom"),))

# -- the Buffer sheet: a circuit, then how it is drawn --------------------------------------------------


def buffer() -> Circuit:
    c = Circuit("Buffer", by_sheet(SIGNALS, "Buffer"))
    u1 = c.part("U1", OPAMP)
    r1 = c.part("R1", R_100K)
    c1, c2 = c.part("C1", C_100N), c.part("C2", C_100N)
    c3 = c.part("C3", C_10U, dnp=True)  # optional bulk: (dnp yes) on the symbol
    j2 = c.part("J2", HDR3)
    c.part("H1", HOLE)
    c.part("H2", HOLE)
    c.signal("IN", u1["3"], r1["1"])  # unit A: + input, with its bias resistor to ground
    c.gnd(r1["2"])
    c.signal("OUT", u1["1"], u1["2"])  # unit A: unity gain
    c.gnd(u1["5"])  # unit B unused: a follower on ground
    c.net(u1["6"], u1["7"], name="B_FB")
    c.rail("+12V", u1["8"], c1["1"], c3["1"], j2["1"])  # unit C: power
    c.rail("-12V", u1["4"], c2["1"], j2["3"])
    c.gnd(c1["2"], c2["2"], c3["2"], j2["2"])
    c.flag("+12V", "-12V", "GND")  # fed from J2: ERC cannot see a source
    c.note("U1B is unused: a follower on ground. C3 is optional bulk, not fitted (DNP).")
    return c


LAYOUT = Layout(
    # units by key: "U1" is unit 1 (A), "U1/B" unit 2, "U1/C" the power unit; R1 stands on its own with an IN label
    parts={"U1": At(101.6, 63.5), "U1/B": At(101.6, 101.6), "U1/C": At(165.1, 76.2), "R1": At(76.2, 50.8), "J2": At(50.8, 101.6)},
    flow=Flow(origin=(152.4, 127.0), width=76.2),  # what the layout leaves out (the holes) goes here
    decouple={("U1", "+12V"): Decouple(caps=("C1", "C3")), ("U1", "-12V"): Decouple(side="left", caps=("C2",)), "J2": Decouple(caps=())},
)

# -- the board ---------------------------------------------------------------------------------------

X0, Y0 = 100.0, 100.0


def _p(ref: str, x: float, y: float, rot: float = 0.0) -> Place:
    return Place(ref, (X0 + x, Y0 + y), rot)


def board(project_dir: Path) -> Board:
    return Board(title="Two-layer line buffer", scope=NAME, outline=(X0, Y0, 50.0, 30.0), radius=2.0, copper_layers=2,
                 planes=(("B.Cu", "GND", "GND bottom"),),
                 placements=(_p("H1", 4.0, 4.0), _p("H2", 46.0, 26.0), _p("J2", 5.0, 12.0), _p("J1", 45.0, 8.0),
                             _p("U1", 25.0, 15.0), _p("R1", 17.0, 15.0, 90), _p("C1", 31.0, 10.0), _p("C3", 31.0, 6.0), _p("C2", 19.0, 21.0)),
                 texts=(Text("LINE BUFFER", (X0 + 25.0, Y0 + 26.0), size=1.2),),
                 routes=project_dir / "routing" / "routes.json")


# -- the project ----------------------------------------------------------------------------------------


def rules() -> Rules:
    """JLCPCB's two-layer set on the package's own KiCad 10 template (``template=None``): silkscreen checks at warning,
    the rails and ground in the Power class."""
    return jlcpcb_2l(assignments=[{"netclass": "Power", "pattern": net} for net in ("+12V", "-12V", "GND")])


def sheets() -> dict:
    return {"Header": DescribedModule(HEADER), "Buffer": Described(buffer(), LAYOUT)}


def make_project(project_dir: Path) -> Project:
    project_dir = Path(project_dir).resolve()
    b = board(project_dir)

    def board_builder(out_path, sheetfile, netlist, symbol_paths, setup_template, with_routes=True):
        return _build_board(b, out_path, sheetfile, netlist, symbol_paths, setup_template, date="2026-09-22", rev="1", company="", with_routes=with_routes)

    return Project(name=NAME, dir=project_dir, title="Two-layer line buffer", date="2026-09-22", rev="1", company="", signals=SIGNALS,
                   root=RootLayout(module_sheet="Header", left_sheets=["Buffer"], right_sheets=[], paper="A4", papers={"Buffer": "A3"}),
                   rules=rules(), sheets=sheets, check_signals=check_signals, board_builder=board_builder)
