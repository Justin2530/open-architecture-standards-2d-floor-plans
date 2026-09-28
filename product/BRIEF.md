# Master product brief

> Recorded verbatim from the project owner on 2026-09-28. This is the governing brief for all
> technical and design decisions in this repository. Future work should support this goal rather
> than optimizing only for the current 40' × 60' barndominium example.
> The architecture response is in [ARCHITECTURE.md](ARCHITECTURE.md).

==================================================
THE PRODUCT
==================================================

We want to build a consumer-facing AI home concept-design platform.

The simplest description is:

"Describe the house you want and AI designs it for you."

This is NOT intended to replace a licensed architect, structural engineer, residential designer, or builder.

It is intended to bridge the enormous gap between:

"I have an idea for the house I want"

and:

"Here is a detailed visual concept I can give to an architect, engineer, builder, or residential designer to turn into construction documents."

The primary customer is an ordinary homeowner.

They generally DO NOT know:

- appropriate room dimensions
- hallway widths
- stair geometry
- architectural terminology
- wall coordinates
- plumbing layouts
- structural grids
- window dimensions
- optimal adjacencies
- circulation requirements
- CAD software

They should not need to.

The AI system is supposed to make those decisions intelligently.

==================================================
THE CORE USER EXPERIENCE
==================================================

A homeowner should be able to type something as simple as:

"I want a 40x60 two-story barndominium. Put a 20x40 garage on one end. I want an open kitchen, living and dining area, master suite downstairs, decent pantry, two bedrooms and a bathroom upstairs, a loft and a 12x40 covered second-story balcony."

That should be enough.

The system should infer reasonable details the customer didn't specify.

For example:

- room dimensions
- room proportions
- room placement
- closets
- pantry placement
- mudroom/laundry relationships
- hallways
- circulation
- staircase location
- door locations
- door swings
- window locations
- exterior entrances
- garage-to-house connection
- plumbing relationships where practical
- logical furniture clearances
- upstairs/downstairs relationships
- open-to-below areas where appropriate
- balcony/porch access
- sensible exterior massing

The system should behave like an experienced residential concept designer.

Do NOT solve uncertainty by constantly asking the homeowner technical questions.

If there is a reasonable residential design decision that can be made automatically, make it.

Only ask the homeowner questions when their preference genuinely matters.

For example:

"Do you want a basement?"

is reasonable.

"What dimensions should Bedroom 2 be?"

is generally NOT reasonable.

The customer came to us because they want the system to design the house.

==================================================
WHAT WE ARE NOT BUILDING
==================================================

We are NOT initially producing:

- stamped plans
- structural engineering
- foundation engineering
- truss engineering
- electrical engineering
- HVAC engineering
- plumbing engineering
- permit-ready construction documents
- code certification
- site-specific engineering

Outputs must be clearly identified as conceptual designs.

Something similar to:

"CONCEPT DESIGN — NOT FOR CONSTRUCTION"

The customer should be able to take the resulting package to a builder, architect, engineer, or residential designer and say:

"This is the house I want. Turn this concept into construction documents."

==================================================
THE FREE PRODUCT
==================================================

The free experience is extremely important because it is our traffic acquisition engine.

A visitor should be able to describe a house and receive a useful design without entering CAD data or learning architectural software.

The free result should include clean, attractive, conventional black-and-white architectural floor plans.

At minimum:

- first-floor plan
- additional floors when applicable
- room names
- room dimensions
- overall building dimensions
- doors
- windows
- stairs
- closets
- major fixtures where useful
- garage
- porches/decks/balconies
- beds/baths
- approximate conditioned square footage
- garage square footage
- porch/deck/balcony square footage

The visual style should resemble a clean residential architectural floor plan.

White background.
Black/dark walls.
Readable black labels.
Clear dimensions.

Do NOT use the dark visualization style of the current Autodesk SVG viewer as the final consumer UI.

The Autodesk/OAS viewer is currently an engineering/prototyping tool for us, not necessarily the final customer renderer.

==================================================
THE PAID PRODUCT
==================================================

