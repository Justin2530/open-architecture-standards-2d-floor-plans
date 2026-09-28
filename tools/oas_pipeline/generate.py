"""Compile a floor-plan design spec into an OAS-Layout document.

The spec is authored in a convenient unit (feet, metres, ...) on a Y-up grid and
describes *what the designer decided*: rooms, wall runs, openings, railings,
slabs and roofs. Everything that can be derived is derived here, so the spec
cannot contradict itself:

* integer-millimetre conversion of every coordinate
* ``adjacent_rooms`` on walls and ``connects_rooms`` on openings (probed geometry)
* door ``swing_direction`` from the room the leaf should swing into
* ``area_m2`` from room polygons
* the circulation graph (``connections``) from doors, wall-less shared
  boundaries and matching stair footprints on consecutive levels
* optional origin normalisation (no negative coordinates)
* one viewer-ready extract per level

See README.md for the spec format.
"""
from __future__ import annotations

import copy
import json
import math
import os
from typing import Any

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

UNIT_MM = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4, "ft": 304.8}
VOID_USAGES = {"void", "open_to_below"}
STAIR_USAGES = {"stair"}
SWING_PROBE_MM = 300      # distance either side of a wall used to find its rooms
MIN_OPEN_EDGE_MM = 800    # a wall-less shared boundary at least this long is walkable


class SpecError(ValueError):
    """The design spec is inconsistent (bad reference, door swinging into a non-adjacent room, ...)."""


