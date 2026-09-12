"""Symbol, footprint and 3D model for an LCSC code, from EasyEDA's component data.

EasyEDA is JLCPCB's own design tool, and nearly every part in the assembly catalogue has a symbol, a
footprint and a 3D model there, drawn by users and JLCPCB staff. ``easyeda2kicad`` (uPesy, PyPI)
converts one part into KiCad library files; this module runs it as a subprocess (it prints to stdout,
which the server must not) into a project's ``lib/`` and reports what was written. The result is a
draft: check the footprint pad for pad against the datasheet drawing before a board relies on it.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from pathlib import Path

from .errors import INVALID_ARGUMENT, LIB_FETCH_FAILED, LayerError
from .models import LibFetch

log = logging.getLogger(__name__)

LCSC_CODE = re.compile(r"C\d{2,9}")
FLAGS = {"full": "--full", "symbol": "--symbol", "footprint": "--footprint", "model": "--3d"}
_SYMBOL = re.compile(r"^\s*\(symbol \"([^\"]+)\"", re.MULTILINE)
_UNIT_SUFFIX = re.compile(r"_\d+_\d+$")


def entries(base: Path) -> dict[str, set[str]]:
    """What a library base (``lib/jlc`` -> ``jlc.kicad_sym``, ``jlc.pretty``, ``jlc.3dshapes``) holds now."""
    out: dict[str, set[str]] = {"symbol": set(), "footprint": set(), "model": set()}
    sym = base.with_suffix(".kicad_sym")
    if sym.is_file():
        out["symbol"] = {n for n in _SYMBOL.findall(sym.read_text(encoding="utf-8", errors="replace")) if not _UNIT_SUFFIX.search(n)}
    pretty = base.with_suffix(".pretty")
    if pretty.is_dir():
        out["footprint"] = {p.stem for p in pretty.glob("*.kicad_mod")}
    shapes = base.with_suffix(".3dshapes")
    if shapes.is_dir():
        out["model"] = {p.name for p in shapes.iterdir() if p.suffix.lower() in (".step", ".wrl")}
    return out


def parse_names(output: str) -> dict[str, str | None]:
    """The names easyeda2kicad reports: ``Symbol name : X``, ``Footprint name: Y``, ``3D model name: Z``."""
    def one(label: str) -> str | None:
        m = re.search(rf"{label}\s*:\s*(\S.*?)\s*$", output, re.MULTILINE)
        return m.group(1) if m else None

    return {"symbol": one("Symbol name"), "footprint": one("Footprint name"), "model": one("3D model name")}


def _count(pattern: str, text: str) -> int:
    return len(re.findall(pattern, text))


def symbol_pins(sym_file: Path, name: str) -> int | None:
    """Pins of one symbol in a .kicad_sym: counted inside its block, up to the next top-level symbol."""
    if not sym_file.is_file():
        return None
    text = sym_file.read_text(encoding="utf-8", errors="replace")
    start = text.find(f'(symbol "{name}"')
    if start < 0:
        return None
    nxt = re.compile(r"^\t\(symbol \"", re.MULTILINE).search(text, start + 10)
    return _count(r"\(pin ", text[start:nxt.start() if nxt else len(text)])


def upgrade_footprint(fp_file: Path, timeout_s: float = 120.0) -> list[str]:
    """Bring one footprint to KiCad's current format with ``kicad-cli fp upgrade``; returns warnings, never raises.

    easyeda2kicad writes the old ``(module ...)`` form with ``fp_text reference REF**``; a board written from
    it shows REF** and rules that name the part miss it. kicad-cli rewrites the file, pads and 3D path kept."""
    import shutil
    import tempfile

    from .cli.discovery import find_kicad_cli

    try:
        cli = find_kicad_cli()
    except Exception as ex:  # KICAD_CLI_NOT_FOUND and friends: the file is usable, just old-style
        return [f"{fp_file.name} left in easyeda2kicad's old format: kicad-cli not found ({ex})"]
    with tempfile.TemporaryDirectory(prefix="fpup-") as tmp:
        src = Path(tmp) / "in.pretty"
        out = Path(tmp) / "out"
        src.mkdir()
        shutil.copy2(fp_file, src / fp_file.name)
        try:
            r = subprocess.run([str(cli.path), "fp", "upgrade", "--output", str(out), str(src)], capture_output=True, text=True, timeout=timeout_s)
        except (OSError, subprocess.TimeoutExpired) as ex:
            return [f"{fp_file.name} left in easyeda2kicad's old format: kicad-cli fp upgrade failed ({ex})"]
        upgraded = out / fp_file.name
        if r.returncode != 0 or not upgraded.is_file():
            return [f"{fp_file.name} left in easyeda2kicad's old format: kicad-cli fp upgrade exit {r.returncode}"]
        text = upgraded.read_text(encoding="utf-8", errors="replace")
        if _count(r"\(pad ", text) != _count(r"\(pad ", fp_file.read_text(encoding="utf-8", errors="replace")):
            return [f"{fp_file.name} left in easyeda2kicad's old format: the upgrade changed the pad count"]
        fp_file.write_text(text, encoding="utf-8", newline="\n")
    return []


def target_dir(lib_dir: Path | None, project_dir: Path | None) -> Path:
    """Where the files go: an explicit folder, else the project's lib/."""
    if lib_dir is not None:
        return lib_dir
    if project_dir is not None:
        return project_dir / "lib"
    raise LayerError(INVALID_ARGUMENT, "lib_fetch needs lib_dir or project_path", hint="Give the project's .kicad_pro (its lib/ folder is used) or a folder inside the workspace.")


