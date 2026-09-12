"""Pad-level comparison of a build's netlist with a reference design's.

For every pin of the module (the part both designs share, a Compute Module here) the report says
which pads its net reaches in each design, named by footprint and pad so the two sides compare
whatever the references are called. Two-pin parts are named by kind and value with what their far
end reaches (a rail, ground, or a net); ICs and connectors by footprint (or symbol) and pad. Net
names never matter. The verdict per pin: ``same``, ``differ``, ``ours only``, ``reference only``,
``power`` (a rail or ground on both sides), ``power mismatch``, or ``nc``.

    python -m kicad_layer.design.compare ours.xml reference.xml --module MOD1,MOD2 --reference-module Module1 [--out report.md]

The build runs it with ``--review`` from the project's ``Review`` data and prints the summary: the
counts and one line per pin that is not the same. The full report, connector pads of both designs
included, goes to the file. Found a display I2C on the wrong bus on 2026-09-08, which
a net-level comparison without pad numbers had passed.
"""
from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

RAIL = re.compile(r"^\+?(\d+V\d*|\d+\.\d+V|VBUS|VCC\w*|VDD\w*|3V3\w*|5V\w*|1V8\w*|V5_\w+|VSYS\w*|VIN\w*|POE_\w+)$", re.I)
GROUND = re.compile(r"^(GND\w*|\w*_GND|AGND|DGND)$", re.I)
PASSIVE = ("R", "C", "L", "D", "FB", "TVS")
CONNECTOR = ("J", "P", "CN")


@dataclass(frozen=True)
class Node:
    """One pad on a net, named so the two designs compare: a footprint or symbol and its pad, or a two-pin part and its far end."""

    label: str
    pin: str
    function: str = ""
    far: str = ""  # two-pin parts: what the other pin reaches ("rail 3.3V", "GND", "net X")
    connector: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return (self.label, self.far) if self.far else (self.label, self.pin)

    def text(self) -> str:
        if self.far:
            return f"{self.label} -> {self.far}"
        return f"{self.label}.{self.pin}" + (f"[{self.function}]" if self.function else "")


@dataclass
class Design:
    comps: dict[str, dict]  # ref -> {"value", "part", "footprint"}
    nets: dict[str, list[tuple[str, str, str]]]  # net -> (ref, pin, pin function)
    pin_net: dict[tuple[str, str], str]

    def pins_of(self, refs: Sequence[str]) -> list[str]:
        """The numeric pins of ``refs``, in order."""
        pins = {p for (r, p) in self.pin_net if r in refs and p.isdigit()}
        return sorted(pins, key=int)


def load(xml_path: Path) -> Design:
    """A ``kicad-cli sch export netlist --format kicadxml`` file."""
    root = ET.parse(xml_path).getroot()
    comps: dict[str, dict] = {}
    for c in root.iter("comp"):
        lib = c.find("libsource")
        comps[c.get("ref", "")] = {"value": (c.findtext("value") or "").strip(), "part": lib.get("part", "") if lib is not None else "",
                                   "footprint": (c.findtext("footprint") or "").split(":")[-1]}
    nets: dict[str, list[tuple[str, str, str]]] = {}
    pin_net: dict[tuple[str, str], str] = {}
    for net in root.iter("net"):
        name = net.get("name", "")
        nodes = [(n.get("ref", ""), n.get("pin", ""), n.get("pinfunction") or "") for n in net.findall("node")]
        nets[name] = nodes
        for ref, pin, _ in nodes:
            pin_net[(ref, pin)] = name
    return Design(comps, nets, pin_net)


def short(net: str) -> str:
    return net.rsplit("/", 1)[-1]


def rail_name(s: str) -> str:
    """'+3V3', '+3.3v' and '3V3' are one rail: '3.3V'."""
    s = s.strip().lstrip("+").upper()
    return re.sub(r"^(\d+)V(\d+)$", r"\1.\2V", s)


def kind(design: Design, net: str | None) -> str | None:
    """'NC', 'GND', 'rail X' or None for a signal."""
    if not net:
        return "NC"
    s = short(net)
    if net.startswith("unconnected-") or len(design.nets.get(net, [])) <= 1:
        return "NC"
    if GROUND.match(s):
        return "GND"
    if RAIL.match(s):
        return "rail " + rail_name(s)
    return None


