# House-generation engine: architecture and reuse strategy

Response to [BRIEF.md](BRIEF.md). Status as of 2026-09-28. This is a proposal to settle before
building the application. Nothing here commits us to OAS; see §7.

---

## 1. What exists today (inventory)

Everything below lives in [`tools/oas_pipeline/`](../tools/oas_pipeline/) unless noted.

| Component | What it does | Reusable? | Still house-specific or limited |
|---|---|---|---|
| `generate.py` | Compiles a **design spec** into OAS-Layout JSON. It derives mm coordinates, wall adjacency, door swings, areas, the circulation graph and per-level files. | **Yes.** Proven on two unrelated specs (barndominium in feet, cabin in metres). | The spec is still written in coordinates: rooms, wall runs and opening offsets. Someone (today an LLM) must decide every number. |
| `validate.py` | Geometry and circulation checks: overlaps, gaps, walls, openings, swing clearance, reachability, guarded voids, stairs, egress, support by the level below. | **Yes.** No plan-specific ids. Thresholds are in a `Rules` object. | Rules are US/IRC-flavoured defaults. It checks correctness, not design *quality*. |
| `program.py` | Checks a layout against an OAS-Program brief: counts, sizes, adjacency, open-concept, daylight, balcony access, footprint, no-pass-through-bedrooms. | **Yes.** It is the first piece of the "House Brain". | Its vocabulary is small and there is no scoring, only pass/fail. |
| `exterior/model.py` | Builds a 3D **exterior model** from any OAS-Layout. Every element carries plan / derived / inferred provenance, and plan inconsistencies are reported. | **Yes.** Proven on the barndominium and the cabin (roof inferred when the plan has none). | Roof reconstruction handles rectangular gable/shed/hip/flat only. Post and beam rules are simple. |
| `exterior/consistency.py` | Checks the 2D plan against the 3D model, and optionally against what the renderer actually drew. | **Yes.** | Does not yet check photoreal images; see §5. |
| `exterior/viewer/` + `render.mjs` | three.js renderer: 4 orthographic elevations, perspective, aerial and provenance views. It draws only the model. | **Yes.** | Massing-quality rendering, not photoreal. Software-rendered here, which is slow. |
| `svg-viewer/` | Engineering floor-plan viewer: conventional black-and-white, floor selector, dimensions. | Engineering tool only (per brief). | Not the consumer renderer. It has no overall dimension strings and no area schedule yet. |
| `examples/barndominium_40x60/` | TEST CASE #1: spec, program (the brief as requirements) and exterior design. | Regression fixture. | `spec.json` is a hand-designed layout, the output of the LLM design step. |
| Scratch scripts from the first session (`gen.py`, `validate.py`, `scene.js`) | Superseded by the modules above. Their data became `spec.json`, and the regression test proves the output is byte-identical. | Retired. | `scene.js` hard-coded the barndominium and re-interpreted the design (see §9). |

**Takeaway:** the deterministic back half of the pipeline is reusable and fast, about **0.6 s** in total:

| Stage | Time |
|---|---|
| generate | 40 ms |
| validate | 385 ms |
| program check | 33 ms |
| exterior model | 70 ms |
| consistency | 39 ms |

The **missing piece is the front half**: going from homeowner language to a design spec *without an
LLM writing coordinates*. In the benchmark session, Claude hand-authored about 19k characters of
coordinates (roughly 5–6k output tokens) through trial and error. That is exactly the expensive, slow
and fragile step the product cannot afford per visitor.

---

## 2. Proposed architecture (simplest version that can work)

