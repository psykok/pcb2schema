"""Wiring is kept unless the net actually changed.

Re-running to pick up a board change used to redraw every net, throwing away any
routing the user had arranged by hand. Symbol positions were preserved but wiring was
not, which made the tool destructive to use iteratively -- the thing you re-run it for
is the small change, and it rewrote everything else around it.
"""

import os
import shutil
import tempfile

import pcbnew
from core import preflight, sexpr, state, sync

from boards import SIMPLE_BOARD


def _index():
    if not hasattr(_index, "cached"):
        from core import symlib
        _index.cached = symlib.SymbolIndex.build(
            cache_path=os.path.join(tempfile.gettempdir(), "p2s-test-index.json")
        )
    return _index.cached


def _project(tmp):
    pcb = os.path.join(tmp, "p.kicad_pcb")
    shutil.copy(SIMPLE_BOARD, pcb)
    return pcb


def _run(pcb):
    board = pcbnew.LoadBoard(pcb)
    return sync.sync_project(board, pcb, index=_index(),
                             tag_policy=preflight.AUTO, write_pcb=False)


def _wires(sch):
    root = sexpr.load(sch)
    out = []
    for w in root.nodes("wire"):
        xy = list(w.node("pts").nodes("xy"))
        out.append(tuple(sorted(
            (round(float(str(p.atoms()[0])), 3), round(float(str(p.atoms()[1])), 3))
            for p in xy)))
    return sorted(out)


def _orient(sch, reference, pos=None, rotation=None, mirror=None):
    """Move / rotate / mirror a symbol, as dragging it in eeschema would."""
    root = sexpr.load(sch)
    for sym in root.nodes("symbol"):
        ref = [p.atoms()[1] for p in sym.nodes("property")
               if p.atoms() and p.atoms()[0] == "Reference"][0]
        if ref != reference:
            continue
        at = sym.node("at").atoms()
        x = pos[0] if pos else float(str(at[0]))
        y = pos[1] if pos else float(str(at[1]))
        r = rotation if rotation is not None else float(str(at[2]))
        sym.node("at").children = [sexpr.num(x), sexpr.num(y), sexpr.num(r)]
        old = sym.node("mirror")
        if old is not None:
            sym.remove(old)
        if mirror:
            sym.append(sexpr.Node("mirror", [sexpr.Sym(a) for a in mirror]))
    sexpr.save(root, sch)


def _orientation_of(sch, reference):
    root = sexpr.load(sch)
    for sym in root.nodes("symbol"):
        ref = [p.atoms()[1] for p in sym.nodes("property")
               if p.atoms() and p.atoms()[0] == "Reference"][0]
        if ref != reference:
            continue
        at = sym.node("at").atoms()
        m = sym.node("mirror")
        return ((round(float(str(at[0])), 3), round(float(str(at[1])), 3)),
                round(float(str(at[2])), 3),
                tuple(str(a) for a in m.atoms()) if m else ())
    raise AssertionError("no symbol %s" % reference)


def _move_symbol(sch, reference, dx, dy):
    root = sexpr.load(sch)
    for sym in root.nodes("symbol"):
        ref = [p.atoms()[1] for p in sym.nodes("property")
               if p.atoms() and p.atoms()[0] == "Reference"][0]
        if ref != reference:
            continue
        a = sym.node("at").atoms()
        sym.node("at").children = [
            sexpr.num(float(str(a[0])) + dx),
            sexpr.num(float(str(a[1])) + dy),
            sexpr.num(0),
        ]
    sexpr.save(root, sch)


def test_unchanged_board_keeps_every_wire():
    with tempfile.TemporaryDirectory() as tmp:
        pcb = _project(tmp)
        sch = _run(pcb).sch_path
        before = _wires(sch)
        assert before

        outcome = _run(pcb)
        assert _wires(sch) == before, "re-run redrew wiring that had not changed"
        assert not outcome.result.rerouted_nets, outcome.result.rerouted_nets
        assert len(outcome.result.reused_nets) == len(outcome.result.nets)


