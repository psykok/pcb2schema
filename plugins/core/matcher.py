"""Rank library symbols against a footprint.

Deciding which symbol a footprint represents is the one genuinely ambiguous step in
the whole conversion, so this module produces a *ranked, explained* list rather than
a single answer. Confident matches are applied silently; anything else is handed to
the user with the reasons attached so the choice can be made in one glance.

Signals, strongest first:

1. the footprint's **Value** naming the symbol. For an IC this is the part number --
   ``NE555``, ``74LS32``, ``L7805`` -- and is by far the most reliable evidence there
   is. A DIP-14 footprint filter matches hundreds of symbols; only one is ``74LS32``;
2. a curated hint table for the everyday passives and connectors;
3. the symbol's ``ki_fp_filters`` globs matching the footprint name;
4. the symbol's default ``Footprint`` property naming this exact footprint -- weaker
   than it sounds, because obscure parts routinely claim generic footprints;
5. token overlap across names, keywords and descriptions.

Pin compatibility is a *gate* rather than a signal: a symbol whose pin numbers cannot
be reconciled with the footprint's pad numbers is wrong however well it scores
elsewhere, and silently accepting one produces a schematic that looks right and is
electrically meaningless.

This module deliberately does not import ``pcbnew`` -- it works on plain data so it
can be tested without a board.
"""

import fnmatch
import re

__all__ = ["FootprintInfo", "Candidate", "rank", "is_confident",
           "check_value_conflict", "AUTO_ACCEPT_SCORE", "AUTO_ACCEPT_LEAD"]


# A candidate must both clear this score and beat the runner-up by this margin
# before it is applied without asking.
AUTO_ACCEPT_SCORE = 600.0
AUTO_ACCEPT_LEAD = 150.0

_SCORE_VALUE_EXACT = 900.0
_SCORE_VALUE_NORMALISED = 780.0
# "NE555" against the symbol "NE555P" is the same part in a package variant, so this
# has to stay well clear of _SCORE_FOOTPRINT_PROPERTY. It previously sat below it and
# NE555 lost to an unrelated op-amp that merely declared the same DIP-8 footprint.
_SCORE_VALUE_PREFIX = 600.0
# A *word* of the value naming the symbol outright: "LED_RED" -> Device:LED. Weaker
# than a whole-value match, since the rest of the word could mean anything, but far
# better evidence than generic token overlap.
_SCORE_VALUE_TOKEN = 200.0

# A symbol naming this exact footprint as its default is real evidence, but it is far
# weaker than it looks on a *generic* footprint: plenty of obscure parts declare
# DIP-8_W7.62mm or LED_D5.0mm, and whichever is found first would otherwise outrank
# both the part number in Value and the curated mappings. It should support a match,
# not decide one on its own.
_SCORE_FOOTPRINT_PROPERTY = 380.0
_SCORE_HINT = 450.0
_SCORE_FP_FILTER = 350.0
_SCORE_PINS_EXACT = 220.0
_SCORE_PINS_SUPERSET = 90.0
_SCORE_PINS_POSITIONAL = 60.0
_SCORE_TOKEN = 26.0
_SCORE_NAME_EQ = 120.0


class FootprintInfo(object):
    """The parts of a PCB footprint that matter for symbol selection."""

    __slots__ = ("lib", "name", "pads", "value", "uuid", "pos", "rotation", "is_smd", "reference")

    def __init__(self, lib, name, pads, value="", uuid="", pos=(0, 0),
                 rotation=0.0, is_smd=False, reference=""):
        self.lib = lib
        self.name = name
        self.pads = list(pads)
        self.value = value
        self.uuid = uuid
        self.pos = pos
        self.rotation = rotation
        self.is_smd = is_smd
        self.reference = reference

    @property
    def lib_id(self):
        return "%s:%s" % (self.lib, self.name)

    def __repr__(self):
        return "FootprintInfo(%s, %d pads)" % (self.lib_id, len(self.pads))


