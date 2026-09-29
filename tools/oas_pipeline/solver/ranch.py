"""Deterministic solver v0: single-story split-bedroom ranch.

Input is a homeowner-level *brief* (see engine.intake): total conditioned area, bedroom and bathroom
counts, garage cars/position, outdoor spaces, lot width. No dimensions or coordinates. Output is a
list of candidate design specs (rooms + relationships + massing), ready for generate.py.

Template (x runs across the lot, y from the street to the back yard; mirrored variants flip x):

    [ master wing ][ great column  ][ kitchen column ][ kids wing (hall | bedrooms) ]
    rear:  master bed    family room      kitchen          closet | bedroom
           ---------                      dining           hall   | bath
    front: bath | WIC    foyer            laundry          closet | bedroom
                         [ covered entry porch ]  [ garage projecting forward (front-load) ]
                         [ covered patio behind family + kitchen ]

Every size comes from brain/rules.json ranges and the area target. Many combinations are
enumerated, filtered with a cheap pre-score, and the best few go to full generation, validation,
exterior checks and scoring. Programs outside the template (e.g. >2 secondary bedrooms or
bathroom counts other than 2) raise Unsupported, and the report names the gap rather than guessing.
"""
from __future__ import annotations

import itertools
import json
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(os.path.dirname(HERE), "brain", "rules.json")) as _fh:
    RULES = json.load(_fh)
R = RULES["rooms"]


class Unsupported(ValueError):
    """The brief needs a layout this solver template cannot produce yet."""


def half(v):
    return round(v * 2) / 2


def check_brief(b):
    if b.get("stories", 1) != 1:
        raise Unsupported("ranch solver v0 handles one-story houses only")
    s = b["bedrooms"] - 1
    if s not in (1, 2):
        raise Unsupported(f"ranch solver v0 handles 2-3 bedrooms (got {b['bedrooms']})")
    if b.get("bathrooms", 2) != 2:
        raise Unsupported(f"ranch solver v0 handles exactly 2 bathrooms (got {b.get('bathrooms')})")
    g = b.get("garage")
    if g and g.get("position", "front") != "front":
        raise Unsupported("ranch solver v0 places attached garages at the front only")
    return s


def candidates(brief, limit=8):
    """Enumerate template parameters, pre-score cheaply, return the best `limit` candidate specs."""
    s = check_brief(brief)
    target = brief["target_conditioned_sf"]
    g = brief.get("garage")
    gw, gd = (RULES["garage"][str(g["cars"])]["width"], RULES["garage"][str(g["cars"])]["depth"]) if g else (0, 0)
    lot = brief.get("lot", {}).get("width_ft")
    setback = RULES["site"]["side_setback_ft"]
    buildable = lot - 2 * setback if lot else None
    hall = RULES["circulation"]["hall_width"]
    island = brief.get("kitchen_island", "none")
    large = island == "large"
    kmin = R["kitchen"]["large_island"]["min_dim"] if large else (R["kitchen"]["min_dim_with_island"] if island not in (None, False, "none") else R["kitchen"]["min_dim"])
    out = []
    grid = itertools.product(
        (13, 14, 15),            # master wing width
        (13, 14, 15) if large else (12, 13, 14),  # kitchen column width
        (11.5, 12, 12.5),        # secondary bedroom width
        (7, 8),                  # foyer depth
        (7, 8),                  # laundry depth
        (kmin + 1, kmin + 2) if large else (kmin, kmin + 1),  # kitchen depth
        (13, 14, 15),            # master bedroom depth
        (6.5, 7.5),              # hall-bath depth (between stacked bedrooms)
        (0, 2, 4),               # width held back from the buildable maximum
        ("side_by_side", "walk_through"),  # master bath/closet arrangement
        (False, True))           # mirrored
    for Wm, Wc, bw, Df, Dl, Dk, Dmb, Dbath, slack, msplit, mirror in grid:
        Wk = bw + hall
        if g and not (3.5 <= gw - Wk <= Wc - 1):
            continue  # garage must overlap the laundry (kitchen column front) for the garage-house door
        W_max = buildable if buildable else Wm + Wc + Wk + 18
        W = W_max - slack
        Wg = W - Wm - Wc - Wk
        if not (14 <= Wg <= 22):
            continue
        D = half(target / W)
        if not (26 <= D <= 38):
            continue
        Ddin = D - Dl - Dk
        Db = (D - Dbath) / s if s == 2 else D - Dbath
        if Ddin < R["dining"]["min_dim"] or Db < R["bedroom"]["min_dim"] or D - Dmb < 8 or D - Df < 13:
            continue
        p = dict(W=W, D=D, Wm=Wm, Wg=Wg, Wc=Wc, Wk=Wk, bw=bw, Df=Df, Dl=Dl, Dk=Dk, Ddin=Ddin, Dmb=Dmb,
                 Dbath=Dbath, Db=Db, s=s, gw=gw, gd=gd, mirror=mirror, hall=hall, msplit=msplit,
                 kitchen_rule="large_island" if large else "kitchen")
        # walk-through: closet (next to the bedroom) then bath (front); side-by-side: bath | closet
        Dm = D - Dmb
        if msplit == "walk_through":
            p["Dwic"] = half(min(max(6, Dm * 0.42), Dm - 7))
            if Dm - p["Dwic"] < 7:
                continue
        out.append((pre_score(p, target), p))
    if not out:
        raise Unsupported("no template parameters satisfy the brief (lot too narrow for the area, or area outside 1,000-3,000 sf)")
    out.sort(key=lambda t: -t[0])
    # Diversify: best candidate per structural choice (mirror images are the same design on a flipped lot,
    # so only the best design is also offered mirrored).
    picked, seen = [], set()
    for sc, p in out:
        key = (p["msplit"], p["Wm"], p["Wc"], p["Dk"], p["bw"])
        if key in seen or p["mirror"]:
            continue
        seen.add(key)
        picked.append((sc, p))
        if len(picked) >= limit - 1:
            break
    if picked:
        top = picked[0][1]
        twin = next(((sc, p) for sc, p in out if p["mirror"] and all(p[k] == top[k] for k in top if k != "mirror")), None)
        if twin:
            picked.append(twin)
    return [{"pre_score": round(sc, 3), "params": p, "spec": build_spec(brief, p)} for sc, p in picked]


