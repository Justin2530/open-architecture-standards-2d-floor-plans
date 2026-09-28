# Project guide for Claude

This repository is the development base for an **AI home concept-design engine**: a homeowner
describes a house in plain English and gets a validated floor plan (free) and a matching exterior
package (paid). Read [product/BRIEF.md](product/BRIEF.md) (governing brief) and
[product/ARCHITECTURE.md](product/ARCHITECTURE.md) (agreed direction) before significant work.

## Non-negotiable rules

1. **The structured house model is the single source of truth.** Every renderer (floor plan,
   elevation, 3D, photoreal) consumes it. A renderer must never reposition, resize or reinterpret a
   wall, door, window, garage door, porch, balcony, deck, stair or any other defined element.
2. **Label everything that is not in the plan.** Exterior elements are `plan`, `derived`
   (deterministic from plan data) or `inferred` (design assumption), each with a rule. Plan
   inconsistencies are **reported, not silently fixed**.
3. **Deterministic code over tokens.** Geometry, walls, dimensions, areas, validation, rendering
   and files are code. The LLM handles intent, concept decisions, judgment and edits, and emits
   small structured outputs (a program or edit ops), never coordinates in production.
4. **Do not hard-code the benchmark house.** The 40'×60' barndominium is TEST CASE #1 only.
   New logic must work on the cabin example too, and eventually on TEST CASE #2 (see ARCHITECTURE §11).
5. **Describe the building, not just the floors.** Roofs, envelope walls and unassigned upper space
   are compiled from the spec's `massing` volumes + typology rules (`tools/oas_pipeline/brain/`),
   never authored independently of the floors (ARCHITECTURE §8).
6. **Concept design only.** Outputs carry "CONCEPT DESIGN — NOT FOR CONSTRUCTION".
7. **Flag cost, latency and lock-in.** Before adding anything that creates recurring AI cost,
   latency, vendor lock-in or significant complexity, say so.

## Commands

```bash
pip install -r tools/oas_pipeline/requirements.txt              # shapely
python3 -m unittest discover -s tools/oas_pipeline/tests        # all tests (must pass)

# floor plan: spec -> OAS-Layout -> validate -> program check (-> render)
python3 tools/oas_pipeline build tools/oas_pipeline/examples/barndominium_40x60/spec.json -o out/ \
    --program tools/oas_pipeline/examples/barndominium_40x60/program.json

# exterior: plan -> 3D exterior model -> consistency check (-> render views + renderer audit)
(cd tools/oas_pipeline/exterior && npm install)                 # three.js (+ playwright)
python3 tools/oas_pipeline exterior svg-viewer/examples/barndominium_40x60.json -o out/ \
    --design tools/oas_pipeline/examples/barndominium_40x60/exterior.json --render
```

Set `CHROMIUM_PATH` if the local Playwright cannot find a browser.

## Regression fixtures

- `svg-viewer/examples/barndominium_40x60*.json` must equal what
  `tools/oas_pipeline/examples/barndominium_40x60/spec.json` compiles to (tests enforce this).
  Change the spec, regenerate, and commit both.
- The 3D exterior is never edited by hand. It is rebuilt from the plan, and the consistency
  check must report 0 errors.
