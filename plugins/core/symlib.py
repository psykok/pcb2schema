"""Index of the available schematic symbols.

Matching a footprint to a symbol needs a searchable view of every symbol KiCad can
see. On this machine that is 224 libraries totalling ~220 MB, which is far too much
to fully parse on every run: the general S-expression parser handles roughly 0.7 MB/s,
so a full parse would take minutes.

Two things keep it fast:

* a **shallow scan** that pulls only the indexable fields out of each library using
  the canonical tab indentation KiCad writes, rather than building a document tree
  (:mod:`core.sexpr` is still used for the handful of symbols actually placed, where
  the complete definition is needed);
* a **JSON cache** keyed on each library's path, mtime and size, so the scan happens
  once and subsequent runs load in milliseconds.

The shallow scan trusts KiCad's formatting. If a library has been reformatted by hand
and the scan finds nothing, it falls back to the real parser for that file.
"""

import json
import os
import re
import sys

from . import sexpr

__all__ = ["SymbolEntry", "SymbolIndex", "kicad_env", "read_lib_table", "scan_library"]


# ---------------------------------------------------------------------------
# Library tables
# ---------------------------------------------------------------------------

_VAR_RE = re.compile(r"\$\{([A-Za-z0-9_]+)\}")


def _config_dir():
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Preferences/kicad")
    if os.name == "nt":
        return os.path.join(os.environ.get("APPDATA", ""), "kicad")
    return os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "kicad"
    )


def global_table_path(version=None):
    """Path to the user's global ``sym-lib-table``, newest version if unspecified."""
    root = _config_dir()
    if not os.path.isdir(root):
        return None
    if version:
        candidate = os.path.join(root, version, "sym-lib-table")
        return candidate if os.path.isfile(candidate) else None
    versions = sorted(
        (d for d in os.listdir(root) if re.match(r"^\d+\.\d+$", d)),
        key=lambda d: [int(p) for p in d.split(".")],
        reverse=True,
    )
    for v in versions:
        candidate = os.path.join(root, v, "sym-lib-table")
        if os.path.isfile(candidate):
            return candidate
    return None


def kicad_env(share_hint=None):
    """Environment for ``${...}`` expansion in library tables.

    KiCad defines ``KICAD*_SYMBOL_DIR`` and friends internally rather than in
    ``kicad_common.json``, so they are absent from ``os.environ`` when we run under
    the bundled interpreter. They are reconstructed from the location of the stock
    library table, which lives inside the same share tree the libraries do -- that
    keeps this correct across platforms and KiCad versions without hard-coded paths.
    """
    env = {k: v for k, v in os.environ.items() if "KICAD" in k}
    if share_hint:
        for var, sub in (
            ("SYMBOL_DIR", "symbols"),
            ("FOOTPRINT_DIR", "footprints"),
            ("3DMODEL_DIR", "3dmodels"),
            ("TEMPLATE_DIR", "template"),
        ):
            path = os.path.join(share_hint, sub)
            if not os.path.isdir(path):
                continue
            for prefix in ("KICAD10", "KICAD9", "KICAD8", "KICAD7", "KICAD"):
                env.setdefault("%s_%s" % (prefix, var), path)
    return env


def _expand(uri, env):
    def repl(m):
        return env.get(m.group(1), m.group(0))

    prev = None
    while prev != uri:  # nested vars
        prev, uri = uri, _VAR_RE.sub(repl, uri)
    return uri