class Generator:
    def __init__(self, spec: dict):
        self.spec = spec
        unit = spec.get("authoring_unit", "mm")
        if unit not in UNIT_MM:
            raise SpecError(f"unknown authoring_unit {unit!r}; use one of {sorted(UNIT_MM)}")
        self.k = UNIT_MM[unit]
        d = spec.get("defaults", {})
        self.wall_height = d.get("wall_height_mm", 2700)
        self.thickness = {"exterior": 200, "interior": 100, **d.get("thickness_mm", {})}
        self.levels = spec.get("levels", [])
        self.level_ids = [lv["id"] for lv in self.levels]

    # -------------------------------------------------------------- units
    def mm(self, v: float) -> int:
        return int(round(v * self.k))

    def pt(self, x: float, y: float) -> dict:
        return {"x": self.mm(x), "y": self.mm(y)}

    def _pts(self, item: dict, what: str) -> list[tuple[float, float]]:
        if "rect" in item:
            x0, y0, x1, y1 = item["rect"]
            return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        if "points" in item:
            return [tuple(p) for p in item["points"]]
        raise SpecError(f"{what} {item.get('id')!r} needs 'rect' or 'points'")

    def _poly(self, pts) -> dict:
        return {"unit": "mm", "closed": True, "points": [self.pt(x, y) for x, y in pts]}

    def _level(self, item: dict, what: str) -> str:
        lv = item.get("level")
        if lv not in self.level_ids:
            raise SpecError(f"{what} {item.get('id', item.get('prefix'))!r} references unknown level {lv!r}")
        return lv

    # -------------------------------------------------------------- build
    def build(self) -> dict:
        from .massing import MassingCompiler
        self._rooms()
        mass = MassingCompiler(self, self.spec["massing"]) if self.spec.get("massing") else None
        if mass:
            mass.add_unassigned_spaces()   # before walls, so wall adjacency sees the new spaces
        self._walls()
        if mass:
            mass.add_envelope_walls()      # before openings, so openings may use envelope walls
        self._openings()
        self._railings()
        slabs = [self._slab(s) for s in self.spec.get("floor_slabs", [])]
        roofs = [self._roof(r) for r in self.spec.get("roofs", [])]
        if mass:
            roofs += mass.roofs(self._floor_tops(slabs))
        connections = self._connections()

        doc = {
            "oas": self.spec.get("oas", "1.1.0"),
            "plan_id": self.spec["plan_id"],
            "title": self.spec.get("title", self.spec["plan_id"]),
            "units": {"length": "mm", "angle": "deg"},
            "levels": self.levels,
            "rooms": self.rooms_json,
            "walls": self.walls_json,
            "openings": self.openings_json,
            "railings": self.railings_json,
            "floor_slabs": slabs,
            "roofs": roofs,
            "connections": connections,
            "metadata": self.spec.get("metadata", {}),
        }
        if mass:
            doc["extensions"] = {"oas-massing": {"version": "0.1.0"}}
            doc["massing"] = {"typology": self.spec["massing"]["typology"], "volumes": mass.volumes_json()}
        if self.spec.get("normalize_origin", False):
            normalize_origin(doc)
        return doc

    def _rooms(self):
        self.room_order: list[str] = []
        self.room_level: dict[str, str] = {}
        self.room_shape: dict[str, Polygon] = {}
        self.room_usage: dict[str, str] = {}
        self.rooms_json = []
        for r in self.spec["rooms"]:
            rid = r["id"]
            if rid in self.room_shape:
                raise SpecError(f"duplicate room id {rid!r}")
            lv = self._level(r, "room")
            pts = self._pts(r, "room")
            shape = Polygon([(self.mm(x), self.mm(y)) for x, y in pts])
            self.room_order.append(rid)
            self.room_level[rid] = lv
            self.room_shape[rid] = shape
            self.room_usage[rid] = r["usage"]
            self.rooms_json.append({
                "id": rid, "name": r.get("name", rid), "usage": r["usage"],
                "type_name": r.get("type_name", "IfcSpace"), "level": lv,
                "boundary_polygon": self._poly(pts),
                "area_m2": round(shape.area / 1e6, 2), "tags": r.get("tags", []),
            })

    def _rooms_at(self, level: str, x: float, y: float) -> list[str]:
        p = Point(x, y)
        return [rid for rid in self.room_order if self.room_level[rid] == level and self.room_shape[rid].contains(p)]

    def _side_rooms(self, wall: dict, s_mm: float) -> tuple[str, str]:
        """Rooms to the left / right of the wall's from->to direction at distance s_mm along it."""
        fx, fy, tx, ty = wall["from"]["x"], wall["from"]["y"], wall["to"]["x"], wall["to"]["y"]
        length = math.hypot(tx - fx, ty - fy)
        ux, uy = (tx - fx) / length, (ty - fy) / length
        px, py = fx + ux * s_mm, fy + uy * s_mm
        left = self._rooms_at(wall["level"], px - uy * SWING_PROBE_MM, py + ux * SWING_PROBE_MM)
        right = self._rooms_at(wall["level"], px + uy * SWING_PROBE_MM, py - ux * SWING_PROBE_MM)
        return (left[0] if left else "exterior"), (right[0] if right else "exterior")

    def _walls(self):
        raw = []  # (id, level, from, to, kind, thickness)
        for w in self.spec.get("walls", []):
            lv = self._level(w, "wall")
            kind = w.get("kind", "interior")
            t = w.get("thickness_mm", self.thickness[kind])
            if "from" in w:  # single explicit wall
                raw.append((w["id"], lv, tuple(w["from"]), tuple(w["to"]), kind, t, w))
                continue
            # a wall *run*: constant x or y, split at `breaks`; segment ids are <prefix>_<n> (1-based, skips keep numbering)
            if ("x" in w) == ("y" in w):
                raise SpecError(f"wall run {w.get('prefix')!r} needs exactly one of 'x' or 'y'")
            skip = set(w.get("skip", []))
            b = w["breaks"]
            for i in range(len(b) - 1):
                if i in skip:
                    continue
                if "y" in w:
                    f, to = (b[i], w["y"]), (b[i + 1], w["y"])
                else:
                    f, to = (w["x"], b[i]), (w["x"], b[i + 1])
                raw.append((f"{w['prefix']}_{i + 1}", lv, f, to, kind, t, w))

        self.walls_json, self.wall_by_id = [], {}
        for wid, lv, f, to, kind, t, src in raw:
            self._add_wall(self.pt(*f), self.pt(*to), wid, lv, kind, {**src, "thickness_mm": t})

    def _add_wall(self, f_mm: dict, to_mm: dict, wid: str, lv: str, kind: str, src: dict):
        if wid in self.wall_by_id:
            raise SpecError(f"duplicate wall id {wid!r}")
        wall = {"id": wid, "type_name": src.get("type_name", "IfcWall"),
                "from": f_mm, "to": to_mm, "unit": "mm",
                "thickness_mm": src.get("thickness_mm", self.thickness[kind]),
                "wall_height_mm": src.get("wall_height_mm", self.wall_height),
                "level": lv, "structural": src.get("structural", kind == "exterior")}
        if "derived_from" in src:
            wall["derived_from"] = src["derived_from"]
        length = math.hypot(wall["to"]["x"] - wall["from"]["x"], wall["to"]["y"] - wall["from"]["y"])
        if length == 0:
            raise SpecError(f"wall {wid!r} has zero length")
        wall["adjacent_rooms"] = list(self._side_rooms(wall, length / 2))
        self.walls_json.append(wall)
        self.wall_by_id[wid] = wall

    def _openings(self):
        self.openings_json = []
        for o in self.spec.get("openings", []):
            oid = o["id"]
            wall = self.wall_by_id.get(o["wall"])
            if wall is None:
                raise SpecError(f"opening {oid!r} references unknown wall {o['wall']!r}")
            typ = o["type"]
            if typ not in ("door", "window"):
                raise SpecError(f"opening {oid!r}: type must be 'door' or 'window'")
            pos = o["position_mm"] if "position_mm" in o else self.mm(o["offset"])
            width = o["width_mm"]
            left, right = self._side_rooms(wall, pos + width / 2)
            op = o.get("operation")
            out = {"id": oid, "opening_type": typ, "type_name": o.get("label", typ), "in_wall": wall["id"],
                   "level": wall["level"], "position_along_wall_mm": pos, "width_mm": width,
                   "height_mm": o.get("height_mm", 2134 if typ == "door" else 1219)}
            if typ == "window":
                out["sill_height_mm"] = o.get("sill_mm", 914)
            out["is_fixed"] = op == "fixed"
            if op and op != "fixed":
                out["operation"] = op
            if op == "swing":
                into = o.get("swing_into")
                # Convention shared with svg-viewer/app.js: "inward" = leaf swings to the LEFT of the
                # host wall's from->to direction; hinge_side "left" = hinge at the opening start.
                if into == left:
                    out["swing_direction"] = "inward"
                elif into == right:
                    out["swing_direction"] = "outward"
                else:
                    raise SpecError(f"{oid}: swing_into {into!r} is not beside wall {wall['id']} ({left} | {right})")
                out["hinge_side"] = o.get("hinge", "left")
            if op == "sliding":
                out["slide_direction"] = o.get("slide_direction", "left-to-right")
            out["connects_rooms"] = [left, right]
            self.openings_json.append(out)

    def _railings(self):
        self.railings_json = []
        for r in self.spec.get("railings", []):
            pts = [self.pt(x, y) for x, y in r["points"]]
            if "z_mm" in r:
                for p, z in zip(pts, r["z_mm"]):
                    p["z"] = z
            self.railings_json.append({
                "id": r["id"], "type_name": r.get("type_name", "IfcRailing"), "level": self._level(r, "railing"),
                "host_type": r.get("host_type", "floor"),
                "path": {"unit": "mm", "closed": bool(r.get("closed", False)), "points": pts},
                "base_offset_mm": r.get("base_offset_mm", 0)})

    def _floor_tops(self, slabs: list) -> dict:
        """Walking-surface height of each room: its level plus the offset of a slab with the same footprint."""
        elev = {lv["id"]: lv.get("elevation_mm", 0) for lv in self.levels}
        tops = {}
        for rid in self.room_order:
            top = elev[self.room_level[rid]]
            for s in slabs:
                sp = Polygon([(p["x"], p["y"]) for p in s["boundary_polygon"]["points"]])
                if s["level"] == self.room_level[rid] and sp.symmetric_difference(self.room_shape[rid]).area < 1e4:
                    top += s.get("height_above_level_mm", 0)
                    break
            tops[rid] = top
        return tops

    def _slab(self, s: dict) -> dict:
        return {"id": s["id"], "type_name": s.get("type_name", "IfcSlab"), "level": self._level(s, "slab"),
                "height_above_level_mm": s.get("height_above_level_mm", 0),
                "boundary_polygon": self._poly(self._pts(s, "slab"))}

    def _roof(self, r: dict) -> dict:
        pts = self._pts(r, "roof")
        for key in ("slope_angles", "defines_slope", "eave_overhang_mm"):
            if key in r and len(r[key]) != len(pts):
                raise SpecError(f"roof {r['id']!r}: {key} needs {len(pts)} entries (one per edge)")
        out = {"id": r["id"], "type_name": r.get("type_name", "IfcRoof"), "level": self._level(r, "roof"),
               "level_offset_mm": r.get("level_offset_mm", 0), "boundary_polygon": self._poly(pts)}
        for key in ("slope_angles", "defines_slope", "eave_overhang_mm"):
            if key in r:
                out[key] = r[key]
        return out

    # -------------------------------------------------------------- circulation graph
    def _connections(self) -> list[dict]:
        edges: dict[frozenset, str] = {}
        for o in self.openings_json:
            if o["opening_type"] == "door":
                edges[frozenset(o["connects_rooms"])] = "door"

        def wline(w):
            return LineString([(w["from"]["x"], w["from"]["y"]), (w["to"]["x"], w["to"]["y"])])

        def rline(r):
            return LineString([(p["x"], p["y"]) for p in r["path"]["points"]])

        for lv in self.level_ids:
            parts = [wline(w) for w in self.walls_json if w["level"] == lv] + \
                    [rline(r) for r in self.railings_json if r["level"] == lv and r["host_type"] == "floor"]
            blockers = unary_union(parts).buffer(2) if parts else None
            ids = [rid for rid in self.room_order if self.room_level[rid] == lv]
            for i, a in enumerate(ids):
                for b in ids[i + 1:]:
                    shared = self.room_shape[a].boundary.intersection(self.room_shape[b].boundary)
                    free = shared.difference(blockers).length if blockers is not None else shared.length
                    if shared.length > 0 and free >= MIN_OPEN_EDGE_MM:
                        edges.setdefault(frozenset((a, b)), "open")

        voids = {rid for rid, u in self.room_usage.items() if u in VOID_USAGES}
        edges = {k: v for k, v in edges.items() if not (k & voids)}

        # Stairs: identical footprints on different levels are one vertical run.
        stairs = [rid for rid in self.room_order if self.room_usage[rid] in STAIR_USAGES]
        for i, a in enumerate(stairs):
            for b in stairs[i + 1:]:
                if self.room_level[a] != self.room_level[b] and \
                        self.room_shape[a].symmetric_difference(self.room_shape[b]).area < 1:
                    edges[frozenset((a, b))] = "stair"

        degree: dict[str, int] = {}
        for k in edges:
            for n in k:
                if n != "exterior":
                    degree[n] = degree.get(n, 0) + 1

        def key(n):
            return self.room_order.index(n) if n in self.room_order else -1

        connections = []
        for k, how in sorted(edges.items(), key=lambda kv: sorted(key(n) for n in kv[0])):
            if how == "stair":
                continue
            a, b = sorted(k, key=key)
            typ = "single_access" if (degree.get(a, 0) == 1 or degree.get(b, 0) == 1) else "direct"
            connections.append({"from": a, "to": b, "type": typ})
        connections.extend(copy.deepcopy(self.spec.get("extra_connections", [])))
        return connections


