"""Parts with identity, and values that are numbers.

A :class:`Part` is the one description of a component: its schematic symbol, its footprint, the
Value text the schematic shows, and the manufacturer data the bill of materials needs. Placing
a part stamps all of that on the symbol, so a symbol and its part cannot drift apart the way a
value string and a lookup table keyed on that string could.

A :class:`Quantity` is a passive's value as a number with a unit and, when stated, a tolerance,
parsed from the same text the schematic shows ("10uF", "2k2", "22uF 10V", "470uF 16V polymer").
The text is kept verbatim: the schematic keeps showing exactly what was written.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from kicad_layer.sch_writer import Placed, SchematicBuilder

_PREFIX = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "k": 1e3, "M": 1e6, "R": 1.0, "": 1.0}
_UNIT = {"F": "F", "H": "H", "R": "Ohm", "ohm": "Ohm", "Ohm": "Ohm", "Ω": "Ohm", "V": "V", "A": "A", "Hz": "Hz"}


@dataclass(frozen=True)
class Quantity:
    text: str
    value: float | None = None  # SI magnitude, None when the text is not a number
    unit: str = ""  # F, H, Ohm, V, A, Hz or ""
    tolerance: float | None = None  # fraction (0.05 for 5 %), None when unstated
    extras: tuple[str, ...] = ()  # the rest of the text: "10V", "polymer", "X7R"

    @classmethod
    def parse(cls, text: str, *, unit_hint: str = "") -> "Quantity":
        """``"4.7nF"``, ``"2k2"``, ``"0R"``, ``"22uF 10V"``, ``"1kohm +/- 5%"``, ``"470uF 16V polymer"``.

        ``unit_hint`` names the unit when the text carries none (a resistor's "10k")."""
        words = text.split()
        if not words:
            return cls(text)
        first, rest = words[0], words[1:]
        tol = None
        joined = " ".join(rest)
        m = re.search(r"(?:\+/-|±)\s*([\d.]+)\s*%", joined)
        if m:
            tol = float(m.group(1)) / 100
            rest = [w for w in joined.replace(m.group(0), "").split() if w]
        # 2k2 / 4R7 style: prefix letter as the decimal point
        m = re.fullmatch(r"(\d+)([pnuµmkMR])(\d+)", first)
        if m:
            mag = float(f"{m.group(1)}.{m.group(3)}") * _PREFIX[m.group(2)]
            unit = "Ohm" if m.group(2) == "R" else unit_hint
            return cls(text, mag, unit, tol, tuple(rest))
        m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([pnuµmkM]?)\s*(F|H|R|ohm|Ohm|Ω|V|A|Hz)?", first)
        if m:
            mag = float(m.group(1)) * _PREFIX[m.group(2)]
            unit = _UNIT.get(m.group(3) or "", "") or unit_hint
            if m.group(3) == "R":
                unit = "Ohm"
            return cls(text, mag, unit, tol, tuple(rest))
        return cls(text, None, "", tol, tuple(rest))

    def __str__(self) -> str:
        return self.text


_UNIT_FOR_SYMBOL = {"R": "Ohm", "C": "F", "C_Polarized": "F", "L": "H"}


@dataclass(frozen=True)
class Part:
    id: str
    symbol: tuple[str, str]  # (library, symbol name)
    footprint: str  # "Library:Footprint"
    value: str  # the Value text the schematic shows
    manufacturer: str
    mpn: str
    package: str = ""  # PCBWay's Package column; empty means "derive it from the footprint name"
    lcsc: str = ""  # LCSC code when the very same part is in JLCPCB's catalogue
    note: str = ""  # PCBWay's "Your notes" column
    hand: bool = False  # through-hole, soldered by hand at assembly
    fields_extra: dict[str, str] = field(default_factory=dict)
    in_bom: bool | None = None  # a purchased part on a symbol KiCad excludes from the BOM by default (a standoff on a mounting hole)

    @property
    def quantity(self) -> Quantity:
        return Quantity.parse(self.value, unit_hint=_UNIT_FOR_SYMBOL.get(self.symbol[1], ""))

    def fields(self) -> dict[str, str]:
        """The symbol properties that carry the part to the bill of materials."""
        f = {"Manufacturer": self.manufacturer, "MPN": self.mpn}
        if self.package:
            f["Package"] = self.package
        if self.lcsc:
            f["LCSC"] = self.lcsc
        if self.note:
            f["PCBWay Note"] = self.note
        f.update(self.fields_extra)
        return f


def place_part(sch: SchematicBuilder, ref: str, part: Part, at: tuple[float, float], *, value_text: str | None = None, **kwargs) -> Placed:
    """Place ``part`` as ``ref``: symbol, footprint, value and manufacturer fields all from the part.

    ``value_text`` overrides the shown value for parts whose label is per placement (a connector's purpose)."""
    if part.in_bom is not None:
        kwargs.setdefault("in_bom", part.in_bom)
    return sch.place(part.symbol[0], part.symbol[1], ref, at, value_text=part.value if value_text is None else value_text,
                     footprint=part.footprint, extra_props=part.fields(), **kwargs)


def verify(sch: SchematicBuilder) -> list[str]:
    """References of BOM symbols on one sheet that carry no manufacturer part."""
    from kicad_layer.sexpr import children, tag, value

    missing: list[str] = []
    for node in sch.items:
        if tag(node) != "symbol":
            continue
        lib_id = value(node, "lib_id") or ""
        if lib_id.startswith("power:") or value(node, "in_bom") == "no":
            continue
        props = {str(p[1]): str(p[2]) for p in children(node, "property")}
        ref = props["Reference"]
        if ref.startswith("#"):
            continue
        if not props.get("MPN"):
            missing.append(ref)
    return missing


def verify_all(builders: list[SchematicBuilder]) -> list[str]:
    """Every BOM symbol on the given sheets carries Manufacturer and MPN; returns the references that do not."""
    out: list[str] = []
    for b in builders:
        out += verify(b)
    return out
