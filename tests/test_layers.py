"""The layers of the package, enforced: what the design package may depend on, and who may import the routers.

kicad_layer.design is the product: a board as data, the KiCad files as build outputs. It sits on the
core (S-expressions, identifiers, libraries, paths, config, models, the kicad-cli wrappers), the two
writers and the routes model, and on nothing else. The routers are frozen and reachable only from
the server's full tool tier and from a project's own routing script.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).parent.parent / "src" / "kicad_layer"

DESIGN_MAY_IMPORT = {
    "kicad_layer", "kicad_layer.sexpr", "kicad_layer.ids", "kicad_layer.kicad_libs", "kicad_layer.libtables",
    "kicad_layer.paths", "kicad_layer.config", "kicad_layer.errors", "kicad_layer.models", "kicad_layer.formats",
    "kicad_layer.cli", "kicad_layer.sch_writer", "kicad_layer.pcb_writer", "kicad_layer.routes", "kicad_layer.review", "kicad_layer.dru",
}
ROUTERS_IMPORTED_BY = {"kicad_layer.tools"}


def _module(path: Path) -> str:
    return ".".join(path.relative_to(SRC.parent).with_suffix("").parts).removesuffix(".__init__")


def _imports(path: Path) -> set[str]:
    """Absolute module names this file imports (relative imports resolved), module level and inside functions."""
    module = _module(path)
    package = module if path.name == "__init__.py" else module.rsplit(".", 1)[0]
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
                name = f"{base}.{node.module}" if node.module else base
            else:
                name = node.module or ""
            found.add(name)
            if name.count(".") < 2:  # ``from kicad_layer import routes`` names a module, not an object
                found.update(f"{name}.{a.name}" for a in node.names)
    return {n for n in found if n.startswith("kicad_layer")}


def _layer(name: str) -> str:
    """kicad_layer.cli.reports -> kicad_layer.cli; kicad_layer.sexpr -> kicad_layer.sexpr."""
    parts = name.split(".")
    return ".".join(parts[:2])


def test_design_depends_only_on_the_core_and_the_writers():
    bad = {}
    for path in (SRC / "design").glob("*.py"):
        outside = {n for n in _imports(path) if not n.startswith("kicad_layer.design")}
        wrong = sorted(n for n in outside if n not in DESIGN_MAY_IMPORT and _layer(n) not in DESIGN_MAY_IMPORT)
        if wrong:
            bad[path.name] = wrong
    assert not bad, f"the design package reaches outside its layer: {bad}"


def test_routers_are_imported_only_by_the_full_tool_tier():
    bad = {}
    for path in SRC.rglob("*.py"):
        module = _module(path)
        if module.startswith("kicad_layer.routers"):
            continue
        hits = sorted(n for n in _imports(path) if n.startswith("kicad_layer.routers"))
        if hits and module not in ROUTERS_IMPORTED_BY:
            bad[module] = hits
    assert not bad, f"the routers are frozen; only the server's full tier may import them: {bad}"


def test_routes_model_needs_no_router():
    assert not {n for n in _imports(SRC / "routes.py") if n.startswith("kicad_layer.routers")}
