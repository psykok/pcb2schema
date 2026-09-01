"""Lossless S-expression parsing and serialisation for KiCad files.

KiCad's ``.kicad_sch`` / ``.kicad_pcb`` / ``.kicad_sym`` files are all S-expressions.
Incremental sync depends on being able to read a file the user has edited, change
only the parts we own, and write it back without perturbing anything else -- so this
layer is deliberately lossless:

* quoted strings and bare tokens are distinct types (:class:`str` vs :class:`Sym`),
  so ``"1"`` never round-trips into ``1``;
* numbers keep their original text, so ``1.270`` is not silently reformatted;
* child order is preserved exactly.

Output follows KiCad's own conventions (tab indent, one child list per line) so that
files we touch stay diff-friendly against files KiCad wrote.
"""

__all__ = ["Sym", "Node", "parse", "dumps", "load", "save", "num", "SexprError"]


class SexprError(ValueError):
    """Raised when a document cannot be parsed."""


class Sym(str):
    """A bare, unquoted token: a keyword (``yes``), a symbol (``solid``) or a number.

    Distinguishing this from :class:`str` is what makes round-tripping exact --
    ``(pin "1")`` and ``(unit 1)`` must serialise differently.
    """

    __slots__ = ()

    def __repr__(self):
        return "Sym(%s)" % str.__repr__(self)


class Node(object):
    """An S-expression list: a head token plus an ordered list of children.

    Children are :class:`Node`, :class:`Sym` or :class:`str`.
    """

    __slots__ = ("name", "children")

    def __init__(self, name, children=None):
        self.name = name
        self.children = list(children) if children else []

    # -- container protocol -------------------------------------------------

    def __iter__(self):
        return iter(self.children)

    def __len__(self):
        return len(self.children)

    def __getitem__(self, i):
        return self.children[i]

    def append(self, child):
        self.children.append(child)
        return child

    def extend(self, children):
        self.children.extend(children)

    # -- navigation ---------------------------------------------------------

    def nodes(self, name=None):
        """Yield child nodes, optionally filtered by head token."""
        for c in self.children:
            if isinstance(c, Node) and (name is None or c.name == name):
                yield c

    def node(self, name):
        """First child node with this head token, or ``None``."""
        for c in self.nodes(name):
            return c
        return None

    def atoms(self):
        """Children that are plain atoms rather than nested lists."""
        return [c for c in self.children if not isinstance(c, Node)]

    def value(self, name, index=0, default=None):
        """Atom *index* of the first child node named *name*."""
        n = self.node(name)
        if n is None:
            return default
        a = n.atoms()
        return a[index] if index < len(a) else default

    def remove(self, child):
        """Remove a child by identity. Returns True if it was present."""
        for i, c in enumerate(self.children):
            if c is child:
                del self.children[i]
                return True
        return False

    def replace(self, old, new):
        """Replace a child by identity, preserving its position."""
        for i, c in enumerate(self.children):
            if c is old:
                self.children[i] = new
                return True
        return False

    def __repr__(self):
        return "Node(%r, %d children)" % (self.name, len(self.children))


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_WHITESPACE = " \t\r\n"
_DELIM = _WHITESPACE + "()"

# KiCad writes these escapes inside quoted strings.
_UNESCAPE = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"'}


def parse(text):
    """Parse a document and return its single root :class:`Node`."""
    pos = 0
    end = len(text)
    # Tolerate a UTF-8 BOM; KiCad does not write one but editors sometimes add it.
    if text.startswith("﻿"):
        pos = 1

    root = None
    stack = []

    while pos < end:
        ch = text[pos]

        if ch in _WHITESPACE:
            pos += 1
            continue

        if ch == "(":
            pos += 1
            # The head token follows immediately.
            while pos < end and text[pos] in _WHITESPACE:
                pos += 1
            start = pos
            while pos < end and text[pos] not in _DELIM:
                pos += 1
            node = Node(text[start:pos])
            if stack:
                stack[-1].append(node)
            elif root is None:
                root = node
            else:
                raise SexprError("multiple root expressions at offset %d" % start)
            stack.append(node)
            continue

        if ch == ")":
            if not stack:
                raise SexprError("unbalanced ')' at offset %d" % pos)
            stack.pop()
            pos += 1
            continue

        if ch == '"':
            pos += 1
            buf = []
            while pos < end:
                c = text[pos]
                if c == "\\":
                    pos += 1
                    if pos >= end:
                        raise SexprError("unterminated escape at offset %d" % pos)
                    buf.append(_UNESCAPE.get(text[pos], text[pos]))
                    pos += 1
                elif c == '"':
                    pos += 1
                    break
                else:
                    buf.append(c)
                    pos += 1
            else:
                raise SexprError("unterminated string")
            atom = "".join(buf)
            if not stack:
                raise SexprError("atom outside any list")
            stack[-1].append(atom)
            continue

        # Bare token.
        start = pos
        while pos < end and text[pos] not in _DELIM:
            pos += 1
        if not stack:
            raise SexprError("atom outside any list at offset %d" % start)
        stack[-1].append(Sym(text[start:pos]))

    if stack:
        raise SexprError("unterminated list: %r" % stack[-1].name)
    if root is None:
        raise SexprError("empty document")
    return root


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