The current monetization concept is:

Free floor-plan generation.

Then offer a premium complete home concept package for approximately $29.99 one-time.

Potential subscription plans can later exist for users who create many houses, such as builders, real-estate professionals, content creators, developers, etc.

The premium package should make the customer's design feel like a real house.

Potential premium outputs include:

- high-resolution floor plans
- front exterior elevation
- rear exterior elevation
- left exterior elevation
- right exterior elevation
- photorealistic front 3/4 exterior rendering
- additional exterior rendering angles
- house specifications
- downloadable complete concept-plan PDF

Potential future premium outputs could include:

- interior kitchen visualization
- great-room visualization
- master-suite visualization
- alternative exterior materials/colors
- alternate roof treatments
- additional design variations

The $29.99 package should feel substantially more valuable than merely downloading the same free floor plan.

==================================================
EXTERIOR DESIGN IS CRITICAL
==================================================

Exterior visualization is not optional to the long-term product.

It is essential for both monetization and traffic acquisition.

Home-design content on Facebook, Pinterest, Instagram, TikTok and similar platforms is heavily driven by beautiful exterior imagery.

A social post should potentially contain:

1. Beautiful photorealistic exterior hero image
2. First-floor plan
3. Second-floor plan
4. Basic specifications
5. CTA: "Design yours free."

The exterior image is the emotional hook.

The floor plan demonstrates that the house is an actual design rather than merely AI artwork.

==================================================
CRITICAL RULE: EXTERIOR AND FLOOR PLAN MUST MATCH
==================================================

We do NOT want an image generator independently hallucinating a pretty house unrelated to the floor plan.

The structured floor plan must remain the source of truth.

Exterior generation should derive as much information as possible from the structured design:

- building footprint
- story count
- exterior wall locations
- garage location
- garage doors
- exterior doors
- window locations
- window proportions
- porches
- balconies
- decks
- major roof masses
- floor heights
- major projections/recesses

From this information we should create a structured exterior specification.

A visualization model can then add aesthetic choices such as:

- siding
- stone
- brick
- timber
- colors
- landscaping
- lighting
- atmosphere
- photorealism

But it should not be allowed to arbitrarily redesign the house.

The floor plan and exterior should look like they belong to the SAME HOUSE.

==================================================
SOCIAL / SEO TRAFFIC STRATEGY
==================================================

A major part of this business depends on generating substantial organic traffic.

Potential acquisition channels include:

- Google SEO
- Google Images
- Pinterest
- Facebook
- Instagram
- TikTok
- other visual/social platforms

We can also generate our own house concepts as content.

Example:

"AI designed this 2,400-square-foot barndominium for a family of four."

Then show:

Exterior rendering
→ Floor 1
→ Floor 2
→ specifications
→ "Design your own free."

Users should also eventually be able to share their own designs.

Shareable images/cards should contain subtle product branding/domain information so shared designs can generate additional traffic.

==================================================
BUSINESS ECONOMICS MATTER
==================================================

This product could eventually receive thousands or tens of thousands of visitors.

Therefore AI usage cost is a major architectural constraint.

We cannot design a system where every anonymous free visitor causes an expensive autonomous coding/agent session.

The current Claude Code prototype may take many minutes because Claude is:

- discovering the repository
- reading skills
- understanding OAS
- writing generators
- writing validators
- debugging
- rendering
- inspecting results

That is acceptable during DEVELOPMENT.

It is NOT acceptable as the eventual production workflow.

The production system should reuse the infrastructure we build now.

We want expensive AI reasoning only where it creates actual value.

Anything that can reliably be deterministic software should eventually become deterministic software.

Examples:

AI SHOULD handle things such as:

- interpreting homeowner intent
- resolving ambiguous requirements
- architectural concept decisions
- room relationships
- design alternatives
- judging overall residential usability
- correcting higher-level design problems

DETERMINISTIC CODE SHOULD handle as much as practical:

