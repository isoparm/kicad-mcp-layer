"""A searchable index of every symbol and footprint KiCad can see.

Built once from the library tables into SQLite with full-text search, refreshed per
library when its files change. Global libraries live in scope ``""``; a project's own
libraries (its ``sym-lib-table`` and ``fp-lib-table`` next to the ``.kicad_pro``) are
indexed under the project directory as scope and shadow global entries with the same
name, exactly as KiCad resolves them. Symbols that ``extends`` another are indexed with
their parent's pins, the way eeschema presents them.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kicad_layer.config import settings
from kicad_layer.errors import NOT_FOUND_IN_DESIGN, LayerError
from kicad_layer.kicad_libs import _parse_pins
from kicad_layer.libtables import LibEntry, library_entries, project_entries
from kicad_layer.sexpr import atoms, child, children, parse, tag, value

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS libs (
    kind TEXT NOT NULL, nickname TEXT NOT NULL, scope TEXT NOT NULL DEFAULT '', path TEXT NOT NULL,
    description TEXT, stamp TEXT NOT NULL, count INTEGER NOT NULL, PRIMARY KEY (kind, nickname, scope));
CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', nickname TEXT NOT NULL, name TEXT NOT NULL, lib_id TEXT NOT NULL,
    description TEXT, keywords TEXT, fp_filters TEXT, footprint TEXT, datasheet TEXT,
    extends TEXT, power INTEGER NOT NULL, units INTEGER NOT NULL, pin_count INTEGER NOT NULL,
    UNIQUE (lib_id, scope));
CREATE INDEX IF NOT EXISTS symbols_nick ON symbols(nickname, scope);
CREATE INDEX IF NOT EXISTS symbols_name ON symbols(name);
CREATE TABLE IF NOT EXISTS symbol_pins (
    symbol_id INTEGER NOT NULL, number TEXT, name TEXT, etype TEXT, unit INTEGER, hidden INTEGER);
CREATE INDEX IF NOT EXISTS symbol_pins_sym ON symbol_pins(symbol_id);
CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(lib_id, name, description, keywords, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS footprints (
    id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', nickname TEXT NOT NULL, name TEXT NOT NULL, lib_id TEXT NOT NULL,
    description TEXT, tags TEXT, attr TEXT, pad_count INTEGER NOT NULL, smd_pads INTEGER NOT NULL,
    tht_pads INTEGER NOT NULL, width REAL, height REAL, model TEXT, path TEXT NOT NULL,
    UNIQUE (lib_id, scope));
CREATE INDEX IF NOT EXISTS footprints_nick ON footprints(nickname, scope);
CREATE INDEX IF NOT EXISTS footprints_name ON footprints(name);
CREATE VIRTUAL TABLE IF NOT EXISTS footprints_fts USING fts5(lib_id, name, description, tags, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

_lock = threading.RLock()


def index_path() -> Path:
    p = settings().cache_dir / f"libindex-v{SCHEMA_VERSION}.sqlite"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(index_path()), timeout=60)
    con.executescript(SCHEMA)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def scope_of(project_dir: Path | None) -> str:
    return os.path.normcase(os.path.realpath(str(project_dir))) if project_dir else ""


# --------------------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------------------


def _stamp_symbol_lib(path: Path) -> str:
    st = path.stat()
    return f"{st.st_size}:{st.st_mtime_ns}"


def _stamp_footprint_lib(path: Path) -> str:
    files = list(path.glob("*.kicad_mod"))
    newest = max((f.stat().st_mtime_ns for f in files), default=0)
    return f"{len(files)}:{newest}"


def _props(node) -> dict[str, str]:
    return {str(p[1]): str(p[2]) for p in children(node, "property") if len(p) > 2}


def parse_symbol_lib(entry: LibEntry) -> list[dict[str, Any]]:
    root = parse(entry.path.read_text(encoding="utf-8", errors="replace"))
    nodes = {str(n[1]): n for n in children(root, "symbol")}

    def resolve(name: str, depth: int = 0) -> tuple[dict[str, str], list, int, bool]:
        node = nodes[name]
        props = _props(node)
        pins = _parse_pins(node)
        units = len({p.unit for p in pins if p.unit > 0}) or 1
        power = child(node, "power") is not None
        ext = child(node, "extends")
        if ext is not None and depth < 8 and str(ext[1]) in nodes:
            parent_props, parent_pins, parent_units, parent_power = resolve(str(ext[1]), depth + 1)
            merged = dict(parent_props)
            merged.update({k: v for k, v in props.items() if v != "" or k not in merged})
            props = merged
            if not pins:
                pins = parent_pins
                units = parent_units
            power = power or parent_power
        return props, pins, units, power

    rows: list[dict[str, Any]] = []
    for name, node in nodes.items():
        props, pins, units, power = resolve(name)
        ext = child(node, "extends")
        rows.append(
            {
                "nickname": entry.nickname,
                "name": name,
                "lib_id": f"{entry.nickname}:{name}",
                "description": props.get("Description", ""),
                "keywords": props.get("ki_keywords", ""),
                "fp_filters": props.get("ki_fp_filters", ""),
                "footprint": props.get("Footprint", ""),
                "datasheet": props.get("Datasheet", ""),
                "extends": str(ext[1]) if ext is not None else "",
                "power": int(power),
                "units": units,
                "pins": [(p.number, p.name, p.etype, p.unit, int(p.hidden)) for p in pins],
            }
        )
    return rows


def parse_footprint(entry: LibEntry, file: Path) -> dict[str, Any]:
    root = parse(file.read_text(encoding="utf-8", errors="replace"))
    pads = children(root, "pad")
    smd = sum(1 for p in pads if len(p) > 2 and str(p[2]) == "smd")
    tht = sum(1 for p in pads if len(p) > 2 and str(p[2]) == "thru_hole")
    xs: list[float] = []
    ys: list[float] = []
    for g in root:
        if not isinstance(g, list) or tag(g) not in ("fp_line", "fp_rect", "fp_poly", "fp_circle", "fp_arc"):
            continue
        if (value(g, "layer") or "") != "F.CrtYd":
            continue
        for k in ("start", "end", "center", "mid"):
            c = child(g, k)
            if c is not None and len(c) > 2:
                xs.append(float(c[1]))
                ys.append(float(c[2]))
        pts = child(g, "pts")
        if pts is not None:
            for xy in children(pts, "xy"):
                xs.append(float(xy[1]))
                ys.append(float(xy[2]))
    attr = child(root, "attr")
    model = child(root, "model")
    name = file.stem
    return {
        "nickname": entry.nickname,
        "name": name,
        "lib_id": f"{entry.nickname}:{name}",
        "description": value(root, "descr") or "",
        "tags": value(root, "tags") or "",
        "attr": " ".join(atoms(attr)) if attr is not None else "",
        "pad_count": len(pads),
        "smd_pads": smd,
        "tht_pads": tht,
        "width": round(max(xs) - min(xs), 3) if xs else None,
        "height": round(max(ys) - min(ys), 3) if ys else None,
        "model": str(model[1]) if model is not None and len(model) > 1 else "",
        "path": str(file),
    }


# --------------------------------------------------------------------------------------
# building
# --------------------------------------------------------------------------------------


@dataclass
class BuildReport:
    symbols: int
    footprints: int
    libraries: int
    rebuilt_libraries: int
    seconds: float
    built_at: str
    scopes: list[str]


def build(*, rebuild: bool = False, project_dir: Path | None = None) -> BuildReport:
    """Create or refresh the index for the global libraries and, if given, one project's own."""
    started = time.time()
    with _lock:
        con = _connect()
        try:
            rebuilt = 0
            rebuilt += _sync_scope(con, "", library_entries("symbol"), library_entries("footprint"), rebuild)
            if project_dir is not None:
                scope = scope_of(project_dir)
                rebuilt += _sync_scope(con, scope, project_entries("symbol", project_dir), project_entries("footprint", project_dir), rebuild)
            built_at = time.strftime("%Y-%m-%dT%H:%M:%S")
            if rebuilt:
                con.execute("INSERT OR REPLACE INTO meta VALUES ('built_at', ?)", (built_at,))
            con.commit()
            return status(con, rebuilt=rebuilt, seconds=time.time() - started)
        finally:
            con.close()


