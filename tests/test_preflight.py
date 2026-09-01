"""The tagging gate: nothing is generated from a board that isn't identified."""

import os
import shutil
import tempfile

import fixtures
import pcbnew
from core import convert, netlist, preflight, symlib, sync

from boards import SIMPLE_BOARD as TEST_PROJECT  # noqa: E402


def _index():
    if not hasattr(_index, "cached"):
        _index.cached = symlib.SymbolIndex.build(
            cache_path=os.path.join(tempfile.gettempdir(), "p2s-test-index.json")
        )
    return _index.cached


def test_placeholder_reference_detection():
    for bad in ("REF**", "", "   ", "R?", "R", "J**"):
        assert preflight.is_placeholder_reference(bad), bad
    for good in ("R1", "D12", "CON1", "U3", "TP_1"):
        assert not preflight.is_placeholder_reference(good), good


def test_placeholder_net_detection():
    """Only a net with no name at all is untagged.

    KiCad puts ``Net-(C1-Pad1)`` on every unlabelled net, so on a real board that is
    most of them. Treating those as untagged would block essentially every design.
    They identify a net perfectly well -- they just are not names a person chose.
    """
    for untagged in ("", "   ", "\t"):
        assert preflight.is_placeholder_net(untagged), repr(untagged)
    for identified in ("1", "GND", "VCC", "3V3", "MY_SIGNAL",
                       "N$1", "Net-(D1-A)", "unconnected-(U1-Pad3)"):
        assert not preflight.is_placeholder_net(identified), identified


def test_auto_generated_net_names_are_recognised():
    """Generated names are valid identifiers but not worth carrying as labels."""
    for auto in ("N$1", "N$27", "Net-(D1-A)", "unconnected-(U1-Pad3)"):
        assert preflight.is_auto_net_name(auto), auto
    for chosen in ("1", "GND", "VCC", "3V3", "MY_SIGNAL", "Net_A"):
        assert not preflight.is_auto_net_name(chosen), chosen


def test_next_tag_continues_from_the_highest_in_use():
    """The board's own numbering wins: after 1, 2, 3 the next tag is 4."""
    gen = preflight.next_net_tag(["1", "2", "3"])
    assert [next(gen) for _ in range(3)] == ["4", "5", "6"]

    # Non-numeric names are still respected as taken, they just don't set the start.
    gen = preflight.next_net_tag(["GND", "VCC"])
    assert [next(gen) for _ in range(2)] == ["1", "2"]

    gen = preflight.next_net_tag(["7", "GND"])
    assert next(gen) == "8"


def test_untagged_board_is_blocked_and_writes_nothing():
    board, _, _ = fixtures.series_rc()
    for fp in board.GetFootprints():
        fp.SetReference("REF**")

    result = convert.convert(board, project_name="t", index=_index(),
                             tag_policy=preflight.REQUIRE)
    assert result.blocked
    assert result.schematic is None, "a blocked run must not build a schematic"
    assert result.tag_issues.untagged_components
    assert result.tag_issues.untagged_nets
    assert result.tag_issues.describe(), "the report must say what is missing"


def test_auto_tag_fills_gaps():
    board, _, _ = fixtures.series_rc()
    for fp in board.GetFootprints():
        fp.SetReference("REF**")

    result = convert.convert(board, project_name="t", index=_index(),
                             tag_policy=preflight.AUTO)
    assert not result.blocked
    assert len(result.auto_tagged_components) == 3
    assert len(result.auto_tagged_nets) == 3
    assert all(not preflight.is_placeholder_net(n.name) for n in result.nets)


