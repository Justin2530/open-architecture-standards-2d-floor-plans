# OAS floor-plan pipeline

A reusable pipeline for producing residential floor plans as OAS-Layout JSON:

```
design spec ──generate──▶ OAS-Layout JSON ──validate──▶ report
(spec.json)               (+ one file per level)   └─program check (program.json)
                                                    └─render (svg-viewer screenshots)
```

The **design spec** holds only the designer's decisions (room outlines, wall runs, where
doors and windows go). Everything derivable is computed, so the output can't contradict itself.
The **validator** checks geometry and circulation for any OAS-Layout file. The **program
check** tests a layout against a brief written in [OAS-Program](../../docs/program.md) form,
which is how different residential requirements plug in.

## Quick start

Requires Python 3.9+ with `shapely` (`pip install -r tools/oas_pipeline/requirements.txt`).
Rendering also needs Node.js and the `playwright` package.

```bash
# full pipeline: generate, validate, check the brief, screenshot each floor
python3 tools/oas_pipeline build tools/oas_pipeline/examples/barndominium_40x60/spec.json \
    -o out/ --program tools/oas_pipeline/examples/barndominium_40x60/program.json --render

# individual steps
python3 tools/oas_pipeline generate SPEC.json -o out/
python3 tools/oas_pipeline validate PLAN.json [--program PROGRAM.json] [--json]
python3 tools/oas_pipeline render   PLAN.json -o out/

# tests
python3 -m unittest discover -s tools/oas_pipeline/tests
```

`build` and `validate` exit non-zero when there are errors.

## Examples

| Example | What it shows |
|---|---|
| [`examples/barndominium_40x60/`](examples/barndominium_40x60/) | Two-story 40'×60' barndominium with garage, loft, open-to-below great room and covered balcony. Authored in feet. Its compiled output is the committed viewer example [`svg-viewer/examples/barndominium_40x60.json`](../../svg-viewer/examples/barndominium_40x60.json); a regression test keeps them identical. `program.json` encodes the original brief. |
| [`examples/simple_cabin/`](examples/simple_cabin/) | Minimal single-level, metric spec: the smallest useful starting point. |

To start a new design, copy `simple_cabin/`, write a `program.json` for the brief, then iterate
on `spec.json` until `build` reports no errors.

## Design spec format

```jsonc
{
  "plan_id": "my_house", "title": "My House",
  "output_basename": "my_house",          // optional, default plan_id
  "authoring_unit": "ft",                 // mm | cm | m | in | ft; coordinates and offsets use this
  "normalize_origin": true,               // shift so no coordinate is negative (the viewer needs this)
  "defaults": { "wall_height_mm": 2743, "thickness_mm": { "exterior": 152, "interior": 114 } },
  "levels": [ { "id": "level_01", "name": "First Floor", "elevation_mm": 0 } ],

  "rooms": [
    { "id": "kitchen", "name": "Kitchen", "usage": "kitchen", "level": "level_01",
      "rect": [38, 26, 60, 40], "tags": ["open_plan"] },               // or "points": [[x,y], ...] CCW
  ],

  "walls": [
    // a run along y = 0 split at each break; segments are named <prefix>_1, <prefix>_2, ...
    { "prefix": "w_south", "level": "level_01", "y": 0, "breaks": [0, 20, 31, 60], "kind": "exterior" },
    // "skip": [1] leaves segment 2 out (a cased opening / open plan edge), numbering unchanged
    { "prefix": "w_x31", "level": "level_01", "x": 31, "breaks": [0, 2, 8, 9], "skip": [1] },
    // or a single wall: { "id": "...", "level": "...", "from": [x, y], "to": [x, y], "kind": "interior" }
  ],

  "openings": [
    { "id": "d_front", "type": "door", "wall": "w_south_2", "offset": 6, "width_mm": 914,
      "operation": "swing", "swing_into": "foyer", "hinge": "right", "label": "Front Door" },
    { "id": "win_1", "type": "window", "wall": "w_south_3", "offset": 2.5, "width_mm": 1219,
      "sill_mm": 914, "operation": "fixed" }                          // offset = from the wall's `from` end
  ],

  "railings":    [ { "id": "r1", "level": "level_02", "points": [[38,0],[38,22]], "host_type": "floor" } ],
  "floor_slabs": [ { "id": "s1", "level": "level_01", "rect": [0, 0, 60, 40] } ],
  "roofs":       [ { "id": "roof", "level": "level_02", "rect": [...], "slope_angles": [...],
                     "defines_slope": [...], "eave_overhang_mm": [...] } ],   // one entry per edge
  "extra_connections": [ { "from": "l1_foyer", "to": "l2_hall", "type": "through" } ],
  "metadata": { "generated_by": "...", "notes": "..." }
}
```

