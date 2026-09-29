"""Build a 3D *exterior model* from an OAS-Layout document.

The exterior model is a neutral, renderer-independent list of 3D elements (millimetres,
Z up, same XY frame as the plan). All architectural interpretation happens here, in
deterministic code; a renderer only draws what it is given and may not move, resize or
reinterpret anything.

Every element carries provenance:

* ``plan``     — geometry taken directly from the OAS document (walls, openings, decks,
                 railing paths, slab footprints and elevations)
* ``derived``  — a deterministic consequence of plan data (roof planes from an OAS roof
                 entity, gable infill between a wall and its roof, the floor-structure band
                 between two stacked exterior walls)
* ``inferred`` — exterior-design information the plan does not contain (support posts,
                 beams, fascia, thicknesses, railing height, grade, site context, a default
                 roof when the plan has none). Driven by the exterior design spec.

Plan inconsistencies found while building (e.g. a roof edge with nothing under it) are
reported in ``issues`` rather than silently fixed.
"""
from __future__ import annotations

import copy
import json
import math
import os

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

EXTERIOR_USAGES = {"porch", "balcony", "deck", "patio", "terrace", "exterior"}
VOID_USAGES = {"void", "open_to_below"}
ENTRY_USAGES = ("entry", "foyer", "circulation", "living")
HERE = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(HERE, "design_defaults.json")) as _fh:
    DEFAULT_DESIGN = json.load(_fh)


def merge(base: dict, override: dict | None) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = v
    return out


def r1(v: float) -> float:
    return round(float(v), 1)


def pt3(x, y, z):
    return [r1(x), r1(y), r1(z)]


class WallFrame:
    def __init__(self, w: dict):
        self.w = w
        self.fx, self.fy = w["from"]["x"], w["from"]["y"]
        self.L = math.hypot(w["to"]["x"] - self.fx, w["to"]["y"] - self.fy)
        self.ux, self.uy = (w["to"]["x"] - self.fx) / self.L, (w["to"]["y"] - self.fy) / self.L
        self.nx, self.ny = -self.uy, self.ux  # left normal

    def at(self, s, off=0.0):
        return self.fx + self.ux * s + self.nx * off, self.fy + self.uy * s + self.ny * off

    def project(self, x, y):
        dx, dy = x - self.fx, y - self.fy
        return dx * self.ux + dy * self.uy, dx * self.nx + dy * self.ny

    def line(self):
        return LineString([(self.fx, self.fy), (self.w["to"]["x"], self.w["to"]["y"])])


