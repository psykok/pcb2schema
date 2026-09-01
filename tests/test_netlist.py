"""Geometric net recovery."""

import glob
import os

import pcbnew

import fixtures
from core import netlist

TEMPLATES = "/Applications/KiCad/KiCad.app/Contents/SharedSupport/template"


def _groups_by_ref(result):
    """Recovered nets as a set of frozensets of ``REF.padnumber``."""
    return set(
        frozenset("%s.%s" % (p.fp.GetReference(), p.number) for p in net.pads)
        for net in result.nets
    )


def test_fixtures():
    """Each synthetic board recovers exactly the netlist it was built to have."""
    for name, build in fixtures.ALL:
        board, expected, n_unconnected = build()
        result = netlist.extract_nets(board)
        got = _groups_by_ref(result)
        assert got == expected, "%s: expected %s, got %s" % (name, expected, got)
        assert len(result.unconnected) == n_unconnected, (
            "%s: expected %d unconnected pads, got %d"
            % (name, n_unconnected, len(result.unconnected))
        )
        assert not result.warnings, "%s: unexpected warnings %s" % (name, result.warnings)


def test_determinism():
    """Repeated extraction is identical -- the basis of idempotent re-runs."""
    for name, build in fixtures.ALL:
        board, _, _ = build()
        first = netlist.extract_nets(board).as_sets()
        second = netlist.extract_nets(board).as_sets()
        assert first == second, "%s: extraction is not deterministic" % name


def test_declared_nets_preserved():
    """On conventionally netted boards the declared grouping is reproduced exactly."""
    checked = 0
    for path in sorted(glob.glob(TEMPLATES + "/*/*.kicad_pcb")):
        board = pcbnew.LoadBoard(path)
        declared = {}
        for fp in board.GetFootprints():
            for pad in fp.Pads():
                if pad.GetNumber() and pad.GetNetname():
                    declared.setdefault(pad.GetNetname(), set()).add(
                        (fp.m_Uuid.AsString(), pad.GetNumber())
                    )
        if not declared:
            continue
        got = netlist.extract_nets(board).as_sets()
        assert got == declared, "%s: netlist mismatch" % os.path.basename(path)
        checked += 1
    assert checked >= 10, "expected a meaningful corpus, only checked %d" % checked


def test_named_single_pad_net_survives():
    """A named net with one pad is real (connector pins) and must not be dropped."""
    board = pcbnew.LoadBoard(TEMPLATES + "/Arduino_Mega/Arduino_Mega.kicad_pcb")
    result = netlist.extract_nets(board)
    singles = [n for n in result.nets if len(n.pads) == 1]
    assert singles, "expected single-pad named nets on a shield template"
    assert all(n.named_from_board for n in singles)
