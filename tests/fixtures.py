"""Synthetic PCBs with known netlists.

The KiCad template projects turn out to be useless for validating geometric net
recovery: every one of them is an unrouted shield outline with zero tracks, so they
only ever exercise the declared-net path. These fixtures fill that gap -- each builds
a board with real copper and states the netlist it should produce, covering vias,
zones, multi-layer routing and abutting pads that no available sample board has.
"""

import os

import pcbnew

FPLIB = "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints"

MM = 1000000  # KiCad internal units (nm) per millimetre


def mm(v):
    return int(round(v * MM))


def vec(x, y):
    return pcbnew.VECTOR2I(mm(x), mm(y))


def _load(board, lib, name, ref, x, y):
    fp = pcbnew.FootprintLoad(os.path.join(FPLIB, lib + ".pretty"), name)
    if fp is None:
        raise RuntimeError("footprint %s:%s not found" % (lib, name))
    # FootprintLoad reads straight from a .pretty directory, so the returned footprint
    # has a bare FPID with no library nickname. A real board always carries
    # "Lib:Name", and the symbol matcher keys off the library part, so set it
    # explicitly -- otherwise these fixtures quietly stop resembling real input.
    fp.SetFPID(pcbnew.LIB_ID(lib, name))
    fp.SetReference(ref)
    fp.SetPosition(vec(x, y))
    board.Add(fp)
    return fp


def _track(board, x1, y1, x2, y2, layer=None):
    t = pcbnew.PCB_TRACK(board)
    t.SetStart(vec(x1, y1))
    t.SetEnd(vec(x2, y2))
    t.SetLayer(pcbnew.F_Cu if layer is None else layer)
    t.SetWidth(mm(0.25))
    board.Add(t)
    return t


def _via(board, x, y):
    v = pcbnew.PCB_VIA(board)
    v.SetPosition(vec(x, y))
    v.SetViaType(pcbnew.VIATYPE_THROUGH)
    v.SetDrill(mm(0.4))
    v.SetWidth(mm(0.8))
    v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
    board.Add(v)
    return v


def _pad_xy(fp, number):
    """Absolute position of a numbered pad, in millimetres."""
    for p in fp.Pads():
        if p.GetNumber() == number:
            pos = p.GetPosition()
            return pos.x / float(MM), pos.y / float(MM)
    raise KeyError("%s has no pad %s" % (fp.GetReference(), number))


def _expect(pairs):
    """Build the expected grouping keyed the same way NetlistResult.as_sets is."""
    return set(frozenset(p) for p in pairs)


# ---------------------------------------------------------------------------
# Fixtures. Each returns (board, expected_groups, expected_unconnected_count).
# ``expected_groups`` is a set of frozensets of "REF.padnumber" strings; using
# references rather than UUIDs keeps the expectations readable.
# ---------------------------------------------------------------------------


def series_rc():
    """R and LED in series off a 2-pin terminal block -- mirrors the test project."""
    b = pcbnew.BOARD()
    j = _load(b, "TerminalBlock_Phoenix",
              "TerminalBlock_Phoenix_MPT-0,5-2-2.54_1x02_P2.54mm_Horizontal", "J1", 112, 67.5)
    r = _load(b, "Resistor_THT",
              "R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal", "R1", 103, 81.5)
    d = _load(b, "LED_THT", "LED_D3.0mm", "D1", 119, 81.5)

    jx1, jy1 = _pad_xy(j, "1")
    jx2, jy2 = _pad_xy(j, "2")
    rx1, ry1 = _pad_xy(r, "1")
    rx2, ry2 = _pad_xy(r, "2")
    dx1, dy1 = _pad_xy(d, "1")
    dx2, dy2 = _pad_xy(d, "2")

    _track(b, jx1, jy1, rx1, jy1)      # J1.1 across
    _track(b, rx1, jy1, rx1, ry1)      # down to R1.1
    _track(b, rx2, ry2, dx1, dy1)      # R1.2 -> D1.1
    _track(b, jx2, jy2, dx2, jy2)      # J1.2 across
    _track(b, dx2, jy2, dx2, dy2)      # down to D1.2

    return b, _expect([
        {"J1.1", "R1.1"},
        {"R1.2", "D1.1"},
        {"J1.2", "D1.2"},
    ]), 0


def via_layer_change():
    """A net that hops F.Cu -> via -> B.Cu. Nothing in the sample corpus covers this."""
    b = pcbnew.BOARD()
    r1 = _load(b, "Resistor_THT",
               "R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal", "R1", 100, 100)
    r2 = _load(b, "Resistor_THT",
               "R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal", "R2", 100, 120)

    ax, ay = _pad_xy(r1, "2")
    bx, by = _pad_xy(r2, "1")
    vx, vy = ax + 5, ay + 5

    _track(b, ax, ay, vx, vy, pcbnew.F_Cu)
    _via(b, vx, vy)
    _track(b, vx, vy, bx, by, pcbnew.B_Cu)

    return b, _expect([{"R1.2", "R2.1"}]), 2  # R1.1 and R2.2 dangle


def zone_pour():
    """Two pads commoned only by a copper pour, with no track between them."""
    b = pcbnew.BOARD()
    r1 = _load(b, "Resistor_THT",
               "R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal", "R1", 100, 100)
    r2 = _load(b, "Resistor_THT",
               "R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal", "R2", 100, 110)

    ax, ay = _pad_xy(r1, "1")
    bx, by = _pad_xy(r2, "1")

    z = pcbnew.ZONE(b)
    z.SetLayer(pcbnew.F_Cu)
    outline = z.Outline()
    outline.NewOutline()
    lo_x, hi_x = min(ax, bx) - 3, max(ax, bx) + 3
    lo_y, hi_y = min(ay, by) - 3, max(ay, by) + 3
    for px, py in ((lo_x, lo_y), (hi_x, lo_y), (hi_x, hi_y), (lo_x, hi_y)):
        outline.Append(mm(px), mm(py))
    b.Add(z)
    try:
        pcbnew.ZONE_FILLER(b).Fill(b.Zones())
    except Exception:
        pass  # extractor falls back to the outline for unfilled zones

    return b, _expect([{"R1.1", "R2.1"}]), 2


def isolated_pads():
    """No copper at all -- every pad should be reported unconnected, not netted."""
    b = pcbnew.BOARD()
    _load(b, "Resistor_THT",
          "R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal", "R1", 100, 100)
    return b, _expect([]), 2


def mounting_holes_ignored():
    """NPTH mounting holes carry no pad number and must never become nets."""
    b = pcbnew.BOARD()
    j = _load(b, "TerminalBlock_Phoenix",
              "TerminalBlock_Phoenix_MPT-0,5-2-2.54_1x02_P2.54mm_Horizontal", "J1", 100, 100)
    r = _load(b, "Resistor_THT",
              "R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal", "R1", 100, 115)
    ax, ay = _pad_xy(j, "1")
    bx, by = _pad_xy(r, "1")
    _track(b, ax, ay, bx, by)
    return b, _expect([{"J1.1", "R1.1"}]), 2  # J1.2 and R1.2 dangle; holes excluded


ALL = [
    ("series_rc", series_rc),
    ("via_layer_change", via_layer_change),
    ("zone_pour", zone_pour),
    ("isolated_pads", isolated_pads),
    ("mounting_holes_ignored", mounting_holes_ignored),
]
