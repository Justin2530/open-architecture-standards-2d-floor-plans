# OAS SVG Viewer

A dependency-free, single-page SVG viewer for [Open Architecture Standards](../docs/index.md) (OAS) JSON floor plans. It draws rooms, walls, doors, windows and railings as a conventional black-on-white architectural plan: room names with dimensions (or area for non-rectangular rooms), door swings, window symbols, hatched exterior spaces, an "open to below" cross, a title block, a graphic scale and a north arrow.

## Run it

No build, no install, no server. Open `index.html` in any modern browser, then click **Upload JSON** and select a plan file.

```bash
open index.html       # macOS
xdg-open index.html   # Linux
start index.html      # Windows
```

If your browser blocks local file access for some reason, serve the folder with any static server, e.g.:

```bash
python3 -m http.server 8000
# then open http://localhost:8000
```

## Try the examples

- [examples/apartment_50m2.json](examples/apartment_50m2.json): a 50 m² single-level apartment with entry hall, living/kitchen, bedroom and bathroom.
- [examples/barndominium_40x60.json](examples/barndominium_40x60.json): a two-story 40'×60' barndominium with attached garage. Use the **First Floor / Second Floor** selector to switch levels. `barndominium_40x60_level1.json` / `_level2.json` are single-level extracts of the same plan. [previews/](examples/previews/) has screenshots. The plan is generated from a spec by [`tools/oas_pipeline`](../tools/oas_pipeline/).

## Controls

- **Upload JSON** — load an OAS plan file
- **Floor selector** (**First Floor / Second Floor**, ...) — appears when the plan defines two or more `levels`; shows only that level's rooms, walls, openings and railings. All levels share one frame, so floors stay aligned when you switch.
- **ft-in / m** — units for room dimensions, areas and the scale bar
- **Mouse drag** — pan
- **Mouse wheel** — zoom around the cursor
- **+ / − buttons** — zoom in / out
- **Fit** — fit plan to screen

## Editing plans

This viewer is read-only by design. To create or edit OAS JSON, use the OAS skills under [.claude/skills/](../.claude/skills/) with Claude Code — describe what you want in natural language and Claude will produce schema-correct JSON you can drop into [examples/](examples/).

Most relevant skills for plans you'd load here:

- [`oas-layout`](../.claude/skills/oas-layout/SKILL.md) — multi-room plans with walls, doors, windows, and circulation
- [`oas-core`](../.claude/skills/oas-core/SKILL.md) — single-room work or the basic entity vocabulary
- [`oas-geometry`](../.claude/skills/oas-geometry/SKILL.md) — coordinate conventions, polygon rules, unit gotchas

See the [root README](../README.md#skills-for-claude-code) for the full skill index.

## Files

| File | Purpose |
|------|---------|
| `index.html` | Page markup, header, viewer container, zoom controls |
| `app.js` | Plain-JS renderer (rooms, walls, openings, railings, labels, title block), level filter, pan/zoom |
| `style.css` | Styling for the viewer chrome and SVG primitives |
| `examples/` | Sample OAS JSON plans |
