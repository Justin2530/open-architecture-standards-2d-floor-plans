"""Massing layer of the house model: building volumes -> envelope walls, unassigned spaces, roofs.

OAS-Layout describes floors (rooms and walls per level) but not the *building*: it cannot say that a
one-story room sits inside a two-story shell, what fills the rest of that story, or which walls a
roof bears on. Authoring floors and roofs independently let them disagree (the barndominium's roof
spanned 60' while its second floor spanned 40').

Here the design spec states only which volumes exist (footprint, levels, role, attachment) plus a
typology. Everything else is compiled deterministically, so the 2D plans and the 3D exterior are
two views of the same massing:

* every level listed by an enclosed volume is fully enclosed by exterior walls
  (missing envelope walls are generated);
* space inside a volume that no room occupies becomes the typology's unassigned space
  (``attic`` or ``open_to_below``), drawn on the floor plan like any room;
* each volume's roof is generated from the typology rules (brain/typologies.json), with a
  well-defined base: OAS ``level + level_offset_mm`` is the height of the sloped (eave) edges on
  the boundary line; gables and sheds rise inward from there.

Emitted into OAS as roof entities (``derived_from: massing:<id>``) plus an ``oas-massing``
extension block (``massing_volumes``).
"""
from __future__ import annotations

import json
import math
import os

from shapely.geometry import LineString, Polygon
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "brain", "typologies.json")) as _fh:
    TYPOLOGIES = json.load(_fh)
CONST = TYPOLOGIES["_constants"]
ENCLOSED_ROLES = {"shell", "wing", "garage"}
UNASSIGNED = {"attic": ("attic", "Attic (unfinished)", ["unconditioned", "from_massing"]),
              "open_to_below": ("void", "Open to Below", ["open_to_below", "from_massing"])}


def pitch_deg(p) -> float:
    if isinstance(p, (int, float)):
        return float(p)
    rise, run = p.split(":")
    num = float(rise.split("/")[0]) / float(rise.split("/")[1]) if "/" in rise else float(rise)
    return math.degrees(math.atan(num / float(run)))