```
Homeowner text
   │ (1) INTAKE ─────────────── LLM, 1 call, small/fast model
   ▼
House Program  (OAS-Program + house-level facts: footprint, stories, garage, style words, must-haves)
   │ (2) CONCEPT ────────────── LLM, 0–1 call: picks typology + zoning strategy (a few hundred tokens)
   ▼
Zoning sketch  (rooms → zones/bands/levels, ordering, which rooms share walls; no coordinates)
   │ (3) SOLVER ─────────────── deterministic: sizes rooms from House Brain tables, places them,
   │                            derives walls from rooms, places doors/windows/stairs by rule,
   │                            emits N candidate design specs
   ▼
Design specs ×N ─(4) generate.py → OAS-Layout ×N          deterministic, ~40 ms each
   │ (5) VALIDATE + PROGRAM CHECK + SCORE ─────────────────── deterministic, <0.5 s each
   ▼
Top 1–3  ──(6) optional LLM JUDGE on a compact summary ── LLM, 1 call, only if scores are close
   │
   ├─(7) FLOOR-PLAN RENDERER (SVG→PNG/PDF) ─────────────── deterministic          → FREE product
   │
   └─(8) EXTERIOR MODEL → consistency → elevations/massing ── deterministic
          │ (9) PHOTOREAL: image model conditioned on our    image model, premium / content only
          │     depth/edge/segmentation passes + verification
          ▼
        Concept package PDF ────────────────────────────── deterministic          → PAID product

Edits ("make the pantry bigger") ─ LLM → structured edit ops on the program/zoning/spec → (3)…(8)
```

Key decisions:

1. **The LLM never writes coordinates in production.** It writes the *program* (what the house
   needs) and the *zoning sketch* (the concept: which rooms go where relative to each other). Deterministic code
   turns that into geometry. This single choice fixes cost, latency and correctness at once: the
   output shrinks from about 6k tokens of fragile numbers to a few hundred tokens of intent.
2. **Walls are derived, not authored.** The internal house model should be **rooms + openings
   between named rooms/facades**. Walls follow from shared room edges and the outer boundary. Today's
   spec still authors wall runs; removing that is the first engineering step (§9, M1).
3. **Typology-driven solver first, general solver later.** Most US homes fit a handful of
   typologies: rectangular box (barndominium, ranch), L, T, narrow lot, 2-story stacked. A solver
   per typology can use banded/slicing layouts (rows of rooms along a spine, like the barndominium:
   service band / open band / private wing). It is fast, predictable and easy to test. Add typologies
   rather than one universal optimiser.
4. **Candidates are cheap, so generate several.** At under a second each, running 5–10 candidates
   (different zonings or typologies) and ranking them costs nothing in tokens.
5. **The House Brain is data plus checks, not a prompt.** It holds:
   - room size and proportion tables by house size class;
   - adjacency preferences (a weighted matrix);
   - clearances, circulation limits, plumbing-stack preferences and privacy zoning.

   It is used three ways: (a) by the solver to size and place rooms, (b) by the validator and
   program checker to reject, and (c) by a scorer to rank. The LLM gets only the small part relevant to
   a given decision.
6. **Structured model is the only source of truth.** Every renderer (floor plan, elevation, 3D,
   photoreal) consumes it and is checked against it. Implemented today for 3D (§5).

---

## 3. Which stages use AI, which are deterministic

| Stage | AI? | Why |
|---|---|---|
| Intake: text → house program | **LLM** (small/fast) | Interprets intent. Fills defaults from the House Brain. Asks only preference questions (basement? garage side? style?). |
| Concept: typology + zoning | **LLM** (0–1 call) or rules | A real design decision. Rules can cover common cases, with the LLM for unusual requests. |
| Room sizing, placement, walls, openings, stairs | Deterministic | Geometry: House Brain tables plus solver. |
| OAS/JSON generation, areas, dimensions | Deterministic | `generate.py` today. |
| Validation, program check, scoring | Deterministic | `validate.py`, `program.py`, plus a scorer (new). |
| Choosing between near-equal candidates | **LLM judge** (optional) | Subjective residential quality. Run on a compact text summary or a small image, not the full JSON. |
| Design-level correction ("the master is too far from…") | **LLM** → edit ops | Deterministic repair first (widen a hall, move a door); the LLM only for concept changes. |
| User edits in plain English | **LLM** → structured edit ops | It never regenerates the house from scratch. |
| Floor-plan SVG/PDF, elevations, 3D massing, package PDF | Deterministic | Rendering. |
| Photoreal exterior/interior | **Image model** (premium/content) | Constrained by our geometry passes (§5). |
| Marketing copy / spec sheet prose | LLM (cheap) or templates | Templates first. |

