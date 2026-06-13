# BirdBrain UX review — naive new user perspective

Reviewed 2026-06-11 against the live site (home, site/Tembe, species/Hadada Ibis, /rare, /review, /diurnal, /about), plus a desktop visual pass in Chrome (see "Visual pass" below). True mobile-viewport testing wasn't possible (window resize blocked); mobile notes are from template analysis.

## The core problem

The site serves two audiences with one UI: casual visitors who want "what birds are these cams hearing?", and you, the operator/curator. A new user immediately hits operator vocabulary (`rollup`, `confidence`, `review`, `diurnal`), confidence scores everywhere, labeling widgets, and uptime error logs. The underlying content is excellent — live video, AI narratives, audio clips — but the killer feature (*listen to the bird that was just heard*) is hidden behind an unlabeled spectrogram image.

## Bugs spotted (fix regardless)

1. **Mara River appears twice** in yesterday's soundscape brief.
2. **"Stony Point — No data provided"** rendered as a brief bullet; suppress empty sites.
3. **Small Buttonquail shows IUCN status "EX"** (extinct) on /rare — clearly wrong, undermines trust in all the badges.
4. **Hadada Ibis has "no photo" and "no range map"** — one of Africa's most common/photographed birds, so the Wikipedia/Wikidata/GBIF fallback chain is failing somewhere.
5. Site narrative says "165-species palimpsest" while the header stat says 175 — narratives go stale; show "as of" more prominently or regenerate on bigger deltas.

## P0 — split the audiences (highest impact, mostly cheap)

1. **Restructure the nav.** Public: `Live · Sites · Species · Highlights · About`. Move `review`, `confidence`, `rollup`, `diurnal` into a single `data` dropdown — or show them only on LAN, like /admin. `/review` especially: it's a curation tool whose buttons 404 publicly; a visitor clicking ✓/✗ just gets a silent failure.
2. **Make listening obvious.** Overlay a ▶ icon on every spectrogram thumbnail; caption the first one "click to hear the call". Consider a hero "Listen to the latest detection" button on the homepage.
3. **Hide operator controls on the public view**: per-species min-confidence editor, label buttons, species-note editor. Replace with nothing (cleaner) or a small "curation is LAN-only" note.
4. **Hide the Uptime section's raw yt-dlp tracebacks** from the public site page; keep the "last 24h: 1m down" summary line only.
5. **Explain the badges.** `LC`/`VU`/`EN` get a tooltip ("IUCN conservation status: Least Concern"); confidence gets one line somewhere visible: "0–1 score of how sure the AI is".

## P1 — first-five-minutes experience

6. **Tame the homepage brief.** It's currently a lab notebook (confidence 0.999, UTC peaks, 15-species comma lists). Keep the headline paragraph, collapse per-site bullets behind a disclosure, and strip raw confidence numbers from the prose prompt — a layperson should read "Barn Owl heard with near-certainty", not "1.0/0.999".
7. **Relative timestamps** in the detections feed ("2 min ago", absolute + timezone on hover). The current mix of EAT/SAST/CAT makes the feed look mis-sorted (09:06 EAT above 08:05 CAT).
8. **Species index needs search-first.** /species is a ~70 KB page; put a search box at top, lazy-load or paginate the rest, add thumbnail photos so browsing is fun rather than a scientific-name wall.
9. **Map legibility**: the dot-and-threads map relies on hover (dead on touch). Add a static legend, larger tap targets, and tap-to-open-site on mobile.
10. **Rename feed filters in plain language.** "group off (all) / 1 min / weekly / annually" is opaque — "combine repeat calls within…" or similar.
11. **Mobile pass.** The detections table (time + site + species + spectrogram + conf) almost certainly overflows a phone; switch to stacked cards under ~640 px. (Unverified — worth a real phone test.)

## P2 — engagement & sharing

12. **Highlights page for laypeople** — best clips of the day (high confidence + rare + good label), big photos, one-tap audio. /rare is close but framed for curators ("max conf ≥ 0.50 · excludes known-bad species").
13. **Per-detection permalinks with OG meta tags** (photo + "Fiery-necked Nightjar heard at Tembe, 04:05") so clips can be shared to social/WhatsApp; currently nothing is shareable.
14. **A "how it works" illustrated page** — mic → 3-second windows → BirdNET → spectrogram, with one annotated example. The /about page is credits, not explanation.
15. **RSS/daily-digest** of the soundscape brief for return visitors.

## Visual pass (Chrome, 1552×784 desktop)

What works: the hero is clean and impressive — the stat row ("244 species · 24h", "last detection 1s ago") immediately conveys "this is alive". The feed's spectrogram thumbnails with confidence overlay look polished, and the detection modal (photo, range map, overlay boxes showing where the bird called) is genuinely good once you find it.

New findings:

