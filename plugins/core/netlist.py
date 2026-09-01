"""Recover a netlist from PCB geometry.

The boards this plugin targets are drawn, not generated: the user drops footprints
and draws tracks, so every item typically carries netcode 0. KiCad's ratsnest is
therefore useless here, but its *geometric* hit-testing is not --
``CONNECTIVITY_DATA.GetConnectedTracks()`` still resolves which tracks physically
touch a pad even when nothing has a net. That primitive plus a union-find over pads,
tracks, vias and zones is enough to rebuild the netlist.

``GetConnectedPads()`` deliberately is *not* used: it traverses by netcode and
returns nothing on an unnetted board.

The extractor also works on conventionally netted boards. Geometry is always the
source of truth for *grouping*; any pre-existing net names are then used to *name*
the resulting clusters, and a cluster spanning two different declared names is
reported as a probable short rather than silently merged away.
"""

import pcbnew

__all__ = ["PadRef", "Net", "NetlistResult", "extract_nets"]


class PadRef(object):
    """One electrical terminal: a numbered pad on a specific footprint."""

    __slots__ = ("fp", "pad", "fp_uuid", "number", "pos")

    def __init__(self, fp, pad):
        self.fp = fp
        self.pad = pad
        self.fp_uuid = fp.m_Uuid.AsString()
        self.number = pad.GetNumber()
        p = pad.GetPosition()
        self.pos = (p.x, p.y)

    @property
    def key(self):
        return (self.fp_uuid, self.number)

    def __repr__(self):
        return "PadRef(%s.%s)" % (self.fp.GetReference(), self.number)


class Net(object):
    """A set of pads that are electrically common."""

    __slots__ = ("name", "pads", "items", "named_from_board")

    def __init__(self, name, pads, items, named_from_board=False):
        self.name = name
        self.pads = pads
        self.items = items  # uuid strings of tracks/vias/zones, for PCB write-back
        self.named_from_board = named_from_board

    def __repr__(self):
        return "Net(%r, %d pads)" % (self.name, len(self.pads))


class NetlistResult(object):
    __slots__ = ("nets", "unconnected", "warnings")

    def __init__(self, nets, unconnected, warnings):
        self.nets = nets
        self.unconnected = unconnected
        self.warnings = warnings

    def as_sets(self):
        """``{net name: {(fp_uuid, pad number), ...}}`` -- handy for tests."""
        return {n.name: set(p.key for p in n.pads) for n in self.nets}


