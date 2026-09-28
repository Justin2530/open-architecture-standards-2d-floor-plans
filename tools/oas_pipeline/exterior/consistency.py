"""Consistency check: structured floor plan (OAS-Layout) vs 3D exterior representation.

Expected values are computed *directly from the plan* (not by calling the model builder),
then compared with the exterior model's element geometry. If a renderer audit is supplied
(world-space bounding boxes of what was actually drawn), every drawn element is compared
with its model element too, so a renderer cannot silently move or resize anything.

Checked (tolerance 2 mm unless noted):

* levels / floor heights        wall bases, deck tops and slab tops sit at plan elevations
* building footprint            ground-floor enclosed outline vs 3D exterior walls
* exterior wall positions       each plan exterior wall fully represented: centreline, thickness,
                                base, top; wall panels + openings tile the wall face exactly
* story extents                 per-level enclosed outline vs that level's 3D walls; inferred
                                elements that visually extend a story are flagged
* exterior doors / windows /
  garage doors                  one 3D opening per plan opening, same position, size, sill
* porches / balconies / decks   footprint polygon and floor elevation match the plan room/slab
* railings                      same path and base elevation
* stairs (externally relevant)  plan envelope represented
* roofs                         every plan roof represented, eave at the plan's roof base
* provenance                    every non-plan element is labelled derived/inferred with a rule;
                                inferred elements do not intrude into plan walls or rooms
* renderer audit (optional)     drawn bounding boxes match the model
"""
from __future__ import annotations

import math
from collections import defaultdict

from shapely.geometry import LineString, Point, Polygon, box as sbox
from shapely.ops import unary_union

from ..validate import Report
from .model import EXTERIOR_USAGES, bbox, bbox_points

TOL = 2.0


class Check:
    def __init__(self):
        self.rep = Report()
        self.table = []  # (area, item, plan value, 3D value, status)

    def row(self, area, item, plan, model, ok, severity="error", note=""):
        self.table.append({"check": area, "item": item, "plan": plan, "model": model,
                           "status": "ok" if ok else severity, "note": note})
        if not ok:
            (self.rep.errors if severity == "error" else self.rep.warnings).append(f"[{area}] {item}: plan {plan} vs 3D {model}{' — ' + note if note else ''}")


def _wall_frame(w):
    fx, fy = w["from"]["x"], w["from"]["y"]
    L = math.hypot(w["to"]["x"] - fx, w["to"]["y"] - fy)
    u = ((w["to"]["x"] - fx) / L, (w["to"]["y"] - fy) / L)
    n = (-u[1], u[0])
    return fx, fy, L, u, n


def _proj(fr, x, y):
    fx, fy, L, u, n = fr
    return (x - fx) * u[0] + (y - fy) * u[1], (x - fx) * n[0] + (y - fy) * n[1]


