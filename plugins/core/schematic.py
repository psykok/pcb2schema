"""Build and write ``.kicad_sch`` documents.

There is no schematic API in KiCad -- ``kicad-cli sch`` only exports, and the IPC API
covers the board only -- so the file is constructed directly as S-expressions on top
of :mod:`core.sexpr`.

**UUIDs are derived, never random.** Every identifier is a UUID5 of a stable key: a
symbol's from the footprint it came from, a wire's from its net and endpoints. Random
UUIDs would make each run produce a textually different file and, worse, silently
relink the PCB's ``(path ...)`` fields, so re-running could never be a no-op. Deriving
them is what makes the incremental sync idempotent.
"""

import uuid as _uuid

from . import sexpr
from .sexpr import Node, Sym, num

__all__ = ["Schematic", "SCHEMATIC_VERSION", "derive_uuid"]

SCHEMATIC_VERSION = "20260306"  # KiCad 10
GENERATOR = "pcb2schema"
GENERATOR_VERSION = "10.0"

# Fixed namespace so identifiers are reproducible across machines and runs.
NAMESPACE = _uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")

DEFAULT_FONT_SIZE = 1.27


def derive_uuid(*parts):
    """A stable UUID for a logical object, derived from its identity."""
    return str(_uuid.uuid5(NAMESPACE, "|".join(str(p) for p in parts)))


def _effects(hidden=False, justify=None, size=DEFAULT_FONT_SIZE):
    font = Node("font", [Node("size", [num(size), num(size)])])
    eff = Node("effects", [font])
    if justify:
        eff.append(Node("justify", [Sym(j) for j in justify.split()]))
    if hidden:
        eff.append(Node("hide", [Sym("yes")]))
    return eff


def _property(key, value, at, hidden=False, justify=None):
    return Node("property", [
        key, value,
        Node("at", [num(at[0]), num(at[1]), num(at[2] if len(at) > 2 else 0)]),
        _effects(hidden, justify),
    ])


