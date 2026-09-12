"""A concrete syntax tree for KiCad files: edit a few nodes, keep every other byte.

:func:`parse_cst` returns the same nested-list shape as :mod:`kicad_layer.sexpr`, but every
list node remembers where it came from in the source text. :func:`render` writes a node
back verbatim from that source unless the node or one of its descendants is dirty, in
which case only the dirty path is pretty-printed in KiCad's own layout. A file loaded and
saved without changes is therefore byte-identical, and an edit touches only the lines of
the nodes it changed.
"""

from __future__ import annotations

from typing import Any

from kicad_layer.sexpr import INLINE_CHILD_TAGS, ParseError, Sym, _atom, tag


class CNode(list):
    """A list node with a source span (start, end offsets) and a dirty flag."""

    __slots__ = ("span", "dirty")

    def __init__(self, items=(), span: tuple[int, int] | None = None, dirty: bool = False) -> None:
        super().__init__(items)
        self.span = span
        self.dirty = dirty


def parse_cst(text: str) -> CNode:
    pos = _skip_ws(text, 0)
    node, pos = _parse_list(text, pos)
    pos = _skip_ws(text, pos)
    if pos != len(text):
        raise ParseError(f"trailing content at offset {pos}")
    return node


def _skip_ws(text: str, pos: int) -> int:
    n = len(text)
    while pos < n and text[pos] in " \t\r\n":
        pos += 1
    return pos


def _parse_list(text: str, pos: int) -> tuple[CNode, int]:
    if pos >= len(text) or text[pos] != "(":
        raise ParseError(f"expected '(' at offset {pos}")
    start = pos
    pos += 1
    node = CNode()
    n = len(text)
    while True:
        pos = _skip_ws(text, pos)
        if pos >= n:
            raise ParseError("unterminated list")
        ch = text[pos]
        if ch == ")":
            node.span = (start, pos + 1)
            return node, pos + 1
        if ch == "(":
            child, pos = _parse_list(text, pos)
            node.append(child)
        elif ch == '"':
            value, pos = _parse_string(text, pos)
            node.append(value)
        else:
            s = pos
            while pos < n and text[pos] not in " \t\r\n()\"":
                pos += 1
            node.append(Sym(text[s:pos]))


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
# editing helpers
# --------------------------------------------------------------------------------------


def make(*parts: Any) -> CNode:
    """A new, dirty node from atoms and child nodes (see sexpr.S for the conventions)."""
    from kicad_layer.sexpr import S

    return to_cnode(S(*parts))


def to_cnode(node: list) -> CNode:
    """Wrap a plain sexpr tree as dirty CNodes (no source spans)."""
    if isinstance(node, CNode):
        return node
    out = CNode(dirty=True)
    for c in node:
        out.append(to_cnode(c) if isinstance(c, list) else c)
    return out


def mark_dirty(node: CNode) -> None:
    node.dirty = True


def is_clean(node: Any) -> bool:
    if not isinstance(node, CNode) or node.dirty or node.span is None:
        return False
    return all(is_clean(c) for c in node if isinstance(c, list))


# --------------------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------------------


def render(node: CNode, source: str, *, indent: int = 0, newline: str = "\n") -> str:
    """Text for ``node``: verbatim from ``source`` when clean, otherwise KiCad-style layout."""
    if is_clean(node):
        return source[node.span[0] : node.span[1]]
    head = [_atom(c) for c in node if not isinstance(c, list)]
    kids = [c for c in node if isinstance(c, list)]
    if not kids:
        return "(" + " ".join(head) + ")"
    pad = "\t" * (indent + 1)
    lines = ["(" + " ".join(head)]
    if all(tag(k) in INLINE_CHILD_TAGS for k in kids):
        per_line = 4 if len(kids) > 4 else len(kids)
        for i in range(0, len(kids), per_line):
            lines.append(pad + " ".join(_inline(k) for k in kids[i : i + per_line]))
    else:
        for k in kids:
            lines.append(pad + render(k, source, indent=indent + 1, newline=newline))
    lines.append("\t" * indent + ")")
    return newline.join(lines)


def _inline(node: list) -> str:
    return "(" + " ".join(_inline(c) if isinstance(c, list) else _atom(c) for c in node) + ")"


def render_file(root: CNode, source: str) -> str:
    newline = "\r\n" if "\r\n" in source[:4000] else "\n"
    text = render(root, source, newline=newline)
    if not text.endswith(newline):
        text += newline
    return text
