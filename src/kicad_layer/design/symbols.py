"""Helpers for a project's own symbol and footprint library, generated so it is reproducible and reviewable.

A project defines its symbols as S-expression trees with these helpers and hands them to
:func:`write_library` from its ``symbol_writer``; the library then registers ahead of KiCad's.
"""
from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path

from kicad_layer.formats import KICAD_RELEASE, SYMBOL_LIB_FORMAT
from kicad_layer.sexpr import S, Sym, dumps


def effects(size: float = 1.27, hide: bool = False) -> list:
    n = S("effects", S("font", S("size", size, size)))
    return n
def prop(name: str, value: str, at: tuple[float, float], hide: bool = False) -> list:
    n = S("property", name, value, S("at", at[0], at[1], 0), S("show_name", Sym("no")), S("do_not_autoplace", Sym("no")))
    if hide:
        n.append(S("hide", Sym("yes")))
    n.append(effects())
    return n
def pin(etype: str, number: str, name: str, at: tuple[float, float], rot: int, length: float = 5.08) -> list:
    return S("pin", Sym(etype), Sym("line"), S("at", at[0], at[1], rot), S("length", length),
             S("name", name, effects()), S("number", number, effects()))
def new_uuid() -> list:
    return S("uuid", str(uuid.uuid4()))
def fp_prop(name: str, value: str, at: tuple[float, float], layer: str, hide: bool = False) -> list:
    n = S("property", name, value, S("at", at[0], at[1], 0), S("layer", layer))
    if hide:
        n.append(S("hide", Sym("yes")))
    n.append(new_uuid())
    n.append(S("effects", S("font", S("size", 1, 1), S("thickness", 0.15))))
    return n
def fp_line(a, b, layer: str, width: float) -> list:
    return S("fp_line", S("start", a[0], a[1]), S("end", b[0], b[1]), S("stroke", S("width", width), S("type", Sym("solid"))), S("layer", layer), new_uuid())
def fp_rect(a, b, layer: str, width: float) -> list:
    return S("fp_rect", S("start", a[0], a[1]), S("end", b[0], b[1]), S("stroke", S("width", width), S("type", Sym("solid"))), S("fill", Sym("no")), S("layer", layer), new_uuid())


def symbol_library(*symbols: list) -> str:
    root = S("kicad_symbol_lib", S("version", SYMBOL_LIB_FORMAT), S("generator", "kicad_layer"), S("generator_version", KICAD_RELEASE), *symbols)
    return dumps(root) + "\n"


def write_library(lib_dir: Path, name: str, symbols: list, footprints: dict[str, Callable[[], str]]) -> list[Path]:
    """Write ``lib/<name>.kicad_sym`` and ``lib/<name>.pretty``; a footprint already on disk is kept so its pad uuids stay stable."""
    lib_dir.mkdir(parents=True, exist_ok=True)
    sym = lib_dir / f"{name}.kicad_sym"
    sym.write_text(symbol_library(*symbols), encoding="utf-8", newline='\n')
    pretty = lib_dir / f"{name}.pretty"
    pretty.mkdir(exist_ok=True)
    out = [sym]
    for fp_name, make in footprints.items():
        fpp = pretty / f"{fp_name}.kicad_mod"
        if not fpp.is_file():
            fpp.write_text(make(), encoding="utf-8", newline='\n')
        out.append(fpp)
    return out