class ExteriorModelBuilder:
    def __init__(self, doc: dict, design: dict | None = None):
        src = doc.get("metadata", {}).get("level_extract_of")
        if src:
            raise ValueError(f"{doc.get('plan_id')} is a single-level extract of {src}; the exterior must be built "
                             "from the complete multi-level house model, never from one floor")
        self.doc = doc
        self.d = merge(DEFAULT_DESIGN, design)
        self.levels = sorted(doc.get("levels", []) or [{"id": None, "elevation_mm": 0}],
                             key=lambda lv: lv.get("elevation_mm", 0))
        self.elev = {lv["id"]: lv.get("elevation_mm", 0) for lv in self.levels}
        self.rooms = {r["id"]: r for r in doc["rooms"]}
        self.poly = {r["id"]: Polygon([(p["x"], p["y"]) for p in r["boundary_polygon"]["points"]]) for r in doc["rooms"]}
        self.walls = {w["id"]: w for w in doc.get("walls", [])}
        self.grade = self.elev[self.levels[0]["id"]] + self.d["grade_offset_mm"]
        self.elements: list[dict] = []
        self.issues: list[dict] = []
        self.support: list[tuple] = []   # (WallFrame, s0, s1, z_top) for roof-support checks

    # ------------------------------------------------------------------ helpers
    def usage(self, rid):
        return self.rooms.get(rid, {}).get("usage")

    def is_outside(self, rid):
        return rid == "exterior" or self.usage(rid) in EXTERIOR_USAGES

    def is_exterior_wall(self, w):
        adj = w.get("adjacent_rooms", [])
        return any(self.is_outside(r) for r in adj) and not all(self.is_outside(r) for r in adj)

    def level_elev(self, level):
        return self.elev.get(level, 0)

    def next_level(self, level):
        ids = [lv["id"] for lv in self.levels]
        i = ids.index(level) if level in ids else -1
        return ids[i + 1] if 0 <= i < len(ids) - 1 else None

    def add(self, kind, klass, geom, *, refs=(), rule, material, level=None, assumptions=(), **extra):
        el = {"id": f"{kind}:{len([e for e in self.elements if e['kind'] == kind]) + 1}", "kind": kind,
              "class": klass, "refs": list(refs), "rule": rule, "level": level, "material": material,
              "geom": geom}
        if assumptions:
            el["assumptions"] = list(assumptions)
        el.update(extra)
        self.elements.append(el)
        return el

    @staticmethod
    def prism(points, extrude, u_axis=None):
        g = {"type": "prism", "points": [pt3(*p) for p in points], "extrude": [r1(v) for v in extrude]}
        if u_axis is not None:
            g["u_axis"] = [round(u_axis[0], 6), round(u_axis[1], 6), 0]
        return g

    def wall_rect(self, f: WallFrame, s0, s1, z0, z1, t, off=0.0):
        a, b = f.at(s0, off - t / 2), f.at(s1, off - t / 2)
        return self.prism([(a[0], a[1], z0), (b[0], b[1], z0), (b[0], b[1], z1), (a[0], a[1], z1)],
                          (f.nx * t, f.ny * t, 0), (f.ux, f.uy))

    def outward_sign(self, w):
        """+1 if the exterior side is the wall's left (n) side, -1 if right."""
        left, right = w.get("adjacent_rooms", ["exterior", "exterior"])[:2]
        return 1 if self.is_outside(left) and not self.is_outside(right) else -1

    # ------------------------------------------------------------------ build
    def build(self) -> dict:
        self.walls_and_openings()
        self.floor_bands()
        self.slabs_and_decks()
        roofs = self.doc.get("roofs", [])
        if roofs:
            for r in roofs:
                self.roof(r, klass="derived", rule="roof_from_oas_roof_entity")
        elif self.d["default_roof"]["enabled"]:
            self.default_roofs()
        self.roof_support_and_overlaps()
        self.roof_clearances()
        self.railings()
        self.exterior_stairs()
        self.supports()
        self.site()
        front = self.front_orientation()
        model = {
            "exterior_model": "0.1.0",
            "plan_id": self.doc.get("plan_id"),
            "title": self.doc.get("title"),
            "units": {"length": "mm", "up": "z"},
            "disclaimer": "CONCEPT DESIGN — NOT FOR CONSTRUCTION",
            "levels": [{"id": lv["id"], "name": lv.get("name", lv["id"]), "elevation_mm": lv.get("elevation_mm", 0)}
                       for lv in self.levels],
            "grade_mm": {"value": self.grade, "class": "inferred", "rule": "grade_offset_mm"},
            "front": front,
            "design": self.d,
            "elements": self.elements,
            "facades": self.facades(front),
            "issues": self.issues,
        }
        model["views"] = self.views(front)
        model["provenance_summary"] = self.summary()
        return model

    # ------------------------------------------------------------------ walls + openings (plan)
    def wall_z(self, w):
        base = self.level_elev(w.get("level")) + w.get("base_offset_mm", 0)
        if "wall_height_mm" in w:
            return base, base + w["wall_height_mm"], []
        return base, base + self.d["default_wall_height_mm"], ["wall_height_mm missing: default used (inferred)"]

    def walls_and_openings(self):
        # every wall top (exterior or party wall) can carry a roof edge
        for w in self.doc.get("walls", []):
            if not self.is_exterior_wall(w):
                _, top, _ = self.wall_z(w)
                self.support.append((WallFrame(w), 0.0, WallFrame(w).L, top))
        by_wall = {}
        for o in self.doc.get("openings", []):
            by_wall.setdefault(o.get("in_wall"), []).append(o)
        for w in self.doc.get("walls", []):
            if not self.is_exterior_wall(w):
                continue
            f = WallFrame(w)
            t = w.get("thickness_mm", 200)
            z0, z1, assume = self.wall_z(w)
            cursor = 0.0
            pieces = []

            def piece(s0, s1, a, b):
                if s1 - s0 > 0.5 and b - a > 0.5:
                    pieces.append((s0, s1, a, b))

            for o in sorted(by_wall.get(w["id"], []), key=lambda o: o["position_along_wall_mm"]):
                s, e = o["position_along_wall_mm"], o["position_along_wall_mm"] + o["width_mm"]
                sill = o.get("sill_height_mm", 0)
                head = sill + o["height_mm"]
                piece(cursor, s, z0, z1)
                piece(s, e, z0, z0 + sill)
                piece(s, e, z0 + head, z1)
                cursor = max(cursor, e)
                self.opening(o, w, f, t, z0 + sill, z0 + head)
            piece(cursor, f.L, z0, z1)
            for s0, s1, a, b in pieces:
                self.add("wall_panel", "plan", self.wall_rect(f, s0, s1, a, b, t), refs=[w["id"]],
                         rule="exterior_wall_from_oas", material=self.d["materials"]["wall"], level=w.get("level"),
                         assumptions=assume)
                self.support.append((f, s0, s1, b))
            # the wall top is continuous above openings, so the support line covers the full length
            self.support.append((f, 0.0, f.L, z1))

    def opening_style(self, o, w):
        rooms = o.get("connects_rooms", [])
        inside = [r for r in rooms if not self.is_outside(r)]
        if o.get("opening_type") == "window":
            return "window"
        if any(self.usage(r) == "garage" for r in inside) and o["width_mm"] >= self.d["vehicle_door_min_width_mm"]:
            return "vehicle_door"
        if o.get("operation") in ("sliding", "slide", "folding"):
            return "glass_door"
        if any(self.usage(r) in ("entry", "foyer") or "entry" in self.rooms.get(r, {}).get("tags", []) for r in inside):
            return "entry_door"
        return "door"

    def opening(self, o, w, f, t, z0, z1):
        s = o["position_along_wall_mm"]
        ox, oy = f.at(s)
        style = self.opening_style(o, w)
        self.add("opening", "plan", {
            "type": "opening", "origin": pt3(ox, oy, z0), "u": [round(f.ux, 6), round(f.uy, 6), 0],
            "n": [round(f.nx * self.outward_sign(w), 6), round(f.ny * self.outward_sign(w), 6), 0],
            "width": o["width_mm"], "height": r1(z1 - z0), "depth": t},
            refs=[o["id"], w["id"]], rule="opening_from_oas", level=w.get("level"),
            material=self.d["materials"].get(style, self.d["materials"]["trim"]), style=style,
            opening_type=o.get("opening_type"), operation=o.get("operation"))

    def floor_bands(self):
        """Exterior sheathing across the floor structure where an exterior wall continues on the level above."""
        ext = [w for w in self.doc.get("walls", []) if self.is_exterior_wall(w)]
        for w in ext:
            nxt = self.next_level(w.get("level"))
            if not nxt:
                continue
            _, top, _ = self.wall_z(w)
            e_next = self.level_elev(nxt)
            if top >= e_next - 0.5:
                continue
            f = WallFrame(w)
            for u in ext:
                if u.get("level") != nxt:
                    continue
                a = f.project(u["from"]["x"], u["from"]["y"])
                b = f.project(u["to"]["x"], u["to"]["y"])
                if abs(a[1]) > 1 or abs(b[1]) > 1:
                    continue
                s0, s1 = max(0.0, min(a[0], b[0])), min(f.L, max(a[0], b[0]))
                if s1 - s0 > 1:
                    self.add("band", "derived", self.wall_rect(f, s0, s1, top, e_next, w.get("thickness_mm", 200)),
                             refs=[w["id"], u["id"]], rule="floor_structure_band", level=w.get("level"),
                             material=self.d["materials"]["wall"])
                    self.support.append((f, s0, s1, e_next))

    # ------------------------------------------------------------------ slabs, decks
    def exterior_room_for_slab(self, s):
        sp = Polygon([(p["x"], p["y"]) for p in s["boundary_polygon"]["points"]])
        for rid, r in self.rooms.items():
            if r.get("level") == s.get("level") and self.is_outside(rid) and \
                    self.poly[rid].symmetric_difference(sp).area < 1e4:
                return rid
        return None

    def slabs_and_decks(self):
        first = self.levels[0]["id"]
        covered = set()
        for s in self.doc.get("floor_slabs", []):
            pts = [(p["x"], p["y"]) for p in s["boundary_polygon"]["points"]]
            top = self.level_elev(s.get("level")) + s.get("height_above_level_mm", 0)
            rid = self.exterior_room_for_slab(s)
            if rid:
                covered.add(rid)
                self.deck(rid, pts, top, s.get("level"), refs=[rid, s["id"]])
            elif s.get("level") == first and self.d["foundation_plinth"]:
                self.add("foundation", "plan", self.prism([(x, y, top) for x, y in pts], (0, 0, self.grade - top)),
                         refs=[s["id"]], rule="ground_slab_from_oas", level=s.get("level"),
                         material=self.d["materials"]["foundation"],
                         assumptions=["slab edge exposed from grade to slab top (grade inferred)"])
        for rid, r in self.rooms.items():  # exterior rooms without a slab: deck at the level elevation
            if self.is_outside(rid) and rid != "exterior" and rid not in covered:
                pts = [(p["x"], p["y"]) for p in r["boundary_polygon"]["points"]]
                self.deck(rid, pts, self.level_elev(r.get("level")), r.get("level"), refs=[rid],
                          extra_assume=["no floor slab in plan: deck top at level elevation"])

    def deck(self, rid, pts, top, level, refs, extra_assume=()):
        on_ground = self.level_elev(level) <= self.level_elev(self.levels[0]["id"])
        bottom = self.grade if on_ground else top - self.d["deck_thickness_mm"]
        longest = max(range(len(pts)), key=lambda i: math.dist(pts[i], pts[(i + 1) % len(pts)]))
        a, b = pts[longest], pts[(longest + 1) % len(pts)]
        L = math.dist(a, b)
        self.add("deck", "plan", self.prism([(x, y, top) for x, y in pts], (0, 0, bottom - top),
                                            ((b[0] - a[0]) / L, (b[1] - a[1]) / L)),
                 refs=refs, rule="exterior_floor_from_oas", level=level, material=self.d["materials"]["deck"],
                 usage=self.usage(rid),
                 assumptions=list(extra_assume) + (["built up from grade (grade inferred)"] if on_ground else
                                                   [f"deck structure depth {self.d['deck_thickness_mm']} mm (inferred)"]))

    # ------------------------------------------------------------------ roofs
    def roof(self, r, klass, rule, assumptions=()):
        pts = [(p["x"], p["y"]) for p in r["boundary_polygon"]["points"]]
        base = self.level_elev(r.get("level")) + r.get("level_offset_mm", 0)
        n = len(pts)
        slopes = r.get("slope_angles", [0.0] * n)
        defines = r.get("defines_slope", [a > 0 for a in slopes])
        over = r.get("eave_overhang_mm", [0] * n)
        t = self.d["roof_thickness_mm"]
        assume = list(assumptions) + [f"roof build-up {t} mm (inferred)"]
        mat = self.d["materials"]["roof"]
        axis_rect = n == 4 and all(pts[i][0] == pts[(i + 1) % 4][0] or pts[i][1] == pts[(i + 1) % 4][1] for i in range(4))
        sloped = [i for i in range(n) if defines[i] and slopes[i] > 0]
        poly = Polygon(pts)
        if not poly.exterior.is_ccw:
            # reversing the points maps new edge k (q_k -> q_k+1) to original edge n-2-k
            pts = pts[::-1]
            slopes, defines, over = ([arr[(n - 2 - k) % n] for k in range(n)] for arr in (slopes, defines, over))
            sloped = [i for i in range(n) if defines[i] and slopes[i] > 0]

        def edge(i):
            a, b = pts[i], pts[(i + 1) % n]
            L = math.dist(a, b)
            u = ((b[0] - a[0]) / L, (b[1] - a[1]) / L)
            out = (u[1], -u[0])  # outward for CCW polygons
            return a, b, u, out, L

        def expanded(i, s_over_prev, s_over_next, off):
            """Eave line of edge i pushed outward by `off`, extended past both ends."""
            a, b, u, out, L = edge(i)
            a2 = (a[0] - u[0] * s_over_prev + out[0] * off, a[1] - u[1] * s_over_prev + out[1] * off)
            b2 = (b[0] + u[0] * s_over_next + out[0] * off, b[1] + u[1] * s_over_next + out[1] * off)
            return a2, b2

        planes = []  # (edge index, tan) for the height function
        infilled = set()  # edges closed by derived infill from the eave line up to the roof
        kind = "flat"
        if axis_rect and len(sloped) == 2 and (sloped[1] - sloped[0]) == 2:
            kind = "gable"
            i, j = sloped
            ti, tj = math.tan(math.radians(slopes[i])), math.tan(math.radians(slopes[j]))
            D = edge((i + 1) % 4)[4]
            di = D * tj / (ti + tj)
            ridge = base + di * ti
            for k, tk, dk in ((i, ti, di), (j, tj, D - di)):
                a, b, u, out, L = edge(k)
                oprev, onext = over[(k - 1) % 4], over[(k + 1) % 4]
                e1, e2 = expanded(k, oprev, onext, over[k])
                r1_, r2_ = expanded(k, oprev, onext, -dk)
                zE = base - over[k] * tk
                self.add("roof_plane", klass, self.prism(
                    [(e1[0], e1[1], zE), (e2[0], e2[1], zE), (r2_[0], r2_[1], ridge), (r1_[0], r1_[1], ridge)],
                    (0, 0, -t), u), refs=[r["id"]], rule=rule, level=r.get("level"), material=mat,
                    assumptions=assume, roof_type="gable")
                planes.append((k, tk))
            for k in range(4):
                if k in sloped:
                    continue
                a, b, u, out, L = edge(k)
                w = self.wall_on_line(a, b, r.get("level"))
                if not w:
                    continue
                # ridge point on this edge line
                ai, bi, ui, outi, Li = edge(i)
                # distance along edge k from its start to the ridge = distance from edge i
                da = abs((a[0] - ai[0]) * outi[0] + (a[1] - ai[1]) * outi[1])
                s_r = abs(di - da)  # edge k starts on edge i's line (da=0) or edge j's line (da=D)
                rp = (a[0] + u[0] * s_r, a[1] + u[1] * s_r)
                f = WallFrame({"from": {"x": a[0], "y": a[1]}, "to": {"x": b[0], "y": b[1]}})
                tt = w.get("thickness_mm", 200)
                p = lambda q, z: (q[0] - f.nx * tt / 2, q[1] - f.ny * tt / 2, z)  # noqa: E731
                infilled.add(k)
                self.add("gable_infill", "derived", self.prism([p(a, base), p(b, base), p(rp, ridge)],
                                                               (f.nx * tt, f.ny * tt, 0), u),
                         refs=[r["id"], w["id"]], rule="gable_end_between_wall_and_roof", level=r.get("level"),
                         material=self.d["materials"]["wall"])
        elif axis_rect and len(sloped) == 1:
            kind = "shed"
            i = sloped[0]
            j = (i + 2) % 4
            ti = math.tan(math.radians(slopes[i]))
            D = edge((i + 1) % 4)[4]
            high = base + D * ti
            a, b, u, out, L = edge(i)
            oprev, onext = over[(i - 1) % 4], over[(i + 1) % 4]
            e1, e2 = expanded(i, oprev, onext, over[i])
            h1, h2 = expanded(i, oprev, onext, -(D + over[j]))
            self.add("roof_plane", klass, self.prism(
                [(e1[0], e1[1], base - over[i] * ti), (e2[0], e2[1], base - over[i] * ti),
                 (h2[0], h2[1], high + over[j] * ti), (h1[0], h1[1], high + over[j] * ti)], (0, 0, -t), u),
                refs=[r["id"]], rule=rule, level=r.get("level"), material=mat, assumptions=assume, roof_type="shed")
            planes.append((i, ti))
            for k in ((i + 1) % 4, (i + 3) % 4):  # side triangles where an exterior wall is under the side edge
                a2, b2, u2, out2, L2 = edge(k)
                w = self.wall_on_line(a2, b2, r.get("level"))
                if not w:
                    continue
                ei = edge(i)  # the end of this side edge that lies on the sloped (low) edge is the low end
                d_a = abs((a2[0] - ei[0][0]) * ei[3][0] + (a2[1] - ei[0][1]) * ei[3][1])
                low, hi = (a2, b2) if d_a < 1 else (b2, a2)
                f = WallFrame({"from": {"x": a2[0], "y": a2[1]}, "to": {"x": b2[0], "y": b2[1]}})
                tt = w.get("thickness_mm", 200)
                p = lambda q, z: (q[0] - f.nx * tt / 2, q[1] - f.ny * tt / 2, z)  # noqa: E731
                infilled.add(k)
                self.add("gable_infill", "derived", self.prism([p(low, base), p(hi, base), p(hi, high)],
                                                               (f.nx * tt, f.ny * tt, 0), u2),
                         refs=[r["id"], w["id"]], rule="shed_side_between_wall_and_roof", level=r.get("level"),
                         material=self.d["materials"]["wall"])
        elif axis_rect and len(sloped) == 4 and len({round(slopes[k], 3) for k in sloped}) == 1:
            kind = "hip"
            tn = math.tan(math.radians(slopes[0]))
            o = min(over)
            L0, L1 = edge(0)[4], edge(1)[4]
            long_i = 0 if L0 >= L1 else 1
            W = min(L0, L1) + 2 * o
            zE = base - o * tn
            ridge = zE + (W / 2) * tn
            corners = [expanded(k, o, o, o)[0] for k in range(4)]
            cx = sum(p[0] for p in pts) / 4
            cy = sum(p[1] for p in pts) / 4
            half = (max(L0, L1) - min(L0, L1)) / 2
            ux, uy = edge(long_i)[2]
            ra = (cx - ux * half, cy - uy * half)
            rb = (cx + ux * half, cy + uy * half)
            for k in range(4):
                c1, c2 = corners[k], corners[(k + 1) % 4]
                u = edge(k)[2]
                if k % 2 == long_i:
                    near = sorted([ra, rb], key=lambda q: math.dist(q, c1))
                    poly3 = [(c1[0], c1[1], zE), (c2[0], c2[1], zE), (near[1][0], near[1][1], ridge), (near[0][0], near[0][1], ridge)]
                else:
                    near = min([ra, rb], key=lambda q: math.dist(q, c1) + math.dist(q, c2))
                    poly3 = [(c1[0], c1[1], zE), (c2[0], c2[1], zE), (near[0], near[1], ridge)]
                self.add("roof_plane", klass, self.prism(poly3, (0, 0, -t), u), refs=[r["id"]], rule=rule,
                         level=r.get("level"), material=mat, assumptions=assume, roof_type="hip")
                planes.append((k, tn))
        else:
            if sloped or not axis_rect:
                assume.append("roof shape not reconstructable from OAS roof data: rendered flat")
                self.issues.append({"severity": "warning", "code": "roof_shape_unsupported", "refs": [r["id"]],
                                    "message": f"{r['id']}: slope configuration not reconstructable; rendered as a flat roof"})
            grown = Polygon(pts).buffer(max(over) if over else 0, join_style=2)
            gp = list(grown.exterior.coords)[:-1]
            self.add("roof_plane", klass, self.prism([(x, y, base + t) for x, y in gp], (0, 0, -t)), refs=[r["id"]],
                     rule=rule, level=r.get("level"), material=mat, assumptions=assume, roof_type="flat")
        self._roofs = getattr(self, "_roofs", [])
        self._roofs.append({"id": r["id"], "kind": kind, "base": base, "pts": pts, "planes": planes, "over": over,
                            "slopes": slopes, "defines": defines, "level": r.get("level"), "klass": klass,
                            "infilled": infilled,
                            "edge": edge, "poly": Polygon(pts)})

    def roof_height(self, roof, x, y):
        """Top-surface height of a roof at (x, y) (inside its footprint)."""
        if not roof["planes"]:
            return roof["base"] + self.d["roof_thickness_mm"]
        hs = []
        for k, tn in roof["planes"]:
            a, b, u, out, L = roof["edge"](k)
            inward = -((x - a[0]) * out[0] + (y - a[1]) * out[1])
            hs.append(roof["base"] + inward * tn)
        return min(hs)

    def wall_on_line(self, a, b, level):
        """An exterior plan wall of `level` lying on segment a-b (collinear, overlapping)."""
        seg = LineString([a, b])
        for w in self.doc.get("walls", []):
            if w.get("level") != level or not self.is_exterior_wall(w):
                continue
            wl = WallFrame(w).line()
            if wl.distance(seg) < 1 and wl.buffer(1).intersection(seg).length > 1:
                return w
        return None

    def default_roofs(self):
        """No roof in the plan: gable over each rectangular top-of-story region (inferred)."""
        dr = self.d["default_roof"]
        for idx, lv in enumerate(self.levels):
            enclosed = [self.poly[r] for r in self.rooms if self.rooms[r].get("level") == lv["id"]
                        and not self.is_outside(r)]
            if not enclosed:
                continue
            region = unary_union(enclosed)
            above = [self.poly[r] for r in self.rooms for l2 in self.levels[idx + 1:]
                     if self.rooms[r].get("level") == l2["id"] and not self.is_outside(r)]
            if above:
                region = region.difference(unary_union(above))
            for part in getattr(region, "geoms", [region]):
                if part.area < 1e6:
                    continue
                minx, miny, maxx, maxy = part.bounds
                rect = abs(part.area - (maxx - minx) * (maxy - miny)) < 1e3
                tops = [self.wall_z(w)[1] for w in self.doc.get("walls", []) if w.get("level") == lv["id"]]
                base = max(tops) if tops else lv.get("elevation_mm", 0) + self.d["default_wall_height_mm"]
                long_x = (maxx - minx) >= (maxy - miny)
                pts = [(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy)] if rect else list(part.exterior.coords)[:-1]
                n = len(pts)
                slopes = ([dr["pitch_deg"], 0, dr["pitch_deg"], 0] if long_x else [0, dr["pitch_deg"], 0, dr["pitch_deg"]]) if rect else [0] * n
                fake = {"id": f"default_roof_{lv['id']}_{len(getattr(self, '_roofs', []))}",
                        "boundary_polygon": {"points": [{"x": x, "y": y} for x, y in pts]}, "level": lv["id"],
                        "level_offset_mm": base - lv.get("elevation_mm", 0), "slope_angles": slopes,
                        "defines_slope": [s > 0 for s in slopes], "eave_overhang_mm": [dr["overhang_mm"]] * n}
                self.roof(fake, klass="inferred", rule="default_roof_when_plan_has_none",
                          assumptions=["plan defines no roof: default roof inferred"])
                self.issues.append({"severity": "info", "code": "roof_inferred", "refs": [fake["id"]],
                                    "message": f"plan has no roof entities; a default {'gable' if rect else 'flat'} roof was inferred over {lv['id']}"})

    def roof_support_and_overlaps(self):
        roofs = getattr(self, "_roofs", [])
        outside_polys = [self.poly[r] for r in self.rooms if self.is_outside(r) and r != "exterior"]
        for roof in roofs:
            pts = roof["pts"]
            n = len(pts)
            for k in range(n):
                a, b = pts[k], pts[(k + 1) % n]
                seg = LineString([a, b])
                L = seg.length
                # roof underside height along the boundary edge
                step = 250.0
                gaps = []
                for i in range(int(L // step) + 1):
                    s = min(L, i * step)
                    x, y = a[0] + (b[0] - a[0]) * s / L, a[1] + (b[1] - a[1]) * s / L
                    z_need = self.roof_height(roof, x, y) - self.d["roof_thickness_mm"]
                    if k in roof["infilled"] or (roof["kind"] == "gable" and not roof["defines"][k]):
                        z_need = roof["base"]  # the wall only has to reach the eave line; infill closes the rest
                    top = self.support_top(x, y)
                    over_outdoor = any(pp.buffer(1).contains(Point(x, y)) or pp.exterior.distance(Point(x, y)) < 1
                                       for pp in outside_polys)
                    if top is None:
                        if not over_outdoor:
                            gaps.append((s, None, z_need))
                    elif z_need - top > self.d["support_gap_tolerance_mm"]:
                        gaps.append((s, top, z_need))
                if not gaps:
                    continue
                # group consecutive samples
                runs, cur = [], [gaps[0]]
                for g in gaps[1:]:
                    if g[0] - cur[-1][0] <= step + 1:
                        cur.append(g)
                    else:
                        runs.append(cur)
                        cur = [g]
                runs.append(cur)
                for run in runs:
                    s0, s1 = run[0][0], run[-1][0]
                    if s1 - s0 < 1:
                        continue  # a lone corner sample: already reported on the adjacent edge
                    tops = [t for _, t, _ in run if t is not None]
                    worst = max((zn - t for _, t, zn in run if t is not None), default=0)
                    where = f"({a[0]:.0f},{a[1]:.0f})->({b[0]:.0f},{b[1]:.0f}) mm, {s0:.0f}-{s1:.0f} mm along it"
                    msg = (f"{roof['id']}: edge {k} {where}: "
                           + (f"roof underside up to {worst / 1000:.2f} m above the top of the exterior wall below"
                              if tops else "no exterior wall and no porch/deck below the roof edge"))
                    self.issues.append({"severity": "error", "code": "roof_edge_unsupported", "refs": [roof["id"]],
                                        "message": msg + " — the plan does not define what encloses or supports this.",
                                        "edge": k, "range_mm": [r1(s0), r1(s1)]})
        vol_of = {f"roof_{v['id']}": v for v in self.doc.get("massing", {}).get("volumes", [])}
        for i, ra in enumerate(roofs):
            for rb in roofs[i + 1:]:
                ov = self.roof_outline(ra).intersection(self.roof_outline(rb))
                if ov.area <= 1e4:
                    continue
                # Overlapping in plan is fine when one roof passes under the other (a porch roof tucked under
                # an eave). It is a conflict when the lower roof's top rises into the upper roof's build-up.
                t = self.d["roof_thickness_mm"]
                samples = list(ov.exterior.coords)[:-1] + [ov.representative_point().coords[0]]
                worst = min(max(self.roof_height(ra, x, y), self.roof_height(rb, x, y)) - t
                            - min(self.roof_height(ra, x, y), self.roof_height(rb, x, y)) for x, y in samples)
                if worst < 0:
                    va, vb = vol_of.get(ra["id"]), vol_of.get(rb["id"])
                    if va and vb and (va.get("attach_to") == vb["id"] or vb.get("attach_to") == va["id"]) \
                            and "cover" not in (va["role"], vb["role"]):
                        self.issues.append({"severity": "info", "code": "roof_junction", "refs": [ra["id"], rb["id"]],
                                            "message": f"{ra['id']} meets {rb['id']} in a valley junction (attached volumes); "
                                                       "the junction line is not modelled, planes intersect in 3D"})
                        continue
                    self.issues.append({"severity": "error", "code": "roofs_conflict", "refs": [ra["id"], rb["id"]],
                                        "message": f"{ra['id']} and {rb['id']} overlap over {ov.area / 1e6:.2f} m2 and intersect there "
                                                   f"(lower roof rises {-worst:.0f} mm into the upper one)."})

    def roof_clearances(self):
        """Roofs must not pass through windows/doors, and covered outdoor floors need headroom."""
        roofs = getattr(self, "_roofs", [])
        t = self.d["roof_thickness_mm"]
        for e in [e for e in self.elements if e["kind"] == "opening"]:
            g = e["geom"]
            o, u, n = g["origin"], g["u"], g["n"]
            sill, head = o[2], o[2] + g["height"]
            for rf in roofs:
                out = self.roof_outline(rf)
                for f in (0.1, 0.5, 0.9):
                    x = o[0] + u[0] * g["width"] * f + n[0] * (g["depth"] / 2 + 60)
                    y = o[1] + u[1] * g["width"] * f + n[1] * (g["depth"] / 2 + 60)
                    if not out.contains(Point(x, y)):
                        continue
                    top = self.roof_height(rf, x, y)
                    if top > sill + 1 and top - t < head - 1:
                        self.issues.append({"severity": "error", "code": "opening_obstructed_by_roof",
                                            "refs": [e["refs"][0], rf["id"]],
                                            "message": f"{rf['id']} passes across {e['refs'][0]} (roof z{top:.0f} between sill z{sill:.0f} and head z{head:.0f})"})
                        break
        need = self.d["min_covered_headroom_mm"]
        for e in [e for e in self.elements if e["kind"] == "deck"]:
            pts = [(p[0], p[1]) for p in e["geom"]["points"]]
            top = e["geom"]["points"][0][2]
            foot = Polygon(pts)
            for rf in roofs:
                # only the part of the deck actually under this roof (its outline with each edge's own
                # overhang; a valley extension into the host roof must not count as cover elsewhere)
                under = foot.intersection(self.roof_outline(rf))
                if under.area < 0.1 * foot.area:
                    continue
                low = min(self.roof_height(rf, x, y) for g in getattr(under, "geoms", [under])
                          for x, y in getattr(g, "exterior", g).coords) - t
                if low <= top:
                    continue  # this roof is below the deck (not covering it)
                # only the lowest covering roof above this deck matters
                if low - top < need and not any(
                        top < self.roof_height(r2, *foot.representative_point().coords[0]) - t < low
                        for r2 in roofs if r2 is not rf):
                    self.issues.append({"severity": "error", "code": "insufficient_headroom", "refs": [e["refs"][0], rf["id"]],
                                        "message": f"{rf['id']} leaves {low - top:.0f} mm headroom over {e['refs'][0]} (need {need} mm)"})

    @staticmethod
    def roof_outline(roof):
        """Roof footprint grown by each edge's own overhang."""
        pts, over = roof["pts"], roof["over"] or [0] * len(roof["pts"])
        n = len(pts)
        lines = []
        for k in range(n):
            a, b, u, out, L = roof["edge"](k)
            lines.append(((a[0] + out[0] * over[k], a[1] + out[1] * over[k]), u))
        corners = []
        for k in range(n):
            (p1, d1), (p2, d2) = lines[k - 1], lines[k]
            det = d1[0] * d2[1] - d1[1] * d2[0]
            if abs(det) < 1e-9:
                corners.append(p2)
                continue
            t = ((p2[0] - p1[0]) * d2[1] - (p2[1] - p1[1]) * d2[0]) / det
            corners.append((p1[0] + d1[0] * t, p1[1] + d1[1] * t))
        return Polygon(corners)

    def support_top(self, x, y):
        """Highest exterior wall/band top whose centerline passes through (x, y)."""
        best = None
        for f, s0, s1, top in self.support:
            s, off = f.project(x, y)
            if abs(off) <= f.w.get("thickness_mm", 200) / 2 + 1 and s0 - 1 <= s <= s1 + 1:
                best = top if best is None else max(best, top)
        return best

    # ------------------------------------------------------------------ railings, stairs
    def railings(self):
        outside = [(r, self.poly[r]) for r in self.rooms if self.is_outside(r) and r != "exterior"]
        for rl in self.doc.get("railings", []):
            if rl.get("host_type", "floor") != "floor":
                continue
            path = LineString([(p["x"], p["y"]) for p in rl["path"]["points"]])
            hosts = [r for r, p in outside if self.rooms[r].get("level") == rl.get("level")
                     and p.buffer(1).contains(path)]
            if not hosts:
                continue
            base = self.level_elev(rl.get("level")) + rl.get("base_offset_mm", 0)
            self.add("railing", "plan", {"type": "railing",
                                         "path": [pt3(p["x"], p["y"], base) for p in rl["path"]["points"]],
                                         "height": self.d["railing_height_mm"]},
                     refs=[rl["id"], *hosts], rule="railing_from_oas", level=rl.get("level"),
                     material=self.d["materials"]["railing"],
                     assumptions=[f"railing height {self.d['railing_height_mm']} mm and baluster pattern (inferred)"])

    def exterior_stairs(self):
        for rid, r in self.rooms.items():
            if r.get("usage") != "stair":
                continue
            p = self.poly[rid]
            touches_outside = any(self.is_outside(o) and self.poly[o].buffer(1).intersects(p) and
                                  self.rooms[o].get("level") == r.get("level") for o in self.rooms if o != "exterior")
            if not touches_outside and "exterior" not in r.get("tags", []):
                continue
            nxt = self.next_level(r.get("level"))
            z0 = self.level_elev(r.get("level"))
            z1 = self.level_elev(nxt) if nxt else z0 + self.d["default_wall_height_mm"]
            pts = [(x, y) for x, y in list(p.exterior.coords)[:-1]]
            self.add("stair", "plan", self.prism([(x, y, z0) for x, y in pts], (0, 0, z1 - z0)), refs=[rid],
                     rule="exterior_stair_envelope", level=r.get("level"), material=self.d["materials"]["deck"],
                     assumptions=["stair drawn as its plan envelope; tread geometry not in OAS"])

    # ------------------------------------------------------------------ inferred supports
    def supports(self):
        """Posts/beams under elevated exterior floors and roofs that are not carried by walls."""
        size, spacing = self.d["post_size_mm"], self.d["post_max_spacing_mm"]
        ext_lines = unary_union([WallFrame(w).line() for w in self.doc.get("walls", []) if self.is_exterior_wall(w)]
                                or [LineString()]).buffer(1)
        groups = []
        for rid in [r for r in self.rooms if self.is_outside(r) and r != "exterior"]:
            for g in groups:
                if self.poly[g[0]].symmetric_difference(self.poly[rid]).area < 1e4:
                    g.append(rid)
                    break
            else:
                groups.append([rid])
        roofs = getattr(self, "_roofs", [])
        for g in groups:
            g.sort(key=lambda r: self.level_elev(self.rooms[r].get("level")))
            foot = self.poly[g[0]]
            low, top_room = g[0], g[-1]
            elevated = self.level_elev(self.rooms[top_room].get("level")) > self.level_elev(self.levels[0]["id"])
            cover = [rf for rf in roofs if rf["poly"].buffer(max(rf["over"] or [0]) + 1).contains(foot.representative_point())]
            if not elevated and not cover:
                continue
            decks = {e["refs"][0]: e for e in self.elements if e["kind"] == "deck"}
            low_level_ground = self.level_elev(self.rooms[low].get("level")) <= self.level_elev(self.levels[0]["id"])
            z_bottom = decks[low]["geom"]["points"][0][2] if (low in decks and low_level_ground) else self.grade
            coords = list(foot.exterior.coords)
            free = []
            for i in range(len(coords) - 1):
                seg = LineString([coords[i], coords[i + 1]])
                rest = seg.difference(ext_lines)
                for part in getattr(rest, "geoms", [rest]):
                    if part.length > 300:
                        free.append(part)
            supported_dirs = []
            for i in range(len(coords) - 1):
                seg = LineString([coords[i], coords[i + 1]])
                if seg.length and seg.difference(ext_lines).length < 1:
                    supported_dirs.append(((coords[i + 1][0] - coords[i][0]) / seg.length,
                                           (coords[i + 1][1] - coords[i][1]) / seg.length))
            posts = []
            for part in free:
                (x0, y0), (x1, y1) = part.coords[0], part.coords[-1]
                d = ((x1 - x0) / part.length, (y1 - y0) / part.length)
                # joists/rafters span from a wall-carried edge to the parallel free edge (the beam line):
                # only that line needs intermediate posts; other free edges get posts at their ends.
                beam_line = any(abs(d[0] * s[1] - d[1] * s[0]) < 1e-3 for s in supported_dirs)
                n = max(1, math.ceil(part.length / spacing)) if beam_line else 1
                for k in range(n + 1):
                    x, y = x0 + (x1 - x0) * k / n, y0 + (y1 - y0) * k / n
                    if ext_lines.buffer(size).contains(Point(x, y)):
                        continue  # this end is carried by the wall (ledger)
                    # pull inside the footprint by half a post
                    for dx, dy in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
                        cx, cy = x + dx * size / 2, y + dy * size / 2
                        if foot.buffer(-size / 2 + 1).buffer(1).contains(Point(cx, cy)):
                            break
                    if all(math.dist((cx, cy), q) > size for q in posts):
                        posts.append((cx, cy))
            posts = self.clear_openings(posts, foot, size, spacing)
            for cx, cy in posts:
                if cover:
                    z_top = min(self.roof_height(rf, cx, cy) for rf in cover) - self.d["roof_thickness_mm"]
                else:
                    z_top = decks[top_room]["geom"]["points"][0][2] - self.d["deck_thickness_mm"] if top_room in decks else z_bottom
                h = size / 2
                self.add("post", "inferred", self.prism(
                    [(cx - h, cy - h, z_bottom), (cx + h, cy - h, z_bottom), (cx + h, cy + h, z_bottom), (cx - h, cy + h, z_bottom)],
                    (0, 0, z_top - z_bottom)), refs=g, rule="support_posts_at_unwalled_edges", level=self.rooms[low].get("level"),
                    material=self.d["materials"]["post"],
                    assumptions=[f"{size} mm posts at <= {spacing} mm spacing along edges without walls (inferred)"])
            # beams under elevated decks along free edges
            for rid in g:
                if rid not in decks or self.level_elev(self.rooms[rid].get("level")) <= self.level_elev(self.levels[0]["id"]):
                    continue
                z = decks[rid]["geom"]["points"][0][2] - self.d["deck_thickness_mm"]
                for part in free:
                    self.beam(part, z - self.d["beam_depth_mm"], z, foot, g, "beam_under_elevated_deck")
            for rf in cover:  # beam under the roof edge where the roof underside is level along it
                for part in free:
                    (x0, y0), (x1, y1) = part.coords[0], part.coords[-1]
                    za = self.roof_height(rf, x0, y0) - self.d["roof_thickness_mm"]
                    zb = self.roof_height(rf, x1, y1) - self.d["roof_thickness_mm"]
                    if abs(za - zb) < 10:
                        self.beam(part, min(za, zb) - self.d["beam_depth_mm"], min(za, zb), foot, g + [rf["id"]], "beam_under_roof_edge")

    def clear_openings(self, posts, foot, size, spacing):
        """Composition rule: a post must not stand in front of a door or window of the wall it faces.
        Shift it sideways (along the beam line) to the nearest clear position; spacing may grow up to
        1.25x the maximum. Posts that cannot be cleared stay and are reported."""
        ops = [e for e in self.elements if e["kind"] == "opening"]
        out = []
        for cx, cy in posts:
            blocked = []
            for e in ops:
                g = e["geom"]
                o, u, n = g["origin"], g["u"], g["n"]
                s, off = (cx - o[0]) * u[0] + (cy - o[1]) * u[1], (cx - o[0]) * n[0] + (cy - o[1]) * n[1]
                if 0 < off < 6000 and -size / 2 - 150 < s < g["width"] + size / 2 + 150:
                    blocked.append((e, s))
            if not blocked:
                out.append((cx, cy))
                continue
            e, s = blocked[0]
            g = e["geom"]
            best = None
            for target in (-size / 2 - 150, g["width"] + size / 2 + 150):
                d = target - s
                nx_, ny_ = cx + g["u"][0] * d, cy + g["u"][1] * d
                if foot.buffer(-size / 2 + 1).buffer(1).contains(Point(nx_, ny_)) and \
                        all(math.dist((nx_, ny_), q) > size for q in out) and (best is None or abs(d) < abs(best[2])):
                    best = (nx_, ny_, d)
            if best:
                out.append((best[0], best[1]))
            else:
                out.append((cx, cy))
                self.issues.append({"severity": "warning", "code": "post_in_front_of_opening", "refs": [e["refs"][0]],
                                    "message": f"porch post could not be moved clear of {e['refs'][0]}"})
        return out

    def beam(self, part, z0, z1, foot, refs, rule):
        size = self.d["post_size_mm"]
        (x0, y0), (x1, y1) = part.coords[0], part.coords[-1]
        L = part.length
        u = ((x1 - x0) / L, (y1 - y0) / L)
        n = (-u[1], u[0])
        mid = ((x0 + x1) / 2 + n[0] * size / 2, (y0 + y1) / 2 + n[1] * size / 2)
        if not foot.contains(Point(mid)):
            n = (-n[0], -n[1])
        f = WallFrame({"from": {"x": x0 + n[0] * size / 2, "y": y0 + n[1] * size / 2},
                       "to": {"x": x1 + n[0] * size / 2, "y": y1 + n[1] * size / 2}})
        self.add("beam", "inferred", self.wall_rect(f, 0, f.L, z0, z1, size), refs=refs, rule=rule,
                 material=self.d["materials"]["beam"], assumptions=["beam size inferred"])

    # ------------------------------------------------------------------ site context (inferred)
    def site(self):
        s = self.d["site"]
        if s.get("ground"):
            minx, miny, maxx, maxy = unary_union(list(self.poly.values())).bounds
            m = 60000
            self.add("ground", "inferred", self.prism([(minx - m, miny - m, self.grade), (maxx + m, miny - m, self.grade),
                                                       (maxx + m, maxy + m, self.grade), (minx - m, maxy + m, self.grade)],
                                                      (0, 0, -10)), rule="site_ground_plane", material=self.d["materials"]["ground"],
                     assumptions=["flat site at inferred grade"])
        if s.get("driveway"):
            doors = [e for e in self.elements if e["kind"] == "opening" and e.get("style") == "vehicle_door"]
            by_wall = {}
            for e in doors:
                by_wall.setdefault(e["refs"][1], []).append(e)
            for wid, es in by_wall.items():
                f = WallFrame(self.walls[wid])
                sgn = self.outward_sign(self.walls[wid])
                ss = [f.project(e["geom"]["origin"][0], e["geom"]["origin"][1])[0] for e in es]
                s0 = min(ss) - 900
                s1 = max(s + e["geom"]["width"] for s, e in zip(ss, es)) + 900
                t = self.walls[wid].get("thickness_mm", 200)
                a0, a1 = f.at(s0, sgn * t / 2), f.at(s1, sgn * t / 2)
                b0, b1 = f.at(s0, sgn * (t / 2 + s["driveway_length_mm"])), f.at(s1, sgn * (t / 2 + s["driveway_length_mm"]))
                z = self.grade + 20
                self.add("driveway", "inferred", self.prism([(a0[0], a0[1], z), (a1[0], a1[1], z), (b1[0], b1[1], z), (b0[0], b0[1], z)],
                                                            (0, 0, -20)), refs=[e["refs"][0] for e in es],
                         rule="driveway_in_front_of_vehicle_doors", material=self.d["materials"]["driveway"])

    # ------------------------------------------------------------------ orientation, facades, views
    def front_orientation(self):
        override = self.d.get("front")
        names = {"south": (0, -1), "north": (0, 1), "east": (1, 0), "west": (-1, 0)}
        if override:
            v = names.get(override, override)
            return {"normal": [v[0], v[1]], "class": "design", "rule": "front override in exterior design spec"}
        ops = [e for e in self.elements if e["kind"] == "opening"]
        for style in ("entry_door", "door", "glass_door"):
            cand = [e for e in ops if e.get("style") == style and e["level"] == self.levels[0]["id"]]
            if cand:
                n = cand[0]["geom"]["n"]
                ang = math.degrees(math.atan2(n[1], n[0]))
                snap = round(ang / 90) * 90
                if abs(ang - snap) < 10:
                    n = [round(math.cos(math.radians(snap))), round(math.sin(math.radians(snap)))]
                return {"normal": [n[0], n[1]], "class": "inferred",
                        "rule": f"facade containing the primary {style.replace('_', ' ')} ({cand[0]['refs'][0]})"}
        return {"normal": [0, -1], "class": "inferred", "rule": "no exterior door found: -Y assumed"}

    def facades(self, front):
        fn = front["normal"]
        rgt = viewer_right(fn)
        out = {}
        for e in self.elements:
            if e["kind"] != "opening":
                continue
            n = e["geom"]["n"]
            dot_f = n[0] * fn[0] + n[1] * fn[1]
            dot_r = n[0] * rgt[0] + n[1] * rgt[1]
            if dot_f > 0.7:
                key = "front"
            elif dot_f < -0.7:
                key = "rear"
            elif dot_r > 0.7:
                key = "right"
            elif dot_r < -0.7:
                key = "left"
            else:
                key = "other"
            out.setdefault(key, []).append({"id": e["refs"][0], "style": e["style"], "level": e["level"],
                                            "width_mm": e["geom"]["width"], "height_mm": e["geom"]["height"],
                                            "sill_z_mm": e["geom"]["origin"][2]})
        return out

    def views(self, front):
        pts = []
        for e in self.elements:
            if e["class"] == "inferred" and e["kind"] in ("ground", "driveway"):
                continue
            pts += bbox_points(e)
        xs, ys, zs = [p[0] for p in pts], [p[1] for p in pts], [p[2] for p in pts]
        c = [(min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, (min(zs) + max(zs)) / 2]
        size = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))
        f = front["normal"]
        rgt = viewer_right(f)
        D = size * 2.2

        def ortho(dirv, name):
            return {"name": name, "type": "orthographic", "direction": [dirv[0], dirv[1], 0],
                    "target": [r1(v) for v in c], "position": [r1(c[0] + dirv[0] * D), r1(c[1] + dirv[1] * D), r1(c[2])]}
        return {
            "front": ortho(f, "Front elevation"),
            "rear": ortho((-f[0], -f[1]), "Rear elevation"),
            "left": ortho((-rgt[0], -rgt[1]), "Left elevation"),
            "right": ortho(rgt, "Right elevation"),
            "perspective": {"name": "Front perspective", "type": "perspective", "fov": 36,
                            "position": [r1(c[0] + f[0] * size * 1.3 - rgt[0] * size * 0.75),
                                         r1(c[1] + f[1] * size * 1.3 - rgt[1] * size * 0.75), r1(self.grade + 4200)],
                            "target": [r1(v) for v in (c[0], c[1], c[2] - size * 0.05)]},
            "aerial": {"name": "Aerial", "type": "perspective", "fov": 36,
                       "position": [r1(c[0] + f[0] * size * 0.95 - rgt[0] * size * 0.8),
                                    r1(c[1] + f[1] * size * 0.95 - rgt[1] * size * 0.8), r1(c[2] + size * 1.05)],
                       "target": [r1(v) for v in c]},
        }

    def summary(self):
        out = {}
        for e in self.elements:
            k = out.setdefault(e["class"], {})
            k[e["kind"]] = k.get(e["kind"], 0) + 1
        return out


def viewer_right(front_normal):
    """Right-hand direction of a person standing outside the front facade, looking at it.
    They look along -front; with Z up, right = forward x up = (-f_y, f_x)."""
    return (-front_normal[1], front_normal[0])


def bbox_points(e):
    g = e["geom"]
    if g["type"] == "prism":
        ps = g["points"] + [[p[0] + g["extrude"][0], p[1] + g["extrude"][1], p[2] + g["extrude"][2]] for p in g["points"]]
        return ps
    if g["type"] == "opening":
        o, u, n = g["origin"], g["u"], g["n"]
        W, H, t = g["width"], g["height"], g["depth"]
        out = []
        for s in (0, W):
            for off in (-t / 2, t / 2):
                for z in (0, H):
                    out.append([o[0] + u[0] * s + n[0] * off, o[1] + u[1] * s + n[1] * off, o[2] + z])
        return out
    if g["type"] == "railing":
        return [[p[0], p[1], p[2] + dz] for p in g["path"] for dz in (0, g["height"])]
    return []


def bbox(e):
    ps = bbox_points(e)
    return [min(p[i] for p in ps) for i in range(3)], [max(p[i] for p in ps) for i in range(3)]


def build_exterior_model(doc: dict, design: dict | None = None) -> dict:
    return ExteriorModelBuilder(doc, design).build()


__all__ = ["DEFAULT_DESIGN", "build_exterior_model", "bbox", "bbox_points", "EXTERIOR_USAGES"]
