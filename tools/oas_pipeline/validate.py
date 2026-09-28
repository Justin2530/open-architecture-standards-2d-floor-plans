"""Validate an OAS-Layout document.

Checks are generic (no plan-specific ids) and read only the JSON:

* schema basics: unique ids, integer millimetres, closed/simple/CCW polygons, area_m2
* per level: no room overlaps, no gaps inside the floor plate
* walls: on a room boundary, correct ``adjacent_rooms``, no duplicated/overlapping walls
* openings: inside their wall, clear of wall ends and each other, correct ``connects_rooms``
* door swings: stay inside the room they swing into, miss other walls and other swings
* circulation: every non-void room reachable from outside; voids fully guarded
* stairs: matching footprints between levels, riser/tread/width limits, landings at both ends
* bedrooms have an exterior (egress-size) window
* upper levels are supported by the level below

Thresholds live in :class:`Rules` (defaults follow common US residential practice / IRC).
"""
from __future__ import annotations

import json
import math
from collections import defaultdict, deque
from dataclasses import dataclass, field

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

VOID_USAGES = {"void", "open_to_below"}
STAIR_USAGES = {"stair"}


@dataclass
class Rules:
    probe_mm: float = 300                 # distance either side of a wall used to identify rooms
    min_open_edge_mm: float = 800         # wall-less shared boundary treated as a walkable opening
    opening_end_clearance_mm: float = 100
    min_gap_between_openings_mm: float = 100
    swing_tolerance_m2: float = 0.001
    max_riser_mm: float = 196.85          # 7 3/4"
    min_tread_mm: float = 254             # 10"
    min_stair_width_mm: float = 914       # 36"
    min_landing_depth_mm: float = 914
    min_egress_window_width_mm: float = 610
    egress_usages: tuple = ("bedroom",)


@dataclass
class Report:
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    info: list = field(default_factory=list)
    open_boundaries: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings,
                "info": self.info, "open_boundaries": self.open_boundaries}

    def format(self) -> str:
        lines = list(self.info)
        if self.open_boundaries:
            lines.append("open (wall-less) boundaries:")
            lines += [f"  {a} <-> {b}: {n} mm" for a, b, n in self.open_boundaries]
        lines.append("WARNINGS: " + ("none" if not self.warnings else ""))
        lines += [f"  {w}" for w in self.warnings]
        lines.append("ERRORS: " + ("none" if not self.errors else ""))
        lines += [f"  {e}" for e in self.errors]
        return "\n".join(lines)


def wall_line(w) -> LineString:
    return LineString([(w["from"]["x"], w["from"]["y"]), (w["to"]["x"], w["to"]["y"])])


def path_line(r) -> LineString:
    return LineString([(p["x"], p["y"]) for p in r["path"]["points"]])