---

## 4. Latency and cost drivers

| Driver | Today (dev prototype) | Target (production) |
|---|---|---|
| Agentic session: repo discovery, reading skills, writing and debugging code | Tens of minutes, very large token use | **Zero per user.** The code already exists and runs as a service. |
| LLM writing coordinates (design spec) | About 5–6k output tokens per attempt, plus retries after validation failures | **Eliminated** by the solver. The LLM emits about 1k tokens total across intake and concept. |
| Validation / retry loops through the LLM | Several rounds | Deterministic repair. The LLM is used at most once, for a concept-level fix. |
| Deterministic generate + validate + exterior | About 0.6 s per candidate (measured) | Same, ×N candidates (parallel). |
| Floor-plan rendering (headless browser) | Seconds | Render SVG server-side directly, with no browser needed. |
| 3D massing views | Minutes for 8 views (CPU software rendering in this container) | GPU rendering or pre-baked; only for premium/content. |
| Photoreal image generation | Not built | **Largest per-unit cost.** Premium and marketing only; never for anonymous free users; cache results. |
| LLM judge | n/a | Only when the top candidates score within a margin. |

Cost levers:
- **Model tiers:** a small model for intake and edits; a larger model only for concept or judging.
- **Prompt caching:** the House Brain excerpt and schemas are a stable prefix.
- **Output size:** keep LLM outputs to program-sized JSON.
- **Caching:** design results by normalised program (many visitors ask for "3 bed 2 bath ranch").
- **Rate limits:** apply to anonymous free sessions.

Target for a free design: **under 10–20 s wall-clock and 1–3 small LLM calls**.

---

## 5. Exterior: what exists now and what is still required

### Exists now: plan-consistent 3D exterior (`tools/oas_pipeline/exterior/`)
- **Exterior model:**
  - Plan elements: exterior walls with exact openings, porches, balconies, decks, railings, ground slabs.
  - Derived elements: roof planes from OAS roofs, gable and shed infill, floor-structure bands.
  - Inferred elements: posts, beams, grade, driveway, and a default roof when the plan has none.
  - Every element is labelled with its class and the rule that produced it.
- **Exterior design spec** (`design_defaults.json`, overridable per house, e.g. `examples/barndominium_40x60/exterior.json`): materials and the parameters behind inferred details. It is purely aesthetic and never moves geometry.
- **Consistency check (plan ↔ 3D ↔ drawn scene):**
  - footprint, exterior walls, floor heights, story extents;
  - doors, windows and garage doors;
  - porches, balconies, decks, railings;
  - exterior stairs, roofs, provenance, and the renderer audit.
- **Views:**
  - front, rear, left and right orthographic elevations;
  - front 3/4 perspective and aerial;
  - a *provenance* view that colours elements by class (plan / derived / inferred).

  "Front" is derived from the primary entry door, and can be overridden.

### Still required for elevations (deterministic, next)
1. **Line-drawing elevations:** orthographic, hidden-line, with dimension strings (floor and plate heights, ridge), in black and white for the package PDF. The 3D model already has the geometry.
2. **Roof generator:** most homeowner requests will not arrive with roofs. We need a deterministic roof planner from footprint + stories + style. It must handle gable/hip/shed and valleys over L/T footprints, plus porch roofs that tie into the main roof, and it must report and resolve roof/wall conflicts.
3. **Exterior style system:** style → materials, trim widths, window types and grille patterns, porch post sizes, roof pitch ranges, overhangs. It is data-driven and inferred-class.
4. **Openings catalogue:** window and door types with real proportions, so elevations look like real products.

### Still required for photoreal renders that match the plan
1. Render **conditioning passes** from the exterior model: depth, normals, edges and per-element segmentation (walls, each window, doors, roof, deck), in the exact camera the customer sees.
2. Use an image model that accepts structural conditioning (ControlNet-style or image-to-image with masks) so that the massing, openings and rooflines come from our passes and the model adds only materials, light and landscaping.
3. **Verify after generation:** compare edges and segmentation of the generated image against our passes (window count and position IoU, roofline deviation), and reject or retry when they diverge. This is the photoreal counterpart of `consistency.py`.
4. Keep our deterministic massing render alongside as ground truth. A "same house" guarantee should be demonstrable.

