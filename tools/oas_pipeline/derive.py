"""Derive walls from rooms, and place openings by relationship instead of coordinates.

With ``"walls": "derive"`` a spec lists only rooms. Walls follow from room boundaries:

* an edge shared by two rooms is an interior wall, unless the pair is declared open
  (``open_between``: open-plan, no wall) or one side is an open-to-below void (a railing);
* an edge between a room and the outside (or a porch/patio/balcony) is an exterior wall;
* an edge between two outdoor spaces, or an outdoor space and the outside, has no wall;
* garage-to-house edges use exterior (fire-separation) thickness.

Openings may name a relationship instead of a wall and offset::

    {"id": "d_bed2", "type": "door", "between": ["hall", "bed2"], "width_mm": 813,
     "operation": "swing", "swing_into": "bed2"}
    {"id": "w_bed2", "type": "window", "room": "bed2", "facade": "south", "width_mm": 1219}
    {"id": "d_front", "type": "door", "between": ["foyer", "porch"], "facade": "south", "position": "center"}

Placement is deterministic. Doors try the start corner, the end corner, then the centre, and keep
the first position whose swing stays inside the room and clears walls and earlier swings.
Windows are centred (``count`` > 1 spreads them evenly). Any request that cannot be satisfied
raises a SpecError naming the opening.
"""
from __future__ import annotations

import math

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

OUTSIDE_USAGES = {"porch", "balcony", "deck", "patio", "terrace", "exterior"}
VOID_USAGES = {"void", "open_to_below"}
END_CLEARANCE_MM = 200
GAP_MM = 150
FACADES = {"north": (0, 1), "south": (0, -1), "east": (1, 0), "west": (-1, 0)}


def _is_outside(gen, rid):
    return rid == "exterior" or gen.room_usage.get(rid) in OUTSIDE_USAGES


# ---------------------------------------------------------------------------- walls
def derive_walls(gen, SpecError):
    open_pairs = {frozenset(p) for p in gen.spec.get("open_between", [])}
    for p in open_pairs:
        for r in p:
            if r not in gen.room_shape:
                raise SpecError(f"open_between names unknown room {r!r}")
    gen._derived_railings = []
    for lv in gen.level_ids:
        rids = [r for r in gen.room_order if gen.room_level[r] == lv]
        polys = {r: gen.room_shape[r] for r in rids}
        verts = {(round(x), round(y)) for r in rids for x, y in list(polys[r].exterior.coords)[:-1]}
        segs = set()
        for r in rids:
            c = list(polys[r].exterior.coords)[:-1]
            for i in range(len(c)):
                a, b = (round(c[i][0]), round(c[i][1])), (round(c[(i + 1) % len(c)][0]), round(c[(i + 1) % len(c)][1]))
                line = LineString([a, b])
                cuts = sorted([v for v in verts if v not in (a, b) and line.distance(Point(v)) < 1],
                              key=lambda v: math.dist(a, v))
                pts = [a] + cuts + [b]
                for p, q in zip(pts, pts[1:]):
                    if p != q:
                        segs.add(tuple(sorted([p, q])))
        runs = {}
        for p, q in segs:
            mx, my = (p[0] + q[0]) / 2, (p[1] + q[1]) / 2
            L = math.dist(p, q)
            ux, uy = (q[0] - p[0]) / L, (q[1] - p[1]) / L
            sides = []
            for sgn in (1, -1):
                hit = [r for r in rids if polys[r].contains(Point(mx - uy * 60 * sgn, my + ux * 60 * sgn))]
                sides.append(hit[0] if hit else "exterior")
            a, b = sides
            if a == b:
                continue
            pair = frozenset((a, b))
            ua, ub = gen.room_usage.get(a), gen.room_usage.get(b)
            if pair in open_pairs or (_is_outside(gen, a) and _is_outside(gen, b)):
                continue
            if (ua in VOID_USAGES) != (ub in VOID_USAGES):
                kind = "railing"
            elif _is_outside(gen, a) or _is_outside(gen, b) or (ua == "garage") != (ub == "garage"):
                kind = "exterior"
            else:
                kind = "interior"
            axis = "x" if abs(p[0] - q[0]) < 1 else ("y" if abs(p[1] - q[1]) < 1 else None)
            if axis is None:
                key = (kind, pair, "d", round(math.atan2(uy, ux), 4), round(p[0] * uy - p[1] * ux))
                start, end = (p, q)
            else:
                key = (kind, pair, axis, p[0] if axis == "x" else p[1])
                start, end = p, q
            runs.setdefault(key, []).append((start, end))
        n = 0
        prefix = f"w{gen.level_ids.index(lv) + 1}"
        for key in sorted(runs, key=lambda k: (k[2], str(k[3]), k[0], sorted(k[1]))):
            kind, pair = key[0], key[1]
            items = sorted(runs[key])
            merged = [list(items[0])]
            for s, e in items[1:]:
                if s == tuple(merged[-1][1]) or math.dist(s, merged[-1][1]) < 1:
                    merged[-1][1] = e
                else:
                    merged.append([s, e])
            for s, e in merged:
                if kind == "railing":
                    gen._derived_railings.append((lv, s, e))
                    continue
                n += 1
                gen._add_wall({"x": int(s[0]), "y": int(s[1])}, {"x": int(e[0]), "y": int(e[1])},
                              f"{prefix}_{n:03d}", lv, kind, {"type_name": "IfcWall", "derived_from": "rooms"})


