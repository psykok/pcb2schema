"""Full symbol definitions: geometry, pins, and the node to embed in a schematic.

:mod:`core.symlib` indexes symbols shallowly for searching. Once a symbol is actually
going to be placed we need the real thing: its complete S-expression (a schematic
embeds a copy of every symbol it uses in ``lib_symbols``), the position of each pin so
wires can be attached, and a body outline so placement can avoid overlaps.

The library -> sheet coordinate transform implemented in :func:`pin_position` was
derived empirically rather than assumed. Against every KiCad template schematic, of
the 183 pins where competing conventions predict different points, this one lands on
the actual wire endpoint 183 times.
"""

import math
import os
import re

from . import sexpr

__all__ = ["Pin", "SymbolDef", "SymbolLoader", "pin_position", "transform"]

_UNIT_SUFFIX = re.compile(r"^(?P<base>.*)_(?P<unit>\d+)_(?P<style>\d+)$")


class Pin(object):
    __slots__ = ("number", "name", "x", "y", "angle", "length", "unit", "style", "etype")

    def __init__(self, number, name, x, y, angle, length, unit, style, etype):
        self.number = number
        self.name = name
        self.x = x
        self.y = y
        self.angle = angle
        self.length = length
        self.unit = unit
        self.style = style
        self.etype = etype

    def __repr__(self):
        return "Pin(%s @ %.2f,%.2f)" % (self.number, self.x, self.y)


def transform(px, py, origin, rotation=0.0, mirror_x=False, mirror_y=False):
    """Map a point from symbol-library coordinates to sheet coordinates.

    Order matters and is not the obvious one:

    1. apply the instance mirror *in library space* -- ``(mirror x)`` negates y,
       ``(mirror y)`` negates x;
    2. negate y, because the library's y axis points opposite the sheet's;
    3. rotate by ``+rotation``;
    4. translate to the symbol's placement.
    """
    if mirror_x:
        py = -py
    if mirror_y:
        px = -px
    px, py = px, -py
    if rotation:
        r = math.radians(rotation)
        c, s = math.cos(r), math.sin(r)
        px, py = px * c - py * s, px * s + py * c
    return origin[0] + px, origin[1] + py


def pin_position(pin, origin, rotation=0.0, mirror_x=False, mirror_y=False):
    """Sheet coordinates of a pin's *connection point*.

    A pin's ``(at ...)`` is already the electrical end -- the body line runs away from
    it -- so no length offset is applied.
    """
    return transform(pin.x, pin.y, origin, rotation, mirror_x, mirror_y)


def _num(a):
    try:
        return float(str(a))
    except (TypeError, ValueError):
        return 0.0


class SymbolDef(object):
    """A fully resolved library symbol."""

    __slots__ = ("lib", "name", "node", "pins", "units", "bbox", "body_bbox",
                 "reference", "value")

    def __init__(self, lib, name, node, pins, units, bbox, body_bbox, reference, value):
        self.lib = lib
        self.name = name
        self.node = node
        self.pins = pins
        self.units = units
        self.bbox = bbox  # (min_x, min_y, max_x, max_y) in library coordinates
        self.body_bbox = body_bbox  # drawn body only; pins excluded so they stay routable
        self.reference = reference
        self.value = value

    @property
    def lib_id(self):
        return "%s:%s" % (self.lib, self.name)

    def pins_for_unit(self, unit, style=1, include_common=True):
        """Pins drawn on *unit*.

        Unit 0 is not a unit: it is KiCad's marker for pins common to every unit,
        typically the power pins. They are emitted on one instance only -- declaring
        them on each unit of a multi-unit part would create duplicate pins.
        """
        seen = set()
        out = []
        for p in self.pins:
            if p.style not in (0, style):
                continue
            if p.unit == 0 and not include_common:
                continue
            if p.unit not in (0, unit):
                continue
            if p.number in seen:
                continue  # De Morgan variants repeat the same pin numbers
            seen.add(p.number)
            out.append(p)
        return out

    def pin_by_number(self, number, unit=None, style=1):
        """Prefer the normal body over the De Morgan alternate."""
        fallback = None
        for p in self.pins:
            if p.number != number or (unit is not None and p.unit not in (0, unit)):
                continue
            if p.style in (0, style):
                return p
            fallback = fallback or p
        return fallback

    def unit_of_pin(self, number, default=1):
        """Which unit instance carries this pin.

        Never returns 0. A unit-0 pin is common to all units and has no instance of
        its own; treating 0 as a unit places a phantom extra symbol and scatters the
        part's pins across two instances, which is how NE555P ended up half-wired.
        """
        p = self.pin_by_number(number)
        if p is None or p.unit == 0:
            return default
        return p.unit

    def size(self):
        return (self.bbox[2] - self.bbox[0], self.bbox[3] - self.bbox[1])

    def __repr__(self):
        return "SymbolDef(%s, %d pins, %d units)" % (self.lib_id, len(self.pins), self.units)


