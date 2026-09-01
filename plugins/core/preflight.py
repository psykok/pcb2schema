"""Check that the board is properly tagged before generating anything.

A conversion is only as good as the identifiers it starts from. If footprints are
still ``REF**`` and nets are unnamed, the generated schematic invents both, and those
invented names then get written back onto the board -- so a silent auto-tag quietly
takes ownership of naming decisions that belong to the user.

So tagging is an explicit gate. :func:`inspect` reports what is untagged; the caller
decides between:

* ``"require"`` -- stop and let the user tag the board themselves;
* ``"auto"``    -- auto-increment the untagged ones here.

Auto-tagging never renames anything that already has a name. It only fills gaps, and
it picks numbers that are not already in use, so hand-assigned identifiers survive
untouched and never collide.
"""

import re

__all__ = ["TagIssues", "inspect", "is_placeholder_reference", "is_placeholder_net",
           "is_auto_net_name", "next_net_tag", "REQUIRE", "AUTO"]

REQUIRE = "require"
AUTO = "auto"

# Names generated rather than chosen: KiCad's stand-ins for unlabelled nets, and our
# own auto-tags. These identify a net perfectly well, so they are *not* a blocker --
# they just aren't worth carrying into the schematic as labels, because KiCad will
# regenerate equivalents from the wiring anyway.
_AUTO_NET = re.compile(r"^(N\$\d+|Net-\(.*\)|unconnected-\(.*\))$")
_REFERENCE = re.compile(r"^([A-Za-z_]+[A-Za-z_0-9]*?)(\d+)$")


def is_placeholder_reference(ref):
    """True for ``REF**``, blanks, and anything else lacking a real number."""
    ref = (ref or "").strip()
    if not ref or ref.endswith("**") or "?" in ref:
        return True
    return _REFERENCE.match(ref) is None


def is_auto_net_name(name):
    """True for names KiCad or this plugin generated, rather than a person."""
    return bool(_AUTO_NET.match((name or "").strip()))


def is_placeholder_net(name):
    """True only when a net has no name at all.

    Deliberately narrower than :func:`is_auto_net_name`. Any board that has been
    through KiCad's netlist import has ``Net-(C1-Pad1)`` on every unlabelled net --
    on a real design that is most of them. Treating those as untagged would block
    essentially every board that isn't hand-named end to end, which is not a useful
    gate. They are identified; they are just not *named*.
    """
    return not (name or "").strip()


def _pad_label(pad_ref):
    """Identify a pad for a human.

    Untagged nets are usually reported on a board whose references are also still
    ``REF**``, so naming pads by reference would print "REF**.1, REF**.1" and tell
    the user nothing about which net to go and name. Fall back to the footprint and
    its board position instead.
    """
    ref = pad_ref.fp.GetReference()
    if not is_placeholder_reference(ref):
        return "%s.%s" % (ref, pad_ref.number)
    name = pad_ref.fp.GetFPIDAsString().split(":")[-1]
    return "%s.%s @ (%.1f, %.1f)" % (
        name[:28], pad_ref.number,
        pad_ref.pos[0] / 1e6, pad_ref.pos[1] / 1e6,
    )


class TagIssues(object):
    """What is missing an identifier, in terms the user can act on."""

    def __init__(self):
        self.untagged_components = []   # FootprintInfo
        self.duplicate_components = {}  # reference -> [FootprintInfo]
        self.untagged_nets = []         # Net objects with no usable name

    @property
    def ok(self):
        return not (self.untagged_components or self.duplicate_components
                    or self.untagged_nets)

    def counts(self):
        return (len(self.untagged_components), len(self.duplicate_components),
                len(self.untagged_nets))

    def describe(self, limit=10):
        lines = []
        if self.untagged_components:
            lines.append("%d footprint(s) have no reference designator:"
                         % len(self.untagged_components))
            for info in self.untagged_components[:limit]:
                lines.append("    %s  (currently %r)"
                             % (info.lib_id, info.reference or ""))
            if len(self.untagged_components) > limit:
                lines.append("    ... and %d more"
                             % (len(self.untagged_components) - limit))
        if self.duplicate_components:
            lines.append("%d reference(s) used more than once:"
                         % len(self.duplicate_components))
            for ref, infos in sorted(self.duplicate_components.items())[:limit]:
                lines.append("    %s  (%d footprints)" % (ref, len(infos)))
        if self.untagged_nets:
            lines.append("%d net(s) have no name:" % len(self.untagged_nets))
            for net in self.untagged_nets[:limit]:
                members = ", ".join(_pad_label(p) for p in net.pads[:4])
                if len(net.pads) > 4:
                    members += ", ..."
                lines.append("    %s" % members)
            if len(self.untagged_nets) > limit:
                lines.append("    ... and %d more" % (len(self.untagged_nets) - limit))
        return "\n".join(lines)


def inspect(footprints, nets):
    """Find untagged and duplicated identifiers on the board."""
    issues = TagIssues()

    seen = {}
    for info in footprints:
        if not info.pads:
            continue  # mechanical-only; nothing to represent, nothing to tag
        if is_placeholder_reference(info.reference):
            issues.untagged_components.append(info)
        else:
            seen.setdefault(info.reference, []).append(info)

    for ref, infos in seen.items():
        if len(infos) > 1:
            issues.duplicate_components[ref] = infos

    for net in nets:
        if is_placeholder_net(net.name):
            issues.untagged_nets.append(net)

    return issues


def next_net_tag(existing):
    """Generator of unused numeric net tags, continuing from the highest in use.

    The board's own convention wins: if nets are already called 1, 2, 3 the next one
    is 4, not ``N$1``. Names that are not plain integers are still respected as taken,
    they just do not influence where counting starts.
    """
    used = set(existing)
    highest = 0
    for name in existing:
        try:
            highest = max(highest, int(str(name).strip()))
        except (TypeError, ValueError):
            continue

    counter = highest + 1
    while True:
        tag = str(counter)
        if tag not in used:
            used.add(tag)
            yield tag
        counter += 1


def auto_tag_nets(nets):
    """Name every unnamed net by auto-increment. Returns the names assigned.

    Nets that already have a name are left exactly as they are.
    """
    named = [n.name for n in nets if not is_placeholder_net(n.name)]
    tags = next_net_tag(named)
    assigned = []
    # Deterministic order: extract_nets already sorts by board position, so repeated
    # runs hand out the same tags.
    for net in nets:
        if is_placeholder_net(net.name):
            net.name = next(tags)
            net.named_from_board = False
            assigned.append(net.name)
    return assigned