# ---------------------------------------------------------------------------- openings
def _wall_frame(w):
    fx, fy = w["from"]["x"], w["from"]["y"]
    L = math.hypot(w["to"]["x"] - fx, w["to"]["y"] - fy)
    ux, uy = (w["to"]["x"] - fx) / L, (w["to"]["y"] - fy) / L
    return fx, fy, L, ux, uy


def _swing(w, s, width, into_left, hinge_start):
    fx, fy, L, ux, uy = _wall_frame(w)
    d = 1 if into_left else -1
    px, py = -uy * d, ux * d
    if hinge_start:
        hx, hy, lx, ly = fx + ux * s, fy + uy * s, ux, uy
    else:
        hx, hy, lx, ly = fx + ux * (s + width), fy + uy * (s + width), -ux, -uy
    steps = [i * math.pi / 32 for i in range(17)]
    return Polygon([(hx, hy)] + [(hx + width * (math.cos(t) * lx + math.sin(t) * px),
                                   hy + width * (math.cos(t) * ly + math.sin(t) * py)) for t in steps])


def resolve_openings(gen, SpecError):
    """Return the spec's openings with every relational opening turned into wall + position_mm."""
    out = []
    placed = {}    # wall id -> [(s, e)]
    swings = {}    # level -> [polygon]
    for o in gen.spec.get("openings", []):
        if "wall" in o:
            out.append(o)
            continue
        oid = o["id"]
        if "between" in o:
            a, b = o["between"]
        elif "room" in o:
            a, b = o["room"], "exterior"
        else:
            raise SpecError(f"opening {oid!r} needs 'wall', 'between' or 'room'")
        for r in (a, b):
            if r != "exterior" and r not in gen.room_shape:
                raise SpecError(f"opening {oid!r} names unknown room {r!r}")
        want = {a, b}
        cands = []
        for w in gen.walls_json:
            adj = w["adjacent_rooms"]
            if "room" in o:  # a window may face the open air or an outdoor room (porch, patio, balcony)
                if a not in adj or not any(_is_outside(gen, x) for x in adj if x != a):
                    continue
                b = next(x for x in adj if x != a)
            elif set(adj) != want:
                continue
            fx, fy, L, ux, uy = _wall_frame(w)
            left, right = adj
            # outward normal from room a towards b
            nx, ny = (-uy, ux) if left == b else (uy, -ux)
            face = o.get("facade")
            if face and face != "any":
                fxd, fyd = FACADES[face]
                if nx * fxd + ny * fyd < 0.9:
                    continue
            cands.append(w)
        if not cands:
            raise SpecError(f"opening {oid!r}: no wall between {a!r} and {b!r}"
                            + (f" on the {o.get('facade')} side" if o.get("facade") else ""))
        cands.sort(key=lambda w: -_wall_frame(w)[2])
        width = o["width_mm"]
        typ = o["type"]
        count = o.get("count", 1)
        made = 0
        for w in cands:
            if made >= count:
                break
            fx, fy, L, ux, uy = _wall_frame(w)
            lv = w["level"]
            n_here = count - made if o.get("spread", "one_wall") == "one_wall" else 1
            usable = L - 2 * END_CLEARANCE_MM
            if usable < width:
                continue
            pos = o.get("position", "auto" if typ == "door" and o.get("operation") == "swing" else "center")
            if isinstance(pos, (int, float)):
                tries = [(max(END_CLEARANCE_MM, min(L - width - END_CLEARANCE_MM, pos * L - width / 2)), True)]
            elif n_here > 1:
                # several windows: centre them in the free intervals left by openings already on this wall,
                # largest interval first, so a centred door never collides with evenly spaced windows
                taken = sorted(placed.get(w["id"], []))
                free, cur = [], END_CLEARANCE_MM
                for b0, e0 in taken:
                    if b0 - GAP_MM - cur >= width:
                        free.append((cur, b0 - GAP_MM))
                    cur = max(cur, e0 + GAP_MM)
                if L - END_CLEARANCE_MM - cur >= width:
                    free.append((cur, L - END_CLEARANCE_MM))
                slots = []
                for f0, f1 in sorted(free, key=lambda iv: -(iv[1] - iv[0])):
                    k = max(1, int((f1 - f0 + GAP_MM) // (width + GAP_MM)))
                    slots += [(f0 + (f1 - f0) * (j + 0.5) / k - width / 2, True) for j in range(k)]
                tries = sorted(slots[:n_here], key=lambda t: t[0])
            elif pos == "center":
                tries = [((L - width) / 2, True)]
            elif pos == "start":
                tries = [(END_CLEARANCE_MM, True)]
            elif pos == "end":
                tries = [(L - width - END_CLEARANCE_MM, False)]
            else:  # auto
                tries = [(END_CLEARANCE_MM, True), (L - width - END_CLEARANCE_MM, False), ((L - width) / 2, True)]
            for s, hinge_start in tries:
                s = round(s)
                if any(s < e + GAP_MM and s + width + GAP_MM > b0 for b0, e in placed.get(w["id"], [])):
                    continue
                hinge = o.get("hinge")
                if typ == "door" and o.get("operation") == "swing":
                    into = o.get("swing_into", a)
                    into_left = w["adjacent_rooms"][0] == into
                    hs = hinge_start if hinge is None else hinge == "left"
                    q = _swing(w, s, width, into_left, hs)
                    room = gen.room_shape.get(into)
                    others = unary_union([LineString([(x["from"]["x"], x["from"]["y"]), (x["to"]["x"], x["to"]["y"])])
                                          .buffer(x["thickness_mm"] / 2, cap_style=2)
                                          for x in gen.walls_json if x["level"] == lv and x["id"] != w["id"]])
                    if room is None or q.difference(room).area > 1e3 or q.intersection(others).area > 2e3 or \
                            any(q.intersection(p).area > 1e3 for p in swings.get(lv, [])):
                        continue
                    swings.setdefault(lv, []).append(q)
                    hinge = "left" if hs else "right"
                placed.setdefault(w["id"], []).append((s, s + width))
                c = {k: v for k, v in o.items() if k not in ("between", "room", "facade", "position", "count", "spread")}
                c.update({"id": oid if count == 1 else f"{oid}_{made + 1}", "wall": w["id"], "position_mm": s})
                if hinge:
                    c["hinge"] = hinge
                if typ == "door" and o.get("operation") == "swing" and "swing_into" not in c:
                    c["swing_into"] = a
                out.append(c)
                made += 1
                if made >= count or n_here == 1:
                    break
        if made < count:
            raise SpecError(f"opening {oid!r}: could only place {made} of {count} between {a!r} and {b!r} "
                            "(walls too short, or every position collides with another door or wall)")
    return out
