"""Joint design score: interior usability + exterior architectural quality (0-100).

A plan that validates can still be a poor house: rooms of awkward size, bedrooms opening onto the
kitchen, a garage-dominated street facade, blank walls, windows that do not line up. The scorer
ranks otherwise-valid candidates on both. Metrics and weights live in brain/scoring.json; room
ranges in brain/rules.json. Every metric reports its measured value, so the ranking is explainable.
"""
from __future__ import annotations

import itertools
import json
import math
import os
import statistics
from collections import defaultdict

from shapely.geometry import Polygon

from .validate import PlanModel

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "brain", "scoring.json")) as _fh:
    CFG = json.load(_fh)
with open(os.path.join(HERE, "brain", "rules.json")) as _fh:
    RULES = json.load(_fh)
SF = 10.7639
OUTSIDE = {"porch", "balcony", "deck", "patio", "terrace", "exterior"}
UNCONDITIONED = OUTSIDE | {"garage", "void", "open_to_below", "attic"}
HABITABLE = {"bedroom", "living", "kitchen", "dining", "loft"}


def lerp_score(v, good, bad):
    """1 at/below `good`, 0 at/above `bad` (works for either direction)."""
    if good == bad:
        return 1.0 if v <= good else 0.0
    t = (v - good) / (bad - good)
    return max(0.0, min(1.0, 1 - t))


def brain_key(r, area_sf):
    u, tags = r.get("usage"), r.get("tags", [])
    if u == "bedroom":
        return "master_bedroom" if "master" in tags else "bedroom"
    if u == "bathroom":
        return "master_bath" if "master" in tags else "bathroom"
    if u == "closet":
        return "walk_in_closet" if area_sf > 30 else "closet"
    if u == "circulation":
        return "foyer" if "entry" in tags else "hall"
    return {"living": "family", "kitchen": "kitchen", "dining": "dining", "laundry": "laundry"}.get(u)