def _sync_scope(con: sqlite3.Connection, scope: str, sym_entries: list[LibEntry], fp_entries: list[LibEntry], rebuild: bool) -> int:
    existing = {(k, n): s for k, n, s in con.execute("SELECT kind, nickname, stamp FROM libs WHERE scope=?", (scope,))}
    rebuilt = 0
    seen: set[tuple[str, str]] = set()
    for entry in sym_entries:
        if not entry.path.is_file():
            continue
        key = ("symbol", entry.nickname)
        seen.add(key)
        stamp = _stamp_symbol_lib(entry.path)
        if not rebuild and existing.get(key) == stamp:
            continue
        _replace_symbols(con, scope, entry, stamp, parse_symbol_lib(entry))
        rebuilt += 1
    for entry in fp_entries:
        if not entry.path.is_dir():
            continue
        key = ("footprint", entry.nickname)
        seen.add(key)
        stamp = _stamp_footprint_lib(entry.path)
        if not rebuild and existing.get(key) == stamp:
            continue
        _replace_footprints(con, scope, entry, stamp, [parse_footprint(entry, f) for f in sorted(entry.path.glob("*.kicad_mod"))])
        rebuilt += 1
    for key in set(existing) - seen:
        _drop_lib(con, scope, *key)
        rebuilt += 1
    return rebuilt


