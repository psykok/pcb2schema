"""End-to-end conversion.

The netlist round-trip is the test that matters. A generated schematic is fed back
through KiCad's own netlist exporter and the result compared, net by net, against the
netlist recovered from the board. Almost every way this pipeline can go wrong -- wrong
symbol, wrong pin mapping, a pin snapped off-grid, a wire that doesn't quite touch, a
missing junction -- shows up as a difference here, and nowhere else.
"""

import os
import subprocess
import tempfile

import fixtures
import pcbnew
from core import convert, netlist, preflight, sexpr, symlib

KICAD_CLI = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
from boards import DEMO_BOARD, SIMPLE_BOARD as TEST_PROJECT  # noqa: E402


def _index():
    if not hasattr(_index, "cached"):
        _index.cached = symlib.SymbolIndex.build(
            cache_path=os.path.join(tempfile.gettempdir(), "p2s-test-index.json")
        )
    return _index.cached


def _cli(*args):
    proc = subprocess.run([KICAD_CLI] + list(args), capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


def _convert(board, name):
    # These boards are the plugin's target case -- drawn, not generated -- so they
    # are not fully tagged. Opt into auto-tagging rather than hitting the gate.
    return convert.convert(board, project_name=name, index=_index(),
                           tag_policy=preflight.AUTO)


def _expected_groups(board, result):
    """Board netlist expressed as ``{frozenset('REF.pad')}``."""
    refs = result.reference_map
    out = set()
    for net in netlist.extract_nets(board).nets:
        members = frozenset(
            "%s.%s" % (refs[p.fp_uuid], p.number)
            for p in net.pads
            if p.fp_uuid in refs
        )
        if len(members) >= 2:
            out.add(members)
    return out


def _exported_groups(path):
    root = sexpr.load(path)
    nets = root.node("nets")
    out = set()
    for net in nets.nodes("net"):
        members = frozenset(
            "%s.%s" % (n.value("ref"), n.value("pin")) for n in net.nodes("node")
        )
        if len(members) >= 2:
            out.add(members)
    return out


# A pad with no copper attached is genuinely unconnected. The plugin deliberately does
# not paper over that with no-connect flags -- hiding it would suppress a real layout
# problem -- so fixtures with intentional dangling pads are allowed this one violation
# type, and nothing else.
_DANGLING = ("pin_not_connected", "pin_not_driven", "no_connect_dangling")


def _erc_violations(sch, tmp, name):
    report = os.path.join(tmp, name + "-erc.json")
    code, out = _cli("sch", "erc", "--format", "json", "-o", report, sch)
    assert os.path.isfile(report), "%s: ERC did not run\n%s" % (name, out)
    import json
    with open(report) as fh:
        data = json.load(fh)
    return [v for sheet in data.get("sheets", []) for v in sheet.get("violations", [])]


def _roundtrip(board, name, tmp, allow_dangling=False):
    """Convert, then read the netlist back out of the generated schematic."""
    result = _convert(board, name)
    sch = os.path.join(tmp, name + ".kicad_sch")
    result.schematic.save(sch)

    violations = _erc_violations(sch, tmp, name)
    if allow_dangling:
        violations = [v for v in violations if v.get("type") not in _DANGLING]
    assert not violations, "%s: ERC violations: %s" % (
        name, [(v.get("type"), v.get("description", "")[:60]) for v in violations]
    )

    net_path = os.path.join(tmp, name + ".net")
    code, out = _cli("sch", "export", "netlist", "--format", "kicadsexpr",
                     "-o", net_path, sch)
    assert code == 0 and os.path.isfile(net_path), "%s: netlist export failed\n%s" % (name, out)

    return result, _exported_groups(net_path)


def test_test_project_roundtrip():
    """The user's own board: 3 parts, no nets assigned, recovered end to end."""
    assert os.path.isfile(TEST_PROJECT), "test project missing"
    board = pcbnew.LoadBoard(TEST_PROJECT)
    with tempfile.TemporaryDirectory() as tmp:
        result, exported = _roundtrip(board, "pcb2schema", tmp)

        assert not result.unresolved, "unresolved footprints: %s" % result.unresolved

        # The board carries its own reference designators now. They must come through
        # exactly as the user set them -- no renaming, no auto-tagging.
        on_board = sorted(fp.GetReference() for fp in board.GetFootprints())
        assert sorted(result.reference_map.values()) == on_board, (
            "references changed: board has %s, result has %s"
            % (on_board, sorted(result.reference_map.values())))
        assert not result.auto_tagged_components, "a tagged board was re-tagged"
        assert not result.auto_tagged_nets, "a named net was re-tagged"
        assert exported == _expected_groups(board, result), (
            "netlist mismatch\n  board:    %s\n  schematic: %s"
            % (sorted(map(sorted, _expected_groups(board, result))),
               sorted(map(sorted, exported)))
        )


def test_fixture_boards_roundtrip():
    """Synthetic boards, including the via and zone cases, survive the round trip."""
    verified = 0
    with tempfile.TemporaryDirectory() as tmp:
        for name, build in fixtures.ALL:
            board, _, _ = build()
            expected = _expected_groups(board, _convert(board, name))
            if not expected:
                continue  # nothing connected to verify (e.g. the isolated-pads case)
            _result, exported = _roundtrip(board, name, tmp, allow_dangling=True)
            assert exported == expected, (
                "%s: netlist mismatch\n  board:     %s\n  schematic: %s"
                % (name, sorted(map(sorted, expected)), sorted(map(sorted, exported)))
            )
            verified += 1

    # Without this the test passes happily when every fixture is skipped, which is
    # exactly what happened when the fixtures stopped carrying library nicknames.
    assert verified >= 4, "only %d fixture(s) actually verified" % verified


def test_conversion_is_idempotent():
    """Two runs must produce byte-identical output, or sync can never be a no-op."""
    board = pcbnew.LoadBoard(TEST_PROJECT)
    first = _convert(board, "pcb2schema")
    second = _convert(board, "pcb2schema")
    assert first.schematic.dumps() == second.schematic.dumps()
    assert first.reference_map == second.reference_map
    assert first.uuid_map == second.uuid_map


def test_generated_schematic_reparses_identically():
    """Our own output must survive our own parser unchanged."""
    board = pcbnew.LoadBoard(TEST_PROJECT)
    result = _convert(board, "pcb2schema")
    text = result.schematic.dumps()
    assert sexpr.dumps(sexpr.parse(text)) == text


def test_every_net_is_represented():
    """No net may be silently dropped -- it is either routed or labelled."""
    board = pcbnew.LoadBoard(TEST_PROJECT)
    result = _convert(board, "pcb2schema")
    text = result.schematic.dumps()
    assert text.count("(wire") >= len(result.nets), "expected at least one wire per net"
    assert not result.labelled_nets, "test project should route cleanly"


def test_unmatched_footprint_is_reported_not_guessed():
    """A footprint we cannot identify must surface, not silently vanish."""
    board = pcbnew.LoadBoard(TEST_PROJECT)
    result = convert.convert(
        board, project_name="pcb2schema", index=_index(),
        resolver=lambda info, cands: None, tag_policy=preflight.AUTO,
    )
    assert len(result.unresolved) == 3
    assert not result.symbols


def test_demo_board_roundtrip():
    """~90 parts, multi-unit logic, a 56-pin ground net: the real regression case.

    This board is why several bugs were found at all. A three-part circuit routes
    every pin as a wire endpoint by luck; here wires run *past* pins, nets share
    channels, and ICs pack four gates plus a power unit into one package. Each of
    those silently dropped pins from nets while the schematic still looked correct,
    and only the exported netlist showed it.
    """
    board = pcbnew.LoadBoard(DEMO_BOARD)
    with tempfile.TemporaryDirectory() as tmp:
        result = convert.convert(board, project_name="demo", index=_index(),
                                 tag_policy=preflight.AUTO)
        sch = os.path.join(tmp, "demo.kicad_sch")
        result.schematic.save(sch)

        net_path = os.path.join(tmp, "demo.net")
        code, out = _cli("sch", "export", "netlist", "--format", "kicadsexpr",
                         "-o", net_path, sch)
        assert code == 0, "netlist export failed\n%s" % out

        placed = {ref for _i, _s, ref, _m in result.symbols}
        expected = set()
        for net in result.nets:
            members = frozenset(
                "%s.%s" % (result.reference_map[p.fp_uuid], p.number)
                for p in net.pads if result.reference_map.get(p.fp_uuid) in placed)
            if len(members) >= 2:
                expected.add(members)

        exported = _exported_groups(net_path)
        assert exported == expected, (
            "netlist mismatch: %d only on board, %d only in schematic\n  %s\n  %s"
            % (len(expected - exported), len(exported - expected),
               sorted(map(sorted, list(expected - exported)[:3])),
               sorted(map(sorted, list(exported - expected)[:3]))))


def test_demo_board_packs_multi_unit_parts():
    """A 74LS00 is four gates plus a power unit, drawn as five symbols sharing a ref."""
    board = pcbnew.LoadBoard(DEMO_BOARD)
    result = convert.convert(board, project_name="demo", index=_index(),
                             tag_policy=preflight.AUTO)
    per_ref = {}
    for sym in result.schematic.symbols():
        ref = [p.atoms()[1] for p in sym.nodes("property")
               if p.atoms() and p.atoms()[0] == "Reference"][0]
        per_ref.setdefault(ref, []).append(sym.value("unit"))

    multi = {r: u for r, u in per_ref.items() if len(u) > 1}
    assert multi, "expected multi-unit parts on this board"
    for ref, units in multi.items():
        assert len(set(units)) == len(units), "%s has duplicate units %s" % (ref, units)
        assert "0" not in units, "%s placed a phantom unit 0" % ref

    # Pins common to all units belong to exactly one instance.
    for ref, syms in per_ref.items():
        seen = set()
        for sym in result.schematic.symbols():
            r = [p.atoms()[1] for p in sym.nodes("property")
                 if p.atoms() and p.atoms()[0] == "Reference"][0]
            if r != ref:
                continue
            for pin in sym.nodes("pin"):
                n = pin.atoms()[0]
                assert n not in seen, "%s declares pin %s on two units" % (ref, n)
                seen.add(n)
