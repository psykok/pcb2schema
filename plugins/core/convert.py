"""The conversion pipeline: board in, schematic out.

Sequence: recover the netlist from copper, choose a symbol per footprint, assign
references, place the symbols, route the nets, and emit the schematic. Anything the
router cannot solve falls back to net labels for that net alone, so the result is
always electrically complete even when it is not pretty.

User interaction enters through the *resolver* callback, keeping this module headless
and testable. The default resolver accepts confident matches and reports the rest.
"""

import math
import os

from . import (matcher, netlist, place, preflight, route, schematic,
               symbody, symlib)

__all__ = ["ConversionResult", "convert", "default_resolver", "MAX_ROUTED_FANOUT",
           "STAGE_SYMBOLS", "STAGE_NETS", "STAGE_ALL"]

# The conversion can be taken in two steps. Placing symbols and routing them are
# separate concerns, and the placement a person wants is rarely the grid one: doing
# symbols first lets them arrange the sheet by hand, then wire it up against that
# arrangement instead of fighting a layout the router already committed to.
STAGE_SYMBOLS = "symbols"   # place symbols, leave any existing wiring alone
STAGE_NETS = "nets"         # symbols (kept where they are) plus wiring
STAGE_ALL = "all"           # both, in one pass

# Above this many pins a net is labelled at each pin instead of being drawn as wires.
# This is how schematics are drawn by hand: nobody routes a 56-pin ground net as a
# spider across the sheet, they hang a GND label on each pin. It is also what makes
# large boards tractable -- tree-routing that net is thousands of A* searches for a
# result nobody could read.
MAX_ROUTED_FANOUT = 6


class ConversionResult(object):
    def __init__(self):
        self.schematic = None
        self.paper = "A4"
        self.symbols = []      # (FootprintInfo, SymbolDef, reference, pin_map)
        self.nets = []
        self.unresolved = []   # footprints with no symbol chosen
        self.labelled_nets = []      # nets the router could not solve
        self.bus_nets = []           # high fan-out nets labelled by design
        self.warnings = []
        self.reference_map = {}  # footprint uuid -> reference
        self.uuid_map = {}       # footprint uuid -> primary symbol uuid
        self.component_units = {}   # footprint uuid -> {unit: symbol uuid}
        self.generated_routing = []  # uuids of wires/junctions/labels we drew
        self.preserved = []          # placeable keys that kept a previous position
        self.preserved_net_names = []  # board net names carried into the schematic
        self.tag_issues = None       # preflight.TagIssues
        self.blocked = False         # stopped because the board is not fully tagged
        self.auto_tagged_nets = []
        self.auto_tagged_components = []
        self.stage = STAGE_ALL

    def summary(self):
        if self.blocked:
            return "blocked: the board is not fully tagged"
        parts = ["%d symbols" % len(self.symbols)]
        if self.stage == STAGE_SYMBOLS:
            parts.append("wiring skipped (symbols-only run)")
        else:
            parts.append("%d nets" % len(self.nets))
        if self.auto_tagged_components:
            parts.append("%d reference(s) auto-tagged" % len(self.auto_tagged_components))
        if self.auto_tagged_nets:
            parts.append("%d net(s) auto-tagged" % len(self.auto_tagged_nets))
        if self.unresolved:
            parts.append("%d unresolved footprint(s)" % len(self.unresolved))
        if self.bus_nets:
            parts.append("%d power/bus net(s) labelled" % len(self.bus_nets))
        if self.labelled_nets:
            parts.append("%d net(s) labelled (unroutable)" % len(self.labelled_nets))
        return ", ".join(parts)


def default_resolver(info, candidates, forced=False):
    """Accept only unambiguous matches; everything else is reported, not guessed.

    *forced* means something about this footprint is inconsistent, so the automatic
    answer must not be used even if it scores well.
    """
    if not forced and candidates and matcher.is_confident(candidates):
        return candidates[0]
    return None


# Rough width of one character at KiCad's default 1.27 mm text size. Only used to
# keep labels off other text, so erring wide is the safe direction.
_CHAR_WIDTH = 1.27 * 0.85
_TEXT_HEIGHT = 1.27 * 2.0
_BODY_PAD = 2.0


