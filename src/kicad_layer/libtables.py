"""KiCad library tables: which libraries exist and where their files are.

KiCad resolves ``Device:R`` through ``sym-lib-table`` files: a global one in the user's
KiCad configuration directory and an optional one next to the project. Entries may point
at another table (KiCad 10's global tables include the stock tables that way) and use
``${VAR}`` paths whose defaults come from the install location.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from kicad_layer.kicad_libs import share_dir
from kicad_layer.sexpr import child, children, parse, value

KICAD_MAJOR = "10"


@dataclass(frozen=True)
class LibEntry:
    kind: str          # "symbol" or "footprint"
    nickname: str
    path: Path         # .kicad_sym file or .pretty directory
    description: str
    table: str         # the table file this entry came from


def config_dir(version: str = f"{KICAD_MAJOR}.0") -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "kicad"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Preferences" / "kicad"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "kicad"
    d = base / version
    if d.is_dir():
        return d
    versions = sorted((p for p in base.glob("*") if p.is_dir() and re.match(r"^\d", p.name)), key=lambda p: p.name)
    return versions[-1] if versions else d


def environment(project_dir: Path | None = None) -> dict[str, str]:
    """KiCad path variables: install defaults, then kicad_common.json, then the process env."""
    share = share_dir()
    v: dict[str, str] = {
        f"KICAD{KICAD_MAJOR}_SYMBOL_DIR": str(share / "symbols"),
        f"KICAD{KICAD_MAJOR}_FOOTPRINT_DIR": str(share / "footprints"),
        f"KICAD{KICAD_MAJOR}_3DMODEL_DIR": str(share / "3dmodels"),
        f"KICAD{KICAD_MAJOR}_TEMPLATE_DIR": str(share / "template"),
        f"KICAD{KICAD_MAJOR}_DESIGN_BLOCK_DIR": str(share / "blocks"),
        f"KICAD{KICAD_MAJOR}_3RD_PARTY": str(Path.home() / "Documents" / "KiCad" / f"{KICAD_MAJOR}.0" / "3rdparty"),
        "KICAD_USER_TEMPLATE_DIR": str(Path.home() / "Documents" / "KiCad" / f"{KICAD_MAJOR}.0" / "template"),
    }
    common = config_dir() / "kicad_common.json"
    if common.is_file():
        try:
            data = json.loads(common.read_text(encoding="utf-8"))
            for k, val in (data.get("environment", {}).get("vars") or {}).items():
                if val:
                    v[str(k)] = str(val)
        except (OSError, ValueError):
            pass
    for k in list(v):
        if os.environ.get(k):
            v[k] = os.environ[k]
    if project_dir is not None:
        v["KIPRJMOD"] = str(project_dir)
    return v


def expand(uri: str, env: dict[str, str]) -> str:
    return re.sub(r"\$\{(\w+)\}", lambda m: env.get(m.group(1), os.environ.get(m.group(1), m.group(0))), uri)


def read_table(path: Path, kind: str, env: dict[str, str], _seen: frozenset[str] = frozenset()) -> list[LibEntry]:
    if not path.is_file():
        return []
    key = os.path.normcase(str(path.resolve()))
    if key in _seen:
        return []
    root = parse(path.read_text(encoding="utf-8"))
    out: list[LibEntry] = []
    for lib in children(root, "lib"):
        if child(lib, "hidden") is not None:
            continue
        name = value(lib, "name") or ""
        typ = value(lib, "type") or ""
        uri = expand(value(lib, "uri") or "", env)
        descr = value(lib, "descr") or ""
        if typ == "Table":
            out.extend(read_table(Path(uri), kind, env, _seen | {key}))
        elif typ == "KiCad" and name:
            out.append(LibEntry(kind=kind, nickname=name, path=Path(uri), description=descr, table=str(path)))
    return out


def symbol_lib_path_for(nickname: str, project_dir: Path | None = None) -> Path:
    """The .kicad_sym file behind a library nickname, project table first."""
    for e in library_entries("symbol", project_dir):
        if e.nickname == nickname:
            if not e.path.is_file():
                raise FileNotFoundError(f"library {nickname} points at {e.path}, which does not exist")
            return e.path
    raise KeyError(f"no symbol library named {nickname!r} in the global or project tables")


def project_entries(kind: str, project_dir: Path) -> list[LibEntry]:
    """Only the libraries a project declares in its own table next to the .kicad_pro."""
    env = environment(project_dir)
    table_name = "sym-lib-table" if kind == "symbol" else "fp-lib-table"
    return sorted(read_table(project_dir / table_name, kind, env), key=lambda e: e.nickname.lower())


def library_entries(kind: str, project_dir: Path | None = None) -> list[LibEntry]:
    """Global table entries, overridden by same-named project table entries."""
    env = environment(project_dir)
    table_name = "sym-lib-table" if kind == "symbol" else "fp-lib-table"
    entries: dict[str, LibEntry] = {}
    for e in read_table(config_dir() / table_name, kind, env):
        entries[e.nickname] = e
    if project_dir is not None:
        for e in read_table(project_dir / table_name, kind, env):
            entries[e.nickname] = e
    if not entries:
        # No usable tables: fall back to the stock libraries so the index still works.
        share = share_dir()
        if kind == "symbol":
            for p in sorted((share / "symbols").glob("*.kicad_sym")):
                entries[p.stem] = LibEntry(kind, p.stem, p, "", "stock")
        else:
            for p in sorted((share / "footprints").glob("*.pretty")):
                entries[p.stem] = LibEntry(kind, p.stem, p, "", "stock")
    return sorted(entries.values(), key=lambda e: e.nickname.lower())