def norm_value(v: str) -> str:
    """'2k2', '2.2K' and '2200' are one value; '100nF' and '0.1uF' are one; anything else lower-cased."""
    t = v.split()[0] if v else ""
    t = t.replace("Ω", "").replace("ohm", "").replace("Ohm", "")
    m = re.match(r"^(\d+)([kKmMrR])(\d+)$", t)  # 2k2, 4R7
    if m:
        t = f"{m.group(1)}.{m.group(3)}{m.group(2)}"
    m = re.match(r"^(\d+(?:\.\d+)?)([kKmMuUnNpPrR]?)(F|H)?$", t)
    if not m:
        return t.lower()
    num, mult = float(m.group(1)), m.group(2).lower()
    val = num * {"k": 1e3, "m": 1e-3, "u": 1e-6, "n": 1e-9, "p": 1e-12, "r": 1, "": 1}[mult]
    if m.group(3) == "F" or mult in ("u", "n", "p"):
        for scale, unit in ((1e-6, "u"), (1e-9, "n"), (1e-12, "p")):
            if val >= scale:
                return f"{val / scale:g}{unit}"
    if val >= 1e6:
        return f"{val / 1e6:g}M"
    if val >= 1e3:
        return f"{val / 1e3:g}k"
    return f"{val:g}"


def _dropped(ref: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch(ref, p) for p in patterns)


def members(design: Design, net: str, skip: Sequence[str] = (), ignore: Sequence[str] = ()) -> list[Node]:
    """Every pad on ``net`` except those of ``skip`` and of references matching ``ignore`` (fnmatch), as comparable nodes."""
    out: list[Node] = []
    for ref, pin, fn in design.nets.get(net, []):
        if ref in skip or _dropped(ref, ignore):
            continue
        c = design.comps.get(ref, {"value": "", "part": "", "footprint": ""})
        prefix = re.match(r"[A-Za-z]+", ref)
        k = prefix.group(0) if prefix else ref
        pins = [p for (r, p) in design.pin_net if r == ref]
        if k in PASSIVE and len(pins) == 2:
            other = next(design.pin_net[(ref, p)] for p in pins if p != pin)
            far = kind(design, other) or ("net " + short(other))
            out.append(Node(f"{k} {norm_value(c['value'])}", pin, fn, far=far))
        else:
            label = c["footprint"] or c["part"] or ref
            out.append(Node(label, pin, fn, connector=k in CONNECTOR))
    return sorted(out, key=lambda n: (n.label, n.far, n.pin))


@dataclass
class Row:
    pin: str
    function: str
    verdict: str
    ours: list[Node] = field(default_factory=list)
    ref: list[Node] = field(default_factory=list)
    ours_kind: str | None = None
    ref_kind: str | None = None


@dataclass
class Report:
    rows: list[Row]
    module: tuple[str, ...]
    reference_module: tuple[str, ...]

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.rows:
            out[r.verdict] = out.get(r.verdict, 0) + 1
        return out

    def summary(self, width: int = 150, max_pins: int = 30) -> list[str]:
        """The counts, then one line per pin that is not the same: what ours reaches, what the reference reaches.

        Power mismatches first, then pins only one side connects, then pins both connect differently;
        after ``max_pins`` the rest is a count and the report has them.
        """
        c = self.counts()
        lines = [", ".join(f"{k} {v}" for k, v in sorted(c.items(), key=lambda kv: -kv[1]))]
        order = {"power mismatch": 0, "ours only": 1, "reference only": 2, "differ": 3}
        rows = sorted((r for r in self.rows if r.verdict in order), key=lambda r: (order[r.verdict], int(r.pin)))
        for r in rows[:max_pins]:
            lines.append(f"{r.pin:>4s} {r.function:20s} {r.verdict:15s} ours: {_side(r.ours, r.ours_kind)}"[:width])
            lines.append(f"{'':41s} ref:  {_side(r.ref, r.ref_kind)}"[:width])
        if len(rows) > max_pins:
            lines.append(f"... and {len(rows) - max_pins} more pins in the report")
        return lines

    def markdown(self, ours_name: str, ref_name: str, ours: Design | None = None, ref: Design | None = None) -> str:
        """The whole report: every module pin with both sides, then the connector pads of both designs."""
        c = self.counts()
        out = [f"# {ours_name} against {ref_name}, pad for pad", "",
               f"Module: {', '.join(self.module)} here, {', '.join(self.reference_module)} in the reference. "
               + ", ".join(f"{k} {v}" for k, v in sorted(c.items(), key=lambda kv: -kv[1])) + ".", "",
               "| pin | function | verdict | ours | reference |", "|---|---|---|---|---|"]
        for r in self.rows:
            if r.verdict == "nc":
                continue
            out.append(f"| {r.pin} | {r.function} | {r.verdict} | {_side(r.ours, r.ours_kind)} | {_side(r.ref, r.ref_kind)} |")
        out.append("")
        for name, d, skip in ((ours_name, ours, self.module), (ref_name, ref, self.reference_module)):
            if d is None:
                continue
            out += [f"## Connector pads in {name}", "", "Signal pads only; pads on a rail or ground are left out.", ""]
            for ref_, comp in sorted(d.comps.items()):
                prefix = re.match(r"[A-Za-z]+", ref_)
                if not prefix or prefix.group(0) not in CONNECTOR:
                    continue
                label = comp["footprint"] or comp["part"] or ref_
                pads = sorted(((r, p) for (r, p) in d.pin_net if r == ref_), key=lambda k: (int(k[1]) if k[1].isdigit() else 999, k[1]))
                lines = []
                for r_, p in pads:
                    net = d.pin_net[(r_, p)]
                    if kind(d, net) is not None:  # open, a rail or ground: not a wiring question
                        continue
                    m = [n for n in members(d, net) if not (n.label == label and n.pin == p)]
                    if not m:
                        continue
                    lines.append(f"  {p:>3s} {short(net)[:24]:24s} " + "; ".join(n.text() for n in m)[:140])
                if lines:
                    out += [f"- **{ref_}** {comp['part']} `{label}`", "", "```", *lines, "```", ""]
        return "\n".join(out) + "\n"


