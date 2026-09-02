"""Splitting the conversion into arrange-then-wire.

The grid placement is mechanical by design, so on any real board a person wants to
lay the sheet out themselves. Doing symbols first, arranging, then wiring means the
router works with that arrangement rather than one about to be discarded.
"""

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


def _project(tmp, board=SIMPLE_BOARD):
    pcb = os.path.join(tmp, "p.kicad_pcb")
    shutil.copy(board, pcb)
    return pcb


def _run(pcb, stage):
    board = pcbnew.LoadBoard(pcb)
    outcome = sync.sync_project(board, pcb, index=_index(),
                                tag_policy=preflight.AUTO, stage=stage,
                                write_pcb=False)
    return outcome


def _counts(sch):
    root = sexpr.load(sch)
    return {k: len(list(root.nodes(k)))
            for k in ("symbol", "wire", "junction", "global_label")}


def _positions(sch):
    root = sexpr.load(sch)
    out = {}
    for sym in root.nodes("symbol"):
        ref = [p.atoms()[1] for p in sym.nodes("property")
               if p.atoms() and p.atoms()[0] == "Reference"][0]
        a = sym.node("at").atoms()
        out[ref] = (round(float(str(a[0])), 3), round(float(str(a[1])), 3))
    return out


def _rearrange(sch):
    """Stand in for the user dragging everything into their own layout."""
    root = sexpr.load(sch)
    placed = {}
    for i, sym in enumerate(root.nodes("symbol")):
        ref = [p.atoms()[1] for p in sym.nodes("property")
               if p.atoms() and p.atoms()[0] == "Reference"][0]
        x, y = 50.8 + (i % 6) * 38.1, 76.2 + (i // 6) * 50.8
        sym.node("at").children = [sexpr.num(x), sexpr.num(y), sexpr.num(0)]
        placed[ref] = (round(x, 3), round(y, 3))
    sexpr.save(root, sch)
    return placed


def test_symbols_stage_places_without_wiring():
    with tempfile.TemporaryDirectory() as tmp:
        pcb = _project(tmp)
        outcome = _run(pcb, sync.STAGE_SYMBOLS)
        counts = _counts(outcome.sch_path)
        assert counts["symbol"] > 0
        assert counts["wire"] == 0, "symbols-only run drew wires"
        assert counts["junction"] == 0
        assert counts["global_label"] == 0
        assert not outcome.result.bus_nets
        assert "wiring skipped" in outcome.result.summary()


def test_nets_stage_wires_the_arrangement_it_finds():
    with tempfile.TemporaryDirectory() as tmp:
        pcb = _project(tmp)
        sch = _run(pcb, sync.STAGE_SYMBOLS).sch_path
        wanted = _rearrange(sch)

        outcome = _run(pcb, sync.STAGE_NETS)
        assert _positions(sch) == wanted, "the user's arrangement was not kept"
        assert _counts(sch)["wire"] > 0, "nets stage drew no wires"
        assert len(outcome.result.preserved) >= len(wanted)


def test_two_stage_matches_one_pass_electrically():
    """Splitting the run must not change the netlist, only where things sit."""
    with tempfile.TemporaryDirectory() as tmp:
        one = _project(tmp)
        single = _run(one, sync.STAGE_ALL)

        two_dir = os.path.join(tmp, "two")
        os.makedirs(two_dir)
        two = os.path.join(two_dir, "p.kicad_pcb")
        shutil.copy(SIMPLE_BOARD, two)
        _run(two, sync.STAGE_SYMBOLS)
        staged = _run(two, sync.STAGE_NETS)

        def nets(result):
            return {frozenset("%s.%s" % (result.reference_map[p.fp_uuid], p.number)
                              for p in n.pads)
                    for n in result.nets}

        assert nets(staged.result) == nets(single.result)


def test_symbols_stage_leaves_existing_wiring_alone():
    """Re-running symbols-only after wiring must not tear the wiring out."""
    with tempfile.TemporaryDirectory() as tmp:
        pcb = _project(tmp)
        sch = _run(pcb, sync.STAGE_ALL).sch_path
        wired = _counts(sch)
        assert wired["wire"] > 0

        _run(pcb, sync.STAGE_SYMBOLS)
        after = _counts(sch)
        assert after["wire"] == wired["wire"], "symbols-only run removed wiring"

        # ...and the record of who owns those wires must survive, or they would be
        # orphaned and never cleaned up on a later full run.
        state = sync.state.SyncState.load(sync.project_paths(pcb)[3])
        assert state.routing, "ownership of the existing wiring was forgotten"


def test_drift_reports_a_part_added_between_stages():
    with tempfile.TemporaryDirectory() as tmp:
        pcb = _project(tmp)
        _run(pcb, sync.STAGE_SYMBOLS)

        board = pcbnew.LoadBoard(pcb)
        fp = pcbnew.FootprintLoad(
            "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints"
            "/Resistor_THT.pretty",
            "R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal")
        fp.SetFPID(pcbnew.LIB_ID(
            "Resistor_THT", "R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal"))
        fp.SetReference("R99")
        fp.SetPosition(pcbnew.VECTOR2I(150000000, 150000000))
        board.Add(fp)
        board.Save(pcb)

        outcome = _run(pcb, sync.STAGE_NETS)
        assert outcome.drift.any, "an added part was not reported"
        assert "R99" in outcome.drift.added, outcome.drift.describe()
        assert "R99" in outcome.drift.describe()


def test_drift_reports_a_part_removed_between_stages():
    with tempfile.TemporaryDirectory() as tmp:
        pcb = _project(tmp)
        _run(pcb, sync.STAGE_SYMBOLS)

        root = sexpr.load(pcb)
        gone = None
        for footprint in list(root.nodes("footprint")):
            if "LED" in str(footprint.atoms()[0]) or "Terminal" in str(footprint.atoms()[0]):
                gone = [p.atoms()[1] for p in footprint.nodes("property")
                        if p.atoms() and p.atoms()[0] == "Reference"][0]
                root.remove(footprint)
                break
        assert gone, "expected something to delete"
        sexpr.save(root, pcb)

        outcome = _run(pcb, sync.STAGE_NETS)
        assert gone in outcome.drift.removed, outcome.drift.describe()


def test_no_drift_reported_when_nothing_changed():
    with tempfile.TemporaryDirectory() as tmp:
        pcb = _project(tmp)
        _run(pcb, sync.STAGE_SYMBOLS)
        outcome = _run(pcb, sync.STAGE_NETS)
        assert not outcome.drift.any, outcome.drift.describe()


def test_demo_board_two_stage_is_electrically_identical():
    with tempfile.TemporaryDirectory() as tmp:
        two = _project(tmp, DEMO_BOARD)
        _run(two, sync.STAGE_SYMBOLS)
        staged = _run(two, sync.STAGE_NETS)
        assert staged.result.symbols
        assert not staged.drift.any
        assert _counts(staged.sch_path)["wire"] > 0