def _drop_lib(con: sqlite3.Connection, scope: str, kind: str, nickname: str) -> None:
    if kind == "symbol":
        ids = [r[0] for r in con.execute("SELECT id FROM symbols WHERE nickname=? AND scope=?", (nickname, scope))]
        con.executemany("DELETE FROM symbol_pins WHERE symbol_id=?", [(i,) for i in ids])
        con.executemany("DELETE FROM symbols_fts WHERE rowid=?", [(i,) for i in ids])
        con.execute("DELETE FROM symbols WHERE nickname=? AND scope=?", (nickname, scope))
    else:
        ids = [r[0] for r in con.execute("SELECT id FROM footprints WHERE nickname=? AND scope=?", (nickname, scope))]
        con.executemany("DELETE FROM footprints_fts WHERE rowid=?", [(i,) for i in ids])
        con.execute("DELETE FROM footprints WHERE nickname=? AND scope=?", (nickname, scope))
    con.execute("DELETE FROM libs WHERE kind=? AND nickname=? AND scope=?", (kind, nickname, scope))


def _replace_symbols(con: sqlite3.Connection, scope: str, entry: LibEntry, stamp: str, rows: list[dict[str, Any]]) -> None:
    _drop_lib(con, scope, "symbol", entry.nickname)
    for r in rows:
        cur = con.execute(
            "INSERT INTO symbols (scope, nickname, name, lib_id, description, keywords, fp_filters, footprint, datasheet, extends, power, units, pin_count)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (scope, r["nickname"], r["name"], r["lib_id"], r["description"], r["keywords"], r["fp_filters"], r["footprint"],
             r["datasheet"], r["extends"], r["power"], r["units"], len(r["pins"])),
        )
        sid = cur.lastrowid
        con.executemany("INSERT INTO symbol_pins VALUES (?,?,?,?,?,?)", [(sid, *p) for p in r["pins"]])
        con.execute("INSERT INTO symbols_fts (rowid, lib_id, name, description, keywords) VALUES (?,?,?,?,?)",
                    (sid, r["lib_id"], _tokens_for_name(r["name"]), r["description"], r["keywords"]))
    con.execute("INSERT OR REPLACE INTO libs VALUES (?,?,?,?,?,?,?)", ("symbol", entry.nickname, scope, str(entry.path), entry.description, stamp, len(rows)))