def _collect_pins(sym_node):
    """Pins from a symbol's unit sub-symbols, tagged with unit and De Morgan style."""
    pins = []
    for unit_node in sym_node.nodes("symbol"):
        atoms = unit_node.atoms()
        unit = style = 1
        if atoms:
            m = _UNIT_SUFFIX.match(str(atoms[0]))
            if m:
                unit = int(m.group("unit"))
                style = int(m.group("style"))
        for pin in unit_node.nodes("pin"):
            at = pin.node("at")
            if at is None:
                continue
            a = at.atoms()
            etype = str(pin.atoms()[0]) if pin.atoms() else "passive"
            length = _num(pin.value("length", 0, 0))
            name_node = pin.node("name")
            num_node = pin.node("number")
            pins.append(Pin(
                number=(num_node.atoms()[0] if num_node and num_node.atoms() else ""),
                name=(name_node.atoms()[0] if name_node and name_node.atoms() else ""),
                x=_num(a[0]) if len(a) > 0 else 0.0,
                y=_num(a[1]) if len(a) > 1 else 0.0,
                angle=_num(a[2]) if len(a) > 2 else 0.0,
                length=length,
                unit=unit,
                style=style,
                etype=etype,
            ))
    return pins


def _collect_bbox(sym_node, include_pins=True):
    """Body extents in library coordinates.

    With *include_pins* the pin endpoints are included, giving the symbol's full
    footprint on the sheet. Without them the result is the drawn body only, which is
    what the router blocks off -- pins must stay reachable or nothing can connect.
    """
    xs, ys = [], []

    def add(x, y):
        xs.append(x)
        ys.append(y)

    for unit_node in sym_node.nodes("symbol"):
        for rect in unit_node.nodes("rectangle"):
            for corner in ("start", "end"):
                c = rect.node(corner)
                if c and len(c.atoms()) >= 2:
                    add(_num(c.atoms()[0]), _num(c.atoms()[1]))
        for poly in list(unit_node.nodes("polyline")) + list(unit_node.nodes("bezier")):
            pts = poly.node("pts")
            if pts:
                for xy in pts.nodes("xy"):
                    a = xy.atoms()
                    if len(a) >= 2:
                        add(_num(a[0]), _num(a[1]))
        for circ in unit_node.nodes("circle"):
            c = circ.node("center")
            r = _num(circ.value("radius", 0, 0))
            if c and len(c.atoms()) >= 2:
                cx, cy = _num(c.atoms()[0]), _num(c.atoms()[1])
                add(cx - r, cy - r)
                add(cx + r, cy + r)
        for arc in unit_node.nodes("arc"):
            for key in ("start", "mid", "end"):
                p = arc.node(key)
                if p and len(p.atoms()) >= 2:
                    add(_num(p.atoms()[0]), _num(p.atoms()[1]))
        if include_pins:
            for pin in unit_node.nodes("pin"):
                at = pin.node("at")
                if at and len(at.atoms()) >= 2:
                    add(_num(at.atoms()[0]), _num(at.atoms()[1]))

    if not xs:
        return (-2.54, -2.54, 2.54, 2.54)
    return (min(xs), min(ys), max(xs), max(ys))