- geometry construction
- walls
- dimensions
- square-foot calculations
- SVG generation
- PDF generation
- overlap detection
- containment
- door/window boundary checks
- stair geometry checks
- connectivity
- room area calculations
- file generation
- rendering
- other mathematically verifiable requirements

Do not use AI tokens to repeatedly solve deterministic problems that software can solve once.

Generation speed and cost must be treated as first-class product requirements.

==================================================
CURRENT AUTODESK / OAS EXPERIMENT
==================================================

We connected this project to Autodesk's Open Architecture Standards 2D Floor Plans repository.

The first benchmark was:

40' × 60' two-story barndominium
20' × 40' garage on one end
open kitchen/living/dining
master downstairs
pantry
two bedrooms upstairs
full bathroom upstairs
loft
12' × 40' second-story balcony

Claude successfully:

- interpreted the homeowner-style description
- selected room dimensions itself
- designed room relationships
- generated structured OAS geometry
- placed walls
- placed doors
- placed windows
- designed stairs
- created multiple levels
- generated OAS JSON
- rendered it using the repository SVG viewer
- wrote a generator script
- wrote a separate validator
- intentionally injected faults to test the validator
- achieved 0 validator errors and 0 warnings

Some good residential reasoning emerged, including:

garage
→ mudroom/laundry
→ pantry
→ kitchen

and placing the master suite at the opposite end of the house.

This is encouraging.

However, the generator and validator created during that experiment were described as scratchpad scripts and may contain assumptions specific to this particular barndominium.

We do NOT want to lose useful work from that experiment.

==================================================
WHAT I WANT YOU TO BUILD TOWARD
==================================================

We want to turn the successful parts of that experiment into a reusable HOUSE GENERATION ENGINE.

Conceptually:

HOMEOWNER NATURAL LANGUAGE
        ↓
REQUIREMENTS INTERPRETER
        ↓
ARCHITECTURAL DESIGN / PLANNING
        ↓
STRUCTURED HOUSE MODEL
        ↓
GEOMETRY GENERATOR
        ↓
DETERMINISTIC VALIDATOR
        ↓
DESIGN QA / CORRECTION
        ↓
FLOOR PLAN RENDERER
        ↓
FREE FLOOR PLAN

For premium customers:

STRUCTURED HOUSE MODEL
        ↓
EXTERIOR GEOMETRY / MASSING SPECIFICATION
        ↓
ELEVATIONS
        ↓
PHOTOREALISTIC VISUALIZATION
        ↓
CONCEPT PACKAGE
        ↓
PDF

OAS may be the structured representation, or it may become one component of a larger internal house model.

Do not force OAS to solve problems it was not designed to solve.

We are using the Autodesk repository because structured geometry is valuable.

We are NOT committed to Autodesk/OAS if testing demonstrates that another representation or architecture is better.

==================================================
THE "HOUSE BRAIN"
==================================================

Long term, I want this system to become increasingly knowledgeable about residential design.

It should have explicit design rules rather than relying entirely on whatever an LLM happens to remember.

Examples:

- sensible bedroom proportions
- reasonable closet relationships
- kitchens relate appropriately to dining areas
- pantries relate appropriately to kitchens
- mudrooms relate appropriately to garages/exterior entrances
- avoid unnecessary hallways
- avoid inaccessible rooms
- avoid doors colliding
- avoid absurd room shapes
- stairs must physically fit
- upper levels must relate logically to lower levels
- plumbing should be consolidated when reasonable
- bedrooms need appropriate exterior windows
- furniture should reasonably fit
- bathrooms need usable clearances
- circulation paths should be obvious
- exterior openings should correspond with interior spaces
- balconies require actual access
- garage entry should make sense
- avoid large unusable dead areas
- room placement should consider privacy
- public and private areas should be sensibly organized

These rules should eventually be documented, testable and improved over time.

Do not hide every design rule inside a giant LLM prompt.

Where practical, encode rules as structured data or deterministic validation.

==================================================
MULTIPLE CANDIDATES
==================================================

Long term, the system may internally generate multiple possible layouts.

For example:

Generate 3–10 candidate layouts.

Validate them.

Reject invalid layouts.