def check_consistency(doc: dict, model: dict, audit: dict | None = None) -> tuple[Report, list]:
    c = Check()
    rooms = {r["id"]: r for r in doc["rooms"]}
    poly = {r["id"]: Polygon([(p["x"], p["y"]) for p in r["boundary_polygon"]["points"]]) for r in doc["rooms"]}
    elev = {lv["id"]: lv.get("elevation_mm", 0) for lv in doc.get("levels", [])}
    levels = sorted(elev, key=elev.get)
    outside = lambda r: r == "exterior" or rooms.get(r, {}).get("usage") in EXTERIOR_USAGES  # noqa: E731
    els = model["elements"]
    by_kind = defaultdict(list)
    for e in els:
        by_kind[e["kind"]].append(e)
    refs = defaultdict(list)
    for e in els:
        for r in e.get("refs", []):
            refs[r].append(e)

    # ---------------------------------------------------------------- levels
    mlev = {lv["id"]: lv["elevation_mm"] for lv in model.get("levels", [])}
    for lid in levels:
        c.row("floor heights", f"{lid} elevation", elev[lid], mlev.get(lid), abs(elev[lid] - mlev.get(lid, math.inf)) <= TOL)

    # ---------------------------------------------------------------- exterior walls
    ext_walls = [w for w in doc.get("walls", [])
                 if any(outside(r) for r in w.get("adjacent_rooms", [])) and not all(outside(r) for r in w.get("adjacent_rooms", []))]
    openings_by_wall = defaultdict(list)
    for o in doc.get("openings", []):
        openings_by_wall[o["in_wall"]].append(o)
    ext_ids = {w["id"] for w in ext_walls}
    for w in ext_walls:
        fr = _wall_frame(w)
        fx, fy, L, u, n = fr
        t = w.get("thickness_mm", 200)
        base = elev.get(w.get("level"), 0) + w.get("base_offset_mm", 0)
        top = base + w.get("wall_height_mm", 0) if "wall_height_mm" in w else None
        panels = [e for e in refs[w["id"]] if e["kind"] == "wall_panel"]
        ops = [e for e in refs[w["id"]] if e["kind"] == "opening"]
        if not panels:
            c.row("exterior walls", w["id"], "exterior wall", "missing", False)
            continue
        bad, rects = [], []
        for e in panels:
            ps = bbox_points(e)
            proj = [_proj(fr, p[0], p[1]) for p in ps]
            offs = [q[1] for q in proj]
            if abs(min(offs) + t / 2) > TOL or abs(max(offs) - t / 2) > TOL:
                bad.append(f"{e['id']} offset/thickness {min(offs):.0f}..{max(offs):.0f}")
            ss, zs = [q[0] for q in proj], [p[2] for p in ps]
            rects.append(sbox(min(ss), min(zs), max(ss), max(zs)))
        for e in ops:
            g = e["geom"]
            s0, off = _proj(fr, g["origin"][0], g["origin"][1])
            rects.append(sbox(s0, g["origin"][2], s0 + g["width"], g["origin"][2] + g["height"]))
        face = unary_union(rects)
        zmin = min(r.bounds[1] for r in rects)
        zmax = max(r.bounds[3] for r in rects)
        expected = sbox(0, base, L, top if top is not None else zmax)
        overlap = sum(r.area for r in rects) - face.area
        gap = expected.difference(face).area
        extra = face.difference(expected).area
        ok = not bad and abs(zmin - base) <= TOL and (top is None or abs(zmax - top) <= TOL) \
            and gap < 1e3 and extra < 1e3 and overlap < 1e3
        c.row("exterior walls", w["id"],
              f"L={L:.0f} t={t} z={base}..{top}", f"z={zmin:.0f}..{zmax:.0f} gap={gap / 1e6:.3f}m2 extra={extra / 1e6:.3f}m2",
              ok, note="; ".join(bad))
    for e in by_kind["wall_panel"]:
        if e["class"] == "plan" and not set(e["refs"]) & ext_ids:
            c.row("exterior walls", e["id"], "no such exterior wall", e["refs"], False, note="plan-class wall panel without a plan exterior wall")

    # ---------------------------------------------------------------- footprint and story extents
    def level_outline(lid):
        rs = [poly[r] for r in rooms if rooms[r].get("level") == lid and not outside(r)]
        return unary_union(rs) if rs else Polygon()

    def walls_outline(lid):
        ps = [e for e in by_kind["wall_panel"] + by_kind["opening"] if e.get("level") == lid and e["class"] == "plan"]
        pts = [p for e in ps for p in bbox_points(e)]
        return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)) if pts else None

    def cover_ratio(outline, lid):
        # share of the enclosed outline's perimeter lying inside some exterior wall/opening footprint of that level
        shapes = []
        for e in by_kind["wall_panel"] + by_kind["opening"]:
            if e.get("level") != lid or e["class"] != "plan":
                continue
            ps = bbox_points(e)
            from shapely.geometry import MultiPoint
            shapes.append(MultiPoint([(p[0], p[1]) for p in ps]).convex_hull.buffer(1))
        if not shapes or outline.is_empty:
            return 0.0
        cov = unary_union(shapes)
        bnd = outline.boundary
        return bnd.intersection(cov).length / bnd.length

    for lid in levels:
        out = level_outline(lid)
        if out.is_empty:
            continue
        wb = walls_outline(lid)
        minx, miny, maxx, maxy = out.bounds
        dims_plan = f"{(maxx - minx) / 304.8:.2f}' x {(maxy - miny) / 304.8:.2f}'"
        if wb is None:
            c.row("story extents", lid, dims_plan, "no exterior walls", False)
            continue
        # 3D wall outer faces extend half a wall beyond the centreline outline
        dx = [(wb[0] - minx), (wb[2] - maxx), (wb[1] - miny), (wb[3] - maxy)]
        ts = [w.get("thickness_mm", 200) / 2 for w in ext_walls if w.get("level") == lid]
        h = max(ts) if ts else 0
        ok_dims = all(abs(abs(d) - h) <= TOL + 1 or abs(d) <= TOL for d in dx)
        cov = cover_ratio(out, lid)
        dims_model = f"{(wb[2] - wb[0] - 2 * h) / 304.8:.2f}' x {(wb[3] - wb[1] - 2 * h) / 304.8:.2f}' (centreline)"
        c.row("story extents", f"{lid} enclosed outline", dims_plan, dims_model + f", perimeter walled {cov:.1%}",
              ok_dims and cov > 0.995)
        if lid == levels[0]:
            c.row("building footprint", "ground floor incl. garage", f"{out.area / 1e6:.2f} m2 ({out.area / 1e6 * 10.7639:.0f} sf) {dims_plan}",
                  dims_model, ok_dims and cov > 0.995)
        # inferred massing that makes a story look bigger than the plan
        zlo = elev[lid]
        nxt = levels[levels.index(lid) + 1] if lid != levels[-1] else None
        for e in els:
            if e["class"] != "inferred" or e["kind"] in ("ground", "driveway", "post", "beam"):
                continue
            lo, hi = bbox(e)
            if hi[2] <= zlo + 1 or (nxt and lo[2] >= elev[nxt] - 1):
                continue
            fp = sbox(lo[0], lo[1], hi[0], hi[1])
            if fp.difference(out.buffer(200)).area > 1e4:
                c.row("story extents", f"{lid}: {e['id']}", "outside plan story outline", e["rule"], False, severity="warning",
                      note="inferred element visually extends this story beyond the plan")

    # ---------------------------------------------------------------- openings
    counts = defaultdict(lambda: [0, 0])
    for w in ext_walls:
        fr = _wall_frame(w)
        base = elev.get(w.get("level"), 0) + w.get("base_offset_mm", 0)
        for o in openings_by_wall[w["id"]]:
            kind = "window" if o["opening_type"] == "window" else "door"
            m = [e for e in refs[o["id"]] if e["kind"] == "opening"]
            is_vehicle = kind == "door" and any(rooms.get(r, {}).get("usage") == "garage" for r in o.get("connects_rooms", [])) \
                and o["width_mm"] >= model["design"]["vehicle_door_min_width_mm"]
            cat = "garage doors" if is_vehicle else ("windows" if kind == "window" else "exterior doors")
            counts[cat][0] += 1
            if len(m) != 1:
                c.row(cat, o["id"], "1 opening", f"{len(m)} found", False)
                continue
            counts[cat][1] += 1
            g = m[0]["geom"]
            s, off = _proj(fr, g["origin"][0], g["origin"][1])
            sill = base + o.get("sill_height_mm", 0)
            exp = (o["position_along_wall_mm"], o["width_mm"], o["height_mm"], sill)
            got = (s, g["width"], g["height"], g["origin"][2])
            ok = all(abs(a - b) <= TOL for a, b in zip(exp, got)) and abs(off) <= TOL
            if is_vehicle and m[0].get("style") != "vehicle_door":
                ok = False
            c.row(cat, o["id"], f"at {exp[0]} w{exp[1]} h{exp[2]} sill z{exp[3]}",
                  f"at {got[0]:.0f} w{got[1]} h{got[2]:.0f} sill z{got[3]:.0f} ({m[0].get('style')})", ok)
    for cat, (p, mm) in counts.items():
        c.row(cat, "count", p, mm, p == mm)
    plan_ops = {o["id"] for w in ext_walls for o in openings_by_wall[w["id"]]}
    for e in by_kind["opening"]:
        if e["refs"][0] not in plan_ops:
            c.row("exterior doors", e["id"], "no plan opening", e["refs"], False)

    # ---------------------------------------------------------------- porches, balconies, decks
    slabs = {s["id"]: s for s in doc.get("floor_slabs", [])}
    for rid, r in rooms.items():
        if not outside(rid) or rid == "exterior":
            continue
        cat = {"porch": "porches", "balcony": "balconies"}.get(r.get("usage"), "decks")
        decks = [e for e in refs[rid] if e["kind"] == "deck"]
        if len(decks) != 1:
            c.row(cat, rid, "1 deck", f"{len(decks)} found", False)
            continue
        e = decks[0]
        top_pts = e["geom"]["points"]
        fp = Polygon([(p[0], p[1]) for p in top_pts])
        diff = fp.symmetric_difference(poly[rid]).area
        slab = next((s for s in slabs.values() if s.get("level") == r.get("level") and
                     Polygon([(p["x"], p["y"]) for p in s["boundary_polygon"]["points"]]).symmetric_difference(poly[rid]).area < 1e4), None)
        z_exp = elev.get(r.get("level"), 0) + (slab.get("height_above_level_mm", 0) if slab else 0)
        z_got = top_pts[0][2]
        minx, miny, maxx, maxy = poly[rid].bounds
        c.row(cat, rid, f"{(maxx - minx) / 304.8:.2f}' x {(maxy - miny) / 304.8:.2f}' at z{z_exp} ({r.get('level')})",
              f"footprint diff {diff / 1e6:.4f} m2, top z{z_got:.0f}",
              diff < 1e3 and abs(z_exp - z_got) <= TOL and all(abs(p[2] - z_got) <= TOL for p in top_pts))
        if r.get("usage") == "balcony":
            access = [o["id"] for o in doc.get("openings", []) if o["opening_type"] == "door" and rid in o.get("connects_rooms", [])]
            c.row(cat, f"{rid} access", "door from the house", access or "none", bool(access))
    for e in by_kind["deck"]:
        if not any(r in rooms and outside(r) for r in e["refs"]):
            c.row("decks", e["id"], "no exterior room", e["refs"], False)

    # ---------------------------------------------------------------- railings
    for rl in doc.get("railings", []):
        m = [e for e in refs[rl["id"]] if e["kind"] == "railing"]
        if not m:
            continue  # interior railing
        pts = [(p["x"], p["y"], elev.get(rl.get("level"), 0) + rl.get("base_offset_mm", 0)) for p in rl["path"]["points"]]
        got = m[0]["geom"]["path"]
        ok = len(got) == len(pts) and all(math.dist(a, b) <= TOL for a, b in zip(pts, got))
        c.row("railings", rl["id"], f"{len(pts)}-point path", f"{len(got)}-point path", ok)

    # ---------------------------------------------------------------- stairs
    ext_stairs = []
    for rid, r in rooms.items():
        if r.get("usage") != "stair":
            continue
        if "exterior" in r.get("tags", []) or any(outside(o) and o != "exterior" and rooms[o].get("level") == r.get("level")
                                                    and poly[o].buffer(1).intersects(poly[rid]) for o in rooms):
            ext_stairs.append(rid)
            ok = any(e["kind"] == "stair" for e in refs[rid])
            c.row("stairs", rid, "exterior stair", "represented" if ok else "missing", ok)
    n_int = sum(1 for r in rooms.values() if r.get("usage") == "stair") - len(ext_stairs)
    c.row("stairs", "externally relevant", f"{len(ext_stairs)} exterior, {n_int} interior (not visible outside)",
          f"{len(by_kind['stair'])} drawn", len(by_kind["stair"]) == len(ext_stairs))

    # ---------------------------------------------------------------- roofs
    for rf in doc.get("roofs", []):
        planes = [e for e in refs[rf["id"]] if e["kind"] == "roof_plane"]
        base = elev.get(rf.get("level"), 0) + rf.get("level_offset_mm", 0)
        if not planes:
            c.row("roofs", rf["id"], "roof", "missing", False)
            continue
        # every sloped edge's eave line (on the plan boundary) must sit at the roof base
        pts = [(p["x"], p["y"]) for p in rf["boundary_polygon"]["points"]]
        bad = []
        for k, (a, b) in enumerate(zip(pts, pts[1:] + pts[:1])):
            if not rf.get("defines_slope", [False] * len(pts))[k]:
                continue
            mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            zs = []
            for e in planes:
                P3 = e["geom"]["points"]
                # plane through the top-surface points; evaluate z at mid
                (x1, y1, z1), (x2, y2, z2), (x3, y3, z3) = P3[0], P3[1], P3[2]
                nx = (y2 - y1) * (z3 - z1) - (z2 - z1) * (y3 - y1)
                ny = (z2 - z1) * (x3 - x1) - (x2 - x1) * (z3 - z1)
                nz = (x2 - x1) * (y3 - y1) - (y2 - y1) * (x3 - x1)
                if abs(nz) < 1e-9 or not Polygon([(p[0], p[1]) for p in P3]).buffer(2).contains(Point(mid)):
                    continue
                zs.append(z1 - (nx * (mid[0] - x1) + ny * (mid[1] - y1)) / nz)
            if not zs or min(abs(z - base) for z in zs) > TOL:
                bad.append(f"edge {k}: eave z {['%.0f' % z for z in zs]}")
        c.row("roofs", rf["id"], f"base z{base}, {len(pts)} edges", f"{len(planes)} plane(s) [{planes[0].get('roof_type')}]",
              not bad, note="; ".join(bad))

    # ---------------------------------------------------------------- provenance
    wall_fp = unary_union([Polygon([(p[0], p[1]) for p in e["geom"]["points"][:2]] +
                                   [(p[0] + e["geom"]["extrude"][0], p[1] + e["geom"]["extrude"][1]) for p in e["geom"]["points"][1::-1]]).buffer(0)
                           for e in by_kind["wall_panel"] if e["class"] == "plan"] or [Polygon()])
    for e in els:
        if e["class"] not in ("plan", "derived", "inferred"):
            c.row("provenance", e["id"], "plan|derived|inferred", e["class"], False)
        if e["class"] != "plan" and not e.get("rule"):
            c.row("provenance", e["id"], "rule required", "none", False)
        if e["kind"] == "post":
            lo, hi = bbox(e)
            fp = sbox(lo[0], lo[1], hi[0], hi[1])
            inside_ext = any(outside(r) and r in poly and poly[r].buffer(1).contains(fp) for r in e["refs"])
            hits = fp.intersection(wall_fp).area
            c.row("inferred supports", e["id"], "within porch/deck footprint, clear of walls",
                  f"inside={inside_ext} wall overlap={hits / 1e6:.4f} m2", inside_ext and hits < 1e3)
    summ = {k: sum(v.values()) for k, v in model.get("provenance_summary", {}).items()}
    c.row("provenance", "element classes", "plan / derived / inferred", summ, True)

    # ---------------------------------------------------------------- renderer audit
    if audit is not None:
        drawn = {a["id"]: a for a in audit.get("elements", [])}
        missing = [e["id"] for e in els if e["id"] not in drawn]
        extra = [i for i in drawn if i not in {e["id"] for e in els}]
        worst, worst_id = 0.0, None
        for e in els:
            a = drawn.get(e["id"])
            if not a:
                continue
            lo, hi = bbox(e)
            tol = TOL + (60 if e["kind"] in ("opening", "railing") else 0)  # frames/sills may stand proud of the opening
            d = max(max(abs(x - y) for x, y in zip(lo, a["min"])), max(abs(x - y) for x, y in zip(hi, a["max"])))
            if e["kind"] in ("opening", "railing"):
                # drawn detail must stay inside the envelope grown by the tolerance
                d = max(max(lo[i] - a["min"][i] for i in range(3)), max(a["max"][i] - hi[i] for i in range(3)), 0)
            if d > worst:
                worst, worst_id = d, e["id"]
            if d > tol:
                c.row("renderer audit", e["id"], f"{[round(v) for v in lo]}..{[round(v) for v in hi]}",
                      f"{[round(v) for v in a['min']]}..{[round(v) for v in a['max']]}", False)
        c.row("renderer audit", "all elements", f"{len(els)} model elements",
              f"{len(drawn)} drawn, worst deviation {worst:.1f} mm ({worst_id})", not missing and not extra,
              note=f"missing {missing[:5]} extra {extra[:5]}" if missing or extra else "")

    for i in model.get("issues", []):
        (c.rep.warnings if i["severity"] == "warning" else c.rep.info).append(f"[plan issue] {i['message']}")
    return c.rep, c.table


def format_table(table) -> str:
    order = list(dict.fromkeys(r["check"] for r in table))
    lines = []
    cur = None
    for r in sorted(table, key=lambda r: order.index(r["check"])):
        if r["check"] != cur:
            cur = r["check"]
            lines.append(f"\n{cur.upper()}")
        mark = {"ok": "  ok ", "warning": " WARN", "error": " FAIL"}[r["status"]]
        lines.append(f"{mark}  {r['item']}: plan {r['plan']} | 3D {r['model']}" + (f"  ({r['note']})" if r["note"] else ""))
    return "\n".join(lines)


__all__ = ["check_consistency", "format_table"]
