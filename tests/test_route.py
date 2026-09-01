"""Orthogonal wire routing.

The invariant that matters is subtle and cost a real bug: **a pin must sit at a wire
*endpoint***. KiCad does not connect a pin that a wire merely passes over, so merging
collinear segments into one long run straight through a pin silently drops it from the
net. Nothing about the schematic looks wrong -- the wire visibly touches the pin -- and
only exporting the netlist reveals it.
"""

from core import route

G = route.GRID


def _pt(cx, cy):
    return (cx * G, cy * G)


def _endpoints(result):
    out = set()
    for a, b in result.segments:
        out.add((round(a[0], 3), round(a[1], 3)))
        out.add((round(b[0], 3), round(b[1], 3)))
    return out


def _covered(result, pt):
    x, y = round(pt[0], 3), round(pt[1], 3)
    for (x1, y1), (x2, y2) in result.segments:
        if round(x1, 3) == round(x2, 3) == x and min(y1, y2) - 1e-6 <= y <= max(y1, y2) + 1e-6:
            return True
        if round(y1, 3) == round(y2, 3) == y and min(x1, x2) - 1e-6 <= x <= max(x1, x2) + 1e-6:
            return True
    return False


def test_collinear_pin_gets_a_wire_endpoint():
    """Three pins in a row: the middle one must not be passed straight over."""
    r = route.Router(200.0, 200.0)
    pts = [_pt(10, 10), _pt(20, 10), _pt(30, 10)]
    for p in pts:
        r.reserve_pin(p)
    result = r.route_nets([("N1", pts)])

    assert not result.failed
    ends = _endpoints(result)
    for p in pts:
        key = (round(p[0], 3), round(p[1], 3))
        assert key in ends, "%s is not a wire endpoint -- KiCad would leave it floating" % (p,)


def test_every_pin_is_an_endpoint_for_various_shapes():
    shapes = {
        "L": [_pt(5, 5), _pt(25, 30)],
        "row": [_pt(5, 50), _pt(15, 50), _pt(25, 50), _pt(35, 50)],
        "column": [_pt(60, 5), _pt(60, 15), _pt(60, 25)],
        "scatter": [_pt(80, 10), _pt(95, 40), _pt(70, 45), _pt(100, 12)],
    }
    for name, pts in shapes.items():
        r = route.Router(300.0, 300.0)
        for p in pts:
            r.reserve_pin(p)
        result = r.route_nets([(name, pts)])
        assert not result.failed, "%s: routing failed" % name
        ends = _endpoints(result)
        for p in pts:
            assert (round(p[0], 3), round(p[1], 3)) in ends, \
                "%s: pin %s is not a wire endpoint" % (name, (p,))


def test_nets_never_share_a_wire_segment():
    """Two nets running along the same segment would be one net in KiCad."""
    r = route.Router(200.0, 200.0)
    a = [_pt(10, 10), _pt(40, 10)]
    b = [_pt(10, 12), _pt(40, 12)]
    for p in a + b:
        r.reserve_pin(p)
    result = r.route_nets([("A", a), ("B", b)])
    assert not result.failed

    def edges(name):
        out = set()
        for (p, q) in result.by_net[name]:
            # normalise to unit steps so overlap is detectable
            if p[1] == q[1]:
                lo, hi = sorted((p[0], q[0]))
                x = lo
                while x < hi - 1e-9:
                    out.add((round(x, 3), round(p[1], 3), "h"))
                    x += G
            else:
                lo, hi = sorted((p[1], q[1]))
                y = lo
                while y < hi - 1e-9:
                    out.add((round(p[0], 3), round(y, 3), "v"))
                    y += G
        return out

    assert not (edges("A") & edges("B")), "two nets share wire segments"


def test_unroutable_net_is_reported_not_dropped():
    """A pin sealed inside an obstacle must fail loudly so the caller can label it."""
    r = route.Router(100.0, 100.0)
    open_pin = _pt(5, 5)
    walled = _pt(50, 50)
    r.block_rect(_pt(45, 45)[0], _pt(45, 45)[1], _pt(55, 55)[0], _pt(55, 55)[1])
    r.reserve_pin(open_pin)
    r.reserve_pin(walled)  # freed cell, but every neighbour is blocked
    result = r.route_nets([("N", [open_pin, walled])])
    assert result.failed == ["N"]
    assert not result.segments


def test_pins_of_other_nets_are_not_crossed():
    """Routing through someone else's pin would connect it to the wrong net."""
    r = route.Router(200.0, 200.0)
    mine = [_pt(10, 20), _pt(40, 20)]
    foreign = _pt(25, 20)  # sits directly on the straight path
    for p in mine + [foreign]:
        r.reserve_pin(p)
    result = r.route_nets([("MINE", mine)])
    assert not result.failed
    assert not _covered(result, foreign), "wire runs over a pin belonging to another net"


def test_routing_is_deterministic():
    pts = [_pt(5, 5), _pt(30, 20), _pt(12, 40)]
    runs = []
    for _ in range(2):
        r = route.Router(200.0, 200.0)
        for p in pts:
            r.reserve_pin(p)
        runs.append(sorted(r.route_nets([("N", pts)]).segments))
    assert runs[0] == runs[1]
