"""A plain copper plot of a board region: pads, tracks and vias by layer, as a PNG.

kicad-cli's 3D render hides copper under the solder mask and its SVG export needs a converter, so
this draws the geometry the layer itself works with. Front copper red, back copper blue, inner
layers grey, pads by their layer, vias green, the outline black. Meant for a quick look at a
routing result, not for documentation.
"""

from __future__ import annotations

import math
from pathlib import Path

from ..review import BoardModel, load_board
from .ses import Routes

COLOURS = {"F.Cu": (200, 40, 40), "B.Cu": (40, 80, 220), "In1.Cu": (120, 120, 120), "In2.Cu": (160, 120, 60)}


def plot(board: Path, dest: Path, *, region: tuple[float, float, float, float] | None = None, extra: Routes | None = None, scale: float = 20.0,
         layers: tuple[str, ...] = ("B.Cu", "F.Cu")) -> Path:
    from PIL import Image, ImageDraw

    bm: BoardModel = load_board(board)
    x0, y0, x1, y1 = region or bm.outline or (0, 0, 100, 100)
    w, h = int((x1 - x0) * scale) + 1, int((y1 - y0) * scale) + 1
    img = Image.new("RGB", (w, h), (245, 242, 232))
    dr = ImageDraw.Draw(img)

    def P(x, y):
        return ((x - x0) * scale, (y - y0) * scale)

    # pads
    for f in bm.footprints:
        for p in f.pads:
            pw, ph = p.size
            if abs((p.angle % 180) - 90) < 1e-6:
                pw, ph = ph, pw
            if p.shape == "circle":
                pw = ph = max(pw, ph)
            top = "F.Cu" in p.layers or "*.Cu" in p.layers or p.kind != "smd"
            col = (230, 150, 150) if top else (150, 170, 240)
            if p.kind == "np_thru_hole":
                col = (200, 200, 200)
            a, b = P(p.x - pw / 2, p.y - ph / 2), P(p.x + pw / 2, p.y + ph / 2)
            if p.shape == "circle":
                dr.ellipse([a, b], fill=col, outline=(90, 90, 90))
            else:
                dr.rectangle([a, b], fill=col, outline=(90, 90, 90))
    # tracks: existing on the board, then the extra routes
    def tracks(segs, vias):
        for layer in layers:
            for s in segs:
                if s.layer != layer:
                    continue
                dr.line([P(s.x1, s.y1), P(s.x2, s.y2)], fill=COLOURS.get(layer, (0, 0, 0)), width=max(1, int(s.width * scale)))
        for v in vias:
            r = v.size / 2
            a, b = P(v.x - r, v.y - r), P(v.x + r, v.y + r)
            dr.ellipse([a, b], fill=(60, 170, 80), outline=(20, 90, 30))
            rd = v.drill / 2
            dr.ellipse([P(v.x - rd, v.y - rd), P(v.x + rd, v.y + rd)], fill=(245, 242, 232))

    tracks(bm.segments, bm.vias)
    if extra is not None:
        tracks(extra.segments, extra.vias)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest)
    return dest