def test_hand_routed_wire_survives_a_rerun():
    """The whole point: a route the user arranged is not thrown away."""
    with tempfile.TemporaryDirectory() as tmp:
        pcb = _project(tmp)
        sch = _run(pcb).sch_path

        # Reshape one of our wires, as dragging it in eeschema would.
        root = sexpr.load(sch)
        wire = next(iter(root.nodes("wire")))
        marker = (241.3, 241.3)
        xy = list(wire.node("pts").nodes("xy"))
        xy[1].children = [sexpr.num(marker[0]), sexpr.num(marker[1])]
        sexpr.save(root, sch)

        _run(pcb)
        assert any(marker in w for w in _wires(sch)), \
            "the hand-edited wire was redrawn"


def test_moving_one_symbol_only_reroutes_its_own_nets():
    """Exactly the nets touching the moved part, and no others.

    Asserted by net rather than by counting surviving wires: this board has three
    nets and R1 sits on two of them, so most of the wiring legitimately changes. The
    invariant that matters is *which* nets were redrawn.
    """
    with tempfile.TemporaryDirectory() as tmp:
        pcb = _project(tmp)
        sch = _run(pcb).sch_path
        first = _run(pcb)  # settle, so nothing is redrawn for unrelated reasons
        assert not first.result.rerouted_nets

        touching = {
            net.name for net in first.result.nets
            for p in net.pads
            if first.result.reference_map.get(p.fp_uuid) == "R1"
        }
        assert touching, "R1 is on no nets; pick a different part"

        _move_symbol(sch, "R1", 25.4, 12.7)
        outcome = _run(pcb)

        assert set(outcome.result.rerouted_nets) == touching, (
            "redrew %s, expected exactly %s"
            % (sorted(outcome.result.rerouted_nets), sorted(touching)))
        assert set(outcome.result.reused_nets) == {
            n.name for n in outcome.result.nets} - touching


