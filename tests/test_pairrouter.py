"""Geometry and search primitives of the differential pair router and the plane stitcher."""

import math

from kicad_layer.routers import pairrouter as pr
from kicad_layer.routers.stitch import _seg_seg_dist


def test_offset_polyline_keeps_distance_on_straight_and_corner():
    pts = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)]
    left = pr._offset_polyline(pts, 0.5)
    right = pr._offset_polyline(pts, -0.5)
    assert left[0] == (0.0, 0.5) and right[0] == (0.0, -0.5)
    # the mitre at a 90 degree corner sits sqrt(2) * d from the corner
    assert math.isclose(math.dist(left[1], (10.0, 0.0)), 0.5 * math.sqrt(2), rel_tol=1e-6)
    assert math.isclose(left[-1][0], 9.5) and math.isclose(right[-1][0], 10.5)


def test_crossings_detects_a_swap():
    p = [("F.Cu", [(0.0, 0.0), (10.0, 1.0)])]
    n = [("F.Cu", [(0.0, 1.0), (10.0, 0.0)])]
    assert len(pr._crossings(p, n)) == 1
    assert pr._crossings(p, [("B.Cu", [(0.0, 1.0), (10.0, 0.0)])]) == []


def test_merge_short_never_folds_the_line_back():
    # a tight U-turn made of short steps must survive the merge instead of collapsing into a hairpin
    pts = [(0.0, 0.0), (1.0, 0.0), (1.2, 0.2), (1.2, 0.4), (1.0, 0.6), (0.0, 0.6)]
    out = pr._merge_short(pts, 0.6)
    for a, b, c in zip(out, out[1:], out[2:]):
        ux, uy = b[0] - a[0], b[1] - a[1]
        vx, vy = c[0] - b[0], c[1] - b[1]
        cos = (ux * vx + uy * vy) / (math.hypot(ux, uy) * math.hypot(vx, vy))
        assert cos >= -0.18, out


def test_astar_respects_headings_and_turn_limit():
    g = pr.Grid(0.0, 0.0, 10.0, 10.0, 0.5, ["F.Cu"])
    # a wall with a gap forces a detour
    for j in range(0, 21):
        if j not in (9, 10, 11):
            g.mark_disk("F.Cu", 5.0, j * 0.5, 0.2)
    start, goal = g.idx(1.0, 1.0), g.idx(9.0, 9.0)
    path = pr.astar(g, (*start, "F.Cu"), (*goal, "F.Cu"), start_dir=(1, 0), goal_dir=(1, 0))
    assert path is not None and path[0][:2] == start and path[-1][:2] == goal
    # first step heads east (within 45 degrees), every step turns at most 45 degrees
    prev = None
    for a, b in zip(path, path[1:]):
        d = (b[0] - a[0], b[1] - a[1])
        if prev is None:
            assert d[0] == 1
        else:
            cos = (d[0] * prev[0] + d[1] * prev[1]) / (math.hypot(*d) * math.hypot(*prev))
            assert cos >= 0.7 - 1e-9
        prev = d
    assert all(not g.blocked("F.Cu", i, j) for i, j, _ in path)


def test_astar_reports_no_path_through_a_closed_wall():
    g = pr.Grid(0.0, 0.0, 10.0, 10.0, 0.5, ["F.Cu"])
    for j in range(0, 21):
        g.mark_disk("F.Cu", 5.0, j * 0.5, 0.3)
    start, goal = g.idx(1.0, 1.0), g.idx(9.0, 9.0)
    assert pr.astar(g, (*start, "F.Cu"), (*goal, "F.Cu")) is None


def test_bump_adds_the_requested_length():
    pts = [(0.0, 0.0), (10.0, 0.0)]
    extra = 0.6
    out = pr._bump(pts, extra, 1)
    assert out is not None
    assert math.isclose(pr._length(out) - 10.0, extra, rel_tol=1e-6)
    assert all(y >= -1e-9 for _, y in out)  # the bump leans to the left of the run


def test_seg_seg_dist_zero_when_crossing():
    assert _seg_seg_dist((0, 0), (2, 2), (0, 2), (2, 0)) == 0.0
    assert math.isclose(_seg_seg_dist((0, 0), (2, 0), (0, 1), (2, 1)), 1.0)