def _label_obstacles(placeables, result):
    """Areas a net label should stay clear of: symbol bodies and their captions.

    The captions matter more than the bodies. A value like
    ``Screw_Terminal_01x02`` is ~25 mm wide against a symbol body of about 6 mm, so
    padding the body alone leaves labels landing squarely on top of the text.
    """
    values = {}
    for info, symdef, ref, _pin_map in result.symbols:
        values[info.uuid] = (ref, _symbol_value(info, symdef))

    boxes = []
    for p in placeables:
        x0, y0, x1, y1 = p.symdef.bbox
        c0 = symbody.transform(x0, y0, p.pos, p.rotation, p.mirror_x, p.mirror_y)
        c1 = symbody.transform(x1, y1, p.pos, p.rotation, p.mirror_x, p.mirror_y)
        lo_x, hi_x = min(c0[0], c1[0]), max(c0[0], c1[0])
        lo_y, hi_y = min(c0[1], c1[1]), max(c0[1], c1[1])
        boxes.append((lo_x - _BODY_PAD, lo_y - _BODY_PAD,
                      hi_x + _BODY_PAD, hi_y + _BODY_PAD))

        ref, value = values.get(p.key[0], ("", ""))
        cx = p.pos[0]
        for text, cy in ((ref, lo_y - 2.54), (value, hi_y + 2.54)):
            if not text:
                continue
            half = max(len(text) * _CHAR_WIDTH / 2.0, 1.27)
            boxes.append((cx - half, cy - _TEXT_HEIGHT / 2.0,
                          cx + half, cy + _TEXT_HEIGHT / 2.0))
    return boxes


def _clearance(point, obstacles):
    """Distance from *point* to the nearest obstacle rectangle; 0 if inside one."""
    best = float("inf")
    px, py = point
    for x0, y0, x1, y1 in obstacles:
        dx = max(x0 - px, 0.0, px - x1)
        dy = max(y0 - py, 0.0, py - y1)
        best = min(best, (dx * dx + dy * dy) ** 0.5)
    return best


def _label_anchor(segments, obstacles=()):
    """Pick where to hang a net label, and which way to rotate it.

    The obvious choice -- the midpoint of the longest wire -- routinely lands on top
    of a symbol's reference or value text, because the longest wire is usually the one
    running past a part. Candidate points along each segment are scored by how far
    they sit from every symbol instead, so the label goes somewhere it can be read.
    """
    best = None
    for (x1, y1), (x2, y2) in segments:
        length = abs(x2 - x1) + abs(y2 - y1)
        if length <= 0:
            continue
        rotation = 90 if abs(x2 - x1) < abs(y2 - y1) else 0
        for frac in (0.5, 0.35, 0.65, 0.25, 0.75):
            point = (place.snap(x1 + (x2 - x1) * frac),
                     place.snap(y1 + (y2 - y1) * frac))
            # Prefer clearance, then longer wires, then a stable order.
            score = (round(_clearance(point, obstacles), 2), round(length, 2))
            if best is None or score > best[0]:
                best = (score, point, rotation)
    if best is None:
        return None, 0
    return best[1], best[2]


def _symbol_value(info, symdef):
    """Pick a sensible Value field for the placed symbol.

    When a footprint is dropped in the PCB editor KiCad sets its Value to the
    footprint's own name. Copying that into the schematic gives every part a caption
    like ``TerminalBlock_Phoenix_MPT-0,5-2-2.54_1x02_P2.54mm_Horizontal``, which runs
    clear off the sheet and says nothing useful. A value that is merely the footprint
    name is treated as absent, and the symbol's own default ("R", "LED") used instead.
    """
    value = (info.value or "").strip()
    if not value or value == info.name or value == info.lib_id:
        return symdef.value or symdef.name
    return value