def read_lib_table(path, env=None, _seen=None):
    """Return ``[(nickname, absolute path)]`` from a ``sym-lib-table``.

    Entries of ``type "Table"`` are indirections to another table (KiCad 10 ships the
    stock libraries this way) and are followed recursively.
    """
    _seen = _seen if _seen is not None else set()
    real = os.path.realpath(path)
    if real in _seen or not os.path.isfile(real):
        return []
    _seen.add(real)

    try:
        root = sexpr.load(real)
    except (sexpr.SexprError, OSError):
        return []

    out = []
    for lib in root.nodes("lib"):
        name = lib.value("name")
        uri = lib.value("uri")
        kind = (lib.value("type") or "").lower()
        if not name or not uri:
            continue
        # A stock table sits next to the libraries it points at, so it tells us where
        # the share tree is; use that to resolve ${KICAD*_SYMBOL_DIR}.
        local_env = env
        if local_env is None:
            share = os.path.dirname(os.path.dirname(real))
            local_env = kicad_env(share)
        resolved = _expand(uri, local_env)
        if not os.path.isabs(resolved):
            resolved = os.path.join(os.path.dirname(real), resolved)
        if kind == "table":
            share = os.path.dirname(os.path.dirname(os.path.realpath(resolved)))
            out.extend(read_lib_table(resolved, kicad_env(share), _seen))
        else:
            out.append((name, resolved))
    return out


def discover_libraries(project_dir=None):
    """All ``(nickname, path)`` pairs visible to KiCad, project table taking priority."""
    libs, seen = [], set()
    tables = []
    if project_dir:
        tables.append(os.path.join(project_dir, "sym-lib-table"))
    g = global_table_path()
    if g:
        tables.append(g)
    for table in tables:
        for name, path in read_lib_table(table):
            if name in seen or not os.path.isfile(path):
                continue
            seen.add(name)
            libs.append((name, path))
    return libs


# ---------------------------------------------------------------------------
# Symbol entries
# ---------------------------------------------------------------------------

_FIELDS = (
    "lib", "name", "reference", "value", "footprint", "datasheet",
    "description", "keywords", "fp_filters", "pins", "units", "extends",
)


class SymbolEntry(object):
    # ``tokens`` is a lazily-populated search cache, not part of the serialised form.
    __slots__ = _FIELDS + ("tokens", "pin_set")

    def __init__(self, lib, name, **kw):
        self.lib = lib
        self.name = name
        self.reference = kw.get("reference", "U")
        self.value = kw.get("value", "")
        self.footprint = kw.get("footprint", "")
        self.datasheet = kw.get("datasheet", "")
        self.description = kw.get("description", "")
        self.keywords = kw.get("keywords", "")
        self.fp_filters = kw.get("fp_filters", [])
        self.pins = kw.get("pins", [])
        self.units = kw.get("units", 1)
        self.extends = kw.get("extends", "")
        self.tokens = None
        self.pin_set = None

    @property
    def lib_id(self):
        return "%s:%s" % (self.lib, self.name)

    @property
    def is_power(self):
        return self.reference.startswith("#PWR") or self.reference == "#FLG"

    def to_json(self):
        return {f: getattr(self, f) for f in _FIELDS}

    @classmethod
    def from_json(cls, d):
        return cls(d.pop("lib"), d.pop("name"), **d)

    def __repr__(self):
        return "SymbolEntry(%s, %d pins)" % (self.lib_id, len(self.pins))


# Canonical KiCad formatting: depth is exactly one tab per nesting level.
_RE_SYMBOL = re.compile(r'^\t\(symbol "((?:[^"\\]|\\.)*)"')
_RE_UNIT = re.compile(r'^\t\t\(symbol "((?:[^"\\]|\\.)*)"')
_RE_PROP = re.compile(r'^\t\t\(property "((?:[^"\\]|\\.)*)" "((?:[^"\\]|\\.)*)"')
_RE_EXTENDS = re.compile(r'^\t\t\(extends "((?:[^"\\]|\\.)*)"')
_RE_NUMBER = re.compile(r'^\t\t\t\t\(number "((?:[^"\\]|\\.)*)"')
_RE_UNIT_SUFFIX = re.compile(r"_(\d+)_(\d+)$")


def _unescape(s):
    return s.replace('\\"', '"').replace("\\\\", "\\")