class SymbolLoader(object):
    """Loads and caches full symbol definitions from ``.kicad_sym`` libraries."""

    def __init__(self, libraries):
        # libraries: iterable of (nickname, path)
        self.paths = dict(libraries)
        self._lib_cache = {}
        self._sym_cache = {}

    def _library(self, nickname):
        if nickname not in self._lib_cache:
            path = self.paths.get(nickname)
            symbols = {}
            if path and os.path.isfile(path):
                try:
                    root = sexpr.load(path)
                    for sym in root.nodes("symbol"):
                        atoms = sym.atoms()
                        if atoms:
                            symbols[str(atoms[0])] = sym
                except (sexpr.SexprError, OSError):
                    pass
            self._lib_cache[nickname] = symbols
        return self._lib_cache[nickname]

    def load(self, lib_id):
        """Return a :class:`SymbolDef`, or ``None`` if the symbol cannot be found."""
        if lib_id in self._sym_cache:
            return self._sym_cache[lib_id]

        nickname, _, name = lib_id.partition(":")
        symbols = self._library(nickname)
        node = symbols.get(name)
        if node is None:
            self._sym_cache[lib_id] = None
            return None

        node = self._resolve_extends(node, symbols)
        pins = _collect_pins(node)
        units = max([p.unit for p in pins] + [1])
        for unit_node in node.nodes("symbol"):
            atoms = unit_node.atoms()
            if atoms:
                m = _UNIT_SUFFIX.match(str(atoms[0]))
                if m:
                    units = max(units, int(m.group("unit")))

        defn = SymbolDef(
            lib=nickname,
            name=name,
            node=node,
            pins=pins,
            units=units,
            bbox=_collect_bbox(node, include_pins=True),
            body_bbox=_collect_bbox(node, include_pins=False),
            reference=self._prop(node, "Reference", "U"),
            value=self._prop(node, "Value", name),
        )
        self._sym_cache[lib_id] = defn
        return defn

    @staticmethod
    def _prop(node, key, default=""):
        for prop in node.nodes("property"):
            a = prop.atoms()
            if len(a) >= 2 and a[0] == key:
                return a[1]
        return default

    def _resolve_extends(self, node, symbols, depth=0):
        """Flatten ``(extends "BASE")`` into a standalone definition.

        A derived symbol carries only its own properties; graphics and pins live on
        the base. A schematic's ``lib_symbols`` must hold a self-contained copy, so
        the base's body is grafted under the derived symbol's name and its properties
        are overlaid.
        """
        ext = node.node("extends")
        if ext is None or not ext.atoms() or depth > 8:
            return node

        base = symbols.get(str(ext.atoms()[0]))
        if base is None:
            return node
        base = self._resolve_extends(base, symbols, depth + 1)

        name = node.atoms()[0] if node.atoms() else ""
        merged = sexpr.Node("symbol", [name])

        own_props = {}
        for prop in node.nodes("property"):
            a = prop.atoms()
            if len(a) >= 2:
                own_props[a[0]] = prop

        for child in base.children:
            if isinstance(child, sexpr.Node) and child.name == "property":
                a = child.atoms()
                if len(a) >= 2 and a[0] in own_props:
                    merged.append(own_props.pop(a[0]))
                    continue
            elif not isinstance(child, sexpr.Node):
                continue  # the base's own name atom
            if isinstance(child, sexpr.Node) and child.name == "extends":
                continue
            merged.append(child)

        for prop in own_props.values():
            merged.append(prop)

        # Unit sub-symbols are named after their parent; rename so the copy is valid.
        base_name = base.atoms()[0] if base.atoms() else ""
        for unit_node in merged.nodes("symbol"):
            atoms = unit_node.atoms()
            if atoms and str(atoms[0]).startswith(str(base_name) + "_"):
                suffix = str(atoms[0])[len(str(base_name)):]
                unit_node.children[0] = str(name) + suffix

        return merged
