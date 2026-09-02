"""Route nets as orthogonal schematic wires.

Wires are drawn rather than replaced by net labels, which means solving an actual
routing problem. The sheet is a 1.27 mm lattice -- even an A2 sheet is only about
470x330 cells -- so a grid A* per connection is comfortably fast and gives clean
right-angled runs.

Symbol bodies are hard obstacles; crossing an existing wire is merely expensive, since
crossings are legal in a schematic and forbidding them would strand nets that have
nowhere else to go. Turns are penalised so paths come out straight rather than
staircased.

Multi-pin nets are grown as a tree: connect the two closest pins, then repeatedly
attach the nearest remaining pin to whatever has been routed so far. That approximates
a rectilinear Steiner tree, and lets later branches reuse earlier trunk wires.

Any net that cannot be routed is reported rather than dropped, so the caller can fall
back to labels for that net alone. Correctness never depends on the router succeeding.
"""

import heapq

__all__ = ["Router", "RouteResult"]

GRID = 1.27

_TURN_COST = 3
_CROSS_COST = 30
_STEP_COST = 2

# Hard ceiling on A* expansions for one connection. A pin walled in by other parts can
# otherwise make the search crawl over the whole sheet before admitting defeat; the
# caller degrades that net to labels instead, which is cheap and always correct.
_MAX_EXPANSIONS = 120000


class RouteResult(object):
    __slots__ = ("segments", "junctions", "failed", "by_net", "junctions_by_net")

    def __init__(self, segments, junctions, failed, by_net=None,
                 junctions_by_net=None):
        # Per-net attribution lets the caller record which items belong to which net,
        # so an unchanged net can keep exactly its own wiring on the next run.
        self.junctions_by_net = junctions_by_net or {}
        # segments: list of ((x1, y1), (x2, y2)) in millimetres
        self.segments = segments
        self.junctions = junctions
        self.failed = failed  # net names that could not be routed
        # net name -> its own segments, so callers can attach a label to a net's wire
        self.by_net = by_net or {}

    def longest_segment(self, net_name):
        """The most open run of wire on a net -- a good place to hang a label."""
        segs = self.by_net.get(net_name)
        if not segs:
            return None
        return max(segs, key=lambda s: abs(s[1][0] - s[0][0]) + abs(s[1][1] - s[0][1]))

    def __repr__(self):
        return "RouteResult(%d segments, %d junctions, %d failed)" % (
            len(self.segments), len(self.junctions), len(self.failed)
        )


