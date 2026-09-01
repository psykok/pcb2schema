"""Writing references, symbol links and nets back into the PCB."""

import os
import shutil
import tempfile

import pcbnew
from core import annotate, convert, preflight, symlib

from boards import SIMPLE_BOARD as TEST_PROJECT  # noqa: E402


def _index():
    if not hasattr(_index, "cached"):
        _index.cached = symlib.SymbolIndex.build(
            cache_path=os.path.join(tempfile.gettempdir(), "p2s-test-index.json")
        )
    return _index.cached


def _converted(tmp):
    """Copy the board somewhere writable, convert, and apply the result to it."""
    path = os.path.join(tmp, "board.kicad_pcb")
    shutil.copy(TEST_PROJECT, path)
    board = pcbnew.LoadBoard(path)
    result = convert.convert(board, project_name="board", index=_index(),
                             tag_policy=preflight.AUTO)
    report = annotate.apply_to_board(board, result)
    board.Save(path)
    return path, result, report


def _board_references(path):
    return sorted(fp.GetReference() for fp in pcbnew.LoadBoard(path).GetFootprints())


def test_write_back_survives_save_and_reload():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "board.kicad_pcb")
        shutil.copy(TEST_PROJECT, path)
        before = _board_references(path)

        path, result, report = _converted(tmp)
        assert len(report.paths) == 3
        assert not report.skipped

        after = _board_references(path)
        assert len(after) == 3
        assert all(not r.endswith("**") for r in after)
        # Whatever the user called them, write-back must not rename them.
        assert after == before, "references changed: %s -> %s" % (before, after)
        assert not result.auto_tagged_components


def test_footprint_paths_point_at_the_generated_symbols():
    """Without this link KiCad would treat every footprint as a new, unmatched part."""
    with tempfile.TemporaryDirectory() as tmp:
        path, result, _ = _converted(tmp)
        board = pcbnew.LoadBoard(path)
        paths = {fp.GetPath().AsString().lstrip("/") for fp in board.GetFootprints()}
        assert paths == set(result.uuid_map.values())
        assert all(paths), "some footprint has an empty path"


def test_nets_are_applied_to_pads_and_tracks():
    with tempfile.TemporaryDirectory() as tmp:
        path, result, _ = _converted(tmp)
        board = pcbnew.LoadBoard(path)

        expected = {n.name for n in result.nets}
        pad_nets = {p.GetNetname() for fp in board.GetFootprints()
                    for p in fp.Pads() if p.GetNumber()}
        assert pad_nets == expected, "pads: %s vs %s" % (pad_nets, expected)

        track_nets = {t.GetNetname() for t in board.GetTracks()}
        assert track_nets == expected, "tracks: %s vs %s" % (track_nets, expected)
        assert "" not in track_nets, "some track was left unnetted"


def test_write_back_is_idempotent():
    """Re-running must not renumber or relink anything, and must settle.

    The board file is not byte-stable between the *first* and second run, and that is
    KiCad's doing rather than ours: its writer orders tracks by netcode, and the codes
    come out differently when nets are created fresh than when they are read back from
    a file. Nothing semantic moves. What matters, and is checked here, is that no
    reference or link changes on re-run and that the file converges immediately.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path, first_result, _ = _converted(tmp)

        digests, results = [], []
        for _ in range(3):
            board = pcbnew.LoadBoard(path)
            result = convert.convert(board, project_name="board", index=_index(),
                             tag_policy=preflight.AUTO)
            report = annotate.apply_to_board(board, result)
            board.Save(path)
            assert not report.references, "re-run renamed parts: %s" % report.references
            digests.append(open(path, "rb").read())
            results.append(result)

        assert digests[0] == digests[1] == digests[2], "board file never settles"
        for result in results:
            assert result.uuid_map == first_result.uuid_map, "symbol links moved"
            assert result.reference_map == first_result.reference_map
            assert result.schematic.dumps() == first_result.schematic.dumps()


def test_existing_annotations_are_preserved():
    """A board the user already annotated must keep its reference designators."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "board.kicad_pcb")
        shutil.copy(TEST_PROJECT, path)
        board = pcbnew.LoadBoard(path)
        for fp in board.GetFootprints():
            if "Resistor" in fp.GetFPIDAsString():
                fp.SetReference("R47")
        result = convert.convert(board, project_name="board", index=_index(),
                             tag_policy=preflight.AUTO)
        assert "R47" in result.reference_map.values(), result.reference_map


def test_backup_is_written_and_pruned():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "board.kicad_pcb")
        shutil.copy(TEST_PROJECT, path)
        for _ in range(14):
            assert annotate.backup(path, keep=10)
        backups = [f for f in os.listdir(tmp) if f.endswith(".bak")]
        assert len(backups) <= 10, "backups were not pruned: %d" % len(backups)


def test_pcb_being_open_does_not_block_the_plugin():
    """The plugin lives on pcbnew's toolbar, so the board is *always* open.

    Treating the board's own lock as a conflict would make the plugin impossible to
    run from the UI it ships in. Only the schematic lock is a real conflict, because
    that is the file written directly.
    """
    with tempfile.TemporaryDirectory() as tmp:
        pcb_lock = os.path.join(tmp, "~proj.kicad_pcb.lck")
        sch_lock = os.path.join(tmp, "~proj.kicad_sch.lck")

        assert annotate.find_lock_files(tmp, "proj") == []

        open(pcb_lock, "w").close()
        assert annotate.find_lock_files(tmp, "proj") == [], \
            "an open PCB must not block the plugin"
        # The headless runner writes the board itself, so there it does matter.
        assert annotate.find_lock_files(tmp, "proj", include_pcb=True) == [pcb_lock]

        open(sch_lock, "w").close()
        assert annotate.find_lock_files(tmp, "proj") == [sch_lock], \
            "an open schematic must always block"
        assert sorted(annotate.find_lock_files(tmp, "proj", include_pcb=True)) == \
            sorted([pcb_lock, sch_lock])

        os.remove(pcb_lock)
        assert annotate.find_lock_files(tmp, "proj") == [sch_lock]