class PlanModel:
    """Geometry + circulation graph of a layout document, shared by the validator and program checker."""

    def __init__(self, doc: dict, rules: Rules | None = None):
        self.doc = doc
        self.rules = rules or Rules()
        self.levels = {lv["id"]: lv for lv in doc.get("levels", [])}
        self.rooms = {r["id"]: r for r in doc["rooms"]}
        self.walls = {w["id"]: w for w in doc.get("walls", [])}
        self.shape = {r["id"]: Polygon([(p["x"], p["y"]) for p in r["boundary_polygon"]["points"]])
                      for r in doc["rooms"]}
        self.by_level = defaultdict(list)
        for r in doc["rooms"]:
            self.by_level[r.get("level")].append(r["id"])
        self.voids = {r["id"] for r in doc["rooms"] if r.get("usage") in VOID_USAGES}
        self.stairs = [r["id"] for r in doc["rooms"] if r.get("usage") in STAIR_USAGES]
        self._blockers = {}
        self.graph = defaultdict(set)   # room -> {(room, how)}
        self.open_edges = []
        self._build_graph()

    def probe(self, level, x, y) -> str:
        p = Point(x, y)
        for rid in self.by_level[level]:
            if self.shape[rid].contains(p):
                return rid
        return "exterior"

    def blockers(self, level):
        if level not in self._blockers:
            parts = [wall_line(w) for w in self.doc.get("walls", []) if w.get("level") == level] + \
                    [path_line(r) for r in self.doc.get("railings", [])
                     if r.get("level") == level and r.get("host_type", "floor") == "floor"]
            self._blockers[level] = unary_union(parts).buffer(2) if parts else Polygon()
        return self._blockers[level]

    def sides(self, wall, s_mm):
        """(left, right) rooms of a wall at distance s_mm from its `from` point."""
        L = wall_line(wall).length
        fx, fy = wall["from"]["x"], wall["from"]["y"]
        ux, uy = (wall["to"]["x"] - fx) / L, (wall["to"]["y"] - fy) / L
        px, py = fx + ux * s_mm, fy + uy * s_mm
        d = self.rules.probe_mm
        return (self.probe(wall["level"], px - uy * d, py + ux * d),
                self.probe(wall["level"], px + uy * d, py - ux * d))

    def link(self, a, b, how):
        self.graph[a].add((b, how))
        self.graph[b].add((a, how))

    def _build_graph(self):
        for o in self.doc.get("openings", []):
            if o.get("opening_type") == "door" and len(o.get("connects_rooms", [])) == 2:
                self.link(*o["connects_rooms"], "door")
        for lv, rids in self.by_level.items():
            block = self.blockers(lv)
            for i, a in enumerate(rids):
                for b in rids[i + 1:]:
                    shared = self.shape[a].boundary.intersection(self.shape[b].boundary)
                    if shared.length > 0:
                        free = shared.difference(block).length
                        if free >= self.rules.min_open_edge_mm:
                            self.link(a, b, "open")
                            self.open_edges.append((a, b, round(free)))
            for a in rids:
                ext = self.shape[a].boundary
                for b in rids:
                    if b != a:
                        ext = ext.difference(self.shape[b].buffer(2))
                free = ext.difference(block).length
                if free >= self.rules.min_open_edge_mm:
                    self.link(a, "exterior", "open")
                    self.open_edges.append((a, "exterior", round(free)))
        for a, b in self.stair_pairs():
            self.link(a, b, "stair")

    def stair_pairs(self):
        pairs = []
        for i, a in enumerate(self.stairs):
            for b in self.stairs[i + 1:]:
                la, lb = self.rooms[a].get("level"), self.rooms[b].get("level")
                if la != lb and self.shape[a].symmetric_difference(self.shape[b]).area < 1:
                    lo, hi = sorted((a, b), key=lambda r: self.levels.get(self.rooms[r]["level"], {}).get("elevation_mm", 0))
                    pairs.append((lo, hi))
        return pairs

    def reachable(self, start="exterior", blocked_usages=()):
        """Rooms reachable from `start` without walking through voids or rooms of `blocked_usages`
        (such rooms can still be reached as destinations)."""
        seen, dq = {start}, deque([start])
        while dq:
            n = dq.popleft()
            for m, _ in self.graph[n]:
                if m in seen or m in self.voids:
                    continue
                seen.add(m)
                if self.rooms.get(m, {}).get("usage") not in blocked_usages:
                    dq.append(m)
        return seen


