# Cover Scenes Catalog (30 templates)

Source: docx from client, 9 May 2026. Used by `7-publisher` agent to pick a cover scene that matches the article topic.

## How the agent uses this catalog

1. Read article context from `drafts/{slug}/meta.json` (title, h1, lead, topic_action, main_keyword, category, description).
2. Identify the article's **central concept** in one sentence — what specifically is this piece about (e.g., "stages of voluntary liquidation when debts exist", "how to challenge a court order in 10 days", "single seller's home protection in bankruptcy practice").
3. Scan the **`Best for:`** tag line of each scene and pick **the single scene whose tags most closely match the article's concept**. Prefer specificity: a scene tagged `enforcement, scales, seizure` beats a generic `general legal` for an article on freezing bank accounts.
4. Within that scene, pick **3-7 specific objects from the allowed pool** that reinforce the article's specific topic (e.g., calculator + piggy bank for cost articles, brass keys + padlock for asset-freeze articles, hourglass + calendar for limitation-period articles).
5. Write the **final English scene description** to `drafts/{slug}/scene.txt` — start from the template, substitute concrete objects from the pool, optionally tweak camera/light if it serves the article. Keep it 60-150 words, one paragraph, no line breaks, no quotes, no `Scene:` prefix.
6. Write the chosen scene number to `drafts/{slug}/scene_template.txt` as `template_id=N` on a single line — used downstream for variety tracking and debugging.

## Selection heuristics (how to match topic to scene)

| Article theme | Likely scenes |
|---|---|
| Authority, formal procedure, weight of decision | 1, 5, 11, 15, 18 |
| Practical "how to" guide, step-by-step | 2, 7, 19, 24 |
| Costs, calculations, payments | 7, 10, 19 (with calculator + piggy bank) |
| Enforcement, seizure, scales of justice | 3, 17, 22 |
| Confidential / heavy / contested | 16, 27 |
| Time-sensitive, deadlines, limitation periods | 22 (hourglass), 4, 28 |
| Modern, fintech, digital, current news | 12, 21, 29 |
| Big institutional / landmark news | 6, 18, 30 |
| Library research, jurisprudence | 8 |
| Single iconic concept (one law, one term) | 4, 24 |
| Negotiation, mediation, settlement | 14 |
| Senior counsel, complex strategy | 16, 25, 27 |
| Architectural threshold (start of a process) | 20 |
| Multi-stage / appeals / escalation | 26 |

When two scenes are equally relevant, prefer the one **not used in the last 5 articles** (to keep covers visually varied across the site). Check by globbing `drafts/_archive/**/scene_template.txt` and the most recent `drafts/*/scene_template.txt` files.

## Variety rule

Avoid picking the same `template_id` for two consecutive articles in the same category. If unavoidable, pick the second-best match instead.

---

## Scene templates

### 1. Stone administrative building (any architectural style)

**Template:** Exterior photograph of a stone administrative or government building in classical, neoclassical, modernist or minimalist architecture. Camera angle is your choice — frontal, three-quarter, low-angle looking up the columns, or reflected in a wet pavement. Cool grey-blue tonal palette, soft diffused city light, optional rain or fog haloing lampposts, long architectural columns or pilasters, brass plaque on the wall (no readable text), wide stone steps. Optional foreground items at the base of frame: a closed leather briefcase, a folded black umbrella, a bound stack of dossiers tied with cord. No people. Atmosphere of authority, weight, formal procedure. Wide cinematic crop.

**Best for:** authority, formal procedures, court decisions, bankruptcy filings, subsidiary liability, any "official body" topic.

### 2. Lawyer's desk by a window

**Template:** Editorial photograph of a lawyer or notary's desk by a tall window. Camera at table-eye level, slight three-quarter angle. Allowed pool of items on the desk (pick 4-6): closed leather portfolio, blank-spine notebook, fountain pen, brass desk lamp, polished round company seal, reading glasses, closed laptop, antique pocket watch, white porcelain coffee cup. Background through the window: blurred urban panorama OR stone wall OR bookshelves, all soft bokeh. Lighting from window — warm golden hour or cool overcast. Materials: walnut, leather, brass. Premium legal-magazine aesthetic, neutral beige-graphite palette.