def score(doc: dict, model: dict | None = None, target_sf: float | None = None) -> dict:
    m = PlanModel(doc)
    rooms = {r["id"]: r for r in doc["rooms"]}
    shape = m.shape
    metrics = {}

    def put(group, name, value_score, measured, note=""):
        metrics[name] = {"group": group, "score": round(max(0.0, min(1.0, value_score)), 3),
                         "weight": CFG[group][name]["weight"], "measured": measured, "note": note}

    cond = sum(shape[r].area for r in rooms if rooms[r].get("usage") not in UNCONDITIONED) / 1e6 * SF

    # ---------------------------------------------------------------- interior
    if target_sf:
        dev = cond / target_sf - 1
        tol = CFG["interior"]["area_fit"]["tolerance"]
        put("interior", "area_fit", 1 if abs(dev) <= tol else lerp_score(abs(dev), tol, tol * 4),
            f"{cond:.0f} sf vs target {target_sf:.0f} ({dev:+.1%})")

    sizes, props, notes_s, notes_p = [], [], [], []
    for rid, r in rooms.items():
        a_sf = shape[rid].area / 1e6 * SF
        key = brain_key(r, a_sf)
        if not key or key not in RULES["rooms"]:
            continue
        rule = RULES["rooms"][key]
        minx, miny, maxx, maxy = shape[rid].bounds
        dims = sorted([(maxx - minx) / 304.8, (maxy - miny) / 304.8])
        lo, hi = rule["area"]
        sc = 1.0 if lo <= a_sf <= hi else max(0.0, 1 - min(abs(a_sf - lo), abs(a_sf - hi)) / lo)
        if "large_island" in r.get("tags", []):
            rule = {**rule, **rule["large_island"]}
            lo, hi = rule["area"]
            sc = 1.0 if lo <= a_sf <= hi else max(0.0, 1 - min(abs(a_sf - lo), abs(a_sf - hi)) / lo)
        md = rule.get("min_dim_with_island") if "island" in r.get("tags", []) and "large_island" not in r.get("tags", []) else None
        md = md or rule["min_dim"]
        if dims[0] < md - 0.01:
            sc *= 0.5
            notes_s.append(f"{rid} narrow ({dims[0]:.1f}' < {md}')")
        elif sc < 1:
            notes_s.append(f"{rid} {a_sf:.0f} sf outside {lo}-{hi}")
        sizes.append(sc)
        asp = dims[1] / dims[0]
        props.append(1.0 if asp <= rule["max_aspect"] else max(0.0, 1 - (asp - rule["max_aspect"]) / rule["max_aspect"]))
        if asp > rule["max_aspect"]:
            notes_p.append(f"{rid} {dims[0]:.1f}'x{dims[1]:.1f}'")
    put("interior", "room_sizes", statistics.mean(sizes) if sizes else 1, f"{len(sizes)} rooms checked", "; ".join(notes_s))
    put("interior", "proportions", statistics.mean(props) if props else 1, f"{len(props)} rooms", "; ".join(notes_p))

    circ = sum(shape[r].area for r in rooms if rooms[r].get("usage") == "circulation") / 1e6 * SF
    ratio = circ / cond if cond else 0
    c = CFG["interior"]["circulation_ratio"]
    put("interior", "circulation_ratio", lerp_score(ratio, c["target_max"], c["zero_at"]), f"{ratio:.1%} of conditioned area")

    def usage_rooms(u):
        return [r for r in rooms if rooms[r].get("usage") == u]

    def hops(a, b, limit=2):
        frontier, seen = {a}, {a}
        for d in range(1, limit + 1):
            nxt = {n for x in frontier for n, how in m.graph[x] if how in ("door", "open")} - seen
            if b in nxt:
                return d
            seen |= nxt
            frontier = nxt
        return None

    adj_scores, adj_notes = [], []
    for ua, ub, w in CFG["adjacency_preferences"]:
        A, B = usage_rooms(ua), usage_rooms(ub)
        if not A or not B:
            continue
        best = min((h for a, b in itertools.product(A, B) if (h := hops(a, b)) is not None), default=None)
        s = 1.0 if best == 1 else (0.5 if best == 2 else 0.0)
        adj_scores += [s] * w
        if s < 1:
            adj_notes.append(f"{ua}-{ub}: {'2 steps' if best == 2 else 'far'}")
    put("interior", "adjacency", statistics.mean(adj_scores) if adj_scores else 1, f"{len(adj_scores)} weighted pairs", "; ".join(adj_notes))

    beds = usage_rooms("bedroom")
    master = [r for r in beds if "master" in rooms[r].get("tags", [])]
    others = [r for r in beds if r not in master]
    priv, pnotes = [], []
    if master and others:
        d = min(shape[a].centroid.distance(shape[b].centroid) for a in master for b in others) / 1000
        priv.append(lerp_score(-d, -12, -6))
        pnotes.append(f"master to nearest bedroom {d:.1f} m")
    for b in beds:
        for n, how in m.graph[b]:
            if how != "door":
                continue
            u = rooms.get(n, {}).get("usage")
            ok = 1.0 if u in ("circulation", "bathroom", "closet", "bedroom") else (0.6 if u == "living" and b in master else 0.3)
            priv.append(ok)
            if ok < 1:
                pnotes.append(f"{b} opens onto {n}")
    put("interior", "privacy", statistics.mean(priv) if priv else 1, "; ".join(pnotes))

    by_level = defaultdict(list)
    for r in rooms:
        if rooms[r].get("usage") in ("bathroom", "laundry", "kitchen"):
            by_level[rooms[r].get("level")].append(shape[r].centroid)
    spread = max((max(p.distance(q) for p, q in itertools.combinations(ps, 2)) for ps in by_level.values() if len(ps) > 1), default=0) / 1000
    c = CFG["interior"]["wet_room_clustering"]
    put("interior", "wet_room_clustering", lerp_score(spread, c["good_m"], c["bad_m"]), f"max wet-room spread {spread:.1f} m")

    lit = {}
    for o in doc.get("openings", []):
        if o["opening_type"] == "window" or o.get("operation") == "sliding":
            cr = o.get("connects_rooms", [])
            if any(x == "exterior" or rooms.get(x, {}).get("usage") in OUTSIDE for x in cr):
                for x in cr:
                    lit[x] = 1.0
    dl, dnotes = [], []
    for r in rooms:
        if rooms[r].get("usage") not in HABITABLE:
            continue
        v = lit.get(r, 0.0)
        if not v and any(lit.get(n) for n, how in m.graph[r] if how == "open"):
            v = 0.6  # borrowed light through an open-plan neighbour
            dnotes.append(f"{r} borrowed light")
        elif not v:
            dnotes.append(f"{r} no window")
        dl.append(v)
    put("interior", "daylight", statistics.mean(dl) if dl else 1, f"{len(dl)} habitable rooms", "; ".join(dnotes))

    # ---------------------------------------------------------------- exterior
    if model:
        exterior_metrics(doc, model, rooms, put)

    # ---------------------------------------------------------------- aggregate
    groups = {}
    for g in ("interior", "exterior"):
        ms = [v for v in metrics.values() if v["group"] == g]
        if ms:
            groups[g] = sum(v["score"] * v["weight"] for v in ms) / sum(v["weight"] for v in ms)
    wsum = sum(CFG["groups"][g] for g in groups)
    total = 100 * sum(groups[g] * CFG["groups"][g] for g in groups) / wsum
    return {"total": round(total, 1), "groups": {g: round(100 * v, 1) for g, v in groups.items()},
            "conditioned_sf": round(cond), "metrics": metrics}