def validate(doc: dict, rules: Rules | None = None) -> Report:
    rules = rules or Rules()
    rep = Report()
    err, warn = rep.errors.append, rep.warnings.append

    # ---- ids and integer millimetres
    ids = [x["id"] for k in ("rooms", "walls", "openings", "railings", "floor_slabs", "roofs") for x in doc.get(k, [])]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        err(f"duplicate ids: {dupes}")

    def walk(o, path=""):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("x", "y", "z") or (k.endswith("_mm") and not isinstance(v, list)):
                    if not isinstance(v, int) or isinstance(v, bool):
                        err(f"non-integer {path}.{k}={v}")
                walk(v, f"{path}.{k}")
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, f"{path}[{i}]")
    walk(doc)

    # ---- rooms
    levels = {lv["id"]: lv for lv in doc.get("levels", [])}
    for r in doc["rooms"]:
        bp = r.get("boundary_polygon", {})
        if bp.get("closed") is not True:
            err(f"{r['id']}: polygon not marked closed")
        if levels and r.get("level") not in levels:
            err(f"{r['id']}: unknown level {r.get('level')!r}")
    m = PlanModel(doc, rules)
    for r in doc["rooms"]:
        p = m.shape[r["id"]]
        if not p.is_valid or not p.exterior.is_simple:
            err(f"{r['id']}: polygon not simple")
        elif not p.exterior.is_ccw:
            err(f"{r['id']}: winding not counter-clockwise")
        if "area_m2" in r and abs(p.area / 1e6 - r["area_m2"]) > 0.01:
            err(f"{r['id']}: area_m2 {r['area_m2']} != {p.area / 1e6:.2f}")
    for lv, rids in m.by_level.items():
        for i, a in enumerate(rids):
            for b in rids[i + 1:]:
                ov = m.shape[a].intersection(m.shape[b]).area
                if ov > 1:
                    err(f"rooms overlap: {a} / {b} ({ov / 1e6:.2f} m2)")
        union = unary_union([m.shape[x] for x in rids])
        parts = list(getattr(union, "geoms", [union]))
        holes = sum(len(g.interiors) for g in parts)
        if holes:
            err(f"{lv}: {holes} gap(s) enclosed inside the floor plate")
        rep.info.append(f"{lv}: {len(rids)} rooms, {union.area / 1e6:.2f} m2 ({union.area / 1e6 * 10.7639:.0f} sf), "
                        f"{len(parts)} part(s)")

    # ---- walls
    for w in doc.get("walls", []):
        L = wall_line(w)
        if L.length == 0:
            err(f"{w['id']}: zero length")
            continue
        bnd = unary_union([m.shape[r].boundary for r in m.by_level[w.get("level")]]).buffer(1)
        if not bnd.contains(L):
            err(f"{w['id']}: not on a room boundary")
        sides = list(m.sides(w, L.length / 2))
        if sorted(sides) != sorted(w.get("adjacent_rooms", [])):
            err(f"{w['id']}: adjacent_rooms {w.get('adjacent_rooms')} != measured {sides}")
        if sides[0] == sides[1]:
            err(f"{w['id']}: same space on both sides ({sides[0]})")
    by_lv = defaultdict(list)
    for w in doc.get("walls", []):
        by_lv[w.get("level")].append(w)
    for ws in by_lv.values():
        for i, a in enumerate(ws):
            for b in ws[i + 1:]:
                if wall_line(a).intersection(wall_line(b)).length > 1:
                    err(f"walls overlap: {a['id']} / {b['id']}")

    # ---- openings and door swings
    by_wall, swings = defaultdict(list), defaultdict(list)
    for o in doc.get("openings", []):
        w = m.walls.get(o.get("in_wall"))
        if not w:
            err(f"{o['id']}: unknown in_wall {o.get('in_wall')!r}")
            continue
        if o.get("level", w.get("level")) != w.get("level"):
            err(f"{o['id']}: level differs from its wall")
        L = wall_line(w).length
        s, e = o["position_along_wall_mm"], o["position_along_wall_mm"] + o["width_mm"]
        if s < 0 or e > L:
            err(f"{o['id']}: outside wall {w['id']} ({s}-{e} of {L:.0f} mm)")
        elif min(s, L - e) < rules.opening_end_clearance_mm:
            warn(f"{o['id']}: within {rules.opening_end_clearance_mm:.0f} mm of the end of {w['id']}")
        by_wall[w["id"]].append((s, e, o["id"]))
        sides = list(m.sides(w, (s + e) / 2))
        if sorted(sides) != sorted(o.get("connects_rooms", [])):
            err(f"{o['id']}: connects_rooms {o.get('connects_rooms')} != measured {sides}")
        if o.get("opening_type") == "door" and o.get("operation") == "swing":
            fx, fy = w["from"]["x"], w["from"]["y"]
            ux, uy = (w["to"]["x"] - fx) / L, (w["to"]["y"] - fy) / L
            d = 1 if o.get("swing_direction", "inward") == "inward" else -1
            px, py = -uy * d, ux * d
            into = sides[0] if d == 1 else sides[1]
            if o.get("hinge_side") == "right":
                hx, hy, lx, ly = fx + ux * e, fy + uy * e, -ux, -uy
            else:
                hx, hy, lx, ly = fx + ux * s, fy + uy * s, ux, uy
            W = o["width_mm"]
            steps = [i * math.pi / 32 for i in range(17)]
            quarter = Polygon([(hx, hy)] + [(hx + W * (math.cos(t) * lx + math.sin(t) * px),
                                              hy + W * (math.cos(t) * ly + math.sin(t) * py)) for t in steps])
            if into == "exterior":
                warn(f"{o['id']}: swings to the exterior")
            else:
                outside = quarter.difference(m.shape[into]).area / 1e6
                if outside > rules.swing_tolerance_m2:
                    err(f"{o['id']}: swing leaves {into} ({outside:.3f} m2 outside)")
                others = unary_union([wall_line(x).buffer(x.get("thickness_mm", 200) / 2, cap_style=2)
                                      for x in doc["walls"] if x.get("level") == w.get("level") and x["id"] != w["id"]])
                hit = quarter.intersection(others).area / 1e6
                if hit > 2 * rules.swing_tolerance_m2:
                    err(f"{o['id']}: swing hits a wall ({hit:.3f} m2)")
            swings[w.get("level")].append((o["id"], quarter))
    for wid, lst in by_wall.items():
        lst.sort()
        for (s1, e1, a), (s2, e2, b) in zip(lst, lst[1:]):
            if s2 < e1 + rules.min_gap_between_openings_mm:
                err(f"openings overlap or touch on {wid}: {a}, {b}")
    for lst in swings.values():
        for i, (a, qa) in enumerate(lst):
            for b, qb in lst[i + 1:]:
                if qa.intersection(qb).area / 1e6 > rules.swing_tolerance_m2:
                    err(f"door swings collide: {a} / {b}")

    # ---- circulation
    rep.open_boundaries = m.open_edges
    reach = m.reachable()
    for rid in m.rooms:
        if rid not in m.voids and rid not in reach:
            err(f"room not reachable from exterior: {rid}")
    for v in m.voids:
        walkable = sorted(n for n, how in m.graph[v] if how == "open")
        if walkable:
            err(f"void {v} has an unguarded open edge to {walkable}")

    # ---- stairs
    for s in m.stairs:
        if not any(s in p for p in m.stair_pairs()) and len(levels) > 1:
            err(f"stair {s} has no matching footprint on another level")
    for lo, hi in m.stair_pairs():
        _check_stair(m, lo, hi, rep, rules)

    # ---- egress windows
    for r in doc["rooms"]:
        if r.get("usage") in rules.egress_usages:
            ok = [o for o in doc.get("openings", []) if o.get("opening_type") == "window"
                  and r["id"] in o.get("connects_rooms", []) and "exterior" in o.get("connects_rooms", [])
                  and o["width_mm"] >= rules.min_egress_window_width_mm]
            if not ok:
                err(f"{r['id']}: no exterior window of at least {rules.min_egress_window_width_mm:.0f} mm")

    # ---- support by the level below
    ordered = sorted(levels.values(), key=lambda lv: lv.get("elevation_mm", 0))
    for below, above in zip(ordered, ordered[1:]):
        base = unary_union([m.shape[x] for x in m.by_level[below["id"]]]).buffer(1)
        for rid in m.by_level[above["id"]]:
            if not base.contains(m.shape[rid]):
                err(f"{rid}: overhangs {below['id']}")
    return rep