def fetch(lcsc: str, lib_dir: Path, lib_name: str = "jlc", *, parts: str = "full", overwrite: bool = False, timeout_s: float = 180.0) -> LibFetch:
    """Run easyeda2kicad for one LCSC code into ``lib_dir/<lib_name>.*`` and report what it wrote."""
    code = lcsc.strip().upper()
    if not LCSC_CODE.fullmatch(code):
        raise LayerError(INVALID_ARGUMENT, f"{lcsc!r} is not an LCSC code", hint="LCSC codes look like C520543; parts_search returns them.")
    if parts not in FLAGS:
        raise LayerError(INVALID_ARGUMENT, f"parts must be one of {', '.join(FLAGS)}, not {parts!r}")
    lib_dir.mkdir(parents=True, exist_ok=True)
    base = lib_dir / lib_name
    before = entries(base)
    # --project-relative makes the footprint's 3D path ${KIPRJMOD}/<base relative to the cwd>: run from the folder
    # above lib_dir, so a project's lib/ gives ${KIPRJMOD}/lib/<lib_name>.3dshapes/..., which KiCad resolves anywhere
    cmd = [sys.executable, "-m", "easyeda2kicad", FLAGS[parts], f"--lcsc_id={code}", "--output", str(base.resolve()), "--project-relative"]
    if overwrite:
        cmd.append("--overwrite")
    log.info("lib_fetch %s -> %s", code, base)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout_s, cwd=str(lib_dir.resolve().parent))
    except subprocess.TimeoutExpired as ex:
        raise LayerError(LIB_FETCH_FAILED, f"easyeda2kicad took longer than {timeout_s:.0f} s for {code}", hint="EasyEDA's servers are slow or unreachable; try again later.") from ex
    output = (r.stdout or "") + ("\n" + r.stderr if r.stderr else "")
    if "No module named easyeda2kicad" in output:
        raise LayerError(LIB_FETCH_FAILED, "easyeda2kicad is not installed in this environment", hint="pip install easyeda2kicad (the kicad-mcp-layer[parts] extra), then call again.")
    errors = [ln.strip() for ln in output.splitlines() if "[ERROR]" in ln or "Traceback" in ln or "Error" in ln and "[INFO]" not in ln]
    names = parse_names(output)
    after = entries(base)
    written = {k: sorted(after[k] - before[k]) for k in after}
    wanted = [k for k in ("symbol", "footprint", "model") if parts == "full" or parts == k]
    got = [k for k in wanted if names.get(k) or written[k]]
    if r.returncode != 0 or not got:
        tail = " | ".join(errors[-3:] or [ln for ln in output.strip().splitlines()[-3:]])
        raise LayerError(LIB_FETCH_FAILED, f"easyeda2kicad wrote nothing for {code}: {tail}",
                         hint="Check the code on jlcpcb.com/parts; a part without an EasyEDA model cannot be fetched, and the network must be up.")
    warnings = [ln.strip() for ln in output.splitlines() if "[WARNING]" in ln]
    if errors:
        warnings.extend(errors)
    fp_name = names["footprint"] or (written["footprint"][0] if written["footprint"] else None)
    sym_name = names["symbol"] or (written["symbol"][0] if written["symbol"] else None)
    fp_file = base.with_suffix(".pretty") / f"{fp_name}.kicad_mod" if fp_name else None
    if fp_file and fp_file.is_file():
        warnings.extend(upgrade_footprint(fp_file))
    pads = _count(r"\(pad ", fp_file.read_text(encoding="utf-8", errors="replace")) if fp_file and fp_file.is_file() else None
    shapes = base.with_suffix(".3dshapes")
    model = names["model"]
    step = shapes / f"{model}.step" if model else None
    wrl = shapes / f"{model}.wrl" if model else None
    files = [f"{lib_name}.kicad_sym"] if sym_name else []
    files += [f"{lib_name}.pretty/{fp_name}.kicad_mod"] if fp_name else []
    files += [f"{lib_name}.3dshapes/{p.name}" for p in (step, wrl) if p and p.is_file()]
    if fp_name and pads is not None and pads == 0:
        warnings.append(f"footprint {fp_name} has no pads")
    warnings.append("EasyEDA footprints are drawn by users: check pad count, pitch, drills and outline against the datasheet drawing before use.")
    return LibFetch(
        lcsc=code, library=lib_name, library_dir=str(lib_dir), symbol=sym_name, footprint=fp_name,
        footprint_id=f"{lib_name}:{fp_name}" if fp_name else None, symbol_id=f"{lib_name}:{sym_name}" if sym_name else None,
        model_step=str(step) if step and step.is_file() else None, model_wrl=str(wrl) if wrl and wrl.is_file() else None,
        pads=pads, pins=symbol_pins(base.with_suffix(".kicad_sym"), sym_name) if sym_name else None,
        files=files, warnings=warnings, source=f"EasyEDA component data for {code} through easyeda2kicad",
    )