def exterior_metrics(doc, model, rooms, put):
    front = model["front"]["normal"]
    right = (-front[1], front[0])
    elev = {lv["id"]: lv.get("elevation_mm", 0) for lv in doc.get("levels", [])}
    plate = RULES["heights"]["plate_mm"]

    def outside(r):
        return r == "exterior" or rooms.get(r, {}).get("usage") in OUTSIDE

    def facade_of(nx, ny):
        df, dr = nx * front[0] + ny * front[1], nx * right[0] + ny * right[1]
        return "front" if df > 0.7 else "rear" if df < -0.7 else "right" if dr > 0.7 else "left" if dr < -0.7 else None

    walls = {}
    for w in doc.get("walls", []):
        adj = w.get("adjacent_rooms", [])
        if not (any(outside(r) for r in adj) and not all(outside(r) for r in adj)):
            continue
        L = math.hypot(w["to"]["x"] - w["from"]["x"], w["to"]["y"] - w["from"]["y"])
        ux, uy = (w["to"]["x"] - w["from"]["x"]) / L, (w["to"]["y"] - w["from"]["y"]) / L
        sgn = 1 if outside(adj[0]) else -1
        f = facade_of(-uy * sgn, ux * sgn)
        walls[w["id"]] = (w, f, L, ux, uy)
    ops = defaultdict(list)
    for o in doc.get("openings", []):
        if o["in_wall"] in walls:
            ops[o["in_wall"]].append(o)
    styles = {e["refs"][0]: e.get("style") for e in model["elements"] if e["kind"] == "opening"}

    # entry prominence
    entries = [o for wid, lst in ops.items() for o in lst if styles.get(o["id"]) == "entry_door"]
    on_front = [o for o in entries if walls[o["in_wall"]][1] == "front"]
    covered = any(any(rooms.get(r, {}).get("usage") == "porch" for r in o.get("connects_rooms", [])) for o in on_front)
    put("exterior", "entry_prominence", (0.6 if on_front else 0) + (0.4 if covered else 0),
        f"entry on front: {bool(on_front)}, covered: {covered}")

    # garage dominance of the street facade
    front_len = sum(L for w, f, L, *_ in walls.values() if f == "front" and w.get("level") == min(elev, key=elev.get))
    gdoors = sum(o["width_mm"] for wid, lst in ops.items() for o in lst
                 if styles.get(o["id"]) == "vehicle_door" and walls[wid][1] == "front")
    share = gdoors / front_len if front_len else 0
    c = CFG["exterior"]["garage_dominance"]
    put("exterior", "garage_dominance", lerp_score(share, c["good_max_share"], c["bad_share"]),
        f"garage doors {share:.0%} of street-facade width")

    # front glazing ratio
    glass = sum(o["width_mm"] * o["height_mm"] for wid, lst in ops.items() for o in lst
                if walls[wid][1] == "front" and (o["opening_type"] == "window" or o.get("operation") == "sliding"))
    area = sum(L * w.get("wall_height_mm", plate) for w, f, L, *_ in walls.values() if f == "front")
    gr = glass / area if area else 0
    lo, hi = CFG["exterior"]["front_glazing"]["good"]
    put("exterior", "front_glazing", 1 if lo <= gr <= hi else (gr / lo if gr < lo else max(0, 1 - (gr - hi) / hi)),
        f"{gr:.1%} of front wall area glazed")

    # blank walls (longest opening-free run per facade; the street facade counts double)
    c = CFG["exterior"]["blank_walls"]
    per, notes = [], []
    runs = defaultdict(float)
    for wid, (w, f, L, ux, uy) in walls.items():
        cuts = sorted((o["position_along_wall_mm"], o["position_along_wall_mm"] + o["width_mm"]) for o in ops[wid])
        cur, longest = 0, 0
        for a, b in cuts:
            longest = max(longest, a - cur)
            cur = max(cur, b)
        longest = max(longest, L - cur)
        runs[(f, w.get("level"))] = max(runs[(f, w.get("level"))], longest / 1000)
    for (f, lv), run in runs.items():
        s = lerp_score(run, c["good_max_m"], c["bad_m"])
        per += [s] * (2 if f == "front" else 1)
        if s < 1:
            notes.append(f"{f} {lv}: {run:.1f} m blank")
    put("exterior", "blank_walls", statistics.mean(per) if per else 1, f"{len(runs)} facade-levels", "; ".join(notes))

    # head alignment per facade and level (privacy/work windows excluded)
    heads = defaultdict(list)
    for wid, lst in ops.items():
        w, f = walls[wid][0], walls[wid][1]
        for o in lst:
            if o["opening_type"] == "window" and o.get("sill_height_mm", 0) >= 1300:
                continue
            if styles.get(o["id"]) == "vehicle_door":
                continue
            heads[(f, w.get("level"))].append(o.get("sill_height_mm", 0) + o["height_mm"])
    tol = CFG["exterior"]["head_alignment"]["tolerance_mm"]
    al = []
    for k, hs in heads.items():
        mode = statistics.mode(hs)
        al.append(sum(abs(h - mode) <= tol for h in hs) / len(hs))
    put("exterior", "head_alignment", statistics.mean(al) if al else 1, f"{sum(len(h) for h in heads.values())} openings")

    # vertical alignment across stories (multi-level only)
    if len(elev) > 1:
        centers = defaultdict(list)
        for wid, lst in ops.items():
            w, f, L, ux, uy = walls[wid]
            for o in lst:
                s = o["position_along_wall_mm"] + o["width_mm"] / 2
                cx, cy = w["from"]["x"] + ux * s, w["from"]["y"] + uy * s
                centers[(f, w.get("level"))].append(cx * right[0] + cy * right[1] if f in ("front", "rear") else cx * front[0] + cy * front[1])
        levels = sorted(elev, key=elev.get)
        tol = CFG["exterior"]["vertical_alignment"]["tolerance_mm"]
        hit = tot = 0
        for f in ("front", "rear", "left", "right"):
            for lo_l, up_l in zip(levels, levels[1:]):
                for cx in centers.get((f, up_l), []):
                    tot += 1
                    hit += any(abs(cx - c2) <= tol for c2 in centers.get((f, lo_l), []))
        if tot:
            put("exterior", "vertical_alignment", hit / tot, f"{hit}/{tot} upper openings over a lower opening")

    # rhythm: spacing regularity along each facade
    cvs = []
    for (f, lv) in runs:
        cs = []
        for wid, (w, ff, L, ux, uy) in walls.items():
            if ff != f or w.get("level") != lv:
                continue
            for o in ops[wid]:
                s = o["position_along_wall_mm"] + o["width_mm"] / 2
                cs.append((w["from"]["x"] + ux * s) * (right[0] if f in ("front", "rear") else front[0]) +
                          (w["from"]["y"] + uy * s) * (right[1] if f in ("front", "rear") else front[1]))
        cs.sort()
        gaps = [b - a for a, b in zip(cs, cs[1:]) if b - a > 1]
        if len(gaps) >= 2:
            cvs.append(statistics.pstdev(gaps) / statistics.mean(gaps))
    if cvs:
        put("exterior", "opening_rhythm", 1 - min(1, statistics.mean(cvs)), f"spacing variation {statistics.mean(cvs):.2f}")

    vols = len(doc.get("massing", {}).get("volumes", [])) or 1
    planes = sum(1 for e in model["elements"] if e["kind"] == "roof_plane")
    c = CFG["exterior"]["massing_simplicity"]
    put("exterior", "massing_simplicity",
        0.5 * lerp_score(vols, c["good_max_volumes"], c["good_max_volumes"] * 2) + 0.5 * lerp_score(planes, c["good_max_roof_planes"], c["good_max_roof_planes"] * 2),
        f"{vols} volumes, {planes} roof planes")


def format_score(sc: dict) -> str:
    lines = [f"TOTAL {sc['total']}/100  (interior {sc['groups'].get('interior')}, exterior {sc['groups'].get('exterior')}); "
             f"conditioned {sc['conditioned_sf']} sf"]
    for g in ("interior", "exterior"):
        for k, v in sc["metrics"].items():
            if v["group"] == g:
                lines.append(f"  {g[:3]} {k:20s} {v['score']:.2f} x{v['weight']}  {v['measured']}" + (f"  [{v['note']}]" if v["note"] else ""))
    return "\n".join(lines)


__all__ = ["score", "format_score"]
