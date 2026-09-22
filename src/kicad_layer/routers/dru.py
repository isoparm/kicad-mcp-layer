"""A board's custom design rules (.kicad_dru), sorted by what a Specctra DSN can carry.

FreeRouting knows net classes with one width and one clearance each, class-to-class clearances and
keep-out areas. A .kicad_dru says much more: creepage, physical clearance, disallowed items, rules
conditioned on rule areas or footprints, per-net-name rules. This module reads the rules and answers
the questions the DSN export asks:

* which nets a rule names (``A.NetClass == 'HV'``, ``A.hasNetclass('HV')``, ``A.NetName == '/HV_IN'``);
* whether a clearance rule is a plain two-class rule that maps onto a DSN ``class_class`` entry;
* which rules disallow vias or tracks inside a named area, so that area becomes a DSN keep-out.

Everything else is reported, never silently dropped: the router cannot honour it.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..sexpr import child, children, parse_all, tag

_UNITS = {"mm": 1.0, "um": 0.001, "mil": 0.0254, "mils": 0.0254, "in": 25.4, "inch": 25.4}

# constraint kinds a DSN cannot express, whose nets FreeRouting would route blind
UNEXPRESSIBLE = ("disallow", "creepage", "physical_clearance", "physical_hole_clearance")
# condition functions that tie a rule to geometry the DSN does not carry
AREA_FUNCS = ("enclosedByArea", "intersectsArea", "insideArea", "memberOfFootprint", "insideCourtyard",
              "intersectsCourtyard", "insideFrontCourtyard", "insideBackCourtyard", "intersectsFrontCourtyard",
              "intersectsBackCourtyard")
# constraint kinds that are a clearance between two items and so may become a class_class rule
CLEARANCE_KINDS = ("clearance", "physical_clearance", "creepage")


def length_mm(text: str) -> float | None:
    """A .kicad_dru length ('7.25mm', '10mil', '0.2') in millimetres; bare numbers are mm."""
    m = re.match(r"^\s*(-?[\d.]+)\s*([a-z]*)\s*$", str(text))
    if not m:
        return None
    unit = m.group(2) or "mm"
    if unit not in _UNITS:
        return None
    return float(m.group(1)) * _UNITS[unit]


@dataclass
class Constraint:
    kind: str
    args: list[str] = field(default_factory=list)  # bare atoms after the kind, e.g. ['via'] for disallow via
    min: float | None = None
    max: float | None = None
    opt: float | None = None


@dataclass
class Rule:
    name: str
    condition: str = ""
    layer: str | None = None
    constraints: list[Constraint] = field(default_factory=list)

    def kinds(self) -> set[str]:
        return {c.kind for c in self.constraints}

    def area_functions(self) -> list[tuple[str, str]]:
        """(function, argument) for every area or footprint function in the condition."""
        return [(m.group(1), m.group(2)) for m in re.finditer(r"\b(" + "|".join(AREA_FUNCS) + r")\(\s*'([^']*)'\s*\)", self.condition)]

    def named_classes(self) -> set[str]:
        """Net classes the condition selects positively (== or hasNetclass, not !=)."""
        out = {m.group(1) for m in re.finditer(r"\b[AB]\.NetClass\s*==\s*'([^']*)'", self.condition)}
        out |= {m.group(1) for m in re.finditer(r"(?<!!)\b[AB]\.hasNetclass\(\s*'([^']*)'\s*\)", self.condition)}
        return out

    def named_nets(self) -> set[str]:
        """Net-name patterns the condition selects positively."""
        return {m.group(1) for m in re.finditer(r"\b[AB]\.NetName\s*==\s*'([^']*)'", self.condition)}


def load_rules(path: Path | None) -> list[Rule]:
    """Every rule in a .kicad_dru; an absent file has none."""
    if path is None or not path.is_file():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    # comments run from '#' to the end of the line, outside strings
    text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    out: list[Rule] = []
    for node in parse_all(text):
        if tag(node) != "rule" or len(node) < 2:
            continue
        cond = child(node, "condition")
        lyr = child(node, "layer")
        rule = Rule(name=str(node[1]), condition=str(cond[1]) if cond is not None and len(cond) > 1 else "",
                    layer=str(lyr[1]) if lyr is not None and len(lyr) > 1 else None)
        for c in children(node, "constraint"):
            if len(c) < 2:
                continue
            con = Constraint(kind=str(c[1]))
            for a in c[2:]:
                if isinstance(a, list) and a and str(a[0]) in ("min", "max", "opt") and len(a) > 1:
                    setattr(con, str(a[0]), length_mm(str(a[1])))
                elif not isinstance(a, list):
                    con.args.append(str(a))
            rule.constraints.append(con)
        out.append(rule)
    return out


def dru_for(board: Path, project: Path | None = None) -> Path | None:
    """The .kicad_dru that goes with a board: next to the project file, else next to the board."""
    for cand in ([project.with_suffix(".kicad_dru")] if project is not None else []) + [board.with_suffix(".kicad_dru")]:
        if cand.is_file():
            return cand
    return None


def nets_of_rule(rule: Rule, nets: list[str], class_of: dict[str, str]) -> set[str]:
    """The board nets a rule names, by class or by name pattern (KiCad's == matches wildcards)."""
    classes = rule.named_classes()
    patterns = rule.named_nets()
    return {n for n in nets if class_of.get(n) in classes or any(fnmatch.fnmatchcase(n, p) for p in patterns)}


# --------------------------------------------------------------------------------------
# two-class clearance rules
# --------------------------------------------------------------------------------------

_TERM_CLASS = re.compile(r"^(!)?\s*([AB])\.(?:NetClass\s*(==|!=)\s*'([^']*)'|hasNetclass\(\s*'([^']*)'\s*\))$")


def _strip_parens(s: str) -> str:
    s = s.strip()
    while s.startswith("(") and s.endswith(")"):
        depth = 0
        for i, ch in enumerate(s):
            depth += ch == "("
            depth -= ch == ")"
            if depth == 0 and i < len(s) - 1:
                return s  # the outer pair does not enclose everything
        s = s[1:-1].strip()
    return s


def class_pairs(rule: Rule, all_classes: list[str]) -> tuple[list[tuple[str, str]], str | None]:
    """The (class, class) pairs a plain net-class clearance condition covers, or ([], reason).

    Understood: ``A.NetClass == 'X' && B.NetClass == 'Y'`` (X to Y), ``A.NetClass == 'X' &&
    B.NetClass != 'X'`` (X to every other class), and ``A.NetClass == 'X'`` alone (X to every class,
    itself included), with ``hasNetclass('X')`` as a synonym for ``NetClass == 'X'``. Anything else
    (an ``||``, a NetName, an area, an item type) is ambiguous for a class matrix and is skipped."""
    cond = _strip_parens(rule.condition)
    if not cond:
        return [], "no condition (a board-wide clearance belongs in the net classes)"
    if "||" in cond:
        return [], "the condition has an OR"
    sides: dict[str, tuple[str, str]] = {}
    for term in (_strip_parens(t) for t in cond.split("&&")):
        m = _TERM_CLASS.match(term)
        if not m:
            return [], f"term {term!r} is not a plain net-class test"
        neg, side = bool(m.group(1)), m.group(2)
        if m.group(4) is not None:
            op, cls = m.group(3), m.group(4)
            if neg:
                op = "!=" if op == "==" else "=="
        else:
            op, cls = ("!=" if neg else "=="), m.group(5)
        if side in sides:
            return [], f"two tests on {side}"
        sides[side] = (op, cls)
    a, b = sides.get("A"), sides.get("B")
    if a is None and b is not None:
        a, b = b, None
    if a is None or a[0] != "==":
        return [], "no side selects a class with =="
    x = a[1]
    if x not in all_classes:
        return [], f"class {x!r} has no nets on the board"
    if b is None:
        return [(x, y) for y in all_classes], None
    op, y = b
    if op == "==":
        if y not in all_classes:
            return [], f"class {y!r} has no nets on the board"
        return [(x, y)], None
    return [(x, z) for z in all_classes if z != y], None