class Router(object):
    def __init__(self, width_mm, height_mm, grid=GRID):
        self.grid = grid
        self.cols = int(width_mm / grid) + 1
        self.rows = int(height_mm / grid) + 1
        self.blocked = set()
        self.occupied = {}   # cell -> net name, for crossing penalties
        # Edge -> net name. Occupancy has to be tracked per *edge*, not per cell.
        # Two wires crossing at a point do not connect in KiCad, but two wires running
        # along the same segment are one wire -- so sharing a cell is fine and sharing
        # an edge silently merges two nets.
        self.used_edges = {}
        self._pin_cells = set()

    # -- geometry -----------------------------------------------------------

    @staticmethod
    def _edge_key(a, b):
        return (a, b) if a <= b else (b, a)

    def cell(self, pt):
        return (int(round(pt[0] / self.grid)), int(round(pt[1] / self.grid)))

    def mm(self, cell):
        return (cell[0] * self.grid, cell[1] * self.grid)

    def block_rect(self, x0, y0, x1, y1):
        """Mark a rectangle (millimetres) as impassable."""
        c0 = (int(x0 / self.grid), int(y0 / self.grid))
        c1 = (int(x1 / self.grid + 0.999), int(y1 / self.grid + 0.999))
        for cx in range(c0[0], c1[0] + 1):
            for cy in range(c0[1], c1[1] + 1):
                self.blocked.add((cx, cy))

    def reserve_pin(self, pt, escape=None):
        """Keep a pin reachable, clearing an exit corridor if one is given.

        Freeing only the pin's own cell is not enough. On symbols like ``Device:LED``
        the connection point lies *inside* the body's bounding box, so blocking the
        body walls the pin in completely and every net touching it becomes unroutable.
        *escape* is the point the wire should leave towards -- the cells between are
        cleared so there is always a way out.
        """
        c = self.cell(pt)
        self._pin_cells.add(c)
        self.blocked.discard(c)
        if escape is None:
            return
        for cell in self._line_cells(c, self.cell(escape)):
            self.blocked.discard(cell)

    @staticmethod
    def _line_cells(a, b):
        """Cells along an axis-aligned-ish line, used to clear pin exit corridors."""
        dx, dy = b[0] - a[0], b[1] - a[1]
        steps = max(abs(dx), abs(dy))
        if steps == 0:
            return [a]
        out = []
        for i in range(steps + 1):
            out.append((a[0] + int(round(dx * i / float(steps))),
                        a[1] + int(round(dy * i / float(steps)))))
        return out

    def reserve_existing(self, net_name, segments):
        """Register wiring that is being kept rather than redrawn.

        Nets whose connections have not changed keep the wires they already have, so
        the router has to know those wires are there: it must not run a different net
        along the same segment, which would merge the two.
        """
        for (p1, p2) in segments:
            a, b = self.cell(p1), self.cell(p2)
            for cell in self._line_cells(a, b):
                self.occupied.setdefault(cell, net_name)
            for u, v in zip(self._line_cells(a, b), self._line_cells(a, b)[1:]):
                self.used_edges.setdefault(self._edge_key(u, v), net_name)

    # -- routing ------------------------------------------------------------

    def route_nets(self, nets):
        """Route ``[(net_name, [points...])]``; returns a :class:`RouteResult`.

        Nets are routed shortest-first: short local connections are the ones most
        likely to find a clean direct path, and routing them early keeps them out of
        the congested channels that longer nets need.
        """
        segments = []
        junction_pts = set()
        failed = []
        by_net = {}
        junctions_by_net = {}

        def spread(item):
            pts = item[1]
            if len(pts) < 2:
                return 0.0
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            return (max(xs) - min(xs)) + (max(ys) - min(ys))

        for net_name, points in sorted(nets, key=spread):
            cells = []
            for p in points:
                c = self.cell(p)
                if c not in cells:
                    cells.append(c)
            if len(cells) < 2:
                continue

            paths = self._route_one(net_name, cells)
            if paths is None:
                failed.append(net_name)
                continue

            edges = set()
            for path in paths:
                for a, b in zip(path, path[1:]):
                    edges.add(self._edge_key(a, b))
                for c in path:
                    self.occupied.setdefault(c, net_name)
            for e in edges:
                self.used_edges.setdefault(e, net_name)

            # Split runs at this net's own pins: see _merge.
            merged = self._merge(edges, split_cells=set(cells))
            segments.extend(merged)
            by_net[net_name] = [(self.mm(a), self.mm(b)) for a, b in merged]
            mine = self._junctions(edges, cells)
            junction_pts.update(mine)
            junctions_by_net[net_name] = [self.mm(c) for c in sorted(mine)]

        return RouteResult(
            segments=[(self.mm(a), self.mm(b)) for a, b in segments],
            junctions=[self.mm(c) for c in sorted(junction_pts)],
            failed=failed,
            by_net=by_net,
            junctions_by_net=junctions_by_net,
        )

    def _route_one(self, net_name, cells):
        """Grow a tree over *cells*; returns a list of paths or ``None`` on failure.

        The next pin to attach is chosen by straight-line distance, not by running a
        full search against every candidate and keeping the shortest. Searching them
        all is quadratic in the pin count -- on a 56-pin ground net that is over 1500
        A* runs across a 300k-cell grid, which does not finish in any useful time.
        Picking the nearest first costs one search per pin and gives near-identical
        trees, because the closest pin almost always is the cheapest to reach.
        """
        connected = {cells[0]}
        remaining = list(cells[1:])
        paths = []

        while remaining:
            target = min(
                remaining,
                key=lambda t: min(abs(t[0] - c[0]) + abs(t[1] - c[1]) for c in connected),
            )
            path = self._astar(connected, target, net_name)
            if path is None:
                return None
            remaining.remove(target)
            connected.update(path)
            paths.append(path)

        return paths

    def _astar(self, starts, goal, net_name):
        """Cheapest orthogonal path from any cell in *starts* to *goal*."""
        if goal in starts:
            return [goal]

        cols, rows = self.cols, self.rows
        blocked = self.blocked
        occupied = self.occupied
        pins = self._pin_cells

        gx, gy = goal

        def heuristic(c):
            return (abs(c[0] - gx) + abs(c[1] - gy)) * _STEP_COST

        # State includes arrival direction so turns can be priced.
        open_heap = []
        best = {}
        for s in starts:
            state = (s, (0, 0))
            best[state] = 0
            heapq.heappush(open_heap, (heuristic(s), 0, state))

        came = {}
        expansions = 0
        while open_heap:
            expansions += 1
            if expansions > _MAX_EXPANSIONS:
                return None
            _, cost, state = heapq.heappop(open_heap)
            cell, direction = state
            if cost > best.get(state, 1 << 30):
                continue
            if cell == goal:
                return self._reconstruct(came, state, starts)

            for d in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nxt = (cell[0] + d[0], cell[1] + d[1])
                if not (0 <= nxt[0] < cols and 0 <= nxt[1] < rows):
                    continue
                if nxt in blocked and nxt not in pins:
                    continue
                # Only the goal pin may be entered; other pins are other parts' business.
                if nxt in pins and nxt != goal and nxt not in starts:
                    continue

                # Running along a segment another net already uses would merge the
                # two nets. Crossing it is fine and merely discouraged.
                edge_owner = self.used_edges.get(self._edge_key(cell, nxt))
                if edge_owner is not None and edge_owner != net_name:
                    continue

                step = _STEP_COST
                if direction != (0, 0) and d != direction:
                    step += _TURN_COST
                owner = occupied.get(nxt)
                if owner is not None and owner != net_name:
                    step += _CROSS_COST

                nstate = (nxt, d)
                ncost = cost + step
                if ncost < best.get(nstate, 1 << 30):
                    best[nstate] = ncost
                    came[nstate] = state
                    heapq.heappush(open_heap, (ncost + heuristic(nxt), ncost, nstate))

        return None

    @staticmethod
    def _reconstruct(came, state, starts):
        path = [state[0]]
        while state in came:
            state = came[state]
            path.append(state[0])
        path.reverse()
        return path

    @staticmethod
    def _merge(edges, split_cells=()):
        """Collapse runs of unit edges into straight segments.

        Runs are broken at *split_cells* -- the net's own pins. This is not cosmetic:
        KiCad only connects a pin to a wire that *ends* at it (or has a junction
        there). A wire merely passing over a pin leaves it unconnected, so merging a
        run straight through one silently drops it from the net. Splitting puts a
        shared wire endpoint on the pin, which does connect.
        """
        h_edges, v_edges = {}, {}
        for a, b in edges:
            if a[1] == b[1]:
                h_edges.setdefault(a[1], set()).add(min(a[0], b[0]))
            else:
                v_edges.setdefault(a[0], set()).add(min(a[1], b[1]))

        cut_x = {}
        cut_y = {}
        for cx, cy in split_cells:
            cut_x.setdefault(cy, set()).add(cx)
            cut_y.setdefault(cx, set()).add(cy)

        out = []
        for y, starts in h_edges.items():
            for lo, hi in _runs(starts):
                for a, b in _split(lo, hi + 1, cut_x.get(y, ())):
                    out.append(((a, y), (b, y)))
        for x, starts in v_edges.items():
            for lo, hi in _runs(starts):
                for a, b in _split(lo, hi + 1, cut_y.get(x, ())):
                    out.append(((x, a), (x, b)))
        return out

    @staticmethod
    def _junctions(edges, pin_cells):
        """Cells where three or more connectable things meet need a junction dot.

        A pin counts as one of them. That matters because runs are deliberately split
        at pins (see :meth:`_merge`), so a pin partway along a net has two wire ends
        plus the pin meeting at it -- three things, and therefore a dot. Counting only
        wire ends scores that as two and silently leaves the dot out, which is both
        wrong by KiCad's convention and visibly odd on the sheet.
        """
        degree = {}
        for a, b in edges:
            degree[a] = degree.get(a, 0) + 1
            degree[b] = degree.get(b, 0) + 1
        pins = set(pin_cells)
        return {c for c, d in degree.items()
                if d >= 3 or (d >= 2 and c in pins)}


def _split(start, end, cuts):
    """Break the span ``start..end`` at every cut strictly inside it."""
    points = sorted({start, end} | {c for c in cuts if start < c < end})
    return list(zip(points, points[1:]))


def _runs(starts):
    """Turn a set of unit-edge start coordinates into ``(lo, hi)`` contiguous runs."""
    out = []
    for s in sorted(starts):
        if out and s == out[-1][1] + 1:
            out[-1][1] = s
        else:
            out.append([s, s])
    return [(lo, hi) for lo, hi in out]
