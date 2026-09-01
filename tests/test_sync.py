"""Incremental sync: re-running must not destroy the user's schematic work."""

import os
import shutil
import tempfile

import pcbnew
from core import preflight, sexpr, symlib, sync

from boards import SIMPLE_BOARD as TEST_PROJECT  # noqa: E402


def _index():
    if not hasattr(_index, "cached"):
        _index.cached = symlib.SymbolIndex.build(
            cache_path=os.path.join(tempfile.gettempdir(), "p2s-test-index.json")
        )
    return _index.cached


def _project(tmp):
    pcb = os.path.join(tmp, "p.kicad_pcb")
    shutil.copy(TEST_PROJECT, pcb)
    return pcb


def _run(pcb):
    board = pcbnew.LoadBoard(pcb)
    outcome = sync.sync_project(board, pcb, index=_index(),
                                tag_policy=preflight.AUTO)
    board.Save(pcb)
    return outcome


def _symbols(sch):
    """``{reference: (x, y)}`` from a schematic on disk."""
    root = sexpr.load(sch)
    out = {}
    for sym in root.nodes("symbol"):
        refs = [p.atoms()[1] for p in sym.nodes("property")
                if p.atoms() and p.atoms()[0] == "Reference"]
        if not refs:
            continue
        a = sym.node("at").atoms()
        out[refs[0]] = (float(str(a[0])), float(str(a[1])))
    return out


def _move(sch, reference, pos):
    root = sexpr.load(sch)
    for sym in root.nodes("symbol"):
        refs = [p.atoms()[1] for p in sym.nodes("property")
                if p.atoms() and p.atoms()[0] == "Reference"]
        if refs and refs[0] == reference:
            sym.node("at").children = [sexpr.num(pos[0]), sexpr.num(pos[1]), sexpr.num(0)]
    sexpr.save(root, sch)


def _add_user_text(sch, message, uuid="deadbeef-0000-4000-8000-000000000001"):
    root = sexpr.load(sch)
    node = sexpr.Node("text", [
        message,
        sexpr.Node("at", [sexpr.num(40), sexpr.num(40), sexpr.num(0)]),
        sexpr.Node("effects", [sexpr.Node("font", [
            sexpr.Node("size", [sexpr.num(1.27), sexpr.num(1.27)])])]),
        sexpr.Node("uuid", [uuid]),
    ])
    root.children.insert(len(root.children) - 2, node)
    sexpr.save(root, sch)


def _texts(sch):
    return [t.atoms()[0] for t in sexpr.load(sch).nodes("text") if t.atoms()]


def _delete_footprint(pcb, needle):
    """Remove a footprint by editing the board file; returns its reference.

    Deliberately not ``BOARD.Remove()``. That hands ownership of the footprint back
    across the SWIG boundary and leaves the runtime in a state where later
    ``LoadBoard`` calls return a bare ``SwigPyObject`` with no BOARD methods at all --
    which shows up as unrelated tests failing further down the run. Editing the file
    is also closer to what actually happens: the user deletes the part and saves.
    """
    root = sexpr.load(pcb)
    for fp in list(root.nodes("footprint")):
        atoms = fp.atoms()
        if not atoms or needle not in str(atoms[0]):
            continue
        reference = ""
        for prop in fp.nodes("property"):
            pa = prop.atoms()
            if len(pa) >= 2 and pa[0] == "Reference":
                reference = pa[1]
        root.remove(fp)
        sexpr.save(root, pcb)
        return reference
    raise AssertionError("no footprint matching %r" % needle)


def test_first_run_creates_then_reruns_are_stable():
    with tempfile.TemporaryDirectory() as tmp:
        pcb = _project(tmp)
        first = _run(pcb)
        assert first.created
        sch = first.sch_path
        assert os.path.isfile(sch)
        assert os.path.isfile(sync.project_paths(pcb)[3]), "state sidecar not written"

        before = open(sch, "rb").read()
        second = _run(pcb)
        assert not second.created, "second run should be an update, not a creation"
        assert open(sch, "rb").read() == before, "re-run changed the schematic"


def test_manual_symbol_positions_are_preserved():
    """Moving a symbol by hand must survive a re-run, or the plugin is unusable."""
    with tempfile.TemporaryDirectory() as tmp:
        pcb = _project(tmp)
        sch = _run(pcb).sch_path

        moved = (60.96, 160.02)
        _move(sch, "R1", moved)

        outcome = _run(pcb)
        assert _symbols(sch)["R1"] == moved, "R1 was moved back"
        assert outcome.result.preserved, "nothing reported as preserved"


def test_user_added_content_survives():
    """Items the plugin never created carry UUIDs it does not own, and must remain."""
    with tempfile.TemporaryDirectory() as tmp:
        pcb = _project(tmp)
        sch = _run(pcb).sch_path

        _add_user_text(sch, "USER NOTE - keep me")
        assert "USER NOTE - keep me" in _texts(sch)

        _run(pcb)
        assert "USER NOTE - keep me" in _texts(sch), "user's note was deleted"


def test_removed_footprint_drops_its_symbol():
    with tempfile.TemporaryDirectory() as tmp:
        pcb = _project(tmp)
        sch = _run(pcb).sch_path
        placed = set(_symbols(sch))
        assert len(placed) == 3, placed

        victim = _delete_footprint(pcb, "LED")
        assert victim in placed, "expected the LED to have been placed"

        _run(pcb)
        remaining = set(_symbols(sch))
        assert victim not in remaining, "symbol for a deleted footprint stayed behind"
        assert remaining == placed - {victim}


def test_added_footprint_gains_a_symbol_without_disturbing_others():
    with tempfile.TemporaryDirectory() as tmp:
        pcb = _project(tmp)
        sch = _run(pcb).sch_path
        _move(sch, "R1", (60.96, 160.02))
        _run(pcb)
        before = _symbols(sch)

        board = pcbnew.LoadBoard(pcb)
        fp = pcbnew.FootprintLoad(
            "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints/Resistor_THT.pretty",
            "R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal")
        fp.SetFPID(pcbnew.LIB_ID(
            "Resistor_THT", "R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal"))
        fp.SetPosition(pcbnew.VECTOR2I(150000000, 150000000))
        board.Add(fp)
        board.Save(pcb)

        _run(pcb)
        after = _symbols(sch)
        assert len(after) == len(before) + 1, "new footprint did not get a symbol"
        for ref, pos in before.items():
            assert after[ref] == pos, "%s moved when a new part was added" % ref


def test_state_file_is_not_required():
    """Losing the sidecar must not licence the plugin to delete the schematic."""
    with tempfile.TemporaryDirectory() as tmp:
        pcb = _project(tmp)
        sch = _run(pcb).sch_path
        _add_user_text(sch, "STILL HERE")

        os.remove(sync.project_paths(pcb)[3])
        _run(pcb)

        assert "STILL HERE" in _texts(sch), "content was dropped after state loss"


def test_dry_run_writes_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        pcb = _project(tmp)
        board = pcbnew.LoadBoard(pcb)
        outcome = sync.sync_project(board, pcb, index=_index(), dry_run=True,
                                    tag_policy=preflight.AUTO)
        assert outcome.result.symbols
        assert not os.path.isfile(outcome.sch_path), "dry run wrote a schematic"
        assert not os.path.isfile(sync.project_paths(pcb)[3]), "dry run wrote state"