def test_reuse_does_not_break_connectivity():
    """Reused wiring plus freshly routed wiring must still be one correct netlist."""
    import subprocess
    cli = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
    with tempfile.TemporaryDirectory() as tmp:
        pcb = _project(tmp)
        sch = _run(pcb).sch_path
        _move_symbol(sch, "R1", 25.4, 12.7)
        outcome = _run(pcb)
        assert outcome.result.reused_nets and outcome.result.rerouted_nets

        net_path = os.path.join(tmp, "out.net")
        proc = subprocess.run(
            [cli, "sch", "export", "netlist", "--format", "kicadsexpr",
             "-o", net_path, sch], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stdout + proc.stderr

        root = sexpr.load(net_path)
        exported = {frozenset("%s.%s" % (n.value("ref"), n.value("pin"))
                              for n in net.nodes("node"))
                    for net in root.node("nets").nodes("net")}
        exported = {m for m in exported if len(m) >= 2}

        placed = {ref for _i, _s, ref, _m in outcome.result.symbols}
        expected = set()
        for net in outcome.result.nets:
            members = frozenset(
                "%s.%s" % (outcome.result.reference_map[p.fp_uuid], p.number)
                for p in net.pads
                if outcome.result.reference_map.get(p.fp_uuid) in placed)
            if len(members) >= 2:
                expected.add(members)
        assert exported == expected, "reused wiring broke the netlist"


def test_deleting_part_of_a_nets_wiring_redraws_that_net():
    """Half-deleted wiring must be replaced, not left half there."""
    with tempfile.TemporaryDirectory() as tmp:
        pcb = _project(tmp)
        sch = _run(pcb).sch_path

        st = state.SyncState.load(sync.project_paths(pcb)[3])
        victim = next(n for n, e in st.nets.items() if e["uuids"])
        doomed = st.nets[victim]["uuids"][0]

        root = sexpr.load(sch)
        for child in list(root.children):
            if getattr(child, "name", None) and child.value("uuid") == doomed:
                root.remove(child)
        sexpr.save(root, sch)

        outcome = _run(pcb)
        assert victim in outcome.result.rerouted_nets, \
            "a net with missing wiring was not redrawn"


def test_state_records_wiring_per_net():
    with tempfile.TemporaryDirectory() as tmp:
        pcb = _project(tmp)
        _run(pcb)
        st = state.SyncState.load(sync.project_paths(pcb)[3])
        assert st.nets, "per-net wiring was not recorded"
        for name, entry in st.nets.items():
            assert entry["signature"], "%s has no signature" % name
            assert entry["uuids"], "%s recorded no items" % name
        # The flat ownership set must stay consistent with the per-net record.
        flat = {u for e in st.nets.values() for u in e["uuids"]}
        assert flat == st.routing


def test_rotation_and_mirror_survive_a_rerun():
    """Position was preserved but orientation was not, which is worse than it sounds.

    Pin positions are computed from the orientation. Writing the symbol unrotated
    while routing to its rotated pins puts every wire on that part in the wrong
    place -- so this is a correctness bug, not just a cosmetic one.
    """
    with tempfile.TemporaryDirectory() as tmp:
        pcb = _project(tmp)
        sch = _run(pcb).sch_path

        wanted = ((203.2, 50.8), 90.0, ("y",))
        _orient(sch, "J1", pos=wanted[0], rotation=wanted[1], mirror=wanted[2])

        _run(pcb)
        assert _orientation_of(sch, "J1") == wanted, (
            "J1 came back as %s, expected %s"
            % (_orientation_of(sch, "J1"), wanted))


def test_wiring_is_correct_against_a_rotated_symbol():
    """The netlist must still match once a part has been turned and mirrored."""
    import subprocess
    cli = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
    with tempfile.TemporaryDirectory() as tmp:
        pcb = _project(tmp)
        sch = _run(pcb).sch_path
        _orient(sch, "J1", pos=(203.2, 50.8), rotation=90, mirror=("y",))
        outcome = _run(pcb)

        net_path = os.path.join(tmp, "out.net")
        proc = subprocess.run(
            [cli, "sch", "export", "netlist", "--format", "kicadsexpr",
             "-o", net_path, sch], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stdout + proc.stderr

        exported = {frozenset("%s.%s" % (n.value("ref"), n.value("pin"))
                              for n in net.nodes("node"))
                    for net in sexpr.load(net_path).node("nets").nodes("net")}
        exported = {m for m in exported if len(m) >= 2}

        placed = {ref for _i, _s, ref, _m in outcome.result.symbols}
        expected = set()
        for net in outcome.result.nets:
            members = frozenset(
                "%s.%s" % (outcome.result.reference_map[p.fp_uuid], p.number)
                for p in net.pads
                if outcome.result.reference_map.get(p.fp_uuid) in placed)
            if len(members) >= 2:
                expected.add(members)
        assert exported == expected, "wiring does not reach a rotated symbol's pins"


def test_repositioned_field_text_is_kept():
    """Dragging a reference label aside should not be undone by a re-run."""
    with tempfile.TemporaryDirectory() as tmp:
        pcb = _project(tmp)
        sch = _run(pcb).sch_path

        root = sexpr.load(sch)
        for sym in root.nodes("symbol"):
            ref = [p.atoms()[1] for p in sym.nodes("property")
                   if p.atoms() and p.atoms()[0] == "Reference"][0]
            if ref != "J1":
                continue
            for prop in sym.nodes("property"):
                if prop.atoms() and prop.atoms()[0] == "Reference":
                    prop.node("at").children = [
                        sexpr.num(11.11), sexpr.num(22.22), sexpr.num(0)]
        sexpr.save(root, sch)

        _run(pcb)

        root = sexpr.load(sch)
        found = None
        for sym in root.nodes("symbol"):
            ref = [p.atoms()[1] for p in sym.nodes("property")
                   if p.atoms() and p.atoms()[0] == "Reference"][0]
            if ref != "J1":
                continue
            for prop in sym.nodes("property"):
                if prop.atoms() and prop.atoms()[0] == "Reference":
                    a = prop.node("at").atoms()
                    found = (round(float(str(a[0])), 2), round(float(str(a[1])), 2))
        assert found == (11.11, 22.22), "the moved reference text was reset to %s" % (found,)