def _replace_footprints(con: sqlite3.Connection, scope: str, entry: LibEntry, stamp: str, rows: list[dict[str, Any]]) -> None:
    _drop_lib(con, scope, "footprint", entry.nickname)
    for r in rows:
        cur = con.execute(
            "INSERT INTO footprints (scope, nickname, name, lib_id, description, tags, attr, pad_count, smd_pads, tht_pads, width, height, model, path)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (scope, r["nickname"], r["name"], r["lib_id"], r["description"], r["tags"], r["attr"], r["pad_count"], r["smd_pads"],
             r["tht_pads"], r["width"], r["height"], r["model"], r["path"]),
        )
        con.execute("INSERT INTO footprints_fts (rowid, lib_id, name, description, tags) VALUES (?,?,?,?,?)",
                    (cur.lastrowid, r["lib_id"], _tokens_for_name(r["name"]), r["description"], r["tags"]))
    con.execute("INSERT OR REPLACE INTO libs VALUES (?,?,?,?,?,?,?)", ("footprint", entry.nickname, scope, str(entry.path), entry.description, stamp, len(rows)))


def _tokens_for_name(name: str) -> str:
    """Names like R_0603_1608Metric or ATtiny1614-SS split into searchable pieces plus the whole."""
    parts = re.split(r"[_\-.:/ ]+", name)
    extra = re.findall(r"[A-Za-z]+|\d+", name)
    return " ".join(dict.fromkeys([name, *parts, *extra]))


# --------------------------------------------------------------------------------------
# querying
# --------------------------------------------------------------------------------------


def status(con: sqlite3.Connection | None = None, *, rebuilt: int = 0, seconds: float = 0.0) -> BuildReport:
    own = con is None
    con = con or _connect()
    try:
        symbols = con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        footprints = con.execute("SELECT COUNT(*) FROM footprints").fetchone()[0]
        libraries = con.execute("SELECT COUNT(*) FROM libs").fetchone()[0]
        built_at = (con.execute("SELECT value FROM meta WHERE key='built_at'").fetchone() or ("",))[0]
        scopes = [r[0] for r in con.execute("SELECT DISTINCT scope FROM libs WHERE scope != ''")]
        return BuildReport(symbols, footprints, libraries, rebuilt, round(seconds, 1), built_at, scopes)
    finally:
        if own:
            con.close()


def ensure_built(project_dir: Path | None = None) -> None:
    """Build the global index on first use, and the project's scope the first time it is asked for."""
    need = False
    p = index_path()
    if not p.is_file() or p.stat().st_size < 4096:
        need = True
    else:
        con = _connect()
        try:
            if con.execute("SELECT COUNT(*) FROM symbols WHERE scope=''").fetchone()[0] == 0:
                need = True
            if project_dir is not None and not con.execute("SELECT 1 FROM libs WHERE scope=? LIMIT 1", (scope_of(project_dir),)).fetchone():
                need = True
        finally:
            con.close()
    if need:
        build(project_dir=project_dir)


def _fts_query(text: str) -> str:
    tokens = re.findall(r"[A-Za-z0-9]+", text)
    if not tokens:
        return ""
    return " ".join(f'"{t}"*' for t in tokens)