- **Clip player shows 0:00 / 0:00 even while playing**, so the seek bar is dead. Cause is almost certainly the ffmpeg transcode in `/clips/{id}` (`-q:a 4` VBR MP3 → unreliable duration metadata in browsers). Try CBR (`-b:a 128k`) or verify the Xing header survives, and add `preload="metadata"` to the `<audio>` element. This badly undercuts the listening experience — the #1 feature.
- **Detection modal leads with the AI analyst note**; the player is below the fold. A naive user clicks a clip to *hear* it — put spectrogram + player first, collapse the note behind a disclosure.
- **Map default zoom shows all of Africa + the Middle East** while all 11 dots cluster in the south/east quadrant; half the map is empty. Fit bounds to markers on load. Dots are also ~10 px — small targets.
- **Homepage right panel has a large dead zone** below the intro text before the "Hosted on a Raspberry Pi 5" footer line — unbalanced against the tall map.
- **The 5 s feed refresh visibly reshuffles rows** while you're reading/clicking (I clicked one row and got a different detection's modal). Pause auto-refresh while a modal is open or when scrolled into the table.
- **`LC` badges on every row are noise** — ~95% of rows. Show badges only for NT and above; the rare statuses then pop.
- **Full datetime ("2026-06-11 08:24:21 SAST") per row** is heavy; date is redundant in a live feed. Confirms the relative-timestamp item above.
- **Mixed spectrogram palettes** (orange/green) read as if color encodes something; pick one palette or make the encoding meaningful.
- **The empty "—" Label column** shows on desktop for every unlabelled row — wasted column for public visitors (it's `hidden md:table-cell`, so mobile already drops it).

Mobile (from templates, unverified on device): stat grids and panels stack properly (`grid-cols-2 md:grid-cols-4`, `lg:grid-cols-2`) and the Label column hides, but the feed table keeps time + site + species + clip at `w-full` with no overflow-x wrapper — at 390 px those four columns will be severely cramped. Worth a real phone test; likely needs a card layout under ~640 px.

## /diurnal click-through critique (tested in Chrome)

What works: the day-phase bands visually unite the two charts; the popup's 24-h clock dial with the clicked hour outlined is a lovely hour-in-context touch; computing the cell from click geometry instead of 288 DOM handlers is Pi-friendly.

1. **Inconsistent affordances.** The bar chart and heatmap look like siblings, but only the heatmap is clickable (`diurnal.html` attaches the handler to the heat SVG only). I clicked a bar first — nothing. Users who try the bars will conclude the graphics are static and never find the heatmap drill-down. Either make bars open the same popup (all-species for that hour) or visually differentiate.
2. **The only hint is below the fold.** "Tip: click a cell…" sits in small grey text *under* the heatmap. By the time you've read the chart you've already decided whether it's interactive. Add a hover highlight + tooltip with the cell value — that teaches clickability instantly and fixes blind aiming.
3. **The tip's promise breaks on the default view.** It says "species & weather for that hour", but with Site = "any" the popup says "Pick a single source to see weather." The default state can't deliver the advertised payoff.
4. **Popup leads with the generic species essay**, not the clicked hour. The AI note (UTC conversions, mean-confidence talk) fills the entire first screen; the hour-specific dial is below it, and the actual value of the clicked cell ("Scops-Owl, 09:00: N detections") is never stated. Invert it: cell stats → dial → weather → collapsed note.
5. **No audio.** The natural next step — *hear this species at this hour* — is missing. There are real detections behind every cell; surface 2–3 sample clips in the popup. "Full species page →" discards the hour context.
6. **No feedback on which cell you hit.** I aimed at one row, got the adjacent species, and nothing marked the clicked cell in the heatmap. Geometric hit-testing with hard-coded margins (`mL=200…`) plus responsive rescaling makes near-misses easy; a hover outline would fix both this and #2.
7. **No way to step between cells.** Comparing 08:00 vs 09:00 means close → re-aim → re-click. Prev/next-hour arrows (or ←/→ keys) in the popup header would make it explorable.
8. **The decorative night-sky constellations read as data.** Dotted points joined by lines inside a chart area are the visual grammar of a scatter plot; I initially tried to interpret them. Lower their opacity further, or drop them from the bar chart where real marks live.

Heaviness note: this page repeatedly froze Chrome's screenshot capture for 30 s+ — the big Plot SVGs plus per-cell rects make for expensive paints. Worth profiling once on a mid-range phone.

## Pi 5 constraints to respect

Pre-generate species thumbnails at fetch time (no on-the-fly resizing), keep HTMX over heavy JS, paginate instead of caching giant pages, and keep the 5 s feed refresh payload small (it already re-sends spectrogram `<img>`s — make sure browsers can cache them with long-lived headers).

## Suggested order

P0 items 1–4 plus the five bugs are roughly a weekend and transform the first impression. Then 6–8, then the rest as appetite allows.