class MassingCompiler:
    def __init__(self, gen, spec_massing: dict):
        from .generate import SpecError  # local import: generate imports this module
        self.SpecError = SpecError
        self.g = gen
        self.spec = spec_massing
        name = spec_massing.get("typology")
        if name not in TYPOLOGIES or name.startswith("_"):
            raise SpecError(f"massing typology {name!r} unknown; known: {[k for k in TYPOLOGIES if not k.startswith('_')]}")
        self.typ = TYPOLOGIES[name]
        self.elev = {lv["id"]: lv.get("elevation_mm", 0) for lv in gen.levels}
        self.vols = {}
        for v in spec_massing.get("volumes", []):
            if v.get("role") not in TYPOLOGIES["_roles"]:
                raise SpecError(f"volume {v.get('id')!r}: role must be one of {sorted(TYPOLOGIES['_roles'])}")
            pts = gen._pts(v, "volume")
            poly = orient(Polygon([(gen.mm(x), gen.mm(y)) for x, y in pts]))
            for lv in v.get("levels", []):
                if lv not in self.elev:
                    raise SpecError(f"volume {v['id']!r} references unknown level {lv!r}")
            self.vols[v["id"]] = {**v, "poly": poly, "roof_cfg": {**self.typ["roofs"][v["role"]], **v.get("roof", {})}}
        self.resolved = {}

    # ------------------------------------------------------------------ helpers
    def plate_z(self, v):
        top = max(v["levels"], key=lambda lv: self.elev[lv])
        return self.elev[top] + v.get("plate_height_mm", self.g.wall_height), top

    @staticmethod
    def edges(poly):
        c = list(poly.exterior.coords)[:-1]
        return [(c[i], c[(i + 1) % len(c)]) for i in range(len(c))]

    # ------------------------------------------------------------------ 1. unassigned spaces (before walls)
    def add_unassigned_spaces(self):
        policy = self.spec.get("unassigned_space", self.typ["unassigned_space"])
        usage, name, tags = UNASSIGNED[policy]
        for vid, v in self.vols.items():
            if v["role"] not in ENCLOSED_ROLES:
                continue
            for lv in v["levels"]:
                rooms = [self.g.room_shape[r] for r in self.g.room_order if self.g.room_level[r] == lv]
                region = v["poly"].difference(unary_union(rooms)) if rooms else v["poly"]
                parts = [p for p in getattr(region, "geoms", [region]) if p.area > 5e5]
                for k, part in enumerate(parts):
                    part = orient(Polygon([(round(x), round(y)) for x, y in list(part.exterior.coords)[:-1]]))
                    rid = f"{vid}_{policy}_{lv}" + (f"_{k + 1}" if len(parts) > 1 else "")
                    self.g.room_order.append(rid)
                    self.g.room_level[rid] = lv
                    self.g.room_shape[rid] = part
                    self.g.room_usage[rid] = usage
                    self.g.rooms_json.append({
                        "id": rid, "name": name, "usage": usage, "type_name": "IfcSpace", "level": lv,
                        "boundary_polygon": {"unit": "mm", "closed": True,
                                             "points": [{"x": int(x), "y": int(y)} for x, y in list(part.exterior.coords)[:-1]]},
                        "area_m2": round(part.area / 1e6, 2), "tags": tags + [f"volume:{vid}"]})

    # ------------------------------------------------------------------ 2. envelope walls (after authored walls)
    def add_envelope_walls(self):
        for vid, v in self.vols.items():
            if v["role"] not in ENCLOSED_ROLES:
                continue
            for lv in v["levels"]:
                have = [LineString([(w["from"]["x"], w["from"]["y"]), (w["to"]["x"], w["to"]["y"])])
                        for w in self.g.walls_json if w["level"] == lv]
                cover = unary_union(have).buffer(1) if have else None
                n = 0
                for a, b in self.edges(v["poly"]):
                    seg = LineString([a, b])
                    rest = seg.difference(cover) if cover is not None else seg
                    for part in getattr(rest, "geoms", [rest]):
                        if part.length < 10:
                            continue
                        (x0, y0), (x1, y1) = part.coords[0], part.coords[-1]
                        n += 1
                        self.g._add_wall({"x": round(x0), "y": round(y0)}, {"x": round(x1), "y": round(y1)},
                                         f"env_{vid}_{lv}_{n}", lv, "exterior",
                                         {"type_name": "IfcWall", "derived_from": f"massing:{vid}"})

    # ------------------------------------------------------------------ 3. roofs
    def roofs(self, floor_tops: dict) -> list:
        out = []
        order = sorted(self.vols.values(), key=lambda v: 0 if v["role"] == "shell" else 1)
        for v in order:
            cfg = v["roof_cfg"]
            if cfg.get("form") in (None, "none"):
                continue
            out.append(self.roof_for(v, cfg, floor_tops))
        return out

    def attach_edge(self, v):
        target = self.vols.get(v.get("attach_to"))
        if not target:
            raise self.SpecError(f"volume {v['id']!r}: shed sloping away from its attachment needs attach_to")
        for k, (a, b) in enumerate(self.edges(v["poly"])):
            seg = LineString([a, b])
            if target["poly"].exterior.buffer(1).intersection(seg).length > 0.5 * seg.length:
                return k, target
        raise self.SpecError(f"volume {v['id']!r} does not share an edge with {v.get('attach_to')!r}")

    def roof_for(self, v, cfg, floor_tops):
        edges = self.edges(v["poly"])
        n = len(edges)
        if n != 4:
            raise self.SpecError(f"volume {v['id']!r}: roof generation supports rectangular volumes only (for now)")
        L = [math.dist(a, b) for a, b in edges]
        is_x = [abs(a[1] - b[1]) < 1 for a, b in edges]  # edge runs along x
        form = cfg["form"]
        slopes, defines, over = [0.0] * n, [False] * n, [0] * n
        info = {"form": form}
        if form == "gable":
            axis = cfg.get("ridge", "long_axis")
            if axis == "perpendicular_to_attachment":
                k_att, _t = self.attach_edge(v)
                axis = "y" if is_x[k_att] else "x"
            if axis == "long_axis":
                axis = "x" if max(L[k] for k in range(n) if is_x[k]) >= max(L[k] for k in range(n) if not is_x[k]) else "y"
            ang = pitch_deg(cfg["pitch"])
            for k in range(n):
                if is_x[k] == (axis == "x"):
                    slopes[k], defines[k], over[k] = ang, True, cfg.get("eave_overhang_mm", 0)
                else:
                    over[k] = cfg.get("rake_overhang_mm", 0)
            base, level = self.plate_z(v)
            info.update(ridge_axis=axis, pitch_deg=round(ang, 2))
            if cfg.get("attach") == "dies_into_host":
                # extend back over the host until this roof's ridge meets the host roof surface (valley)
                k_att, target = self.attach_edge(v)
                host = self.resolved[target["id"]]
                span = L[k_att]
                ridge_z = base + span / 2 * math.tan(math.radians(ang))
                ext = max(0.0, ridge_z - host["base_z"]) / math.tan(math.radians(host["pitch_deg"]))
                over[k_att] = int(round(ext))
                info.update(attached_edge=k_att, extends_into_host_mm=over[k_att], junction_with=f"roof_{target['id']}")
        elif form == "hip":
            ang = pitch_deg(cfg["pitch"])
            slopes, defines, over = [ang] * n, [True] * n, [cfg.get("eave_overhang_mm", 0)] * n
            base, level = self.plate_z(v)
            info.update(pitch_deg=round(ang, 2))
        elif form == "shed":
            k_att, target = self.attach_edge(v)
            k_low = (k_att + 2) % n
            depth = L[(k_att + 1) % n]
            if cfg.get("attach_height") == "below_eave":
                t_base = self.resolved[target["id"]]["base_z"]
                h_attach = t_base - 2 * CONST["roof_assembly_mm"] - CONST["roof_to_roof_clearance_mm"]
                covered = [r for r in v.get("covers", []) if r in floor_tops]
                if not covered:
                    raise self.SpecError(f"volume {v['id']!r}: cover roof needs `covers` naming rooms with floors")
                top_room = max(covered, key=lambda r: floor_tops[r])
                floor = floor_tops[top_room]
                cands = CONST["auto_pitch_candidates"] if cfg.get("pitch") == "auto" else [cfg["pitch"]]
                chosen = None
                for p in cands:
                    z_low = h_attach - depth * math.tan(math.radians(pitch_deg(p)))
                    if z_low - CONST["roof_assembly_mm"] - floor >= cfg.get("min_headroom_mm", 2134):
                        chosen = (p, z_low)
                        break
                if not chosen:
                    raise self.SpecError(
                        f"volume {v['id']!r}: no roof pitch in {cands} gives {cfg.get('min_headroom_mm', 2134)} mm headroom "
                        f"over the covered floor at z{floor} when attached below the eave of {target['id']!r} "
                        f"(z{t_base}); raise the plate height or reduce the cover depth")
                p, base = chosen
                level = self.g.room_level[top_room]
                info.update(attach_height_z=round(h_attach), headroom_mm=round(base - CONST["roof_assembly_mm"] - floor))
            else:
                p = cfg["pitch"]
                base, level = self.plate_z(v)
            ang = pitch_deg(p)
            slopes[k_low], defines[k_low] = ang, True
            for k in range(n):
                over[k] = 0 if k == k_att else (cfg.get("eave_overhang_mm", 0) if k == k_low else cfg.get("rake_overhang_mm", 0))
            info.update(pitch=p, pitch_deg=round(ang, 2), attached_edge=k_att, low_edge=k_low,
                        high_z=round(base + depth * math.tan(math.radians(ang))))
        elif form == "flat":
            base, level = self.plate_z(v)
            over = [cfg.get("eave_overhang_mm", 0)] * n
        else:
            raise self.SpecError(f"volume {v['id']!r}: unknown roof form {form!r}")
        info.update(base_z=round(base), level=level)
        self.resolved[v["id"]] = info
        return {"id": f"roof_{v['id']}", "type_name": "IfcRoof", "level": level,
                "level_offset_mm": int(round(base - self.elev[level])),
                "boundary_polygon": {"unit": "mm", "closed": True,
                                     "points": [{"x": int(round(a[0])), "y": int(round(a[1]))} for a, _ in edges]},
                "slope_angles": [round(s, 2) for s in slopes], "defines_slope": defines, "eave_overhang_mm": over,
                "derived_from": f"massing:{v['id']}"}

    # ------------------------------------------------------------------ 4. extension block
    def volumes_json(self):
        out = []
        for v in self.vols.values():
            out.append({"id": v["id"], "role": v["role"], "levels": v.get("levels", []),
                        "attach_to": v.get("attach_to"), "covers": v.get("covers", []),
                        "footprint": {"unit": "mm", "closed": True,
                                      "points": [{"x": int(round(x)), "y": int(round(y))} for x, y in list(v["poly"].exterior.coords)[:-1]]},
                        **({"plate_z_mm": self.plate_z(v)[0]} if v.get("levels") else {}),
                        "roof": {"id": f"roof_{v['id']}", **self.resolved.get(v["id"], {})}})
        return out