def search(query: str, kind: str = "both", limit: int = 20, project_dir: Path | None = None) -> dict[str, Any]:
    ensure_built(project_dir)
    q = _fts_query(query)
    scope = scope_of(project_dir)
    out: dict[str, Any] = {"symbols": [], "footprints": [], "symbol_total": 0, "footprint_total": 0}
    if not q:
        return out
    ql = query.strip().lower()
    sym_cols = "s.lib_id, s.description, s.keywords, s.pin_count, s.units, s.footprint, s.power, s.name, s.scope"
    fp_cols = "fp.lib_id, fp.description, fp.tags, fp.attr, fp.pad_count, fp.width, fp.height, fp.name, fp.scope"
    with _connect() as con:
        if kind in ("symbol", "both"):
            out["symbol_total"] = con.execute(
                "SELECT COUNT(*) FROM symbols_fts f JOIN symbols s ON s.id=f.rowid WHERE symbols_fts MATCH ? AND s.scope IN ('', ?)", (q, scope)
            ).fetchone()[0]
            exact = con.execute(f"SELECT {sym_cols} FROM symbols s WHERE (lower(s.name)=? OR lower(s.lib_id)=?) AND s.scope IN ('', ?) ORDER BY s.scope DESC, s.lib_id", (ql, ql, scope)).fetchall()
            prefix = con.execute(f"SELECT {sym_cols} FROM symbols s WHERE lower(s.name) LIKE ? AND s.scope IN ('', ?) ORDER BY s.scope DESC, LENGTH(s.name), s.lib_id LIMIT ?", (ql + "%", scope, limit)).fetchall()
            fts = con.execute(
                f"SELECT {sym_cols} FROM symbols_fts f JOIN symbols s ON s.id = f.rowid WHERE symbols_fts MATCH ? AND s.scope IN ('', ?)"
                " ORDER BY bm25(symbols_fts, 1.0, 8.0, 2.0, 3.0), s.scope DESC LIMIT ?",
                (q, scope, limit * 2),
            ).fetchall()
            out["symbols"] = _merge(exact, prefix, fts, limit=limit)
        if kind in ("footprint", "both"):
            out["footprint_total"] = con.execute(
                "SELECT COUNT(*) FROM footprints_fts f JOIN footprints fp ON fp.id=f.rowid WHERE footprints_fts MATCH ? AND fp.scope IN ('', ?)", (q, scope)
            ).fetchone()[0]
            exact = con.execute(f"SELECT {fp_cols} FROM footprints fp WHERE (lower(fp.name)=? OR lower(fp.lib_id)=?) AND fp.scope IN ('', ?) ORDER BY fp.scope DESC, fp.lib_id", (ql, ql, scope)).fetchall()
            prefix = con.execute(f"SELECT {fp_cols} FROM footprints fp WHERE lower(fp.name) LIKE ? AND fp.scope IN ('', ?) ORDER BY fp.scope DESC, LENGTH(fp.name), fp.lib_id LIMIT ?", (ql + "%", scope, limit)).fetchall()
            fts = con.execute(
                f"SELECT {fp_cols} FROM footprints_fts f JOIN footprints fp ON fp.id = f.rowid WHERE footprints_fts MATCH ? AND fp.scope IN ('', ?)"
                " ORDER BY bm25(footprints_fts, 1.0, 8.0, 2.0, 3.0), fp.scope DESC LIMIT ?",
                (q, scope, limit * 2),
            ).fetchall()
            out["footprints"] = _merge(exact, prefix, fts, limit=limit)
    return out


def _merge(*groups: list, limit: int) -> list:
    """Exact name matches, then prefix matches, then full-text hits; a lib_id appears once."""
    seen: set[str] = set()
    out: list = []
    for group in groups:
        for row in group:
            if row[0] not in seen:
                seen.add(row[0])
                out.append(row)
            if len(out) >= limit:
                return out
    return out


def _record(con: sqlite3.Connection, table: str, lib_id: str, scope: str) -> dict[str, Any] | None:
    cols = [d[0] for d in con.execute(f"SELECT * FROM {table} LIMIT 0").description]
    row = con.execute(f"SELECT * FROM {table} WHERE lib_id=? AND scope IN ('', ?) ORDER BY scope DESC LIMIT 1", (lib_id, scope)).fetchone()
    if row is None:
        rows = con.execute(f"SELECT * FROM {table} WHERE name=? AND scope IN ('', ?) ORDER BY scope DESC", (lib_id.split(":")[-1], scope)).fetchall()
        if len(rows) == 1:
            row = rows[0]
    return dict(zip(cols, row)) if row else None


