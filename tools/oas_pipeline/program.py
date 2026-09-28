"""Check an OAS-Layout document against an OAS-Program (design requirements).

Supported OAS-Program fields (see docs/program.md) plus a few layout-matching extensions:

Top level
  ``global_constraints``: ``min_area_m2`` / ``max_area_m2`` / ``target_area_m2`` (conditioned area:
  rooms whose usage is not in ``unconditioned_usages``), ``stories`` (number of building-story levels),
  ``building_footprint`` ``{"level", "exclude_usages", "dims_m": [a, b]}`` (must be a full rectangle).
  ``circulation``: ``no_pass_through_usages`` (e.g. ``["bedroom"]``: every room must be reachable
  without walking through a room of those usages).

Per room requirement
  ``id``, ``usage``; matching extensions ``level``, ``count`` (default 1) or explicit ``layout_rooms``.
  ``desired_area_m2`` ``{min, max}`` (per matched room), ``exact_dims_m`` ``[a, b]`` (rectangle, any order).
  ``must_have``: ``daylight`` (exterior window), ``exterior_access`` (door to outside or an exterior
  space), ``balcony_access`` (door/opening to a balcony).
  ``adjacency``: ``must_touch`` / ``should_touch`` / ``avoid_touch`` (share a boundary),
  ``must_connect`` (door or open passage), ``must_open_to`` (wall-less shared boundary: open concept).
  Targets name other requirement ids.
"""
from __future__ import annotations

import json

from shapely.ops import unary_union

from .validate import PlanModel, Report, Rules

EXTERIOR_USAGES = {"porch", "balcony", "deck", "patio", "terrace", "exterior"}
DEFAULT_UNCONDITIONED = sorted(EXTERIOR_USAGES | {"garage", "void", "open_to_below"})
TOUCH_MIN_MM = 600
TOL_M = 0.02