_ESCAPE = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}

# Nodes whose list children KiCad packs several-per-line rather than one per line.
_INLINE_CHILDREN = frozenset(["pts"])

# Nodes whose atom children KiCad packs several-per-line once they get long. Kept to
# an explicit set rather than applied to every atom-only list, because most short
# lists (``(size 1.7 1.7)``, ``(color 0 0 0 0)``) must stay on one line.
_WRAP_ATOMS = frozenset(["layers", "members"])

# Column at which each packed construct wraps, counting a tab as one character.
# KiCad's writers are not uniform here -- these were derived empirically by sweeping
# for the values that reproduce the reference corpus byte-for-byte.
#
# The two constructs also differ in *when* they break. Atom lists (``layers``,
# ``members``) never exceed their limit: a token that would overflow starts the next
# line. ``pts`` instead appends first and breaks once the line has reached the limit,
# so its lines routinely run past 99 columns. Getting this backwards reproduces most
# of the corpus but fails on dense polygons, which is how the distinction surfaced.
_WRAP_LIMITS = {"layers": 80, "members": 99, "pts": 99}
_WRAP_DEFAULT = 80


def _limit(name):
    return _WRAP_LIMITS.get(name, _WRAP_DEFAULT)


def _quote(s):
    out = [_ESCAPE.get(c, c) for c in s]
    return '"' + "".join(out) + '"'


def _atom(a):
    if isinstance(a, Sym):
        return str(a)
    if isinstance(a, str):
        return _quote(a)
    if isinstance(a, bool):
        return "yes" if a else "no"
    if isinstance(a, float):
        return num(a)
    return str(a)


def _write_wrapped(node, out, depth):
    """Emit an atom-only list, wrapping at :data:`_WRAP_COLUMN` as KiCad does.

    When the list fits on one line the closing paren stays inline; once it wraps,
    KiCad puts the closing paren on its own line at the parent's indent.
    """
    pad = "\t" * depth
    cont = "\t" * (depth + 1)
    limit = _limit(node.name)
    toks = [_atom(a) for a in node.children]

    single = "%s(%s%s)" % (pad, node.name, "".join(" " + t for t in toks))
    if len(single) <= limit:
        out.append(single)
        return

    line = "%s(%s" % (pad, node.name)
    for t in toks:
        if len(line) + 1 + len(t) > limit:
            out.append(line)
            line = cont + t
        else:
            line += " " + t
    out.append(line)
    out.append(pad + ")")


def _fill_tokens(toks, out, depth, limit):
    """Pack pre-rendered tokens, breaking once a line has reached *limit*.

    Note the ordering: the token is appended first and the line is flushed
    afterwards, so lines may end past the limit. This is what KiCad does for
    ``pts`` -- see the note on :data:`_WRAP_LIMITS`.
    """
    cont = "\t" * depth
    line = ""
    for t in toks:
        line = (cont + t) if not line else (line + " " + t)
        if len(line) >= limit:
            out.append(line)
            line = ""
    if line:
        out.append(line)


def _write(node, out, depth):
    pad = "\t" * depth
    head = ["%s(%s" % (pad, node.name)]

    child_nodes = [c for c in node.children if isinstance(c, Node)]

    # Leading atoms share the head's line, matching KiCad's layout.
    for c in node.children:
        if isinstance(c, Node):
            break
        head.append(" " + _atom(c))

    if not child_nodes:
        if node.name in _WRAP_ATOMS:
            _write_wrapped(node, out, depth)
            return
        head.append(")")
        out.append("".join(head))
        return

    out.append("".join(head))

    if node.name in _INLINE_CHILDREN:
        # e.g. (pts (xy 1 2) (xy 3 4)) -- children packed several per line.
        toks = [
            "(%s %s)" % (c.name, " ".join(_atom(a) for a in c.atoms()))
            for c in child_nodes
        ]
        _fill_tokens(toks, out, depth + 1, _limit(node.name))
    else:
        seen_node = False
        for c in node.children:
            if isinstance(c, Node):
                seen_node = True
                _write(c, out, depth + 1)
            elif seen_node:
                # An atom after a nested list is unusual but legal; keep it.
                out.append("\t" * (depth + 1) + _atom(c))

    out.append(pad + ")")


def dumps(node, trailing_newline=True):
    """Serialise a :class:`Node` tree to KiCad-style text."""
    out = []
    _write(node, out, 0)
    text = "\n".join(out)
    return text + "\n" if trailing_newline else text


def load(path):
    """Parse the file at *path*."""
    with open(path, "r", encoding="utf-8") as fh:
        return parse(fh.read())


def save(node, path):
    """Serialise *node* to *path* using LF line endings, as KiCad does."""
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(dumps(node))


# ---------------------------------------------------------------------------
# Helpers for building new content
# ---------------------------------------------------------------------------


def num(x, places=6):
    """Format a number the way KiCad does: fixed precision, trailing zeros trimmed.

    Returns a :class:`Sym` so it serialises unquoted.
    """
    if isinstance(x, int):
        return Sym(str(x))
    s = "%.*f" % (places, float(x))
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if s in ("-0", ""):
        s = "0"
    return Sym(s)