class Schematic(object):
    """A schematic document under construction, or one being edited in place."""

    def __init__(self, root=None, project_name="", uuid=None):
        self.project_name = project_name
        if root is not None:
            self.root = root
        else:
            self.root = self._empty(uuid or derive_uuid("sheet", project_name))
        self._lib_symbols = self._ensure_lib_symbols()

    # -- construction -------------------------------------------------------

    @staticmethod
    def _empty(sheet_uuid):
        return Node("kicad_sch", [
            Node("version", [Sym(SCHEMATIC_VERSION)]),
            Node("generator", [GENERATOR]),
            Node("generator_version", [GENERATOR_VERSION]),
            Node("uuid", [sheet_uuid]),
            Node("paper", ["A4"]),
            Node("lib_symbols", []),
            Node("sheet_instances", [Node("path", ["/", Node("page", ["1"])])]),
            Node("embedded_fonts", [Sym("no")]),
        ])

    @classmethod
    def load(cls, path, project_name=""):
        return cls(root=sexpr.load(path), project_name=project_name)

    @property
    def sheet_uuid(self):
        return self.root.value("uuid") or ""

    def _ensure_lib_symbols(self):
        node = self.root.node("lib_symbols")
        if node is None:
            node = Node("lib_symbols", [])
            # Must precede the items that reference it.
            idx = len(self.root.children)
            for i, child in enumerate(self.root.children):
                if isinstance(child, Node) and child.name in ("symbol", "wire", "junction"):
                    idx = i
                    break
            self.root.children.insert(idx, node)
        return node

    def set_paper(self, size):
        node = self.root.node("paper")
        if node is None:
            self.root.children.insert(4, Node("paper", [size]))
        else:
            node.children = [size]

    # -- content ------------------------------------------------------------

    def has_lib_symbol(self, lib_id):
        for sym in self._lib_symbols.nodes("symbol"):
            atoms = sym.atoms()
            if atoms and atoms[0] == lib_id:
                return True
        return False

    def add_lib_symbol(self, symdef):
        """Embed a symbol definition, keyed by full ``lib:name`` as KiCad does."""
        if self.has_lib_symbol(symdef.lib_id):
            return
        node = Node(symdef.node.name, list(symdef.node.children))
        node.children[0] = symdef.lib_id
        self._lib_symbols.append(node)

    def add_symbol(self, symdef, reference, value, footprint, pos, unit=1,
                   rotation=0.0, mirror_x=False, mirror_y=False, uuid=None,
                   dnp=False, in_bom=True, on_board=True, datasheet="~",
                   description=None, include_common=True):
        """Place a symbol instance and return its node."""
        self.add_lib_symbol(symdef)
        sym_uuid = uuid or derive_uuid("symbol", symdef.lib_id, reference, unit)

        x, y = pos
        bx0, by0, bx1, by1 = symdef.bbox
        # Reference above the body, value below -- in sheet coordinates the library's
        # y axis is inverted, so the *minimum* library y is the visual bottom.
        ref_at = (x, y - (by1 - (by0 + by1) / 2.0) - 2.54, 0)
        val_at = (x, y + (by1 - (by0 + by1) / 2.0) + 2.54, 0)

        node = Node("symbol", [
            Node("lib_id", [symdef.lib_id]),
            Node("at", [num(x), num(y), num(rotation)]),
        ])
        if mirror_x or mirror_y:
            node.append(Node("mirror", [Sym("x" if mirror_x else "y")]))
        node.extend([
            Node("unit", [Sym(str(unit))]),
            Node("exclude_from_sim", [Sym("no")]),
            Node("in_bom", [Sym("yes" if in_bom else "no")]),
            Node("on_board", [Sym("yes" if on_board else "no")]),
            Node("dnp", [Sym("yes" if dnp else "no")]),
            Node("uuid", [sym_uuid]),
            _property("Reference", reference, ref_at),
            _property("Value", value, val_at),
            _property("Footprint", footprint, (x, y, 0), hidden=True),
            _property("Datasheet", datasheet, (x, y, 0), hidden=True),
            _property("Description", description or "", (x, y, 0), hidden=True),
        ])

        for pin in sorted(symdef.pins_for_unit(unit, include_common=include_common),
                          key=lambda p: p.number):
            if not pin.number:
                continue
            node.append(Node("pin", [
                pin.number,
                Node("uuid", [derive_uuid("pin", sym_uuid, pin.number)]),
            ]))

        node.append(Node("instances", [
            Node("project", [
                self.project_name,
                Node("path", [
                    "/" + self.sheet_uuid,
                    Node("reference", [reference]),
                    Node("unit", [Sym(str(unit))]),
                ]),
            ]),
        ]))

        self._insert_before_tail(node)
        return node

    def add_wire(self, p1, p2, net_name=""):
        node = Node("wire", [
            Node("pts", [
                Node("xy", [num(p1[0]), num(p1[1])]),
                Node("xy", [num(p2[0]), num(p2[1])]),
            ]),
            Node("stroke", [Node("width", [num(0)]), Node("type", [Sym("default")])]),
            Node("uuid", [derive_uuid("wire", net_name, p1, p2)]),
        ])
        self._insert_before_tail(node)
        return node

    def add_junction(self, pt, net_name=""):
        node = Node("junction", [
            Node("at", [num(pt[0]), num(pt[1])]),
            Node("diameter", [num(0)]),
            Node("color", [num(0), num(0), num(0), num(0)]),
            Node("uuid", [derive_uuid("junction", net_name, pt)]),
        ])
        self._insert_before_tail(node)
        return node

    def add_label(self, text, pt, rotation=0, justify="left bottom", scope="local"):
        """Attach a net name to a wire.

        ``scope="global"`` matters more than it looks: KiCad prefixes a root-sheet
        *local* label with the sheet path, so a board net called ``1`` comes back as
        ``/1`` and the next PCB update renames it. A global label round-trips the name
        exactly, which is the whole point of preserving the user's tags.
        """
        if scope == "global":
            node = Node("global_label", [
                text,
                Node("shape", [Sym("input")]),
                Node("at", [num(pt[0]), num(pt[1]), num(rotation)]),
                _effects(justify="left"),
                Node("uuid", [derive_uuid("label", text, pt)]),
            ])
            self._insert_before_tail(node)
            return node
        node = Node("label", [
            text,
            Node("at", [num(pt[0]), num(pt[1]), num(rotation)]),
            _effects(justify=justify),
            Node("uuid", [derive_uuid("label", text, pt)]),
        ])
        self._insert_before_tail(node)
        return node

    def add_no_connect(self, pt):
        node = Node("no_connect", [
            Node("at", [num(pt[0]), num(pt[1])]),
            Node("uuid", [derive_uuid("noconnect", pt)]),
        ])
        self._insert_before_tail(node)
        return node

    # -- queries and edits --------------------------------------------------

    def symbols(self):
        return list(self.root.nodes("symbol"))

    def symbol_by_uuid(self, uuid):
        for sym in self.symbols():
            if sym.value("uuid") == uuid:
                return sym
        return None

    def remove_by_uuid(self, uuid):
        """Remove any top-level item carrying this uuid. Returns True if removed."""
        for child in list(self.root.children):
            if isinstance(child, Node) and child.value("uuid") == uuid:
                self.root.remove(child)
                return True
        return False

    def item_uuids(self, *names):
        out = {}
        for child in self.root.nodes():
            if names and child.name not in names:
                continue
            u = child.value("uuid")
            if u:
                out[u] = child
        return out

    def _insert_before_tail(self, node):
        """Insert before ``sheet_instances``/``embedded_fonts``, which KiCad keeps last."""
        tail = ("sheet_instances", "embedded_fonts")
        for i, child in enumerate(self.root.children):
            if isinstance(child, Node) and child.name in tail:
                self.root.children.insert(i, node)
                return
        self.root.append(node)

    # -- output -------------------------------------------------------------

    def dumps(self):
        return sexpr.dumps(self.root)

    def save(self, path):
        sexpr.save(self.root, path)