def _assign_references(chosen, existing):
    """Number references per prefix, ordered by board position.

    Ordering by geometry rather than iteration order keeps numbering stable between
    runs and gives R1, R2, R3 running left-to-right across the board, which is what
    someone reading the schematic next to the layout expects.
    """
    used = set(existing.values())
    refs = {}
    by_prefix = {}

    for info, cand in chosen:
        prefix = cand.entry.reference or "U"
        by_prefix.setdefault(prefix, []).append((info, cand))

    for prefix, group in by_prefix.items():
        group.sort(key=lambda ic: (round(ic[0].pos[1], -5), round(ic[0].pos[0], -5)))
        counter = 1
        for info, _cand in group:
            keep = existing.get(info.uuid)
            if keep and keep != "REF**" and not keep.endswith("**"):
                refs[info.uuid] = keep
                continue
            while "%s%d" % (prefix, counter) in used:
                counter += 1
            ref = "%s%d" % (prefix, counter)
            used.add(ref)
            refs[info.uuid] = ref
            counter += 1
    return refs


def _pinned_positions(doc, sync_state):
    """Positions of symbols we previously generated, so re-runs leave them put."""
    if doc is None or sync_state is None:
        return {}
    by_uuid = {}
    for sym in doc.symbols():
        u = sym.value("uuid")
        if u:
            by_uuid[u] = sym

    pinned = {}
    for fp_uuid, entry in sync_state.components.items():
        for unit, sym_uuid in entry.get("units", {}).items():
            sym = by_uuid.get(sym_uuid)
            if sym is None:
                continue
            at = sym.node("at")
            if at is None or len(at.atoms()) < 2:
                continue
            a = at.atoms()
            mirror = sym.node("mirror")
            ms = [str(x) for x in mirror.atoms()] if mirror else []
            pinned[(fp_uuid, int(unit))] = (
                (float(str(a[0])), float(str(a[1]))),
                float(str(a[2])) if len(a) > 2 else 0.0,
                "x" in ms,
                "y" in ms,
            )
    return pinned