def scan_library(path, nickname):
    """Shallow-scan one ``.kicad_sym`` into :class:`SymbolEntry` objects."""
    entries = []
    current = None
    pins = None
    units = None
    in_unit = False

    try:
        fh = open(path, "r", encoding="utf-8")
    except OSError:
        return entries

    with fh:
        for line in fh:
            line = line.rstrip("\n")

            m = _RE_SYMBOL.match(line)
            if m:
                if current is not None:
                    current.pins = sorted(pins)
                    current.units = max(units) if units else 1
                    entries.append(current)
                current = SymbolEntry(nickname, _unescape(m.group(1)))
                pins, units, in_unit = set(), set(), False
                continue

            if current is None:
                continue

            m = _RE_UNIT.match(line)
            if m:
                in_unit = True
                sub = _RE_UNIT_SUFFIX.search(m.group(1))
                if sub:
                    units.add(int(sub.group(1)))
                continue

            if in_unit:
                m = _RE_NUMBER.match(line)
                if m:
                    pins.add(_unescape(m.group(1)))
                continue

            m = _RE_PROP.match(line)
            if m:
                key, val = _unescape(m.group(1)), _unescape(m.group(2))
                if key == "Reference":
                    current.reference = val
                elif key == "Value":
                    current.value = val
                elif key == "Footprint":
                    current.footprint = val
                elif key == "Datasheet":
                    current.datasheet = val
                elif key == "Description":
                    current.description = val
                elif key == "ki_keywords":
                    current.keywords = val
                elif key == "ki_fp_filters":
                    current.fp_filters = val.split()
                continue

            m = _RE_EXTENDS.match(line)
            if m:
                current.extends = _unescape(m.group(1))

    if current is not None:
        current.pins = sorted(pins)
        current.units = max(units) if units else 1
        entries.append(current)

    if not entries and os.path.getsize(path) > 0:
        entries = _scan_library_fallback(path, nickname)

    return _resolve_extends(entries)


def _scan_library_fallback(path, nickname):
    """Full parse, for libraries that aren't in KiCad's canonical formatting."""
    try:
        root = sexpr.load(path)
    except (sexpr.SexprError, OSError):
        return []

    entries = []
    for sym in root.nodes("symbol"):
        name = sym.atoms()[0] if sym.atoms() else None
        if not isinstance(name, str):
            continue
        e = SymbolEntry(nickname, name)
        for prop in sym.nodes("property"):
            a = prop.atoms()
            if len(a) < 2:
                continue
            key, val = a[0], a[1]
            if key == "Reference":
                e.reference = val
            elif key == "Value":
                e.value = val
            elif key == "Footprint":
                e.footprint = val
            elif key == "Datasheet":
                e.datasheet = val
            elif key == "Description":
                e.description = val
            elif key == "ki_keywords":
                e.keywords = val
            elif key == "ki_fp_filters":
                e.fp_filters = val.split()
        ext = sym.node("extends")
        if ext and ext.atoms():
            e.extends = ext.atoms()[0]
        pins, units = set(), set()
        for unit in sym.nodes("symbol"):
            ua = unit.atoms()
            if ua:
                sub = _RE_UNIT_SUFFIX.search(str(ua[0]))
                if sub:
                    units.add(int(sub.group(1)))
            for pin in unit.nodes("pin"):
                numbers = pin.node("number")
                if numbers and numbers.atoms():
                    pins.add(numbers.atoms()[0])
        e.pins = sorted(pins)
        e.units = max(units) if units else 1
        entries.append(e)
    return entries


def _resolve_extends(entries):
    """Derived symbols inherit pins and filters from the symbol they extend."""
    by_name = {e.name: e for e in entries}
    for e in entries:
        seen = set()
        base = e
        while base.extends and base.extends not in seen:
            seen.add(base.extends)
            base = by_name.get(base.extends)
            if base is None:
                break
            if not e.pins:
                e.pins = list(base.pins)
            if not e.fp_filters:
                e.fp_filters = list(base.fp_filters)
            if e.units <= 1:
                e.units = base.units
            if not e.description:
                e.description = base.description
            if not e.keywords:
                e.keywords = base.keywords
    return entries