This is the main vendor and cost decision still open. It adds per-image cost and a GPU or API dependency; it should be premium-only, and cached for marketing content.

---

## 6. What OAS lacks for reliable exterior elevations

Found while building the exterior model. Each item is currently either inferred (labelled) or impossible.

| Gap | Why it matters | Today |
|---|---|---|
| **Massing.** No building volumes, no "what fills this story", no roof-bears-on-wall. *Now provided by our massing layer (§8).* | Otherwise floors and roofs can disagree while every per-floor check passes. | `massing` in the house model → OAS rooms/walls/roofs + `oas-massing` block. |
| **Roof semantics.** Per-edge slopes only; no ridge/hip/valley topology for non-rectangular roofs; `level_offset_mm` is ambiguous for shed roofs (eave or high edge?); no roof-to-wall or roof-to-roof junctions; no "this roof covers that porch". | Roofs are the dominant exterior mass. | Rectangular gable/shed/hip reconstructed; conflicts reported (barndominium: 3 unsupported edges and 1 overlap). |
| **What is above a space.** No ceiling type (flat, vaulted, open to roof), no attic/unconditioned volume, no knee walls. | Decides whether a one-story wing under a two-story roof is enclosed. That is the barndominium's open question. | Reported as `roof_edge_unsupported`. Optional inferred knee walls, flagged. |
| **Vertical build-up.** Floor structure depth, plate height vs floor-to-floor, and continuous multi-story exterior walls are not modelled. | Elevation band lines, eave heights, window head alignment. | Derived "floor-structure band". |
| **Grade and foundation.** No grade elevation, slope, foundation type (slab, crawl, basement), finished floor height above grade, steps or stoops. | Every elevation starts at grade; porches need steps. | Inferred grade 300 mm below the first floor. |
| **Exterior assemblies and materials per facade/zone.** The OAS-Materials extension is a stub with no facade mapping (wainscot, gable accent, trim). | Exterior style and photoreal conditioning. | Exterior design spec (ours). |
| **Opening types.** No window type (double-hung, casement, picture, fixed-over-awning), grilles, trim or frame size. Only `swing`/`sliding`; garage doors are not a type (we infer them from width plus the garage room). Which side is exterior is not explicit, and swing semantics are ambiguous. | Elevation fidelity; garage doors. | Style inferred by rule; exterior side derived from `adjacent_rooms`. |
| **Porch/deck/balcony structure.** No columns or posts as entities, no beams, no decking direction; railing height and style are missing; no porch stairs. | Front elevations of most houses. | Posts and beams inferred by rule; railing height inferred at 914 mm. |
| **Projections.** No bay windows, bump-outs, cantilevers, dormers, chimneys, shutters, gutters or downspouts. | Character and realism. | Not supported. |
| **Orientation and site.** OAS-Site is a stub: north angle, street/front side, setbacks, driveway, walks. | Which facade is "front"; solar and shadows; lot fit. | Front inferred from the entry door; driveway inferred from garage doors. |
| **Controlled vocabularies.** `usage`, `type_name` and tags are free strings. | Rules keyed on usage (garage, porch, stair) are fragile across generators. | We use a de-facto set; it needs a schema. |
| **Architectural style.** No style or design-intent link from the program to the exterior. | Drives roof, materials, windows. | Exterior design spec (ours). |

Recommendation: keep OAS-Layout as the **interchange/output format**, and define our own
**House Model** (versioned JSON schema). It contains:
- the program;
- rooms and openings-by-relationship (the authoring layer);
- derived OAS-Layout geometry;
- an exterior spec block (roof plan, style, assemblies, grade, orientation).

Where OAS has a natural place (Site, Materials), we write it there as extensions.

---

## 7. OAS: keep, extend or replace?

