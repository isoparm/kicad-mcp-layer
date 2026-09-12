"""A small, faithful S-expression reader and KiCad-10-style writer.

Reading keeps every atom as text: bare atoms become :class:`Sym`, quoted strings become
``str``. Nothing is converted to numbers, so library content round-trips unchanged.
Writing follows the shape KiCad 10 emits: a node whose children are all atoms stays on
one line; any node with child nodes opens on its own line, indents its children with
tabs, and closes on its own line. ``pts`` nodes keep their ``xy`` children inline.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


class Sym(str):
    """A bare (unquoted) atom such as ``xy``, ``1.27`` or ``yes``."""

    __slots__ = ()

    def __repr__(self) -> str:
        return f"Sym({str.__repr__(self)})"


Node = list  # a node is a list whose first element is the tag (a Sym)


class ParseError(ValueError):
    pass


def parse(text: str) -> Node:
    """Parse one top-level S-expression."""
    items, pos = _parse_list(text, _skip_ws(text, 0))
    pos = _skip_ws(text, pos)
    if pos != len(text):
        raise ParseError(f"trailing content at offset {pos}")
    return items


def parse_all(text: str) -> list[Node]:
    out: list[Node] = []
    pos = _skip_ws(text, 0)
    while pos < len(text):
        node, pos = _parse_list(text, pos)
        out.append(node)
        pos = _skip_ws(text, pos)
    return out


def _skip_ws(text: str, pos: int) -> int:
    n = len(text)
    while pos < n and text[pos] in " \t\r\n":
        pos += 1
    return pos


def _parse_list(text: str, pos: int) -> tuple[Node, int]:
    if pos >= len(text) or text[pos] != "(":
        raise ParseError(f"expected '(' at offset {pos}")
    pos += 1
    node: Node = []
    n = len(text)
    while True:
        pos = _skip_ws(text, pos)
        if pos >= n:
            raise ParseError("unterminated list")
        ch = text[pos]
        if ch == ")":
            return node, pos + 1
        if ch == "(":
            child, pos = _parse_list(text, pos)
            node.append(child)
        elif ch == '"':
            value, pos = _parse_string(text, pos)
            node.append(value)
        else:
            start = pos
            while pos < n and text[pos] not in " \t\r\n()\"":
                pos += 1
            node.append(Sym(text[start:pos]))


def _parse_string(text: str, pos: int) -> tuple[str, int]:
    pos += 1
    out: list[str] = []
    n = len(text)
    while pos < n:
        ch = text[pos]
        if ch == "\\" and pos + 1 < n:
            nxt = text[pos + 1]
            out.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
            pos += 2
            continue
        if ch == '"':
            return "".join(out), pos + 1
        out.append(ch)
        pos += 1
    raise ParseError("unterminated string")


# --------------------------------------------------------------------------------------
# navigation helpers
# --------------------------------------------------------------------------------------


def tag(node: Any) -> str | None:
    return str(node[0]) if isinstance(node, list) and node and isinstance(node[0], Sym) else None


def children(node: Node, name: str) -> list[Node]:
    return [c for c in node if isinstance(c, list) and c and c[0] == name]


def child(node: Node, name: str) -> Node | None:
    for c in node:
        if isinstance(c, list) and c and c[0] == name:
            return c
    return None


def atoms(node: Node) -> list[str]:
    return [str(c) for c in node[1:] if not isinstance(c, list)]


def value(node: Node, name: str, index: int = 0, default: str | None = None) -> str | None:
    c = child(node, name)
    if c is None:
        return default
    vals = atoms(c)
    return vals[index] if index < len(vals) else default


def replace_child(node: Node, name: str, new: Node) -> None:
    for i, c in enumerate(node):
        if isinstance(c, list) and c and c[0] == name:
            node[i] = new
            return
    node.append(new)


def remove_children(node: Node, name: str) -> None:
    node[:] = [c for c in node if not (isinstance(c, list) and c and c[0] == name)]


# --------------------------------------------------------------------------------------
# construction helpers
# --------------------------------------------------------------------------------------


def num(x: float | int) -> Sym:
    """A number formatted the way KiCad writes it: no exponent, no trailing zeros."""
    if isinstance(x, bool):
        raise TypeError("bool is not a number here")
    if isinstance(x, int):
        return Sym(str(x))
    if x != x:  # NaN guard
        raise ValueError("NaN")
    text = f"{x:.6f}".rstrip("0").rstrip(".")
    if text in ("-0", ""):
        text = "0"
    return Sym(text)


def S(*parts: Any) -> Node:
    """Build a node: ``S("at", 1.27, 2.54, 0)`` -> ``(at 1.27 2.54 0)``.

    Strings are quoted; ints and floats formatted; ``Sym`` kept bare; lists nested; None skipped.
    """
    out: Node = []
    for i, p in enumerate(parts):
        if p is None:
            continue
        if i == 0:
            out.append(Sym(p) if not isinstance(p, Sym) else p)
        elif isinstance(p, list):
            out.append(p)
        elif isinstance(p, Sym):
            out.append(p)
        elif isinstance(p, bool):
            out.append(Sym("yes" if p else "no"))
        elif isinstance(p, (int, float)):
            out.append(num(p))
        elif isinstance(p, str):
            out.append(p)
        else:
            raise TypeError(f"unsupported atom {p!r}")
    return out


def yesno(flag: bool) -> Sym:
    return Sym("yes" if flag else "no")


# --------------------------------------------------------------------------------------
# writer
# --------------------------------------------------------------------------------------


def _quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _atom(a: Any) -> str:
    return str(a) if isinstance(a, Sym) else _quote(str(a))


INLINE_CHILD_TAGS = {"xy"}


def dumps(node: Node, indent: int = 0, newline: str = "\n") -> str:
    """Serialise a node in KiCad's layout. Returns text without a trailing newline."""
    lines: list[str] = []
    _dump(node, indent, lines)
    return newline.join(lines)


def _dump(node: Node, indent: int, lines: list[str]) -> None:
    pad = "\t" * indent
    head = [_atom(c) for c in node if not isinstance(c, list)]
    kids = [c for c in node if isinstance(c, list)]
    if not kids:
        lines.append(pad + "(" + " ".join(head) + ")")
        return
    lines.append(pad + "(" + " ".join(head))
    if all(tag(k) in INLINE_CHILD_TAGS for k in kids):
        # (pts (xy ...) (xy ...)) : xy's inline, a few per line
        per_line = 4 if len(kids) > 4 else len(kids)
        for i in range(0, len(kids), per_line):
            lines.append("\t" * (indent + 1) + " ".join(_inline(k) for k in kids[i : i + per_line]))
    else:
        for k in kids:
            _dump(k, indent + 1, lines)
    lines.append(pad + ")")


def _inline(node: Node) -> str:
    parts: list[str] = []
    for c in node:
        parts.append(_inline(c) if isinstance(c, list) else _atom(c))
    return "(" + " ".join(parts) + ")"


def write_file(path: str, node: Node) -> None:
    text = dumps(node) + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def walk(node: Node) -> Iterable[Node]:
    yield node
    for c in node:
        if isinstance(c, list):
            yield from walk(c)