def check_program(doc: dict, program: dict, rules: Rules | None = None) -> Report:
    m = PlanModel(doc, rules)
    rep = Report()
    err, warn, info = rep.errors.append, rep.warnings.append, rep.info.append
    rooms = doc["rooms"]

    # ---- resolve requirement ids to layout rooms
    matched: dict[str, list[str]] = {}
    for req in program.get("rooms", []):
        if "layout_rooms" in req:
            ids = [r for r in req["layout_rooms"] if r in m.rooms]
            missing = set(req["layout_rooms"]) - set(ids)
            if missing:
                err(f"{req['id']}: layout rooms not found {sorted(missing)}")
        else:
            ids = [r["id"] for r in rooms if r.get("usage") == req.get("usage")
                   and ("level" not in req or r.get("level") == req["level"])]
            want = req.get("count", 1)
            if len(ids) < want:
                err(f"{req['id']}: needs {want} room(s) with usage {req.get('usage')!r}"
                    f"{' on ' + req['level'] if 'level' in req else ''}, found {len(ids)}")
            elif len(ids) > want:
                warn(f"{req['id']}: {len(ids)} rooms match usage {req.get('usage')!r}, expected {want}")
        matched[req["id"]] = ids
        info(f"{req['id']}: {', '.join(ids) or '-'}")

    def touching(a, b):
        shared = m.shape[a].boundary.intersection(m.shape[b].boundary)
        return m.rooms[a].get("level") == m.rooms[b].get("level") and shared.length >= TOUCH_MIN_MM

    def connected(a, b, hows=("door", "open")):
        return any(n == b and how in hows for n, how in m.graph[a])

    # ---- per requirement
    for req in program.get("rooms", []):
        rid, ids = req["id"], matched[req["id"]]
        area = req.get("desired_area_m2")
        for r in ids:
            a = m.shape[r].area / 1e6
            if area and "min" in area and a < area["min"] - 1e-6:
                err(f"{rid}: {r} is {a:.1f} m2, below min {area['min']}")
            if area and "max" in area and a > area["max"] + 1e-6:
                err(f"{rid}: {r} is {a:.1f} m2, above max {area['max']}")
            if "exact_dims_m" in req:
                minx, miny, maxx, maxy = m.shape[r].bounds
                got = sorted([(maxx - minx) / 1000, (maxy - miny) / 1000])
                want = sorted(req["exact_dims_m"])
                if abs(m.shape[r].area - (maxx - minx) * (maxy - miny)) > 1 or \
                        any(abs(g - w) > TOL_M for g, w in zip(got, want)):
                    err(f"{rid}: {r} is {got[0]:.2f} x {got[1]:.2f} m, required {want[0]} x {want[1]} m")
        for feature in req.get("must_have", []):
            for r in ids:
                if feature == "daylight":
                    # a window to outside, or onto an exterior space such as a porch or balcony
                    ok = any(o.get("opening_type") == "window" and r in o.get("connects_rooms", [])
                             and any(n == "exterior" or m.rooms.get(n, {}).get("usage") in EXTERIOR_USAGES
                                     for n in o["connects_rooms"] if n != r)
                             for o in doc.get("openings", []))
                elif feature == "exterior_access":
                    ok = any(n == "exterior" or m.rooms.get(n, {}).get("usage") in EXTERIOR_USAGES
                             for n, how in m.graph[r] if how in ("door", "open"))
                elif feature == "balcony_access":
                    ok = any(m.rooms.get(n, {}).get("usage") == "balcony" for n, how in m.graph[r] if how in ("door", "open"))
                else:
                    warn(f"{rid}: must_have {feature!r} is not machine-checkable; skipped")
                    break
                if not ok:
                    err(f"{rid}: {r} lacks {feature}")
        adj = req.get("adjacency", {})
        for kind, targets in adj.items():
            for t in targets:
                if t not in matched:
                    err(f"{rid}: adjacency target {t!r} is not a requirement id")
                    continue
                pairs = [(a, b) for a in ids for b in matched[t]]
                if kind == "must_touch":
                    if not any(touching(a, b) for a, b in pairs):
                        err(f"{rid} must touch {t}")
                elif kind == "should_touch":
                    if not any(touching(a, b) for a, b in pairs):
                        warn(f"{rid} should touch {t}")
                elif kind == "avoid_touch":
                    if any(touching(a, b) for a, b in pairs):
                        err(f"{rid} touches {t} (avoid_touch)")
                elif kind == "must_connect":
                    if not any(connected(a, b) for a, b in pairs):
                        err(f"{rid} has no door/opening to {t}")
                elif kind == "must_open_to":
                    if not any(connected(a, b, ("open",)) for a, b in pairs):
                        err(f"{rid} is not open (wall-less) to {t}")
                else:
                    warn(f"{rid}: adjacency kind {kind!r} not supported; skipped")

    # ---- global constraints
    g = program.get("global_constraints", {})
    uncond = set(g.get("unconditioned_usages", DEFAULT_UNCONDITIONED))
    conditioned = sum(m.shape[r["id"]].area for r in rooms if r.get("usage") not in uncond) / 1e6
    info(f"conditioned area {conditioned:.1f} m2 ({conditioned * 10.7639:.0f} sf)")
    if "min_area_m2" in g and conditioned < g["min_area_m2"]:
        err(f"conditioned area {conditioned:.1f} m2 < min {g['min_area_m2']}")
    if "max_area_m2" in g and conditioned > g["max_area_m2"]:
        err(f"conditioned area {conditioned:.1f} m2 > max {g['max_area_m2']}")
    if "target_area_m2" in g and abs(conditioned - g["target_area_m2"]) > 0.1 * g["target_area_m2"]:
        warn(f"conditioned area {conditioned:.1f} m2 is >10% from target {g['target_area_m2']}")
    if "stories" in g:
        n = sum(1 for lv in doc.get("levels", []) if lv.get("is_building_story", True))
        if n != g["stories"]:
            err(f"{n} building stories, required {g['stories']}")
    fp = g.get("building_footprint")
    if fp:
        ids = [r["id"] for r in rooms if r.get("level") == fp["level"] and r.get("usage") not in set(fp.get("exclude_usages", []))]
        u = unary_union([m.shape[i] for i in ids])
        minx, miny, maxx, maxy = u.bounds
        got = sorted([(maxx - minx) / 1000, (maxy - miny) / 1000])
        want = sorted(fp["dims_m"])
        info(f"building footprint {got[0]:.2f} x {got[1]:.2f} m")
        if abs(u.area - (maxx - minx) * (maxy - miny)) > 1:
            err("building footprint is not a full rectangle")
        if any(abs(a - b) > TOL_M for a, b in zip(got, want)):
            err(f"building footprint {got[0]:.2f} x {got[1]:.2f} m, required {want[0]} x {want[1]} m")

    # ---- circulation rules
    circ = program.get("circulation", {})
    blocked = tuple(circ.get("no_pass_through_usages", []))
    if blocked:
        reach = m.reachable(blocked_usages=blocked)
        # Spaces that legitimately open only off a bedroom (its closet, an en-suite bath).
        behind = set(circ.get("allow_behind_usages", ["closet", "bathroom"]))
        for r in m.rooms:
            if r in reach or r in m.voids:
                continue
            owners = [n for n, _ in m.graph[r] if m.rooms.get(n, {}).get("usage") in blocked and n in reach]
            if m.rooms[r].get("usage") in behind and owners:
                continue
            if r not in m.voids and r not in reach:
                err(f"{r} is only reachable by passing through a {'/'.join(blocked)}")
    return rep


def load(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


__all__ = ["check_program", "load"]