def from_footprint(fp):
    """Build a :class:`FootprintInfo` from a pcbnew ``FOOTPRINT``."""
    import pcbnew

    fpid = fp.GetFPIDAsString()
    lib, _, name = fpid.partition(":")
    if not name:
        lib, name = "", fpid

    pads = sorted(set(p.GetNumber() for p in fp.Pads() if p.GetNumber()), key=_pad_sort_key)
    pos = fp.GetPosition()
    smd = any(p.GetAttribute() == pcbnew.PAD_ATTRIB_SMD for p in fp.Pads())
    return FootprintInfo(
        lib=lib,
        name=name,
        pads=pads,
        value=fp.GetValue(),
        uuid=fp.m_Uuid.AsString(),
        pos=(pos.x, pos.y),
        rotation=fp.GetOrientationDegrees(),
        is_smd=smd,
        reference=fp.GetReference(),
    )


def _pad_sort_key(n):
    """Sort pad/pin labels naturally: 1, 2, 10 -- and A1 after 9."""
    m = re.match(r"^(\d+)$", n)
    return (0, int(m.group(1)), "") if m else (1, 0, n)


class Candidate(object):
    __slots__ = ("entry", "score", "reasons", "pin_map")

    def __init__(self, entry, score, reasons, pin_map):
        self.entry = entry
        self.score = score
        self.reasons = reasons
        # pad number -> symbol pin number. Identity for the overwhelmingly common
        # case; populated when a positional fallback was needed.
        self.pin_map = pin_map

    @property
    def lib_id(self):
        return self.entry.lib_id

    @property
    def is_positional(self):
        return any(k != v for k, v in self.pin_map.items())

    def __repr__(self):
        return "Candidate(%s, %.0f)" % (self.lib_id, self.score)


# ---------------------------------------------------------------------------
# Hints for the parts that appear on nearly every board
# ---------------------------------------------------------------------------

def _conn_hint(prefix):
    def hint(fp, npads):
        return ["Connector_Generic:%s_%02dx%02d" % (prefix, 1, npads)]
    return hint


_LIB_HINTS = (
    (r"^Resistor_(THT|SMD)$", lambda fp, n: ["Device:R"]),
    # A "CP_" footprint is a polarised part; offering the plain ceramic symbol first
    # gets the polarity silently wrong.
    (r"^Capacitor_(THT|SMD)$", lambda fp, n: (
        ["Device:C_Polarized", "Device:CP"]
        if fp.name.startswith("CP") or "Polarized" in fp.name
        else ["Device:C"])),
    (r"^Inductor_(THT|SMD)$", lambda fp, n: ["Device:L"]),
    (r"^LED_(THT|SMD)$", lambda fp, n: ["Device:LED"]),
    (r"^Diode_(THT|SMD)$", lambda fp, n: ["Device:D"]),
    (r"^Crystal$", lambda fp, n: ["Device:Crystal", "Device:Crystal_GND24"]),
    (r"^Fuse$", lambda fp, n: ["Device:Fuse"]),
    (r"^Potentiometer.*$", lambda fp, n: ["Device:R_Potentiometer"]),
    (r"^Varistor$", lambda fp, n: ["Device:Varistor"]),
    (r"^Button_Switch_(THT|SMD)$", lambda fp, n: ["Switch:SW_Push"]),
    (r"^MountingHole$", lambda fp, n: ["Mechanical:MountingHole"]),
    (r"^TerminalBlock", lambda fp, n: ["Connector:Screw_Terminal_01x%02d" % n]),
)