def _side(nodes: list[Node], k: str | None) -> str:
    if nodes:
        return "; ".join(n.text() for n in nodes)
    return "open" if k in (None, "NC") else k


def compare(ours: Design, ref: Design, module: Sequence[str], reference_module: Sequence[str], *,
            ignore: Sequence[str] = (), ignore_reference: Sequence[str] = ()) -> Report:
    """One row per module pin, both designs side by side, with a verdict.

    ``ignore`` and ``ignore_reference`` are fnmatch patterns of references left out of the comparison
    (a reference design's expansion header or test points, say); a pin that reaches nothing else counts
    as open on that side.
    """
    ours_pins = {p: r for (r, p) in ours.pin_net if r in module and p.isdigit()}
    ref_pins = {p: r for (r, p) in ref.pin_net if r in reference_module and p.isdigit()}
    rows: list[Row] = []
    for pin in sorted(set(ours_pins) | set(ref_pins), key=int):
        onet = ours.pin_net.get((ours_pins[pin], pin)) if pin in ours_pins else None
        rnet = ref.pin_net.get((ref_pins[pin], pin)) if pin in ref_pins else None
        fn = next((f for r, p, f in ours.nets.get(onet or "", []) if p == pin and r in module), "") or \
            next((f for r, p, f in ref.nets.get(rnet or "", []) if p == pin and r in reference_module), "")
        ok, rk = kind(ours, onet), kind(ref, rnet)
        om = members(ours, onet, module, ignore) if onet and ok is None else []  # a rail or ground is named, not listed
        rm = members(ref, rnet, reference_module, ignore_reference) if rnet and rk is None else []
        if ok is None and not om:
            ok = "NC"
        if rk is None and not rm:
            rk = "NC"
        if ok == "NC" and rk == "NC":
            verdict = "nc"
        elif (ok in ("GND",) or (ok or "").startswith("rail")) or (rk in ("GND",) or (rk or "").startswith("rail")):
            verdict = "power" if ok == rk else "power mismatch"
        elif ok == "NC":
            verdict = "reference only"
        elif rk == "NC":
            verdict = "ours only"
        else:
            verdict = "same" if {n.key for n in om} == {n.key for n in rm} else "differ"
        rows.append(Row(pin, fn, verdict, om, rm, ok, rk))
    return Report(rows, tuple(module), tuple(reference_module))


USAGE = ("usage: python -m kicad_layer.design.compare ours.xml reference.xml --module MOD1,MOD2 --reference-module Module1 "
         "[--ignore J9,TP*] [--ignore-reference J8,TP*] [--out report.md]")


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if len(args) < 2 or "--module" not in argv or "--reference-module" not in argv:
        print(USAGE)
        return 2
    module = tuple(argv[argv.index("--module") + 1].split(","))
    ref_module = tuple(argv[argv.index("--reference-module") + 1].split(","))
    ignore = tuple(argv[argv.index("--ignore") + 1].split(",")) if "--ignore" in argv else ()
    ignore_ref = tuple(argv[argv.index("--ignore-reference") + 1].split(",")) if "--ignore-reference" in argv else ()
    ours, ref = load(Path(args[0])), load(Path(args[1]))
    report = compare(ours, ref, module, ref_module, ignore=ignore, ignore_reference=ignore_ref)
    for line in report.summary():
        print(line)
    if "--out" in argv:
        out = Path(argv[argv.index("--out") + 1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report.markdown(Path(args[0]).stem, Path(args[1]).stem, ours, ref), encoding="utf-8", newline='\n')
        print(f"report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