def _check_stair(m: PlanModel, lo: str, hi: str, rep: Report, rules: Rules):
    lv_lo, lv_hi = m.rooms[lo]["level"], m.rooms[hi]["level"]
    rise = m.levels[lv_hi]["elevation_mm"] - m.levels[lv_lo]["elevation_mm"]
    minx, miny, maxx, maxy = m.shape[lo].bounds
    along_y = (maxy - miny) >= (maxx - minx)
    width = (maxx - minx) if along_y else (maxy - miny)
    run = (maxy - miny) if along_y else (maxx - minx)
    risers = max(1, math.ceil(rise / rules.max_riser_mm - 1e-9))
    treads = risers - 1
    need = treads * rules.min_tread_mm
    rep.info.append(f"stair {lo}->{hi}: rise {rise} mm = {risers} risers @ {rise / risers:.1f} mm, "
                    f"{treads} treads @ {rules.min_tread_mm:.0f} = {need:.0f} mm; run {run:.0f} mm, width {width:.0f} mm")
    if run < need:
        rep.errors.append(f"stair {lo}: run {run:.0f} mm shorter than {need:.0f} mm")
    if width < rules.min_stair_width_mm:
        rep.errors.append(f"stair {lo}: width {width:.0f} mm < {rules.min_stair_width_mm:.0f} mm")

    # The two short ends: bottom is the end open to a walkable room on the lower level,
    # top is the end open to a walkable room on the upper level.
    if along_y:
        ends = {"low": (LineString([(minx, miny), (maxx, miny)]), (0, -1)),
                "high": (LineString([(minx, maxy), (maxx, maxy)]), (0, 1))}
    else:
        ends = {"low": (LineString([(minx, miny), (minx, maxy)]), (-1, 0)),
                "high": (LineString([(maxx, miny), (maxx, maxy)]), (1, 0))}

    def landing(level, end):
        seg, (dx, dy) = ends[end]
        free = seg.difference(m.blockers(level)).length
        if free < 0.8 * seg.length:
            return None
        cx, cy = seg.centroid.x, seg.centroid.y
        d = rules.min_landing_depth_mm
        room = m.probe(level, cx + dx * d / 2, cy + dy * d / 2)
        deep = m.probe(level, cx + dx * (d - 10), cy + dy * (d - 10))
        unwalkable = {"exterior"} | m.voids
        if room in unwalkable or deep in unwalkable:
            return None
        return room

    options = []
    for bottom, top in (("low", "high"), ("high", "low")):
        b, t = landing(lv_lo, bottom), landing(lv_hi, top)
        if b and t:
            options.append((bottom, b, t))
    if not options:
        rep.errors.append(f"stair {lo}->{hi}: no end opens onto a walkable landing on both levels")
        return
    bottom, b, t = options[0]
    rep.info.append(f"  ascends toward the {'high' if bottom == 'low' else 'low'} {'y' if along_y else 'x'} end; "
                    f"bottom landing {b}, top landing {t}")


def load(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


__all__ = ["PlanModel", "Report", "Rules", "load", "validate"]