_NAME_HINTS = (
    (r"^PinHeader_1x(\d+)", lambda fp, n: ["Connector_Generic:Conn_01x%02d" % n]),
    (r"^PinSocket_1x(\d+)", lambda fp, n: ["Connector_Generic:Conn_01x%02d" % n]),
    (r"^PinHeader_2x(\d+)", lambda fp, n: [
        "Connector_Generic:Conn_02x%02d_Odd_Even" % (n // 2)]),
    (r"^PinSocket_2x(\d+)", lambda fp, n: [
        "Connector_Generic:Conn_02x%02d_Odd_Even" % (n // 2)]),
)


def _hinted_ids(info):
    n = len(info.pads)
    out = []
    for pattern, fn in _LIB_HINTS:
        if re.match(pattern, info.lib):
            out.extend(fn(info, n))
    for pattern, fn in _NAME_HINTS:
        if re.match(pattern, info.name):
            out.extend(fn(info, n))
    return out


# ---------------------------------------------------------------------------
# Pin gate
# ---------------------------------------------------------------------------

def _pin_fit(entry, info, pad_set, identity_map):
    """Score how well a symbol's pins fit the footprint's pads.

    Returns ``(score, reason, pin_map)`` or ``None`` when they cannot be reconciled.
    *pad_set* and *identity_map* are supplied by the caller because this runs against
    every symbol in the pool and rebuilding them here dominated the cost.
    """
    if not entry.pins:
        # Graphical or power symbols carry no pins we can check against.
        return (0.0, "symbol declares no pins", {})

    pin_set = entry.pin_set

    if pad_set == pin_set:
        return (_SCORE_PINS_EXACT, "pin numbers match pads exactly", identity_map)

    if pad_set and pad_set <= pin_set:
        extra = len(pin_set - pad_set)
        return (
            _SCORE_PINS_SUPERSET - min(extra, 8) * 5.0,
            "symbol has %d pin(s) not on the footprint" % extra,
            identity_map,
        )

    # Same count, different labels: symbols like Device:D_Zener use A/K where the
    # footprint uses 1/2. Map positionally, but score it low -- it is a guess, and
    # the picker surfaces it so the user can check polarity.
    if len(pad_set) == len(pin_set):
        ordered_pads = sorted(pad_set, key=_pad_sort_key)
        ordered_pins = sorted(pin_set, key=_pad_sort_key)
        return (
            _SCORE_PINS_POSITIONAL,
            "pin names differ from pad numbers; mapped in order",
            dict(zip(ordered_pads, ordered_pins)),
        )

    return None


# ---------------------------------------------------------------------------
# Token overlap
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_STOPWORDS = frozenset("""
the a an and or of for with without to from in on at by is are be
smd tht mm package generic single row series type style pitch horizontal vertical
""".split())


def _tokens(*parts):
    out = set()
    for part in parts:
        if not part:
            continue
        for tok in _TOKEN_RE.findall(part):
            tok = tok.lower()
            if len(tok) > 1 and tok not in _STOPWORDS:
                out.add(tok)
    return out


def _normalise_part(text):
    """Reduce a part number to its comparable core: ``74LS00`` == ``74ls00``."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _value_match(entry, info):
    """Score the footprint's Value against a symbol name.

    For an IC the Value field almost always *is* the part number -- ``NE555``,
    ``74LS32``, ``L7805`` -- which makes it the single most reliable signal available,
    stronger even than footprint filters: a DIP-14 filter matches hundreds of symbols,
    but only one is called ``74LS32``.

    Harmless for passives, where Value is ``10k`` or ``100n`` and simply matches
    nothing. It does the right thing for ``LED`` and ``Conn_01x02`` too, which are
    symbol names in their own right.

    Returns ``(score, reason)`` or ``None``.
    """
    value = (info.value or "").strip()
    if not value or value == info.name or value == info.lib_id:
        return None

    name = entry.name
    if value.lower() == name.lower():
        return (_SCORE_VALUE_EXACT, "value %r is this symbol" % value)

    norm_value, norm_name = _normalise_part(value), _normalise_part(name)
    if norm_value and norm_value == norm_name:
        return (_SCORE_VALUE_NORMALISED, "value %r matches this symbol" % value)

    # "L7805" against "L7805ABD2T-TR": the same part in a specific package.
    if len(norm_value) >= 4 and norm_name.startswith(norm_value):
        return (_SCORE_VALUE_PREFIX, "symbol is a variant of %r" % value)

    # "LED_RED" -> the symbol actually called "LED".
    if len(name) > 1 and name.lower() in _tokens(value):
        return (_SCORE_VALUE_TOKEN, "value %r names this symbol" % value)

    return None


def check_value_conflict(info, index):
    """Warn when Value names a real symbol that the pads rule out.

    If a part is labelled ``SW_Push_SPDT`` and ``Switch:SW_Push_SPDT`` exists but has
    three pins against the footprint's four pads, something is genuinely wrong -- the
    wrong footprint, or the wrong value. Quietly ranking a different four-pin switch
    to the top would bury that. Returns a message, or ``None``.
    """
    value = (info.value or "").strip()
    if not value or value == info.name or value == info.lib_id:
        return None

    target = _normalise_part(value)
    if not target:
        return None

    for entry in index.entries:
        if entry.is_power or _normalise_part(entry.name) != target:
            continue
        if entry.pin_set is None:
            entry.pin_set = frozenset(entry.pins)
        if _pin_fit(entry, info, frozenset(info.pads),
                    {p: p for p in info.pads}) is not None:
            return None  # it matched and is in the running; nothing to report
        return (
            "%s is labelled %r and %s exists, but it has %d pin(s) against %d pad(s) "
            "-- check the footprint or the value."
            % (info.reference or info.lib_id, value, entry.lib_id,
               len(entry.pins), len(info.pads))
        )
    return None


def _fp_filter_match(entry, info):
    """Does any ``ki_fp_filters`` glob match this footprint?"""
    for pattern in entry.fp_filters:
        if ":" in pattern:
            if fnmatch.fnmatchcase(info.lib_id, pattern):
                return pattern
        elif fnmatch.fnmatchcase(info.name, pattern):
            return pattern
    return None


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

def rank(info, index, limit=30):
    """Rank symbols in *index* against footprint *info*, best first."""
    hinted = {}
    for order, lib_id in enumerate(_hinted_ids(info)):
        hinted.setdefault(lib_id, order)

    fp_tokens = _tokens(info.lib, info.name, info.value)
    fp_name_lower = info.name.lower()
    pad_set = frozenset(info.pads)
    identity_map = {p: p for p in info.pads}
    results = []

    pool = index.candidates_for_pad_count(len(info.pads))
    for entry in pool:
        if entry.is_power:
            continue

        if entry.pin_set is None:
            entry.pin_set = frozenset(entry.pins)

        fit = _pin_fit(entry, info, pad_set, identity_map)
        if fit is None:
            continue
        score, pin_reason, pin_map = fit
        reasons = [pin_reason] if pin_reason else []

        value_hit = _value_match(entry, info)
        if value_hit:
            score += value_hit[0]
            reasons.insert(0, value_hit[1])

        if entry.footprint and entry.footprint == info.lib_id:
            # Divided by how many symbols claim this footprint: exclusive claims are
            # strong evidence, shared ones are nearly worthless.
            claims = max(index.claim_count(info.lib_id), 1)
            score += _SCORE_FOOTPRINT_PROPERTY / float(min(claims, 8))
            reasons.insert(0, "symbol's default footprint is this footprint"
                           + ("" if claims == 1 else " (shared with %d others)" % (claims - 1)))

        if entry.lib_id in hinted:
            score += _SCORE_HINT - hinted[entry.lib_id] * 20.0
            reasons.insert(0, "known mapping for %s footprints" % info.lib)

        pattern = _fp_filter_match(entry, info)
        if pattern:
            score += _SCORE_FP_FILTER
            reasons.append("footprint filter %r matches" % pattern)

        if entry.name.lower() == fp_name_lower:
            score += _SCORE_NAME_EQ
            reasons.append("symbol and footprint share a name")

        # Tokenising 22k descriptions per footprint dominated the runtime; cache it
        # on the entry so a large board pays for it once rather than once per part.
        if entry.tokens is None:
            entry.tokens = _tokens(entry.name, entry.keywords, entry.description)
        overlap = fp_tokens & entry.tokens
        if overlap:
            score += min(len(overlap), 5) * _SCORE_TOKEN
            reasons.append("shared terms: %s" % ", ".join(sorted(overlap)[:4]))

        if score <= _SCORE_PINS_EXACT:
            continue  # pins alone are not evidence; nearly every 2-pin part fits

        results.append(Candidate(entry, score, reasons, pin_map))

    results.sort(key=lambda c: (-c.score, c.lib_id))
    return results[:limit]


def is_confident(candidates):
    """True when the top candidate is safe to apply without asking the user."""
    if not candidates:
        return False
    top = candidates[0]
    if top.score < AUTO_ACCEPT_SCORE or top.is_positional:
        return False
    runner_up = candidates[1].score if len(candidates) > 1 else 0.0
    return (top.score - runner_up) >= AUTO_ACCEPT_LEAD
