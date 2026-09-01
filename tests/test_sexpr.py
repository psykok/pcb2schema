"""S-expression round-trip fidelity.

Incremental sync reads a schematic the user has edited and writes it back with only
our own content changed. That is only safe if parse->serialise is the identity, so
this is checked against every KiCad file on the system rather than a sample.
"""

import glob
import os

from core import sexpr

SHARED = "/Applications/KiCad/KiCad.app/Contents/SharedSupport"


def _corpus():
    files = []
    files += glob.glob(SHARED + "/template/*/*.kicad_sch")
    files += glob.glob(SHARED + "/template/*/*.kicad_pcb")
    files += glob.glob(SHARED + "/symbols/*.kicad_sym")
    return sorted(files)


def test_roundtrip_is_byte_identical():
    failures = []
    for path in _corpus():
        original = open(path, encoding="utf-8").read()
        if sexpr.dumps(sexpr.parse(original)) != original:
            failures.append(os.path.basename(path))
    assert not failures, "%d files did not round-trip: %s" % (
        len(failures),
        ", ".join(failures[:8]),
    )


def test_quoted_and_bare_atoms_stay_distinct():
    """``(pin "1")`` and ``(unit 1)`` must not collapse into the same thing."""
    node = sexpr.parse('(root (pin "1") (unit 1) (flag yes))')
    assert isinstance(node.node("pin").atoms()[0], str)
    assert not isinstance(node.node("pin").atoms()[0], sexpr.Sym)
    assert isinstance(node.node("unit").atoms()[0], sexpr.Sym)
    assert sexpr.dumps(node, trailing_newline=False) == (
        '(root\n\t(pin "1")\n\t(unit 1)\n\t(flag yes)\n)'
    )


def test_numbers_keep_their_original_text():
    """Reformatting 1.270 -> 1.27 would churn diffs on files we only partly touch."""
    text = "(a\n\t(b 1.270 -0.0 3)\n)\n"
    assert sexpr.dumps(sexpr.parse(text)) == text


def test_escapes_survive():
    node = sexpr.parse(r'(a (b "he said \"hi\"") (c "back\\slash"))')
    assert node.node("b").atoms()[0] == 'he said "hi"'
    assert node.node("c").atoms()[0] == "back\\slash"
    assert sexpr.dumps(sexpr.parse(sexpr.dumps(node))) == sexpr.dumps(node)


def test_navigation_helpers():
    node = sexpr.parse('(sym (property "Reference" "R1") (property "Value" "10k"))')
    props = list(node.nodes("property"))
    assert len(props) == 2
    assert props[1].atoms() == ["Value", "10k"]
    assert node.value("property", 1) == "R1"
    assert node.node("missing") is None
    assert node.value("missing", 0, "fallback") == "fallback"


def test_num_formatting():
    assert str(sexpr.num(1.27)) == "1.27"
    assert str(sexpr.num(1.0)) == "1"
    assert str(sexpr.num(-0.0)) == "0"
    assert str(sexpr.num(3)) == "3"
    assert str(sexpr.num(0.1 + 0.2)) == "0.3"


def test_malformed_input_raises():
    for bad in ["(a", "a)", "", "(a) (b)", '(a "unterminated']:
        try:
            sexpr.parse(bad)
        except sexpr.SexprError:
            continue
        raise AssertionError("expected SexprError for %r" % bad)
