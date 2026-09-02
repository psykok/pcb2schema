"""Junction dots.

KiCad draws a dot wherever three or more connectable things meet, and **a pin counts
as one of them**. That matters here because wire runs are deliberately split at pins
(so the pin lands on a wire *endpoint* rather than mid-span). A pin partway along a
net therefore has two wire ends plus the pin meeting at it -- three things, and so a
dot. Counting only wire ends scores that as two and leaves the dot out.

The netlist still came out correct without them, which is why this went unnoticed:
only looking at the sheet, or checking against KiCad's convention, shows it.
"""

import collections
import math
import os
import shutil
import tempfile

import pcbnew
from core import preflight, sexpr, sync

from boards import DEMO_BOARD, SIMPLE_BOARD


def _index():
    if not hasattr(_index, "cached"):
        from core import symlib
        _index.cached = symlib.SymbolIndex.build(
            cache_path=os.path.join(tempfile.gettempdir(), "p2s-test-index.json")
        )
    return _index.cached


def _generate(tmp, source):
    pcb = os.path.join(tmp, "p.kicad_pcb")
    shutil.copy(source, pcb)
    board = pcbnew.LoadBoard(pcb)
    outcome = sync.sync_project(board, pcb, index=_index(),
                                tag_policy=preflight.AUTO, write_pcb=False)
    return outcome.sch_path


def _num(a):
    return float(str(a))


def _wires(root):
    out = []
    for w in root.nodes("wire"):
        xy = list(w.node("pts").nodes("xy"))
        out.append(((round(_num(xy[0].atoms()[0]), 3), round(_num(xy[0].atoms()[1]), 3)),
                    (round(_num(xy[1].atoms()[0]), 3), round(_num(xy[1].atoms()[1]), 3))))
    return out


def _junctions(root):
    return {(round(_num(j.node("at").atoms()[0]), 3),
             round(_num(j.node("at").atoms()[1]), 3))
            for j in root.nodes("junction")}


def _pin_points(root):
    """Sheet position of every placed pin, read back out of the written file."""
    lib = {}
    for sym in root.node("lib_symbols").nodes("symbol"):
        lib_id = sym.atoms()[0]
        for block in sym.nodes("symbol"):
            name = str(block.atoms()[0])
            bits = name.rsplit("_", 2)
            unit = bits[-2] if len(bits) >= 3 else "1"
            for pin in block.nodes("pin"):
                number = pin.node("number")
                at = pin.node("at").atoms()
                if number:
                    lib.setdefault((lib_id, unit, number.atoms()[0]),
                                   (_num(at[0]), _num(at[1])))

    points = []
    for sym in root.nodes("symbol"):
        lib_id = sym.value("lib_id")
        unit = sym.value("unit") or "1"
        at = sym.node("at").atoms()
        ox, oy = _num(at[0]), _num(at[1])
        rot = _num(at[2]) if len(at) > 2 else 0.0
        mirror = sym.node("mirror")
        ms = [str(x) for x in mirror.atoms()] if mirror else []
        for pin in sym.nodes("pin"):
            key = (lib_id, unit, pin.atoms()[0])
            if key not in lib:
                continue
            px, py = lib[key]
            if "x" in ms:
                py = -py
            if "y" in ms:
                px = -px
            x, y = px, -py
            if rot:
                r = math.radians(rot)
                c, s = math.cos(r), math.sin(r)
                x, y = x * c - y * s, x * s + y * c
            points.append((round(ox + x, 3), round(oy + y, 3)))
    return points


def _missing_dots(sch):
    """Points where KiCad would want a junction and there is none."""
    root = sexpr.load(sch)
    wires, junctions = _wires(root), _junctions(root)
    pins = set(_pin_points(root))

    ends = collections.Counter()
    for a, b in wires:
        ends[a] += 1
        ends[b] += 1

    def crossings(pt):
        x, y = pt
        n = 0
        for (x1, y1), (x2, y2) in wires:
            if x1 == x2 == x and min(y1, y2) < y < max(y1, y2):
                n += 1
            if y1 == y2 == y and min(x1, x2) < x < max(x1, x2):
                n += 1
        return n

    missing = set()
    for pt, count in ends.items():
        # three or more wire ends, or a wire end landing mid-way along another wire
        if count >= 3 or crossings(pt):
            if pt not in junctions:
                missing.add(pt)
    for pt in pins:
        # a pin plus two wire ends is three things meeting
        if ends[pt] >= 2 and pt not in junctions:
            missing.add(pt)
    return sorted(missing)


def test_simple_board_has_every_junction_dot():
    with tempfile.TemporaryDirectory() as tmp:
        sch = _generate(tmp, SIMPLE_BOARD)
        missing = _missing_dots(sch)
        assert not missing, "no junction dot at %s" % (missing[:6],)


def test_demo_board_has_every_junction_dot():
    with tempfile.TemporaryDirectory() as tmp:
        sch = _generate(tmp, DEMO_BOARD)
        missing = _missing_dots(sch)
        assert not missing, "no junction dot at %s" % (missing[:6],)


def test_dot_appears_where_a_run_is_split_at_a_pin():
    """The specific case: a pin partway along a net, not at the end of it."""
    from core import route

    r = route.Router(200.0, 200.0)
    pts = [(10 * route.GRID, 10 * route.GRID),
           (20 * route.GRID, 10 * route.GRID),
           (30 * route.GRID, 10 * route.GRID)]
    for p in pts:
        r.reserve_pin(p)
    result = r.route_nets([("N", pts)])

    middle = (round(pts[1][0], 3), round(pts[1][1], 3))
    dots = {(round(x, 3), round(y, 3)) for (x, y) in result.junctions}
    assert middle in dots, "the middle pin of a straight run got no junction dot"

    # The outer two are net ends: one wire each, so no dot.
    for end in (pts[0], pts[2]):
        assert (round(end[0], 3), round(end[1], 3)) not in dots


def test_no_dot_where_only_two_wires_meet():
    """A plain corner is two wire ends and nothing else -- no dot."""
    from core import route

    r = route.Router(200.0, 200.0)
    pts = [(5 * route.GRID, 5 * route.GRID), (25 * route.GRID, 30 * route.GRID)]
    for p in pts:
        r.reserve_pin(p)
    result = r.route_nets([("N", pts)])
    assert not result.junctions, "a two-pin net needs no junctions"
