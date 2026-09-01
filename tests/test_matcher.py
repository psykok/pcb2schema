"""Footprint -> symbol ranking."""

import os
import tempfile

from core import matcher, symlib


def _index():
    if not hasattr(_index, "cached"):
        _index.cached = symlib.SymbolIndex.build(
            cache_path=os.path.join(tempfile.gettempdir(), "p2s-test-index.json")
        )
    return _index.cached


def _info(lib, name, pads, value=""):
    return matcher.FootprintInfo(lib=lib, name=name, pads=pads, value=value)


# (footprint library, footprint name, pads, expected symbol)
EXPECTED = [
    ("Resistor_THT", "R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal", ["1", "2"], "Device:R"),
    ("Resistor_SMD", "R_0805_2012Metric", ["1", "2"], "Device:R"),
    ("Capacitor_SMD", "C_0603_1608Metric", ["1", "2"], "Device:C"),
    ("Inductor_SMD", "L_0805_2012Metric", ["1", "2"], "Device:L"),
    ("LED_THT", "LED_D3.0mm", ["1", "2"], "Device:LED"),
    ("LED_SMD", "LED_0805_2012Metric", ["1", "2"], "Device:LED"),
    ("Crystal", "Crystal_HC49-4H_Vertical", ["1", "2"], "Device:Crystal"),
    ("Fuse", "Fuse_1206_3216Metric", ["1", "2"], "Device:Fuse"),
    ("Button_Switch_THT", "SW_PUSH_6mm", ["1", "2"], "Switch:SW_Push"),
    ("TerminalBlock_Phoenix",
     "TerminalBlock_Phoenix_MPT-0,5-2-2.54_1x02_P2.54mm_Horizontal",
     ["1", "2"], "Connector:Screw_Terminal_01x02"),
    ("Connector_PinHeader_2.54mm", "PinHeader_1x04_P2.54mm_Vertical",
     ["1", "2", "3", "4"], "Connector_Generic:Conn_01x04"),
    ("Connector_PinHeader_2.54mm", "PinHeader_2x05_P2.54mm_Vertical",
     [str(i) for i in range(1, 11)], "Connector_Generic:Conn_02x05_Odd_Even"),
]


def test_everyday_parts_match_and_are_confident():
    idx = _index()
    misses = []
    unsure = []
    for lib, name, pads, expected in EXPECTED:
        cands = matcher.rank(_info(lib, name, pads), idx)
        top = cands[0].lib_id if cands else "(none)"
        if top != expected:
            misses.append("%s -> %s (expected %s)" % (name, top, expected))
        elif not matcher.is_confident(cands):
            unsure.append(name)
    assert not misses, "wrong top match:\n  " + "\n  ".join(misses)
    assert not unsure, "should not need to ask the user about: %s" % ", ".join(unsure)


def test_pin_count_gate_rejects_implausible_symbols():
    """A 2-pad footprint must never be offered a 100-pin part."""
    idx = _index()
    cands = matcher.rank(_info("Resistor_SMD", "R_0805_2012Metric", ["1", "2"]), idx)
    assert cands
    for c in cands:
        assert len(c.entry.pins) <= 10, "%s has %d pins" % (c.lib_id, len(c.entry.pins))


def test_every_candidate_can_map_all_pads():
    """A candidate that cannot account for every pad would silently lose connections."""
    idx = _index()
    for lib, name, pads, _ in EXPECTED:
        for c in matcher.rank(_info(lib, name, pads), idx, limit=10):
            assert set(c.pin_map) == set(pads), "%s drops pads" % c.lib_id
            assert set(c.pin_map.values()) <= set(c.entry.pins) or not c.entry.pins


def test_positional_mapping_is_never_auto_accepted():
    """Mapping A/K onto 1/2 is a guess about polarity -- always confirm it."""
    idx = _index()
    positional = []
    for lib, name, pads, _ in EXPECTED:
        positional += [c for c in matcher.rank(_info(lib, name, pads), idx) if c.is_positional]
    for c in positional:
        assert not matcher.is_confident([c]), "%s auto-accepted a positional map" % c.lib_id


def test_power_symbols_are_never_offered():
    idx = _index()
    for lib, name, pads, _ in EXPECTED:
        for c in matcher.rank(_info(lib, name, pads), idx):
            assert not c.entry.is_power, "%s is a power symbol" % c.lib_id


def test_unknown_footprint_is_not_confident():
    """Something we have no evidence about must fall through to the user."""
    idx = _index()
    cands = matcher.rank(_info("Weird_Custom_Lib", "ZZ_Mystery_Part_QQ", ["1", "2", "3"]), idx)
    assert not matcher.is_confident(cands)


def test_ranking_is_deterministic():
    idx = _index()
    info = _info("Resistor_SMD", "R_0805_2012Metric", ["1", "2"])
    a = [c.lib_id for c in matcher.rank(info, idx)]
    b = [c.lib_id for c in matcher.rank(info, idx)]
    assert a == b


def test_reasons_are_populated():
    """The picker shows these; an unexplained candidate is not actionable."""
    idx = _index()
    for c in matcher.rank(_info("Resistor_SMD", "R_0805_2012Metric", ["1", "2"]), idx, limit=5):
        assert c.reasons, "%s has no explanation" % c.lib_id
