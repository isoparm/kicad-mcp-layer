from pathlib import Path

import pytest

from kicad_layer.errors import FILE_NOT_FOUND, WORKSPACE_VIOLATION, LayerError
from kicad_layer.paths import locate_project, resolve_in_workspace, root_schematic_for


def test_relative_path_resolves_inside_workspace(workspace):
    p = resolve_in_workspace("complex_hierarchy/complex_hierarchy.kicad_sch")
    assert p.is_file()
    assert p.name == "complex_hierarchy.kicad_sch"


def test_escape_is_refused(workspace):
    with pytest.raises(LayerError) as exc:
        resolve_in_workspace("../../research/README.md", must_exist=False)
    assert exc.value.code == WORKSPACE_VIOLATION


def test_absolute_outside_is_refused(workspace):
    outside = Path(workspace).parent.parent
    with pytest.raises(LayerError) as exc:
        resolve_in_workspace(outside / "anything.kicad_sch", must_exist=False)
    assert exc.value.code == WORKSPACE_VIOLATION


def test_missing_file(workspace):
    with pytest.raises(LayerError) as exc:
        resolve_in_workspace("complex_hierarchy/nope.kicad_sch")
    assert exc.value.code == FILE_NOT_FOUND


def test_locate_project_from_subsheet(workspace):
    files = locate_project("complex_hierarchy/ampli_ht.kicad_sch")
    assert files.name == "complex_hierarchy"
    assert files.root_schematic and files.root_schematic.name == "complex_hierarchy.kicad_sch"
    assert files.board and files.board.name == "complex_hierarchy.kicad_pcb"
    assert len(files.schematics) == 2


def test_root_schematic_for_subsheet(workspace):
    assert root_schematic_for("pic_programmer/pic_sockets.kicad_sch").name == "pic_programmer.kicad_sch"


def test_locate_project_from_directory(workspace):
    files = locate_project("multichannel")
    assert files.name == "multichannel_mixer"
    assert {b.name for b in files.boards} == {"multichannel_mixer.kicad_pcb", "multichannel_mixer-unrouted.kicad_pcb"}