- **Keep** for 2D geometry interchange: rooms, walls, openings, levels and slabs fit well, and the
  skills help an LLM read and write it.
- **Do not force it** to be the authoring model. Coordinate-level walls and openings are the wrong
  abstraction for an LLM or a solver to *author*. They should be compiled outputs.
- **Extend** in our own namespace for the exterior gaps in §6.
- Re-evaluate after TEST CASE #2. If the House Model ends up carrying most of the meaning, OAS
  becomes just one export format, and that is fine.

---

## 8. Massing layer: the house model must describe the *building*, not only the floors

**Incident 2 (after the renderer fix).** The 3D model matched the plan exactly, yet it did not look
like the barndominium the homeowner described. Trace:

| Layer | What happened |
|---|---|
| Intent | "40x60 **two-story** barndominium" means one 40×60 shell, two stories tall, under one gable. |
| Floor plans | The finished second floor covered only 40' of it. Nothing said what fills the other 20' of that story. |
| OAS | Floors are rooms + walls per level. Roofs are free-floating entities. There is no building shell, no "what is above this room" and no roof-bears-on-wall relation, and a shed roof's height reference is ambiguous. |
| Our spec (house model) | Inherited the gap. Rooms, walls and roofs were authored independently: a 60' roof over a 40' second floor, and a balcony roof at a height that ran into the main eave. |
| Exterior model / renderer | Faithfully drew two contradictory facts: a floating roof and an open gable. |
| Consistency check | Passed, because it compared 3D against 2D and both came from the same contradictory data. |

**Fix: a massing layer in the house model** (`tools/oas_pipeline/massing.py`,
`tools/oas_pipeline/brain/typologies.json`):

- **The spec states volumes, not roofs.** Each volume has a footprint, the levels it spans, a role
  (`shell`, `wing`, `garage` or `cover`), what it attaches to, and which rooms a cover covers. It also
  names a typology. For the barndominium that is three small objects: shell 60×40 over both levels,
  a garage attached to it, and a balcony cover over `l1_porch` and `l2_balcony`.
- **Everything else is compiled deterministically from the typology rules:**
  - envelope walls wherever a volume's level has no wall;
  - the unassigned space inside a volume, which becomes a real room (`attic` for a barndominium,
    `open_to_below` for a two-story traditional) and so appears on the 2D plan too;
  - roofs, with one explicit height convention: OAS `level + level_offset` is the height of the
    sloped eave edges on the boundary line.
- **A cover roof hung "below the eave"** gets the steepest pitch that still leaves the required
  headroom over the covered floor: 1/4:12 here, giving 2192 mm. If none works, the generator stops
  with a clear design error.