def convert(board, project_name="", project_dir=None, resolver=None,
            index=None, loader=None, progress=None,
            existing=None, sync_state=None, tag_policy=preflight.REQUIRE,
            stage=STAGE_ALL):
    """Convert *board* into a :class:`ConversionResult`.

    Passing *existing* (a :class:`~core.schematic.Schematic`) and *sync_state* turns
    this into an incremental update: symbols the plugin placed before keep their
    positions, everything the plugin drew is regenerated, and anything the user added
    is carried through untouched.
    """
    resolver = resolver or default_resolver
    result = ConversionResult()
    result.stage = stage
    wire_it_up = stage != STAGE_SYMBOLS

    def step(msg):
        if progress:
            progress(msg)

    libraries = symlib.discover_libraries(project_dir)
    index = index or symlib.SymbolIndex.build(project_dir)
    loader = loader or symbody.SymbolLoader(libraries)

    # -- 1. netlist ---------------------------------------------------------
    step("Recovering netlist from copper")
    # auto_name=False so genuinely unnamed nets stay visibly unnamed: the tagging gate
    # below has to be able to tell them apart from ones the user named.
    nets = netlist.extract_nets(board, auto_name=False)
    result.nets = nets.nets
    result.warnings.extend(nets.warnings)

    # -- 2. symbol selection ------------------------------------------------
    step("Matching footprints to symbols")
    footprints = [matcher.from_footprint(fp) for fp in board.GetFootprints()]
    footprints.sort(key=lambda i: (i.pos[1], i.pos[0], i.uuid))

    chosen = []
    for info in footprints:
        if not info.pads:
            continue  # mechanical-only footprint: nothing to represent
        candidates = matcher.rank(info, index)

        # A part labelled with a name we can find, but whose pads rule it out, is a
        # real inconsistency. Report it and always ask rather than quietly ranking a
        # different part to the top.
        conflict = matcher.check_value_conflict(info, index)
        if conflict:
            result.warnings.append(conflict)
            pick = resolver(info, candidates, forced=True)
        else:
            pick = resolver(info, candidates)
        if pick is None:
            result.unresolved.append(info)
            continue
        chosen.append((info, pick))

    # -- 3. tagging gate ----------------------------------------------------
    # Everything must be identified before a schematic is generated from it. If the
    # board is not fully tagged the caller chooses: stop and let the user do it, or
    # auto-increment the gaps here. Nothing already named is ever renamed.
    step("Checking references and net names")
    considered = [i for i in footprints if i.pads]
    result.tag_issues = preflight.inspect(considered, result.nets)

    if not result.tag_issues.ok and tag_policy != preflight.AUTO:
        # Stop before writing anything; the caller reports what needs tagging.
        result.blocked = True
        return result

    if result.tag_issues.untagged_nets:
        result.auto_tagged_nets = preflight.auto_tag_nets(result.nets)

    # -- 4. references ------------------------------------------------------
    existing_refs = {i.uuid: i.reference for i in footprints}
    result.auto_tagged_components = [
        i.reference for i in result.tag_issues.untagged_components
    ]
    result.reference_map = _assign_references(chosen, existing_refs)
    result.auto_tagged_components = sorted(
        result.reference_map[i.uuid]
        for i in result.tag_issues.untagged_components
        if i.uuid in result.reference_map
    )

    # -- 5. placement -------------------------------------------------------
    step("Placing symbols")
    placeables = []
    defs = {}
    for info, cand in chosen:
        symdef = loader.load(cand.lib_id)
        if symdef is None:
            result.warnings.append(
                "Symbol %s could not be loaded; %s skipped." % (cand.lib_id, info.lib_id)
            )
            result.unresolved.append(info)
            continue
        defs[info.uuid] = (symdef, cand)
        # A multi-unit part becomes several symbols sharing one reference.
        needed = sorted(set(
            symdef.unit_of_pin(cand.pin_map.get(pad, pad)) for pad in info.pads
        ) or {1})
        for unit in needed:
            placeables.append(place.Placeable(
                key=(info.uuid, unit),
                symdef=symdef,
                unit=unit,
                board_pos=(info.pos[0] / 1e6, info.pos[1] / 1e6),
            ))

    pinned = _pinned_positions(existing, sync_state)
    paper, sheet_w, sheet_h = place.place_symbols(
        placeables, pinned=pinned,
        paper=(sync_state.paper if sync_state and pinned else None),
    )
    result.paper = paper
    by_key = {p.key: p for p in placeables}
    result.preserved = sorted(k for k in pinned if k in by_key)

    # -- 6. build the schematic ---------------------------------------------
    step("Writing symbols")
    if existing is not None:
        doc = existing
        # Strip only what we generated last time. Items the user added carry UUIDs we
        # never recorded, so they are invisible to this and survive the update.
        # A symbols-only run must not tear out wiring it is not going to replace.
        for uuid in (list(sync_state.routing) if (sync_state and wire_it_up) else []):
            doc.remove_by_uuid(uuid)
        for uuid in sorted(sync_state.owned_symbol_uuids()) if sync_state else []:
            doc.remove_by_uuid(uuid)
    else:
        doc = schematic.Schematic(project_name=project_name)
    doc.set_paper(paper)

    for info, cand in chosen:
        if info.uuid not in defs:
            continue
        symdef, cand = defs[info.uuid]
        ref = result.reference_map[info.uuid]
        own = [pl for pl in placeables if pl.key[0] == info.uuid]
        lowest = min(pl.unit for pl in own) if own else 1
        for p in own:
            node = doc.add_symbol(
                symdef=symdef,
                reference=ref,
                value=_symbol_value(info, symdef),
                footprint=info.lib_id,
                pos=p.pos,
                unit=p.unit,
                uuid=schematic.derive_uuid("symbol", info.uuid, p.unit),
                description=symdef.name,
                # Pins common to every unit belong on exactly one instance.
                include_common=(p.unit == lowest),
            )
            result.component_units.setdefault(info.uuid, {})[p.unit] = node.value("uuid")
            if p.unit == lowest:
                result.uuid_map[info.uuid] = node.value("uuid")
        result.symbols.append((info, symdef, ref, cand.pin_map))

    # -- 7. pin positions ---------------------------------------------------
    pin_pos = {}
    pin_escape = {}
    for info, symdef, ref, pin_map in result.symbols:
        for pad in info.pads:
            pin_number = pin_map.get(pad, pad)
            pin = symdef.pin_by_number(pin_number)
            if pin is None:
                result.warnings.append(
                    "%s: pad %s has no matching pin on %s." % (ref, pad, symdef.lib_id)
                )
                continue
            unit = symdef.unit_of_pin(pin_number)
            p = by_key.get((info.uuid, unit)) or by_key.get((info.uuid, 1))
            if p is None:
                continue
            key = (info.uuid, pad)
            pin_pos[key] = symbody.pin_position(
                pin, p.pos, p.rotation, p.mirror_x, p.mirror_y
            )
            # A pin's angle points from its connection point *into* the body, so the
            # wire leaves in the opposite direction. Record where it exits to so the
            # router can clear a corridor -- some symbols draw their body over the
            # pin's own cell.
            rad = math.radians(pin.angle + 180.0)
            reach = max(pin.length, 2.54) + 1.27
            pin_escape[key] = symbody.transform(
                pin.x + reach * math.cos(rad),
                pin.y + reach * math.sin(rad),
                p.pos, p.rotation, p.mirror_x, p.mirror_y,
            )

    if not wire_it_up:
        step("Symbols placed; wiring skipped")
        result.schematic = doc
        return result

    # -- 8. routing ---------------------------------------------------------
    step("Routing nets")
    router = route.Router(sheet_w, sheet_h)
    for p in placeables:
        x0, y0, x1, y1 = p.symdef.body_bbox
        c0 = symbody.transform(x0, y0, p.pos, p.rotation, p.mirror_x, p.mirror_y)
        c1 = symbody.transform(x1, y1, p.pos, p.rotation, p.mirror_x, p.mirror_y)
        router.block_rect(min(c0[0], c1[0]), min(c0[1], c1[1]),
                          max(c0[0], c1[0]), max(c0[1], c1[1]))
    for key, pt in pin_pos.items():
        router.reserve_pin(pt, pin_escape.get(key))

    routable, unplaced, high_fanout = [], [], []
    for net in result.nets:
        pts = [pin_pos[(p.fp_uuid, p.number)] for p in net.pads
               if (p.fp_uuid, p.number) in pin_pos]
        if len(pts) > MAX_ROUTED_FANOUT:
            high_fanout.append((net.name, pts))
        elif len(pts) >= 2:
            routable.append((net.name, pts))
        elif len(pts) == 1:
            unplaced.append((net.name, pts))

    routed = router.route_nets(routable)
    for (a, b) in routed.segments:
        result.generated_routing.append(doc.add_wire(a, b).value("uuid"))
    for pt in routed.junctions:
        result.generated_routing.append(doc.add_junction(pt).value("uuid"))

    # -- 9a. keep the board's own net names ---------------------------------
    # A wire carries no name; only a label does. If the board names a net, that name
    # is the user's and has to survive into the schematic -- otherwise KiCad renames
    # it Net-(D1-A), and the next "Update PCB from Schematic" pushes that back over
    # the original. Nets we or KiCad named automatically are left unlabelled.
    obstacles = _label_obstacles(placeables, result)

    # Only names a person chose are worth carrying over. KiCad regenerates its own
    # Net-(C1-Pad1) style from the wiring, so labelling those would paper the sheet
    # with noise for no gain.
    named = {n.name for n in result.nets
             if n.name and not preflight.is_auto_net_name(n.name)}
    for net_name in sorted(named - set(routed.failed)):
        segments = routed.by_net.get(net_name)
        if not segments:
            continue
        anchor, rotation = _label_anchor(segments, obstacles)
        if anchor is None:
            continue
        result.generated_routing.append(
            doc.add_label(net_name, anchor, rotation, scope="global").value("uuid")
        )
        result.preserved_net_names.append(net_name)

    # -- 9b. label fallback -------------------------------------------------
    # A net the router could not solve still has to be electrically correct, so it
    # becomes labels on its pins instead of being silently dropped.
    failed = set(routed.failed)
    for name, pts in routable:
        if name in failed:
            result.labelled_nets.append(name)
            for pt in pts:
                result.generated_routing.append(doc.add_label(name, pt).value("uuid"))
    # Power and ground: a label on every pin, as a person would draw it.
    for name, pts in high_fanout:
        result.bus_nets.append(name)
        for pt in pts:
            result.generated_routing.append(
                doc.add_label(name, pt, scope="global").value("uuid"))

    for name, pts in unplaced:
        # Single-pad named nets (connector pins) are labels by nature.
        result.labelled_nets.append(name)
        for pt in pts:
            result.generated_routing.append(
                doc.add_label(name, pt, scope="global").value("uuid"))

    result.schematic = doc
    step("Done")
    return result