def test_auto_tag_never_renames_what_is_already_named():
    """The whole point: fill the gaps, leave everything else exactly as it is."""
    board, _, _ = fixtures.series_rc()

    # Tag one part by hand and leave the others as placeholders.
    kept = None
    for fp in board.GetFootprints():
        if "Resistor" in fp.GetFPIDAsString():
            fp.SetReference("R47")
            kept = "R47"
        else:
            fp.SetReference("REF**")

    # Name one net by hand, out of sequence, and leave the rest unnamed.
    nets = netlist.extract_nets(board, auto_name=False).nets
    nets[0].name = "9"
    nets[0].named_from_board = True
    assigned = preflight.auto_tag_nets(nets)

    assert nets[0].name == "9", "a named net was renamed"
    assert "9" not in assigned
    # Auto-increment continues past the highest tag in use rather than colliding.
    assert assigned == ["10", "11"], assigned

    result = convert.convert(board, project_name="t", index=_index(),
                             tag_policy=preflight.AUTO)
    assert kept in result.reference_map.values(), "R47 was renamed"
    assert kept not in result.auto_tagged_components


def test_fully_tagged_board_needs_no_tagging():
    board = pcbnew.LoadBoard(TEST_PROJECT)
    nets = netlist.extract_nets(board, auto_name=False).nets
    from core import matcher
    infos = [matcher.from_footprint(fp) for fp in board.GetFootprints()]
    issues = preflight.inspect([i for i in infos if i.pads], nets)
    assert issues.ok, issues.describe()

    result = convert.convert(board, project_name="p", index=_index(),
                             tag_policy=preflight.REQUIRE)
    assert not result.blocked
    assert not result.auto_tagged_components
    assert not result.auto_tagged_nets


def test_duplicate_references_are_reported():
    board, _, _ = fixtures.series_rc()
    for fp in board.GetFootprints():
        fp.SetReference("R1")
    from core import matcher
    infos = [matcher.from_footprint(fp) for fp in board.GetFootprints()]
    issues = preflight.inspect(infos, [])
    assert "R1" in issues.duplicate_components
    assert not issues.ok


def test_sync_blocks_without_touching_the_project():
    with tempfile.TemporaryDirectory() as tmp:
        pcb = os.path.join(tmp, "p.kicad_pcb")
        shutil.copy(TEST_PROJECT, pcb)

        # Strip the tags back off so the gate has something to catch.
        from core import sexpr
        root = sexpr.load(pcb)
        for fp in root.nodes("footprint"):
            for prop in fp.nodes("property"):
                pa = prop.atoms()
                if len(pa) >= 2 and pa[0] == "Reference":
                    prop.children[1] = "REF**"
        sexpr.save(root, pcb)
        before = open(pcb, "rb").read()

        board = pcbnew.LoadBoard(pcb)
        outcome = sync.sync_project(board, pcb, index=_index(),
                                    tag_policy=preflight.REQUIRE)
        assert outcome.blocked
        assert not os.path.isfile(outcome.sch_path), "blocked run wrote a schematic"
        assert not os.path.isfile(sync.project_paths(pcb)[3]), "blocked run wrote state"
        assert open(pcb, "rb").read() == before, "blocked run modified the board"


def test_board_net_names_survive_into_the_schematic():
    """A net the *user* named must come back out under the same name.

    Without a label a wire carries no name, so KiCad invents ``Net-(D1-A)`` and the
    next PCB update would overwrite the chosen tag with it. Names KiCad generated in
    the first place are deliberately left unlabelled -- reproducing them would just
    clutter the sheet.
    """
    board = pcbnew.LoadBoard(TEST_PROJECT)
    result = convert.convert(board, project_name="p", index=_index(),
                             tag_policy=preflight.AUTO)
    text = result.schematic.dumps()

    chosen = [n.name for n in result.nets
              if n.name and not preflight.is_auto_net_name(n.name)]
    assert chosen, "expected the board to have some hand-named nets"
    for name in chosen:
        assert '"%s"' % name in text, "net %r has no label" % name

    generated = [n.name for n in result.nets if preflight.is_auto_net_name(n.name)]
    for name in generated:
        assert name not in result.preserved_net_names, \
            "%r is a generated name and should not be labelled" % name