def pre_score(p, target):
    """Cheap score from room dimensions alone (0..1): sizes and proportions against brain ranges."""
    Dm = p["D"] - p["Dmb"]
    if p["msplit"] == "walk_through":
        mb, wic = (p["Wm"], Dm - p["Dwic"]), (p["Wm"], p["Dwic"])
    else:
        mb, wic = (p["Wm"] * 0.55, Dm), (p["Wm"] * 0.45, Dm)
    rooms = [("master_bedroom", p["Wm"], p["Dmb"]), ("family", p["Wg"], p["D"] - p["Df"]),
             (p["kitchen_rule"], p["Wc"], p["Dk"]), ("dining", p["Wc"], p["Ddin"]), ("laundry", p["Wc"], p["Dl"]),
             ("foyer", p["Wg"], p["Df"]), ("bedroom", p["bw"], p["Db"]), ("bathroom", p["bw"], p["Dbath"]),
             ("master_bath", *mb), ("walk_in_closet", *wic)]
    sc = 0.0
    for key, a, b in rooms:
        r = R[key] if key != "large_island" else {**R["kitchen"], **R["kitchen"]["large_island"]}
        area = a * b
        lo, hi = r["area"]
        sc += 1 if lo <= area <= hi else max(0, 1 - min(abs(area - lo), abs(area - hi)) / lo)
        sc += 1 if max(a, b) / min(a, b) <= r["max_aspect"] else 0.3
    sc /= 2 * len(rooms)
    area = p["W"] * p["D"]
    sc -= abs(area - target) / target
    return sc