**Best for:** practical guides, how-to step-by-step, advisory tone, individual legal advice.

### 3. Legal attributes mid-process

**Template:** Close-up cinematic photograph of legal/administrative implements arranged as if mid-process. Allowed pool (pick 4-7): brass scales of justice with empty pans, wooden judge's gavel on a round sound block, stack of closed dossiers tied with cord, polished round seal, bunch of brass keys on a ring, antique brass padlock with chain, sand-filled hourglass, closed law books with embossed gilt spines (no readable titles). Camera: tabletop level, three-quarter angle. Lighting: soft directional sidelight casting elegant shadows. Materials: brass, dark walnut, weathered leather. Atmosphere of due process underway.

**Best for:** enforcement, court orders, asset freezing, repossession, statute/law explanations, vzysk category default.

### 4. Single iconic object on neutral studio background

**Template:** Studio close-up of a single iconic object or a tight cluster of 2-3 objects against a clean neutral background — dark stone slab, smoky gradient, or matte graphite seamless. Subject options (pick the one or two that best symbolize the article's central concept): brass scales of justice, vintage brass padlock with chain, sand-filled hourglass, stack of brass keys, closed leather case folder, polished company seal, antique pocket watch, brass weight on a square base. Soft directional studio light from upper left, gentle shadow falloff, no environmental context. Hyperrealistic detail, premium product-photography polish.

**Best for:** focused single-concept articles — one law, one cost, one term, one specific procedure.

### 5. Architectural details of an official building

**Template:** Close-up architectural photograph of stone columns, marble staircase treads, heavy doors, arches, or tall windows of a courthouse or administrative building. Camera angle: low-angle looking up, or symmetrical front-on, or diagonal across worn marble. Lighting: cool morning daylight or warm golden-hour glow on marble veining. Materials: weathered limestone, polished marble, oxidized brass details, cast-iron balustrade. Optional minimal foreground item — a closed portfolio on a step, or a folded umbrella against a column — adding a subtle legal hint without dominating the frame.

**Best for:** heritage / tradition, long-standing law, constitutional matters, broad institutional themes.

### 6. Top-floor office overlooking the city

**Template:** Editorial photograph of a corner of a desk in a high-floor executive office or conference room. Foreground: 4-6 business items (closed thin laptop, sleek pen, leather notebook, smartphone face-down, water glass, framed succulent without label, polished round seal, white coffee cup). Background through floor-to-ceiling windows: blurred urban skyline at dusk, distant lights, or low fog. Camera: shallow depth of field, three-quarter angle, eye-level. Lighting: cool ambient daylight blended with warm interior tungsten. Materials: walnut, dark glass, polished steel.

**Best for:** corporate matters, юр lic procedures, M&A, executive-level decisions, business-owner audiences.

### 7. Business interior accenting documents and tools

**Template:** Editorial photograph of a working desk in a modern business interior. Camera: slight overhead three-quarter, or side angle. Allowed pool (pick 4-6 to match topic): scientific calculator, closed thin laptop, leather folder, blank-spine notebook, fountain pen, smartphone face-down, polished round seal, reading glasses, white coffee cup. Materials: walnut top, brushed steel, leather inlay. Background: stone-and-wood feature wall or city skyline through tall windows, blurred. Lighting: directional warm light from upper left.

**Best for:** practical hands-on procedural articles where tools matter — calculations, filings, budgets, computations.

### 8. Library, archive or document room

**Template:** Cinematic photograph of a private legal library or archive. Walls lined with closed dark leather-bound books with gilt embossing on spines (no readable titles). Foreground: a wooden reading desk with a brass green-glass library lamp, a closed portfolio, a fountain pen, an antique pocket watch on a chain, reading glasses on top of a stack of unmarked dossiers. Camera: low-angle looking up the shelves OR three-quarter at desk level. Lighting: warm tungsten pools of light, deep shadows in book stacks, soft window haze. Mood: scholarly, authoritative, archival.

**Best for:** research-heavy articles, jurisprudence, case studies, historical/precedent context.

### 9. Marble interior detail with foreground objects

**Template:** Close-up architectural photograph emphasizing the texture of polished marble or carved stonework, with a small cluster of business accessories in the foreground. Camera: tight crop, eye-level or slight low-angle. Allowed foreground items: closed leather portfolio, antique brass pocket watch, fountain pen, polished round seal, small silver tray with a folded handkerchief. Lighting: soft diffused daylight grazing the stone, revealing veining and grain. Palette: cool whites, warm cream, restrained brass. Mood: refined, premium, institutional.

**Best for:** prestige, white-collar matters, elite legal practice, expensive procedures.

### 10. Top-down flat-lay on a polished walnut desk

**Template:** Top-down flat-lay editorial photograph on a polished dark walnut desk. Soft golden morning light from a window casting a long warm diagonal across the surface. Allowed pool (pick 4-7 relevant to topic): closed leather case folder, wooden judge's gavel, stack of closed law books with embossed gilt spines and a small national emblem, fountain pen, white porcelain coffee cup on saucer, reading glasses, brass round seal, calculator, antique pocket watch. Arrange with editorial composition. Premium legal-magazine aesthetic, neutral beige and graphite palette with warm gold accents.

**Best for:** versatile general default — any fiz article, mid-funnel guides, when no specific scene matches better.

### 11. Spacious official room with minimal objects

**Template:** Wide cinematic photograph of a large official room — a courthouse antechamber, a council hall, a notary's grand office. Architecture dominates: high ceilings, tall windows, marble or stone floors, paneled walls. Center of frame holds a single feature: a long polished table with a closed portfolio on it, OR an empty wooden judge's bench with a gavel resting on its block, OR a single tall-backed leather chair with a stack of dossiers beside it. Camera: symmetric, eye-level or slight low-angle. Lighting: late-afternoon golden window light casting long parallel shadows. Mood: hushed, ceremonial, awaiting decision.

**Best for:** high-stakes decisions, court rulings, big-impact themes, subsidiary liability.

### 12. Modern administrative building in rain/fog/night

**Template:** Exterior photograph of a contemporary administrative or business building in adverse weather — heavy rain, dense fog, or evening dusk. Glass façade reflects the wet street, soft amber lampposts glow through the haze (no readable signage). Camera: street-level, slight low-angle, OR across a puddle reflecting the building upside-down. Foreground: distant silhouettes of pedestrians with black umbrellas (faceless, far away, no detail), wet stone steps, oxidized brass plaque on the entry pillar. Palette: cool blue-grey with warm amber pin-points. Mood: contemporary urban legal/financial drama.

**Best for:** news category default, contemporary regulation, financial topics, current events.

### 13. Recent activity close-up, no people

**Template:** Tight close-up suggesting recent human activity but with absolutely no people, hands, or faces in frame. Allowed setups: an opened leather briefcase with papers fanning out (no readable text), a fountain pen lying uncapped next to a small inkwell, a chair pushed back from a desk seen from behind, an unbuttoned suit jacket draped over the back of a chair, a half-finished cup of coffee beside a stack of closed dossiers. Camera: shallow depth of field, three-quarter angle. Lighting: warm directional from one side. Atmosphere: paused mid-process, just-stepped-away.

**Best for:** case-study articles, narrative arcs, "what happens next" themes.

### 14. Conference room with minimalist composition

**Template:** Editorial photograph of a polished conference table with a deliberately minimal arrangement — at most 3-4 objects on a wide expanse of dark walnut or marble. Allowed: closed leather portfolio, fountain pen aligned parallel to the portfolio's edge, water carafe with two empty glasses, single red leather chair-back at the far end. Camera: symmetric down-the-table OR three-quarter from one corner. Background through floor-to-ceiling windows: rain, sunset, or fog over a city. Lighting: cool overcast from windows, warm pendant glow. Mood: serious negotiation, gravitas.

**Best for:** negotiation, settlement, mediation, agreements, mirovoe soglashenie.

### 15. Massive stone building exterior emphasizing texture

**Template:** Close-to-medium architectural photograph of a massive stone government building, focusing on textures, geometric repetition, and dramatic light. Camera: extreme low-angle up the columns, OR diagonal across the limestone façade, OR partial framing showing only a doorway with carved emblem (no readable inscriptions). Lighting: hard side-light revealing every chisel mark, deep cast shadows from architectural features. Palette: warm sandstone, cool grey shadow, hint of moss in cracks. Mood: weight, permanence, immutable authority.

**Best for:** constitutional law, foundational articles, historical precedent, weighty institutional themes.

### 16. Office interior with subdued light, leather and wood

**Template:** Photograph of a private office interior with low-key lighting. Materials: deep walnut paneling, oxblood leather chair, brass library lamp, vintage brown leather chesterfield in corner. Desk in foreground holds 4-6 items: closed laptop, leather-bound notebook, fountain pen on a stand, brass round seal, antique pocket watch, small whisky tumbler with empty contents. Background: blurred bookshelves OR a dark window with city lights bokeh. Camera: three-quarter, eye-level. Lighting: single warm tungsten desk lamp, deep ambient shadow. Mood: solitary deliberation, late-night work.

**Best for:** complex deliberative topics, contested cases, senior-counsel advice, confidential matters.

### 17. Macro of metal/business objects on stone, wood, glass

**Template:** Macro close-up photograph emphasizing surface materials and tactile detail. Subject options: brass keys on a leather coaster, polished round seal pressed onto a closed envelope with wax, fountain pen across a closed portfolio, polished brass scales-of-justice pan close-up, antique brass padlock with weathered patina. Background: weathered stone slab, dark walnut grain, or backlit clear glass. Camera: macro, very shallow DOF, isolating the subject. Lighting: directional contrast — sharp specular highlights on metal, soft fill on shadow side. Palette: brass, deep brown, charcoal.

**Best for:** highly focused articles on specific tools/procedures (sealing documents, locking accounts, securing assets).

### 18. Spacious symmetric interior with architectural lines

**Template:** Wide-angle photograph of a grand symmetric interior — a parliament chamber, a courthouse central hall, a marble-floored bank lobby. Strong architectural lines lead into the frame. Center holds a single understated point of interest: a long table with a closed portfolio, OR an empty podium with brass railings, OR a marble pedestal holding a sealed bound document. Camera: dead-center symmetric, eye-level, slight wide-angle. Lighting: cool natural daylight from skylights or tall windows. Palette: cool whites, polished brass, deep walnut. Mood: institutional, civic, ceremonial.

**Best for:** legislative/constitutional topics, sweeping policy news, broad institutional reform.

### 19. Workspace by a window with business atmosphere

**Template:** Editorial photograph of a workspace adjacent to a window. Foreground: a desk with 4-6 well-arranged items (closed leather portfolio, fountain pen, smartphone face-down, brass desk lamp, reading glasses, white porcelain coffee cup, blank-spine notebook). Background through the glass: cityscape in rain, evening lights blurred, fog, or neutral overcast skyline. Camera: three-quarter angle, eye-level. Lighting: window-driven cool daylight + warm interior fill. Materials: walnut, leather, brass. Mood: focused, contemplative, professional.

**Best for:** advisory tone, "what to do" guides, personal-finance / personal-legal themes.

### 20. Massive doors / official entrances

**Template:** Photograph of a massive ornate door or grand entrance to an official building — bronze-bound oak, brass studs, carved stone surround, marble steps. Camera: dead-on symmetric, OR slight low-angle, OR partial diagonal. Optional small foreground item: a folded umbrella leaned against the doorframe, a closed leather briefcase on the steps, a stack of bound documents tied with cord. Lighting: long evening shadows reaching across stone, OR cool morning light, OR dramatic side-light revealing every grain of the wood. Mood: threshold, transition, formal entry into a process.

**Best for:** starting a case, filing procedures, beginning bankruptcy/trial, initiation themes.

### 21. Modern conference room or workspace, natural materials

**Template:** Photograph of a contemporary conference room or shared workspace built with natural materials — light oak, raw concrete, linen-upholstered chairs, indoor plants in raw clay pots. Foreground: 4-6 modern business items (sleek closed laptop, ceramic water bottle, leather-bound notebook, fountain pen, smartphone face-down, polished brass round seal, white coffee cup). Background: floor-to-ceiling windows opening onto cityscape, daylight or evening. Camera: three-quarter angle, eye-level, shallow DOF. Lighting: bright cool daylight, optional warm pendant fill. Mood: contemporary, wellness-aware professional.

**Best for:** modern fintech / digital topics, online procedures, news on IT-adjacent legal matters, mfc and gosuslugi themes.

### 22. Hourglass + dossiers + seals on stone/wood/dark void

**Template:** Editorial composition centering on time and process. Hero objects: a sand-filled brass hourglass mid-flow, a stack of 3-4 closed dossiers tied with cord, a polished round seal, a fountain pen. Background: dark slate, weathered stone slab, or completely black void with subtle backlight. Camera: three-quarter angle OR straight-on, tabletop level. Lighting: focused directional spot creating dramatic shadow, gold rim-light on brass surfaces. Palette: deep charcoal, brass, oxblood leather accents. Mood: passage of time, deliberation, due process.

**Best for:** deadline / timing articles, srok iskovoy davnosti, moratorium duration, procedural-period explanations.

### 23. Space between buildings — geometry and reflections

**Template:** Photograph of an architectural in-between space — a passage between two stone administrative buildings, a courtyard, a covered colonnade. Wet pavement reflects the architecture above. Camera: low-angle along the passageway, vanishing point centered, OR diagonal three-quarter. Lighting: cool overcast OR warm glow from a single distant lamppost, fog softens the far end. Optional minimal foreground items at the very base of frame: a folded umbrella, a small closed briefcase. Palette: cool grey-blue with warm pin-points. Mood: transition, between-stages, the quiet moment between proceedings.

**Best for:** in-between procedural stages, appeals process, contested transitions.

### 24. Minimalist top-down legal flat-lay

**Template:** Minimalist top-down flat-lay on a clean light-grey or polished walnut surface. Sparse arrangement: 3-4 well-spaced objects with deliberate negative space between them. Allowed pool: brass scales of justice, fountain pen, closed leather portfolio, single closed law book, polished round seal, smartphone face-down, single key on a brass ring. Camera: directly overhead. Lighting: soft even daylight, very subtle directional shadow. Palette: pale neutrals, warm brass accents. Mood: clean editorial, contemporary legal magazine.

**Best for:** short clear how-to articles, definitions, single-concept news pieces.

### 25. Spacious executive office with high ceilings

**Template:** Wide editorial photograph of a private executive office — high ceilings, tall windows with heavy drapes, walnut bookshelves lining the walls, an oxblood leather chesterfield, a large mahogany desk in the foreground with 4-6 business items (closed laptop, leather portfolio, brass desk lamp, fountain pen on a stand, antique pocket watch, white porcelain cup). Background through windows: city skyline in fog, golden hour, or blue evening. Camera: three-quarter angle, slight low-angle to emphasize ceiling. Lighting: cool window light + warm tungsten desk lamp. Mood: senior decision-maker, gravity of consequence.

**Best for:** yur category default, top-of-funnel premium articles, articles aimed at business owners.

### 26. Stone staircase or interior architectural passage

**Template:** Photograph of a stone or marble staircase, or a long arched corridor inside an official building. Camera: low-angle from the bottom of stairs looking up, OR symmetric dead-center down the corridor, OR diagonal three-quarter. Optional small foreground items at the base of frame: a sealed envelope on a step, a closed portfolio leaning against a column, a folded umbrella on the marble floor. Lighting: shafts of cool daylight from high windows OR warm dramatic side-light. Materials: weathered marble, oxidized brass railings, deep red carpet runner. Mood: ascent through procedural stages, escalation, appeal.

**Best for:** appeals, multi-stage processes, escalating matters, kassatsiya / nadzor topics.

### 27. Dark interior with wood and pool-of-light items

**Template:** Low-key photograph of a dark wood-paneled interior — a private library nook, a club chamber, a senior partner's office at night. Single warm pool of light from a brass library lamp illuminates 4-6 items on a leather-topped desk: closed leather-bound diary, fountain pen, antique pocket watch on a chain, polished brass round seal, reading glasses, closed law book with embossed gilt spine. Camera: three-quarter angle, eye-level. Lighting: dominant warm tungsten pool, deep ambient shadow elsewhere. Palette: oxblood, walnut, brass on near-black. Mood: confidential, deliberative, after-hours.

**Best for:** confidential/sensitive topics, complex strategic articles, senior-counsel themes.

### 28. Stone/marble surface with foreground objects, soft architectural background

**Template:** Editorial photograph of a polished marble or weathered stone slab in the foreground holding 4-6 business objects — closed leather portfolio, fountain pen, polished round seal, antique pocket watch, brass key ring, small white porcelain cup. Soft architectural background: blurred columns, distant marble wall, or limestone arch out of focus. Camera: tabletop level OR slight three-quarter. Lighting: natural soft daylight grazing the stone, revealing veins and texture. Palette: warm cream, cool grey shadow, brass accents. Mood: timeless professional.

**Best for:** traditional procedural topics, classical legal frameworks, statute-of-law explanations.

### 29. View through rain-streaked glass at a modern building

**Template:** Photograph through wet glass — the camera views a contemporary administrative or financial building through a window beaded with raindrops. Foreground objects on the windowsill or nearby surface (sharp focus): a closed laptop, a white porcelain coffee cup, a leather notebook, a fountain pen, reading glasses. Background (soft, blurred through droplets): glass façade reflecting the rainy street, distant amber lampposts. Camera: tight crop, eye-level. Lighting: cool overcast from outside, warm interior glow from a desk lamp. Palette: cool blue-grey with warm interior pin-points. Mood: contemporary, contemplative, late-day urban.

**Best for:** financial/banking topics, digital banking, tsifrovoy rubl, contemporary news.

### 30. Symmetric corridor or hall with central composition

**Template:** Wide symmetric photograph down a long official corridor or grand hall — colonnaded passage, marble-floored gallery, paneled corridor with brass sconces. Center of frame holds a single point of interest: a small wooden table with a closed portfolio, OR a brass scales of justice on a marble pedestal, OR a single tall leather chair facing forward. Camera: dead-center symmetric, eye-level, slight wide-angle pulling the lines into perspective. Lighting: cool natural daylight from above OR warm even ambient. Palette: cool whites and grays with subtle gold trim. Mood: ceremonial, momentous, decision-pending.

**Best for:** high-impact news, landmark rulings, big-picture institutional articles.

---

## Strict rules that apply to ALL 30 scenes (already enforced in `tools/image_gen.py`)

- **No people, no faces, no hands.** Distant blurred silhouettes (umbrella shapes) are acceptable in scenes 12 and 23 where explicitly mentioned, otherwise none.
- **No readable text anywhere.** No book titles, document text, signage, banners, labels, plaque inscriptions, banknote numbers, screen text. Decorative gilt patterns and embossed emblems without letters are allowed.
- **No logos** other than the empty lower-right corner kept clear for the platform watermark.
- **Lower-right area of the frame** must stay smooth, softly lit, and empty — that's where the "ЛИКВИДАТОР" logo overlay goes.
- **16:9 horizontal aspect ratio.**

These are enforced automatically — do not duplicate them in your scene description.