Score remaining layouts according to objective criteria.

Use AI judgment for subjective residential-design quality where appropriate.

Then either:

- present the best design, or
- present 2–3 strong alternatives.

Do not assume the first layout generated is necessarily the best layout.

==================================================
EDITING / REVISIONS
==================================================

Eventually users should be able to say:

"Make the pantry bigger."

"Move the master bedroom to the back."

"I want the kitchen island larger."

"Add another bedroom upstairs."

"Make the garage 30 feet wide."

"Move the laundry closer to the bedrooms."

The system should modify the structured house model rather than generating an unrelated floor plan from scratch.

Eventually simple direct manipulation may also be useful:

- drag wall
- resize room
- move door
- move window

The underlying design must therefore remain structured and editable.

==================================================
PRODUCT PRINCIPLES
==================================================

1. HOMEOWNERS SPEAK NORMAL ENGLISH.

Do not require them to become architects.

2. AI MAKES REASONABLE DESIGN DECISIONS.

That is the primary value proposition.

3. STRUCTURED GEOMETRY IS THE SOURCE OF TRUTH.

Do not build the product around AI-generated floor-plan images.

4. VISUALIZATION MAY USE GENERATIVE AI.

Photorealistic exterior/interior imagery can use image generation, but it should be constrained by the actual design.

5. FREE MUST BE USEFUL.

The free floor plan should be something a homeowner is excited to receive.

6. PREMIUM MUST FEEL SIGNIFICANTLY BETTER.

The paid concept package should make their house feel real.

7. SPEED MATTERS.

A consumer should not wait 20 minutes for routine generation.

8. COST MATTERS.

Thousands of free users cannot each trigger expensive autonomous agent sessions.

9. VALIDATION MATTERS.

A pretty but nonsensical house is not acceptable.

10. WE ARE BUILDING CONCEPT DESIGNS, NOT PRETENDING TO PROVIDE ENGINEERING.

Maintain that distinction throughout the product.

==================================================
YOUR ROLE DURING DEVELOPMENT
==================================================

While working in this repository, do not behave as though your only objective is to complete the next coding request.

Act as the technical partner building toward this product.

Before introducing architecture that creates significant recurring AI cost, latency, vendor lock-in, or technical complexity, identify that consequence.

Preserve useful work already created.

Prefer reusable components over one-off scripts.

Do not hard-code solutions to the 40' × 60' barndominium.

Use that house as TEST CASE #1.

We need to prove the system works across substantially different houses.

Future benchmark examples should include things such as:

- simple single-story ranch
- two-story traditional house
- barndominium
- narrow house
- house with attached garage
- house without garage
- split bedroom layout
- larger luxury home

The system should generalize.

==================================================
IMMEDIATE OBJECTIVE
==================================================

Do NOT immediately start rewriting everything.

First:

1. Read this product brief.
2. Examine what you created during the 40' × 60' barndominium experiment.
3. Examine the relevant Autodesk/OAS architecture and skills.
4. Identify what portions of the generator and validator are reusable versus hard-coded to the benchmark house.
5. Preserve the useful generator/validator work in the repository.
6. Propose the simplest architecture that could turn this prototype into a reusable home concept-design engine.
7. Explicitly identify which stages should use Claude/AI and which should become deterministic code.
8. Identify the likely major latency and API-cost drivers.
9. Identify what additional capability is required to create floor-plan-consistent exterior elevations and photorealistic exterior renderings.
10. Propose TEST CASE #2, which should be substantially different from the current barndominium and should require no technical dimensions from the homeowner beyond overall house constraints they would naturally know.

Do not implement the entire application yet.

I want the architecture and reuse strategy settled first.

Our next milestone is NOT "build the website."

Our next milestone is:

PROVE THAT THIS CAN BECOME A FAST, REUSABLE, AFFORDABLE HOUSE-GENERATION ENGINE THAT CONSISTENTLY PRODUCES GOOD RESIDENTIAL CONCEPT DESIGNS FROM ORDINARY HOMEOWNER LANGUAGE.