def build_spec(brief, p):
    W, D, Wm, Wg, Wc, Wk, hall = p["W"], p["D"], p["Wm"], p["Wg"], p["Wc"], p["Wk"], p["hall"]
    mirror = p["mirror"]
    X = (lambda x: W - x) if mirror else (lambda x: x)
    east, west = ("west", "east") if mirror else ("east", "west")

    def rect(x0, y0, x1, y1):
        a, b = sorted((X(x0), X(x1)))
        return [half(a), half(y0), half(b), half(y1)]

    rooms = []

    def room(rid, name, usage, r, tags=()):
        rooms.append({"id": rid, "name": name, "usage": usage, "level": "level_01", "rect": r, "tags": list(tags)})

    xg0, xc0, xk0 = Wm, Wm + Wg, Wm + Wg + Wc
    wmb = half(Wm * 0.55)
    # master wing
    room("master_bed", "Master Bedroom", "bedroom", rect(0, D - p["Dmb"], Wm, D), ["master"])
    if p["msplit"] == "walk_through":
        yw = D - p["Dmb"] - p["Dwic"]
        room("master_bath", "Master Bath", "bathroom", rect(0, 0, Wm, yw), ["master", "wet_area"])
        room("master_wic", "Walk-in Closet", "closet", rect(0, yw, Wm, D - p["Dmb"]), ["master"])
    else:
        room("master_bath", "Master Bath", "bathroom", rect(0, 0, wmb, D - p["Dmb"]), ["master", "wet_area"])
        room("master_wic", "Walk-in Closet", "closet", rect(wmb, 0, Wm, D - p["Dmb"]), ["master"])
    # great column
    room("foyer", "Foyer", "circulation", rect(xg0, 0, xc0, p["Df"]), ["entry"])
    room("family", "Family Room", "living", rect(xg0, p["Df"], xc0, D), ["open_plan"])
    # kitchen column
    room("laundry", "Laundry / Mud", "laundry", rect(xc0, 0, xk0, p["Dl"]), ["wet_area"])
    room("dining", "Dining", "dining", rect(xc0, p["Dl"], xk0, p["Dl"] + p["Ddin"]), ["open_plan"])
    island = brief.get("kitchen_island", "none")
    room("kitchen", "Kitchen", "kitchen", rect(xc0, D - p["Dk"], xk0, D),
         ["open_plan", "wet_area"] + ({"large": ["island", "large_island"], "none": [], None: [], False: []}.get(island, ["island"])))
    # kids wing: inner hall column + outer bedroom column (outer so bedrooms get end-wall windows)
    xh1 = xk0 + hall
    run = RULES["circulation"]["min_door_run_ft"]
    if p["s"] == 2:
        y1, y2 = p["Db"], p["Db"] + p["Dbath"]
        room("bed2", "Bedroom 2", "bedroom", rect(xh1, 0, W, y1))
        room("bath2", "Bathroom 2", "bathroom", rect(xh1, y1, W, y2), ["wet_area"])
        room("bed3", "Bedroom 3", "bedroom", rect(xh1, y2, W, D))
        room("closet2", "Closet", "closet", rect(xk0, 0, xh1, y1 - run))
        room("hall", "Hall", "circulation", rect(xk0, y1 - run, xh1, y2 + run))
        room("closet3", "Closet", "closet", rect(xk0, y2 + run, xh1, D))
        beds = ["bed2", "bed3"]
    else:
        y1 = p["Db"]
        room("bed2", "Bedroom 2", "bedroom", rect(xh1, 0, W, y1))
        room("bath2", "Bathroom 2", "bathroom", rect(xh1, y1, W, D), ["wet_area"])
        room("closet2", "Closet", "closet", rect(xk0, 0, xh1, y1 - run))
        room("hall", "Hall", "circulation", rect(xk0, y1 - run, xh1, D))
        beds = ["bed2"]
    g = brief.get("garage")
    if g:
        room("garage", "Garage", "garage", rect(W - p["gw"], -p["gd"], W, 0))
    porch_x1 = (W - p["gw"]) if g else xc0
    covers = []
    if RULES["outdoor"]["covered_entry_by_default"]:
        room("porch", "Covered Entry", "porch", rect(xg0, -RULES["outdoor"]["entry_porch_depth"], porch_x1, 0), ["exterior", "covered"])
        covers.append(("porch_cover", "porch"))
    patio = next((o for o in brief.get("outdoor", []) if o.get("kind") == "patio"), None)
    if patio:
        room("patio", "Covered Patio" if patio.get("covered") else "Patio", "patio",
             rect(xg0, D, xk0, D + RULES["outdoor"]["patio_depth"]), ["exterior"] + (["covered"] if patio.get("covered") else []))
        if patio.get("covered"):
            covers.append(("patio_cover", "patio"))

    d, w = RULES["doors"], RULES["windows"]
    # the kids hall opens from whichever centre room it shares the longest boundary with
    hy = next(r for r in rooms if r["id"] == "hall")["rect"]
    shared = {rid: max(0, min(hy[3], r["rect"][3]) - max(hy[1], r["rect"][1]))
              for rid in ("dining", "kitchen") for r in rooms if r["id"] == rid}
    hall_from = max(shared, key=shared.get)

    def door(oid, a, b, kind, into=None, **kw):
        spec = {"id": oid, "type": "door", "between": [a, b], "width_mm": d[kind]["width_mm"], "operation": d[kind]["operation"],
                "label": d[kind].get("label", "Door")}
        if d[kind]["operation"] == "swing":
            spec["swing_into"] = into or b
        spec.update(kw)
        return spec

    def windows(oid, rid, rule, facade, count=None):
        r = w[rule]
        return {"id": oid, "type": "window", "room": rid, "facade": facade, "width_mm": r["width_mm"],
                "height_mm": r["height_mm"], "sill_mm": r["sill_mm"], "operation": "fixed" if rule == "bathroom" else "sliding",
                "count": count or r["per_exterior_side"], "label": r.get("label", "Window")}

    ops = [
        door("d_entry", "foyer", "porch", "entry", into="foyer", facade="south", position="center", label="Front Entry Door"),
        door("d_master", "family", "master_bed", "bedroom"),
        *([door("d_master_wic", "master_bed", "master_wic", "closet_walk_in"),
           door("d_master_bath", "master_wic", "master_bath", "bath")] if p["msplit"] == "walk_through" else
          [door("d_master_bath", "master_bed", "master_bath", "bath"),
           door("d_master_wic", "master_bed", "master_wic", "closet_walk_in")]),
        door("d_laundry_dining", "dining", "laundry", "utility", into="laundry"),
        door("d_hall", hall_from, "hall", "hall_entry", position="start"),
        door("d_bath2", "hall", "bath2", "bath"),
    ]
    for i, b in enumerate(beds):
        ops.append(door(f"d_{b}", "hall", b, "bedroom"))
        ops.append(door(f"d_{b}_closet", b, f"closet{b[-1]}", "closet_reach_in"))
    if g:
        ops.append(door("d_garage_house", "garage", "laundry", "garage_to_house", into="laundry"))
        ops.append({"id": "d_garage_vehicle", "type": "door", "between": ["garage", "exterior"], "facade": "south",
                    "width_mm": RULES["garage"]["vehicle_door"]["width_mm"], "height_mm": RULES["garage"]["vehicle_door"]["height_mm"],
                    "count": g["cars"] if g["cars"] <= 2 else g["cars"], "label": "Overhead Sectional Garage Door"})
        ops.append(door("d_garage_service", "garage", "exterior", "service", into="garage", facade=east, label="Service Door"))
    if patio:
        ops.append(door("d_patio", "family", "patio", "patio", facade="north", position="center"))
    # windows by room rule on each exterior side the room has
    ops += [
        windows("w_master_rear", "master_bed", "master_bedroom", "north"),
        windows("w_master_side", "master_bed", "bedroom", west),
        windows("w_master_bath", "master_bath", "bathroom", "south"),
        windows("w_foyer", "foyer", "circulation", "south"),
        windows("w_kitchen", "kitchen", "kitchen", "north"),
    ]
    for b in beds:
        ops.append(windows(f"w_{b}_side", b, "bedroom", east))
    if p["s"] == 2:
        ops.append(windows("w_bed3_rear", "bed3", "bedroom", "north"))
        ops.append(windows("w_bath2", "bath2", "bathroom", east))
    else:
        ops.append(windows("w_bath2", "bath2", "bathroom", "north"))
    if not g:
        ops.append(windows("w_bed2_front", "bed2", "bedroom", "south"))

    volumes = [{"id": "main", "role": "shell", "rect": rect(0, 0, W, D), "levels": ["level_01"]}]
    if g:
        volumes.append({"id": "garage", "role": "garage", "rect": rect(W - p["gw"], -p["gd"], W, 0), "levels": ["level_01"],
                        "attach_to": "main"})
    for vid, rid in covers:
        r = next(x for x in rooms if x["id"] == rid)["rect"]
        volumes.append({"id": vid, "role": "cover", "rect": r, "attach_to": "main", "covers": [rid]})
    slabs = [{"id": "slab_house", "level": "level_01", "rect": rect(0, 0, W, D)}]
    for rid in ("garage", "porch", "patio"):
        r = next((x for x in rooms if x["id"] == rid), None)
        if r:
            slabs.append({"id": f"slab_{rid}", "level": "level_01", "height_above_level_mm": -102, "rect": r["rect"]})
    return {
        "plan_id": brief.get("plan_id", "ranch"), "title": brief.get("title", "One-Story Ranch"),
        "authoring_unit": "ft", "normalize_origin": True,
        "defaults": {"wall_height_mm": RULES["heights"]["plate_mm"], "thickness_mm": {"exterior": 152, "interior": 114}},
        "levels": [{"id": "level_01", "name": "First Floor", "elevation_mm": 0, "is_building_story": True}],
        "rooms": rooms, "walls": "derive",
        "open_between": [["foyer", "family"], ["family", "kitchen"], ["kitchen", "dining"], ["dining", "family"]],
        "openings": ops, "floor_slabs": slabs,
        "massing": {"typology": "ranch", "volumes": volumes},
        "metadata": {"generated_by": "oas_pipeline solver ranch v0", "notes": "Generated from a homeowner brief; see solver params.",
                     "solver_params": p},
    }


__all__ = ["Unsupported", "candidates", "build_spec", "check_brief"]
