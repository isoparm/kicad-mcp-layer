"""Build the two-layer example with the package's pipeline: sheets, lint, ERC, netlist gates, board, DRC.

Usage: python examples/two_layer_basic/build.py <project_dir> [flags of kicad_layer.design.build, e.g. --sch-only]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from design import make_project  # noqa: E402

from kicad_layer.design import build  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1].startswith("--"):
        print(__doc__)
        return 2
    project = make_project(Path(argv[1]))
    project.dir.mkdir(parents=True, exist_ok=True)
    return build.main(project, argv[2:])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