What the generator derives:

- **Units:** integer millimetres everywhere; `area_m2` computed from each polygon.
- **Room relationships:** `adjacent_rooms` for walls and `connects_rooms` for openings, found by probing either side.
- **Door swings:** `swing_direction` from `swing_into`. It uses the viewer's convention: `inward` means the leaf swings to the left of the wall's from→to direction.
- **Circulation graph (`connections`):** built from doors, shared room boundaries with no wall or railing (open plan), and stair rooms with identical footprints on different levels.
- **Per-level files:** one viewer-ready file per level.

The generator raises `SpecError` on inconsistencies, for example a door swinging into a room
that isn't beside its wall, or an unknown wall or level.

## Validation checks

`validate.py` works on any OAS-Layout file. Thresholds are in `Rules`, with defaults drawn
from common US residential practice (IRC).

- **Schema:** unique ids, integer millimetres, polygons closed, simple and counter-clockwise, `area_m2` correct.
- **Each level:** no overlapping rooms, no gaps inside the floor plate.
- **Walls:** each lies on a room boundary, `adjacent_rooms` is correct, no duplicate or overlapping walls.
- **Openings:** each fits inside its wall, stays clear of wall ends and other openings, and `connects_rooms` is correct.
- **Door swings:** stay inside the room they open into, hit no other wall, and don't collide with other swings.
- **Circulation:** every room is reachable from outside, and every open-to-below void is guarded by walls or railings.
- **Stairs:**
  - footprints match between levels;
  - riser, tread and width limits are met, and the run is long enough;
  - the bottom and top landings open onto walkable rooms (direction is detected automatically).
- **Bedrooms:** each has an exterior window at least 610 mm wide.
- **Upper levels:** every upper room sits over the level below.

## Program (requirements) checks

`program.py` resolves each requirement to layout rooms. It matches by `usage` (optionally narrowed
by `level`, with an expected `count`), or you can name rooms explicitly with `layout_rooms`.
Supported checks:

- **Room size:** `desired_area_m2` `{min, max}`, and `exact_dims_m` for a required rectangle.
- **`must_have`:**
  - `daylight`: a window to outside or onto a porch/balcony;
  - `exterior_access`;
  - `balcony_access`.
- **`adjacency`:**
  - `must_touch`, `should_touch`, `avoid_touch`: whether rooms share a boundary;
  - `must_connect`: a door or open passage between them;
  - `must_open_to`: a wall-less shared boundary (open concept).
- **Global:** `stories`, `min_area_m2` / `max_area_m2` / `target_area_m2` on conditioned area, and
  `building_footprint` (a rectangle of the given size).
- **Circulation:** `no_pass_through_usages`, for example: no room may be reached only by walking through a bedroom.
  Closets and en-suite baths are allowed off a bedroom via `allow_behind_usages`.

## Files

| File | Purpose |
|---|---|
| `generate.py` | spec → OAS-Layout compiler, per-level extracts |
| `validate.py` | `PlanModel` (geometry + circulation graph), `validate()`, `Rules` |
| `program.py` | `check_program()` against an OAS-Program brief |
| `render.mjs` | Playwright script: screenshot every level in `svg-viewer` |
| `__main__.py` | CLI (`build`, `generate`, `validate`, `render`) |
| `tests/` | regression, fault-injection and spec-error tests |
