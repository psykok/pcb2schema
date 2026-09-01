"""Decide where symbols sit on the sheet.

Placement echoes the board: a part on the left of the PCB lands on the left of the
schematic. That costs nothing and makes the generated sheet navigable for someone who
already knows the layout, which is the whole audience for this plugin.

Exact relative positions cannot be kept -- symbols have their own sizes and must not
overlap -- so board coordinates are mapped onto a coarse cell grid and each symbol
takes the free cell nearest its ideal spot. Everything lands on the 1.27 mm schematic
grid, without which pins silently fail to connect to wires.
"""

import math

__all__ = ["Placeable", "PAPER_SIZES", "place_symbols", "choose_paper"]

GRID = 1.27

# KiCad paper sizes in millimetres, landscape, smallest first.
PAPER_SIZES = [
    ("A4", 297.0, 210.0),
    ("A3", 420.0, 297.0),
    ("A2", 594.0, 420.0),
    ("A1", 841.0, 594.0),
    ("A0", 1189.0, 841.0),
]


def snap(v, grid=GRID):
    return round(v / grid) * grid


class Placeable(object):
    """One symbol unit awaiting a position on the sheet."""

    __slots__ = ("key", "symdef", "unit", "board_pos", "pos", "rotation", "mirror_x", "mirror_y")

    def __init__(self, key, symdef, unit, board_pos):
        self.key = key
        self.symdef = symdef
        self.unit = unit
        self.board_pos = board_pos  # (x, y) in board millimetres
        self.pos = (0.0, 0.0)
        self.rotation = 0.0
        self.mirror_x = False
        self.mirror_y = False

    def cell_size(self, spacing):
        x0, y0, x1, y1 = self.symdef.bbox
        return (x1 - x0 + spacing, y1 - y0 + spacing)

    def __repr__(self):
        return "Placeable(%s u%d)" % (self.key, self.unit)