def symbol_info(lib_id: str, project_dir: Path | None = None) -> dict[str, Any]:
    ensure_built(project_dir)
    scope = scope_of(project_dir)
    with _connect() as con:
        rec = _record(con, "symbols", lib_id, scope)
        if rec is None:
            hits = search(lib_id.replace(":", " "), "symbol", 8, project_dir)["symbols"]
            hint = "Did you mean: " + ", ".join(h[0] for h in hits) if hits else "Use lib_search to find the right lib_id."
            raise LayerError(NOT_FOUND_IN_DESIGN, f"No symbol {lib_id!r} in the library index.", hint=hint)
        rec["pins"] = con.execute(
            "SELECT number, name, etype, unit, hidden FROM symbol_pins WHERE symbol_id=? ORDER BY unit, LENGTH(number), number", (rec["id"],)
        ).fetchall()
        lib = con.execute("SELECT path FROM libs WHERE kind='symbol' AND nickname=? AND scope=?", (rec["nickname"], rec["scope"])).fetchone()
        rec["library_path"] = lib[0] if lib else ""
        rec["matching_footprints"] = matching_footprints(rec["fp_filters"], con=con, scope=scope) if rec["fp_filters"] else []
        return rec


def matching_footprints(fp_filters: str, *, con: sqlite3.Connection | None = None, scope: str = "", limit: int = 40) -> list[str]:
    """Footprint lib_ids matching KiCad footprint filters (``R_*``, ``Package_SO:SOIC*``)."""
    patterns = [p for p in fp_filters.split() if p]
    if not patterns:
        return []
    own = con is None
    con = con or _connect()
    try:
        rows = con.execute("SELECT lib_id, name FROM footprints WHERE scope IN ('', ?) ORDER BY scope DESC, lib_id", (scope,)).fetchall()
    finally:
        if own:
            con.close()
    out: list[str] = []
    for lib_id, name in rows:
        for pat in patterns:
            target = lib_id if ":" in pat else name
            if fnmatch.fnmatchcase(target.lower(), pat.lower()):
                out.append(lib_id)
                break
        if len(out) >= limit:
            break
    return out


def footprint_info(lib_id: str, project_dir: Path | None = None) -> dict[str, Any]:
    ensure_built(project_dir)
    scope = scope_of(project_dir)
    with _connect() as con:
        rec = _record(con, "footprints", lib_id, scope)
        if rec is None:
            hits = search(lib_id.replace(":", " "), "footprint", 8, project_dir)["footprints"]
            hint = "Did you mean: " + ", ".join(h[0] for h in hits) if hits else "Use lib_search to find the right lib_id."
            raise LayerError(NOT_FOUND_IN_DESIGN, f"No footprint {lib_id!r} in the library index.", hint=hint)
    root = parse(Path(rec["path"]).read_text(encoding="utf-8", errors="replace"))
    pads = []
    for p in children(root, "pad"):
        at = child(p, "at") or []
        size = child(p, "size") or []
        drill = child(p, "drill")
        layers = child(p, "layers") or []
        pads.append(
            {
                "number": str(p[1]),
                "kind": str(p[2]) if len(p) > 2 else "",
                "shape": str(p[3]) if len(p) > 3 and not isinstance(p[3], list) else "",
                "x_mm": float(at[1]) if len(at) > 1 else 0.0,
                "y_mm": float(at[2]) if len(at) > 2 else 0.0,
                "size_x_mm": float(size[1]) if len(size) > 1 else None,
                "size_y_mm": float(size[2]) if len(size) > 2 else None,
                "drill_mm": float(atoms(drill)[0]) if drill is not None and atoms(drill) and atoms(drill)[0] != "oval" else None,
                "layers": [str(x) for x in layers[1:]],
            }
        )
    rec["pads"] = pads
    return rec