- **New plan-level invariants**, checked by the validator before any 3D exists:
  - every roof edge bears on a wall or sits over an outdoor space;
  - roofs do not intersect;
  - no roof passes across a window or door;
  - covered outdoor floors have at least 2134 mm (7'-0") headroom.
- **The consistency check gains a `massing` section:**
  - each volume is fully enclosed on every level it spans (party walls with attached volumes excluded);
  - wall tops reach the volume's plate height;
  - the roof has the right form and ridge axis.

Result for the barndominium:
- The level-2 plan shows the full 60×40 shell. The east 20' is "Attic (unfinished)", reached by an
  access door from the Bedroom 3 closet.
- The exterior is one continuous two-story barn shell with closed gables.
- The garage has a 1:12 lean-to.
- The balcony cover is tucked under the main eave.
- The new obstruction check caught a real latent conflict: two loft windows were taller than the
  cover-roof ledger allows. They were shortened to 4'-0".

The typology rule resolves the ambiguity, not the homeowner. The barndominium typology is a
full-height shell with an attic for unassigned space. A two-story traditional is a set of stacked
volumes where lower-only areas become wings with their own roofs.

**Why OAS alone could not do this, and what the house model now carries:** building volumes and
their roles, volume-to-volume attachment, covers-to-rooms relationships, the typology, what fills
unassigned volume space, and roofs as consequences of volumes. These are emitted into OAS as
ordinary rooms, walls and roofs (roofs carry `derived_from: massing:<id>`) plus an `oas-massing`
extension block, so OAS consumers still get complete geometry.

## 9. Lesson from the 3D mismatch (why the rules in CLAUDE.md exist)

The first 3D render widened the second story to the full 60' and moved the balcony roof, because
the scratch renderer "fixed" what looked wrong. The root cause was that the renderer made
architectural decisions. The structural fix is in place:

- All interpretation lives in deterministic model code, with a provenance label on every element.
- The renderer only draws.
- A consistency check compares the plan, the 3D model and the drawn scene.
- Plan inconsistencies are reported, never silently repaired.

The same principle must hold for photoreal images (§5) and for any future consumer renderer.

---

## 10. Milestones: prove a fast, reusable, affordable engine

| # | Milestone | Proves |
|---|---|---|
| M0 ✅ | **Massing layer** (§8): volumes + typology rules → envelope walls, unassigned spaces, roofs; plan-level roof/opening/headroom invariants. | 2D and 3D compile from one building description. |
| M1 | **House Model v0 + walls-from-rooms.** Authoring = rooms (polygons or zone slots) + openings by room pair/facade; walls and exterior openings derived. Re-express the barndominium in it; the regression test must still produce the same plan. | The authoring surface an LLM or solver needs is small. |
| M2 | **House Brain v0 as data.** Room size/proportion tables, adjacency matrix, clearances, window rules, privacy zones, plus a **scorer** (circulation %, dead area, adjacency satisfaction, proportions, wet-wall clustering). | Rules are explicit and testable. |
| M3 | **Typology solver v0** for rectangular 1- and 2-story shells (banded layouts), placing doors, windows and stairs by rule and emitting N candidates. | No LLM coordinates. |
| M4 | **Intake prompt → house program** (one small-model call) + optional concept call. | Homeowner language in, program out. |
| M5 | **TEST CASE #2 end-to-end**, timed and costed, plus the barndominium re-run through the same path. | Generalisation and cost. |
| M6 | Consumer floor-plan renderer (server-side SVG→PDF: overall dimensions, area schedule, disclaimer). | Free product quality. |
| M7 | Roof planner + line-drawing elevations from the exterior model. | Premium elevations. |
| M8 | Photoreal pipeline with conditioning passes + verification. | Premium hero image that matches the plan. |

Success criteria for M5: under 20 s wall-clock, at most 3 LLM calls, about 3k LLM output
tokens total, 0 validator errors, the program check passes, and a human reviewer rates the plan
"reasonable to hand to a builder".

---

## 11. TEST CASE #2 (proposed)

> "We'd like a one-story ranch, about 1,800 square feet, three bedrooms and two bathrooms. Master
> bedroom away from the kids' rooms. Open kitchen and family room with a big island. Two-car garage
> attached at the front. Covered patio out back. Our lot is 70 feet wide."

Why this one:

| Barndominium (#1) | Ranch (#2) |
|---|---|
| Two stories, stairs, loft, open-to-below, balcony | One story: no stairs, no voids |
| Footprint given exactly (40×60) | Only an **area target** and a **lot width**. The engine must choose the footprint and shape (likely L-shaped with a front-loading garage). |
| Garage on the side (side-load) | Garage at the front (front-load); the entry must still read as the front door |
| Master downstairs, others upstairs | **Split-bedroom** plan on one level (privacy zoning) |
| 1 full + 1 half bath downstairs, 1 bath upstairs | 2 full baths: master en-suite + hall bath shared by two bedrooms |
| Balcony and porch at the front | Covered patio at the **rear**; the roof must extend over it |
| Gable box roof | **Hip or L-gable roof** (tests the roof planner) |

It needs no technical dimensions from the homeowner: only square footage, bed/bath counts and lot
width, which people naturally know. It exercises area-driven sizing, L-shaped massing, privacy
zoning, a front garage and a rear covered outdoor space.

Next benchmarks after it: a narrow-lot two-story (≤ 30' wide), a house without a garage, and a
larger luxury home.
