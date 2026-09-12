"""Connectivity from kicad-cli, never from home-grown wire tracing.

``kicad-cli sch export netlist --format kicadxml`` on the root sheet is the source of truth
for nets. The export is cached under the cache directory, keyed by the root sheet path and a
fingerprint of every schematic and project file in its directory, so unchanged designs are
never re-exported.
"""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from pathlib import Path

from kicad_layer.cli import runner
from kicad_layer.cli.discovery import find_kicad_cli
from kicad_layer.config import settings
from kicad_layer.errors import KICAD_CLI_FAILED, NOT_FOUND_IN_DESIGN, LayerError
from kicad_layer.models import (
    Component,
    Net,
    Netlist,
    NetNode,
    PinTrace,
    SheetInfo,
    TraceResult,
)
from kicad_layer.paths import display


def _fingerprint(root: Path) -> str:
    h = hashlib.sha256(str(root).encode("utf-8", "replace"))
    for p in sorted(root.parent.glob("*.kicad_sch")) + sorted(root.parent.glob("*.kicad_pro")):
        st = p.stat()
        h.update(f"{p.name}|{st.st_size}|{st.st_mtime_ns}".encode())
    return h.hexdigest()[:20]


def _cache_path(root: Path) -> Path:
    d = settings().cache_dir / "netlists"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{root.stem}-{_fingerprint(root)}.xml"


def _text(el: ET.Element | None) -> str | None:
    if el is None or el.text is None:
        return None
    return el.text.strip() or None


def parse_kicadxml(xml_path: Path) -> tuple[list[SheetInfo], list[Component], list[Net]]:
    tree = ET.parse(xml_path)
    export = tree.getroot()
    sheets: list[SheetInfo] = []
    for sh in export.iterfind("./design/sheet"):
        sheets.append(
            SheetInfo(
                number=int(sh.get("number", "0") or 0),
                name=sh.get("name", ""),
                tstamps=sh.get("tstamps", ""),
            )
        )
    components: list[Component] = []
    for comp in export.iterfind("./components/comp"):
        libsource = comp.find("libsource")
        sheetpath = comp.find("sheetpath")
        fields = {f.get("name", ""): (f.text or "").strip() for f in comp.iterfind("./fields/field")}
        properties = {p.get("name", ""): p.get("value", "") for p in comp.iterfind("./property")}
        pins = [pin.get("num", "") for pin in comp.iterfind("./units/unit/pins/pin")]
        components.append(
            Component(
                ref=comp.get("ref", ""),
                value=_text(comp.find("value")),
                footprint=_text(comp.find("footprint")),
                lib=libsource.get("lib") if libsource is not None else None,
                part=libsource.get("part") if libsource is not None else None,
                description=(libsource.get("description") or None) if libsource is not None else None,
                sheet_name=properties.get("Sheetname"),
                sheet_file=properties.get("Sheetfile"),
                sheet_path=sheetpath.get("names") if sheetpath is not None else None,
                tstamps=_text(comp.find("tstamps")),
                fields=fields,
                properties=properties,
                pins=pins,
            )
        )
    nets: list[Net] = []
    for net in export.iterfind("./nets/net"):
        nodes = [
            NetNode(
                ref=n.get("ref", ""),
                pin=n.get("pin", ""),
                pin_function=n.get("pinfunction"),
                pin_type=n.get("pintype"),
            )
            for n in net.iterfind("node")
        ]
        nets.append(
            Net(
                code=int(net.get("code", "0") or 0),
                name=net.get("name", ""),
                netclass=net.get("class"),
                nodes=nodes,
            )
        )
    return sheets, components, nets


def export_netlist(root_schematic: Path, *, refresh: bool = False) -> tuple[Path, bool, list[str]]:
    """Export (or reuse) the kicadxml netlist for ``root_schematic``.

    Returns (xml_path, cache_hit, command).
    """
    xml_path = _cache_path(root_schematic)
    if xml_path.is_file() and xml_path.stat().st_size > 0 and not refresh:
        return xml_path, True, []
    cli = find_kicad_cli()
    cmd = [cli.path, "sch", "export", "netlist", "--format", "kicadxml", "-o", xml_path, root_schematic]
    result = runner.run(cmd, timeout_s=settings().cli_long_timeout_s, cwd=root_schematic.parent)
    if not result.ok or not xml_path.is_file():
        raise LayerError(
            KICAD_CLI_FAILED,
            f"Netlist export failed (exit {result.returncode}) for {display(root_schematic)}.",
            hint="Run run_erc on the root sheet to see what KiCad complains about.",
            data={"output": result.tail()},
        )
    return xml_path, False, result.command


def load_netlist(
    root_schematic: Path,
    *,
    refresh: bool = False,
    include_components: bool = True,
    max_nets: int = 500,
) -> Netlist:
    xml_path, cache_hit, cmd = export_netlist(root_schematic, refresh=refresh)
    sheets, components, nets = parse_kicadxml(xml_path)
    truncated = len(nets) > max_nets
    return Netlist(
        source=display(root_schematic),
        netlist_path=str(xml_path),
        cache_hit=cache_hit,
        sheets=sheets,
        component_count=len(components),
        net_count=len(nets),
        components=components if include_components else [],
        nets=nets[:max_nets],
        truncated=truncated,
        command=cmd,
    )


def trace(root_schematic: Path, ref: str, pin: str | None = None) -> TraceResult:
    xml_path, _, _ = export_netlist(root_schematic)
    _, components, nets = parse_kicadxml(xml_path)
    comp = next((c for c in components if c.ref == ref), None)
    if comp is None:
        known = ", ".join(sorted(c.ref for c in components)[:40])
        raise LayerError(
            NOT_FOUND_IN_DESIGN,
            f"No component with reference {ref!r} in {display(root_schematic)}.",
            hint=f"Known references include: {known}",
        )
    by_pin: dict[str, PinTrace] = {}
    for p in comp.pins:
        by_pin[p] = PinTrace(pin=p)
    for net in nets:
        for node in net.nodes:
            if node.ref != ref:
                continue
            entry = by_pin.setdefault(node.pin, PinTrace(pin=node.pin))
            entry.pin_function = node.pin_function
            entry.pin_type = node.pin_type
            entry.net = net.name
            entry.netclass = net.netclass
            entry.connected_to = [n for n in net.nodes if not (n.ref == ref and n.pin == node.pin)]
    pins = list(by_pin.values())
    if pin is not None:
        pins = [p for p in pins if p.pin == pin]
        if not pins:
            raise LayerError(
                NOT_FOUND_IN_DESIGN,
                f"{ref} has no pin {pin!r}. Its pins are: {', '.join(comp.pins)}.",
            )
    pins.sort(key=lambda p: (len(p.pin), p.pin))
    return TraceResult(
        ref=ref,
        value=comp.value,
        footprint=comp.footprint,
        sheet_path=comp.sheet_path,
        pins=pins,
    )