def choose_paper(count, cell_w, cell_h, margin):
    """Smallest paper that fits *count* cells, falling back to the largest."""
    for name, w, h in PAPER_SIZES:
        cols = max(1, int((w - 2 * margin) // cell_w))
        rows = max(1, int((h - 2 * margin) // cell_h))
        if cols * rows >= count:
            return name, w, h, cols, rows
    name, w, h = PAPER_SIZES[-1]
    cols = max(1, int((w - 2 * margin) // cell_w))
    rows = max(1, int((h - 2 * margin) // cell_h))
    return name, w, h, cols, rows


def _paper_for(cols, rows, cell_w, cell_h, margin):
    """Smallest paper that holds a cols x rows arrangement of cells."""
    need_w = 2 * margin + cols * cell_w
    need_h = 2 * margin + rows * cell_h
    for name, w, h in PAPER_SIZES:
        if w >= need_w and h >= need_h:
            return name, w, h
    return PAPER_SIZES[-1]


def place_symbols(placeables, spacing=7.62, margin=12.7, pinned=None, paper=None):
    """Assign a sheet position to every placeable.

    *pinned* maps a placeable key to ``(pos, rotation, mirror_x, mirror_y)`` for
    symbols that already exist in the schematic. Those keep exactly where the user
    left them -- moving someone's symbol because an unrelated part was added to the
    board would make re-running unusable -- and only new symbols are given positions,
    in cells the pinned ones are not already using.

    Returns ``(paper_name, sheet_w, sheet_h)``.
    """
    if not placeables:
        size = _size_of(paper) or PAPER_SIZES[0]
        return size[0], size[1], size[2]

    pinned = pinned or {}
    for p in placeables:
        if p.key in pinned:
            p.pos, p.rotation, p.mirror_x, p.mirror_y = pinned[p.key]

    fresh = [p for p in placeables if p.key not in pinned]
    if not fresh:
        size = _size_of(paper) or _fit_existing(placeables, margin)
        return size[0], size[1], size[2]
    if pinned:
        return _place_incremental(placeables, fresh, spacing, margin, paper)

    cell_w = max(p.cell_size(spacing)[0] for p in placeables)
    cell_h = max(p.cell_size(spacing)[1] for p in placeables)
    cell_w = max(snap(cell_w), GRID * 4)
    cell_h = max(snap(cell_h), GRID * 4)

    # Size the grid to the number of parts, not to the sheet. Spreading a handful of
    # symbols across a whole page to "preserve" board positions puts them in opposite
    # corners with metres of wire between them; a compact grid keeps relative order
    # while leaving parts close enough to read.
    xs = [p.board_pos[0] for p in placeables]
    ys = [p.board_pos[1] for p in placeables]
    board_w = (max(xs) - min(xs)) or 1.0
    board_h = (max(ys) - min(ys)) or 1.0
    aspect = min(max(board_w / board_h, 0.25), 4.0)

    n = len(placeables)
    cols = max(1, int(round(math.sqrt(n * aspect))))
    rows = max(1, int(math.ceil(n / float(cols))))

    paper, sheet_w, sheet_h = _paper_for(cols, rows, cell_w, cell_h, margin)

    # Normalise board coordinates into the cell grid. Board y already grows downward,
    # same as the sheet, so no flip is needed here.
    min_x, min_y = min(xs), min(ys)

    def ideal_cell(p):
        cx = (p.board_pos[0] - min_x) / board_w * (cols - 1) if cols > 1 else 0
        cy = (p.board_pos[1] - min_y) / board_h * (rows - 1) if rows > 1 else 0
        return int(round(cx)), int(round(cy))

    # Place in reading order of the ideal grid so that collisions displace parts
    # predictably instead of depending on footprint iteration order.
    ordered = sorted(placeables, key=lambda p: (ideal_cell(p)[1], ideal_cell(p)[0], p.key))

    taken = {}
    for p in ordered:
        taken[_nearest_free(ideal_cell(p), taken, cols, rows)] = p

    # Centre the block of used cells on the sheet rather than pinning it to the top
    # left corner, which leaves a small design marooned in one corner of the page.
    used_cols = max(c[0] for c in taken) + 1
    used_rows = max(c[1] for c in taken) + 1
    off_x = max(margin, (sheet_w - used_cols * cell_w) / 2.0)
    off_y = max(margin, (sheet_h - used_rows * cell_h) / 2.0)

    for (col, row), p in taken.items():
        cx = off_x + col * cell_w + cell_w / 2.0
        cy = off_y + row * cell_h + cell_h / 2.0
        # Shift so the symbol's own bbox is centred in its cell.
        x0, y0, x1, y1 = p.symdef.bbox
        p.pos = (snap(cx - (x0 + x1) / 2.0), snap(cy + (y0 + y1) / 2.0))

    return paper, sheet_w, sheet_h


def _size_of(paper):
    for entry in PAPER_SIZES:
        if entry[0] == paper:
            return entry
    return None


def _fit_existing(placeables, margin):
    """Smallest paper that still contains every already-placed symbol."""
    max_x = max(p.pos[0] + p.symdef.bbox[2] for p in placeables) + margin
    max_y = max(p.pos[1] + p.symdef.bbox[3] for p in placeables) + margin
    for entry in PAPER_SIZES:
        if entry[1] >= max_x and entry[2] >= max_y:
            return entry
    return PAPER_SIZES[-1]


def _place_incremental(placeables, fresh, spacing, margin, paper):
    """Position new symbols around symbols that are already on the sheet."""
    existing = [p for p in placeables if p not in fresh]
    size = _size_of(paper) or _fit_existing(existing, margin)

    cell_w = max(snap(max(p.cell_size(spacing)[0] for p in placeables)), GRID * 4)
    cell_h = max(snap(max(p.cell_size(spacing)[1] for p in placeables)), GRID * 4)

    def to_cell(pos):
        return (int((pos[0] - margin) // cell_w), int((pos[1] - margin) // cell_h))

    taken = {to_cell(p.pos): p for p in existing}

    while True:
        cols = max(1, int((size[1] - 2 * margin) // cell_w))
        rows = max(1, int((size[2] - 2 * margin) // cell_h))
        overflow = False
        for p in sorted(fresh, key=lambda q: q.key):
            cell = _nearest_free(to_cell(p.pos) if p.pos != (0.0, 0.0)
                                 else (cols // 2, rows // 2), taken, cols, rows)
            taken[cell] = p
            if cell[1] >= rows:
                overflow = True
            col, row = cell
            cx = margin + col * cell_w + cell_w / 2.0
            cy = margin + row * cell_h + cell_h / 2.0
            x0, y0, x1, y1 = p.symdef.bbox
            p.pos = (snap(cx - (x0 + x1) / 2.0), snap(cy + (y0 + y1) / 2.0))
        if not overflow:
            break
        # New parts ran off the bottom: grow the sheet rather than overlap.
        bigger = _next_paper(size)
        if bigger is size:
            break
        size = bigger
        taken = {to_cell(p.pos): p for p in existing}

    return size[0], size[1], size[2]


def _next_paper(size):
    for i, entry in enumerate(PAPER_SIZES):
        if entry[0] == size[0] and i + 1 < len(PAPER_SIZES):
            return PAPER_SIZES[i + 1]
    return size


def _nearest_free(ideal, taken, cols, rows):
    """Free cell closest to *ideal*, searching outward in rings."""
    ix, iy = ideal
    ix = min(max(ix, 0), max(cols - 1, 0))
    iy = min(max(iy, 0), max(rows - 1, 0))
    if (ix, iy) not in taken:
        return (ix, iy)

    limit = max(cols, rows)
    for radius in range(1, limit + 1):
        ring = []
        for dx in range(-radius, radius + 1):
            for dy in (-radius, radius):
                ring.append((ix + dx, iy + dy))
        for dy in range(-radius + 1, radius):
            for dx in (-radius, radius):
                ring.append((ix + dx, iy + dy))
        # Prefer cells inside the sheet and closest to the ideal spot.
        ring = [c for c in ring if 0 <= c[0] < cols and 0 <= c[1] < rows and c not in taken]
        if ring:
            ring.sort(key=lambda c: ((c[0] - ix) ** 2 + (c[1] - iy) ** 2, c[1], c[0]))
            return ring[0]

    # Sheet is full: extend downward rather than stacking symbols on top of each other.
    row = rows
    while True:
        for col in range(cols):
            if (col, row) not in taken:
                return (col, row)
        row += 1