class _UnionFind(object):
    def __init__(self):
        self.parent = {}

    def add(self, x):
        self.parent.setdefault(x, x)

    def find(self, x):
        p = self.parent
        p.setdefault(x, x)
        root = x
        while p[root] != root:
            root = p[root]
        while p[x] != root:  # path compression
            p[x], x = root, p[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _uuid(item):
    return item.m_Uuid.AsString()


def _copper_layers(item):
    """Copper layers an item sits on."""
    try:
        ls = item.GetLayerSet()
    except Exception:
        return []
    try:
        return list(ls.CuStack())
    except Exception:
        return [l for l in ls.Seq() if pcbnew.IsCopperLayer(l)]


def _pads_touch(a, b):
    """True if two pads physically overlap on a shared copper layer.

    Pads can be commoned without any track between them (stacked pads, deliberately
    overlapping SMD pads). Preferred path is exact shape collision; the fallback
    covers SWIG builds that don't expose it.
    """
    shared = set(_copper_layers(a)) & set(_copper_layers(b))
    if not shared:
        return False
    for layer in shared:
        try:
            if a.GetEffectiveShape(layer).Collide(b.GetEffectiveShape(layer), 0):
                return True
        except Exception:
            # Fallback: is either centre inside the other?
            if a.HitTest(b.GetPosition(), 0) or b.HitTest(a.GetPosition(), 0):
                return True
    return False


def _zone_covers(zone, layer, pos, allow_outline):
    """True if *zone* connects to something at *pos* on *layer*.

    The filled area is the authoritative test, but it is useless on the boards this
    plugin exists for: with every item on netcode 0, KiCad's filler treats each pad
    as foreign copper and carves a clearance hole around it, so a pour sitting right
    on top of a pad reports no overlap at all.

    So when the pour *and* the pad are both unnetted -- the bare-geometry case -- fall
    back to plain containment in the zone outline, which is what the user visibly
    drew. That fallback is deliberately not applied once nets exist, because there a
    clearance gap is meaningful: a signal pad crossing a ground pour must stay
    separate, and using the outline would short them together.
    """
    try:
        if zone.HitTestFilledArea(layer, pos, 0):
            return True
    except Exception:
        pass
    if allow_outline:
        try:
            return zone.Outline().Contains(pos)
        except Exception:
            pass
    return False


def extract_nets(board, prefix="N$", auto_name=True):
    """Rebuild the netlist of *board* from geometry.

    Returns a :class:`NetlistResult`. Net ordering and generated names are derived
    from board coordinates, not from UUIDs, so repeated runs over an unchanged board
    produce identical output -- which is what makes the incremental sync idempotent.
    """
    board.BuildConnectivity()
    conn = board.GetConnectivity()
    uf = _UnionFind()
    warnings = []

    # -- collect terminals ---------------------------------------------------
    pads = []
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            # Unnumbered pads are mechanical (NPTH mounting holes) -- not terminals.
            if not pad.GetNumber():
                continue
            if not pad.IsOnCopperLayer():
                continue
            pads.append(PadRef(fp, pad))

    by_uuid = {}
    for pr in pads:
        u = _uuid(pr.pad)
        by_uuid[u] = pr
        uf.add(u)

    tracks = list(board.GetTracks())  # includes vias
    for t in tracks:
        uf.add(_uuid(t))

    zones = [z for z in board.Zones() if z.IsOnCopperLayer()]
    for z in zones:
        uf.add(_uuid(z))

    # -- seed the unions -----------------------------------------------------

    # Pads sharing a footprint and a pad number are one terminal (split/thermal pads).
    same_number = {}
    for pr in pads:
        same_number.setdefault(pr.key, []).append(_uuid(pr.pad))
    for group in same_number.values():
        for other in group[1:]:
            uf.union(group[0], other)

    # Pad <-> track, using KiCad's own layer- and shape-aware hit testing.
    for pr in pads:
        for t in conn.GetConnectedTracks(pr.pad):
            uf.union(_uuid(pr.pad), _uuid(t))

    # Track <-> track (and via <-> track).
    for t in tracks:
        ut = _uuid(t)
        for t2 in conn.GetConnectedTracks(t):
            uf.union(ut, _uuid(t2))

    # Pad <-> pad with no track in between.
    for i, a in enumerate(pads):
        for b in pads[i + 1 :]:
            if a.key == b.key:
                continue
            if _pads_touch(a.pad, b.pad):
                uf.union(_uuid(a.pad), _uuid(b.pad))

    # Zones swallow whatever sits inside them.
    for z in zones:
        uz = _uuid(z)
        zone_unnetted = z.GetNetCode() == 0
        z_layers = _copper_layers(z)
        for layer in z_layers:
            for pr in pads:
                if layer not in _copper_layers(pr.pad):
                    continue
                outline_ok = zone_unnetted and pr.pad.GetNetCode() == 0
                if _zone_covers(z, layer, pr.pad.GetPosition(), outline_ok):
                    uf.union(uz, _uuid(pr.pad))
            for t in tracks:
                if layer not in _copper_layers(t):
                    continue
                outline_ok = zone_unnetted and t.GetNetCode() == 0
                if _zone_covers(z, layer, t.GetStart(), outline_ok):
                    uf.union(uz, _uuid(t))

    # Respect nets the board already declares, so a declared-but-unrouted net still
    # groups correctly.
    by_netcode = {}
    for pr in pads:
        code = pr.pad.GetNetCode()
        if code:
            by_netcode.setdefault(code, []).append(_uuid(pr.pad))
    for group in by_netcode.values():
        for other in group[1:]:
            uf.union(group[0], other)

    # -- collect clusters ----------------------------------------------------
    clusters = {}
    for u, pr in by_uuid.items():
        clusters.setdefault(uf.find(u), {"pads": [], "items": []})["pads"].append(pr)
    for t in tracks:
        root = uf.find(_uuid(t))
        if root in clusters:
            clusters[root]["items"].append(_uuid(t))
    for z in zones:
        root = uf.find(_uuid(z))
        if root in clusters:
            clusters[root]["items"].append(_uuid(z))

    # -- name and order ------------------------------------------------------
    nets, unconnected = [], []
    for data in clusters.values():
        cluster_pads = data["pads"]

        declared = {}
        for pr in cluster_pads:
            name = pr.pad.GetNetname()
            if name:
                declared[name] = declared.get(name, 0) + 1

        # A lone pad is only "unconnected" if nobody named it. A named single-pad net
        # is deliberate -- connector and test-point pins on breakout boards are
        # routinely a net of one, and dropping them would silently lose real signals.
        if len(cluster_pads) < 2 and not declared:
            unconnected.extend(cluster_pads)
            continue
        if len(declared) > 1:
            warnings.append(
                "Pads of different nets (%s) are physically connected -- possible short."
                % ", ".join(sorted(declared))
            )

        chosen = max(sorted(declared), key=lambda n: declared[n]) if declared else None
        nets.append(
            Net(
                name=chosen,
                pads=sorted(cluster_pads, key=lambda p: (p.pos[1], p.pos[0], p.number)),
                items=sorted(data["items"]),
                named_from_board=chosen is not None,
            )
        )

    # Deterministic order from board coordinates, so generated names are stable.
    nets.sort(key=lambda n: (n.pads[0].pos[1], n.pads[0].pos[0], n.pads[0].number))
    unconnected.sort(key=lambda p: (p.pos[1], p.pos[0], p.number))

    if not auto_name:
        # The caller wants to see which nets are genuinely unnamed so it can gate on
        # tagging rather than have placeholder names appear behind its back.
        return NetlistResult(nets, unconnected, warnings)

    used = set(n.name for n in nets if n.name)
    counter = 1
    for net in nets:
        if net.name:
            continue
        while "%s%d" % (prefix, counter) in used:
            counter += 1
        net.name = "%s%d" % (prefix, counter)
        used.add(net.name)
        counter += 1

    return NetlistResult(nets, unconnected, warnings)
