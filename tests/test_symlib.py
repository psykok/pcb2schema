"""Symbol library discovery, scanning and caching."""

import os
import tempfile

from core import symlib

SYMBOLS = "/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols"


def _index():
    if not hasattr(_index, "cached"):
        _index.cached = symlib.SymbolIndex.build(
            cache_path=os.path.join(tempfile.gettempdir(), "p2s-test-index.json")
        )
    return _index.cached


def test_library_tables_resolve():
    """The stock table is reached through a nested ``type "Table"`` indirection."""
    libs = symlib.discover_libraries()
    assert len(libs) > 100, "expected the stock libraries, got %d" % len(libs)
    names = dict(libs)
    assert "Device" in names
    assert os.path.isfile(names["Device"])


def test_env_vars_derived_from_share_tree():
    """${KICAD*_SYMBOL_DIR} is absent from the environment and must be reconstructed."""
    env = symlib.kicad_env(os.path.dirname(SYMBOLS))
    assert env.get("KICAD10_SYMBOL_DIR") == SYMBOLS


def test_shallow_scan_matches_known_symbols():
    idx = _index()
    assert len(idx) > 20000, "expected a full index, got %d symbols" % len(idx)

    r = idx.get("Device:R")
    assert r.reference == "R"
    assert r.pins == ["1", "2"]
    assert r.fp_filters == ["R_*"]
    assert r.units == 1

    led = idx.get("Device:LED")
    assert led.reference == "D"
    assert "LED*" in led.fp_filters

    # Multi-unit: two amplifiers plus a power unit.
    lm358 = idx.get("Amplifier_Operational:LM358")
    assert lm358.units == 3
    assert len(lm358.pins) == 8


def test_derived_symbols_inherit_from_base():
    """``(extends ...)`` symbols carry no pins of their own."""
    idx = _index()
    derived = [e for e in idx.entries if e.extends and e.lib == "Device"]
    assert derived, "expected extended symbols in Device"
    assert all(e.pins for e in derived), "extended symbols did not inherit pins"


def test_shallow_scan_agrees_with_full_parse():
    """The fast path must produce the same answer as the real parser."""
    path = os.path.join(SYMBOLS, "Device.kicad_sym")
    fast = {e.name: e for e in symlib.scan_library(path, "Device")}
    slow = {e.name: e for e in symlib._scan_library_fallback(path, "Device")}
    slow = {e.name: e for e in symlib._resolve_extends(list(slow.values()))}
    assert set(fast) == set(slow), "symbol sets differ"
    for name, a in fast.items():
        b = slow[name]
        assert a.pins == b.pins, "%s: pins %s vs %s" % (name, a.pins, b.pins)
        assert a.reference == b.reference, name
        assert a.fp_filters == b.fp_filters, name
        assert a.units == b.units, "%s: units %d vs %d" % (name, a.units, b.units)


def test_cache_roundtrip_is_faithful():
    with tempfile.TemporaryDirectory() as tmp:
        cache = os.path.join(tmp, "index.json")
        cold = symlib.SymbolIndex.build(cache_path=cache)
        assert os.path.isfile(cache)
        warm = symlib.SymbolIndex.build(cache_path=cache)
        assert len(cold) == len(warm)
        a, b = cold.get("Device:R"), warm.get("Device:R")
        assert (a.pins, a.reference, a.fp_filters, a.units) == (
            b.pins, b.reference, b.fp_filters, b.units
        )


def test_pin_count_buckets_cover_every_symbol():
    idx = _index()
    assert sum(len(v) for v in idx.by_pin_count.values()) == len(idx)
    two_pad = idx.candidates_for_pad_count(2)
    assert all(len(e.pins) == 0 or 2 <= len(e.pins) <= 10 for e in two_pad)