def normalize_origin(doc: dict) -> None:
    """Translate the plan so no room coordinate is negative (the svg-viewer fit assumes this).
    Opening positions are wall-relative and therefore unaffected."""
    xs = [p["x"] for r in doc["rooms"] for p in r["boundary_polygon"]["points"]]
    ys = [p["y"] for r in doc["rooms"] for p in r["boundary_polygon"]["points"]]
    dx, dy = -min(0, min(xs)), -min(0, min(ys))
    if not dx and not dy:
        return

    def shift(o):
        if isinstance(o, dict):
            if isinstance(o.get("x"), int) and isinstance(o.get("y"), int):
                o["x"] += dx
                o["y"] += dy
            for v in o.values():
                shift(v)
        elif isinstance(o, list):
            for v in o:
                shift(v)

    for k in ("rooms", "walls", "railings", "floor_slabs", "roofs"):
        shift(doc.get(k, []))
    shift(doc.get("massing", {}))


def level_extracts(doc: dict) -> list[tuple[str, dict]]:
    """One self-contained document per level (suffix, doc), for viewers without a level filter."""
    out = []
    notes = doc.get("metadata", {}).get("notes", "")
    for i, lv in enumerate(doc.get("levels", []), start=1):
        lid, lname = lv["id"], lv.get("name", lv["id"])
        room_ids = {r["id"] for r in doc["rooms"] if r.get("level") == lid}
        pick = lambda key: [x for x in doc.get(key, []) if x.get("level") == lid]  # noqa: E731
        sub = {
            "oas": doc["oas"], "plan_id": f"{doc['plan_id']}_level{i}",
            "title": f"{doc['title']} - {lname}", "units": doc["units"],
            "levels": [lv],
            "rooms": pick("rooms"), "walls": pick("walls"), "openings": pick("openings"),
            "railings": pick("railings"), "floor_slabs": pick("floor_slabs"), "roofs": pick("roofs"),
            **({"extensions": doc["extensions"], "massing": doc["massing"]} if "massing" in doc else {}),
            "connections": [c for c in doc.get("connections", [])
                            if all(n == "exterior" or n in room_ids for n in (c["from"], c["to"]))],
            "metadata": {**doc.get("metadata", {}), "notes": f"{lname} extract of {doc['plan_id']}. " + notes},
        }
        out.append((f"level{i}", sub))
    return out


def generate(spec: dict) -> dict:
    return Generator(spec).build()


def write_outputs(spec: dict, out_dir: str, per_level: bool = True) -> list[str]:
    doc = generate(spec)
    base = spec.get("output_basename", spec["plan_id"])
    os.makedirs(out_dir, exist_ok=True)
    paths = [os.path.join(out_dir, f"{base}.json")]
    with open(paths[0], "w") as fh:
        json.dump(doc, fh, indent=2)
    if per_level and len(doc.get("levels", [])) > 1:
        for suffix, sub in level_extracts(doc):
            p = os.path.join(out_dir, f"{base}_{suffix}.json")
            with open(p, "w") as fh:
                json.dump(sub, fh, indent=2)
            paths.append(p)
    return paths


def load_spec(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


__all__ = ["Generator", "SpecError", "generate", "level_extracts", "load_spec", "normalize_origin", "write_outputs"]