# ---------------------------------------------------------------------------
# Index with on-disk cache
# ---------------------------------------------------------------------------

CACHE_VERSION = 1


def default_cache_path():
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Caches/pcb2schema")
    elif os.name == "nt":
        base = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "pcb2schema")
    else:
        base = os.path.join(
            os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "pcb2schema"
        )
    return os.path.join(base, "symbol-index.json")


class SymbolIndex(object):
    """All known symbols, backed by a staleness-checked JSON cache."""

    def __init__(self, entries=None):
        self.entries = entries or []
        self._by_id = {e.lib_id: e for e in self.entries}
        # Bucketing by pin count lets the matcher skip the overwhelming majority of
        # symbols outright: a 2-pad footprint has no business being compared against
        # a 100-pin MCU, and scanning all 22k entries per footprint was the single
        # biggest cost in the pipeline.
        self.by_pin_count = {}
        for e in self.entries:
            self.by_pin_count.setdefault(len(e.pins), []).append(e)

        # How many symbols name each footprint as their default. A footprint claimed
        # by one symbol is strong evidence for that symbol; one claimed by a dozen
        # (LED_D5.0mm, DIP-20_W7.62mm) says almost nothing, and treating the two the
        # same lets an obscure part outrank the obvious answer.
        self.footprint_claims = {}
        for e in self.entries:
            if e.footprint:
                self.footprint_claims[e.footprint] = \
                    self.footprint_claims.get(e.footprint, 0) + 1

    def claim_count(self, footprint_lib_id):
        return self.footprint_claims.get(footprint_lib_id, 0)

    def candidates_for_pad_count(self, pad_count, slack=8):
        """Symbols whose pin count could plausibly serve *pad_count* pads.

        Allows a symbol to have a handful more pins than the footprint has pads --
        hidden power pins are common -- but not fewer, and not wildly more.
        """
        counts = [0, pad_count] + list(range(pad_count + 1, pad_count + slack + 1))
        out = []
        for n in counts:
            out.extend(self.by_pin_count.get(n, ()))
        return out

    def __len__(self):
        return len(self.entries)

    def get(self, lib_id):
        return self._by_id.get(lib_id)

    def libraries(self):
        return sorted(set(e.lib for e in self.entries))

    @classmethod
    def build(cls, project_dir=None, cache_path=None, progress=None, use_cache=True):
        libs = discover_libraries(project_dir)
        cache_path = cache_path or default_cache_path()
        cached = cls._load_cache(cache_path) if use_cache else {}

        entries, fresh = [], {}
        for i, (nickname, path) in enumerate(libs):
            if progress:
                progress(i, len(libs), nickname)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            stamp = [path, int(stat.st_mtime), stat.st_size]
            hit = cached.get(nickname)
            if hit and hit.get("stamp") == stamp:
                lib_entries = [SymbolEntry.from_json(dict(d)) for d in hit["symbols"]]
            else:
                lib_entries = scan_library(path, nickname)
            entries.extend(lib_entries)
            fresh[nickname] = {
                "stamp": stamp,
                "symbols": [e.to_json() for e in lib_entries],
            }

        if use_cache:
            cls._save_cache(cache_path, fresh)
        return cls(entries)

    @staticmethod
    def _load_cache(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        if data.get("version") != CACHE_VERSION:
            return {}
        return data.get("libraries", {})

    @staticmethod
    def _save_cache(path, libraries):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"version": CACHE_VERSION, "libraries": libraries}, fh)
            os.replace(tmp, path)
        except OSError:
            pass  # a cache we cannot write is a slow run, not a failure
