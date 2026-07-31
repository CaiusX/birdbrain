# System performance, first 11 weeks

**Snapshot:** 2026-07-28 07:17 UTC. Window: 2026-05-12 → 2026-07-28 (77 days,
72 of them with detections). All figures come from the live database
(`data/birdbrain.sqlite`) and are reproducible from the queries described in
[Method](#method).

This is a report on how well BirdNET-on-live-streams actually works — where it
is trustworthy, where it is not, and what the failure modes look like. It is
deliberately unflattering where the data says so.

---

## Headline

| | |
|---|---|
| Detections logged | **944,459** |
| Distinct species labels emitted | **633** |
| Species at the ≥0.7 trust floor | **424** |
| Species at ≥0.7 **and** ≥5 detections **and** ≥2 sites | **~282** |
| Sources ever analysed | **21** (18 Africam YouTube cams, 1 USB mic, 2 remote BirdBrain units) |
| Stream-hours analysed | **~23,750 h** ≈ 990 source-days |
| 3-second windows analysed | **~28.5 million** |
| Windows that produced any detection | **922,347** (~3.2%) |
| Days with an AI daily brief | 65 |

Growth was source-driven, not model-driven — the jump from May to June is
sources coming online, not the model improving:

| Month | Detections | ≥0.7 | Species | Sources |
|---|---|---|---|---|
| 2026-05 (from 12th) | 52,118 | 17,368 | 354 | 8 |
| 2026-06 | 372,244 | 155,308 | 581 | 21 |
| 2026-07 (to 28th) | 520,098 | 220,185 | 587 | 19 |

Current steady state is **~20,000 detections/day across ~19 sources, ~320
distinct species/day**, of which ~40% clear 0.7.

---

## Method

- A **detection** is one BirdNET result on one non-overlapping 3-second window
  at 48 kHz (`chunk_seconds = 3.0`). One calling bird therefore produces many
  detections — see [Volume is not observations](#volume-is-not-observations).
- **Trust floor.** Confidence ≥0.7 is used throughout as the working floor.
  It is not a validated threshold; it is where the labelled sample stops being
  dominated by junk (see next section). Per-source `min_confidence` gates
  writes at 0.3–0.5, so the raw table is already floor-limited below that.
- **Uptime** is derived from `worker_downtime` (per-source outage episodes).
  That table starts 2026-05-30, so uptime for the three oldest sources is
  slightly overstated for their first 18 days.
- Sources that were stopped (Safarihoek, Stony Point, Majete Thawale Lodge,
  tbb-mems, tbb-test) have their spans capped at their last heartbeat rather
  than at the snapshot time.

---

## What the confidence score is worth

292 detections have been hand-labelled by an operator. Precision within the
labelled set, counting `good` vs `bad` and setting `unsure` aside:

| Confidence | Labelled | good | bad | unsure | good/(good+bad) |
|---|---|---|---|---|---|
| 0.90 – 1.00 | 99 | 89 | 3 | 7 | **0.97** |
| 0.70 – 0.90 | 54 | 35 | 11 | 8 | **0.76** |
| 0.50 – 0.70 | 48 | 34 | 9 | 5 | **0.79** |
| 0.30 – 0.50 | 76 | 47 | 7 | 22 | 0.87 |
| < 0.30 | 15 | 7 | 3 | 5 | 0.70 |

**Read this cautiously.** 292 of 944,459 is 0.03% of the corpus, and the sample
is *not* random — it is what an operator chose to click, which skews toward
both showcases and suspicious-looking hits. The 0.3–0.5 row scoring higher than
0.7–0.9 is a sampling artefact, not a real inversion; note also that a third of
that row is `unsure`, i.e. the operator could not adjudicate it either way.

What the table does support:

1. **≥0.9 is close to reliable.** 89/92 adjudicated correct. This matches the
   independent Xeno-canto probe result: fed clean reference recordings, the
   model recognises the species at 0.99–1.00 and, where it errs, lands on a
   congener. The model is not weak or promiscuous.
2. **0.7–0.9 is a coin-flip-plus, not a verdict.** Roughly a quarter of the
   adjudicated hits in that band are wrong, and the wrong ones are wrong at
   high confidence (see failure modes).
3. **Confidence alone is not a sufficient filter at any threshold.** Every
   high-confidence failure below sits at ≥0.75.

**There is no recall measurement.** Nothing in this system establishes what was
actually calling in a window, so we cannot say what fraction of real
vocalisations were caught or missed. Every number here is about precision and
yield. Treat the 633 species labels as an upper bound on species detected, and
none of it as a species *inventory* of the sites.

---

## Species inventory and saturation

Raw 633 labels, 424 above the trust floor. The tail is thin and mostly noise:

- 56 species have exactly one detection ever; 106 have ≤3.
- 209 species never once exceeded 0.7. **344 never exceeded 0.9.**
- Applying floor + persistence + cross-site corroboration (≥0.7, ≥5
  detections, ≥2 sources) leaves ~282 — the defensible number.

New-species discovery per week (first-ever detection, and first ≥0.7
detection):

| Week | New labels | New at ≥0.7 |
|---|---|---|
| W19 (start) | 220 | 96 |
| W20 | 7 | 11 |
| W21 (+8 sources) | 127 | 82 |
| W22 | 60 | 54 |
| W23 | 70 | 49 |
| W24 | 45 | 30 |
| W25 | 50 | 40 |
| W26 | 18 | 21 |
| W27 | 12 | 14 |
| W28 | 10 | 9 |
| W29 | 14 | 14 |

Discovery has **saturated** at ~10–14 new labels/week and is still falling.
Each of the two step-ups (W19, W21) is a batch of new sources. Once a site has
run for ~3 weeks it stops producing new species — which is the expected shape
for a fixed set of fixed-position microphones, and means additional *time* on
existing cams buys very little new inventory. Additional *sites* do.

Conservation status is enriched for all 633 labels: 587 LC, 13 NT, 7 VU, 5 EN,
1 CR. The CR entry (Long-billed Tailorbird) is a confirmed false positive —
see failure modes. Rarity flags in this system should be read as "look at
this", never as a record.

---

## Volume is not observations

The top 10 species account for **65% of all ≥0.7 detections**, and the top two
for 26%:

| Species | ≥0.7 detections | Cumulative |
|---|---|---|
| African Scops-Owl | 50,387 | 12.8% |
| Fiery-necked Nightjar | 50,313 | 25.6% |
| Egyptian Goose | 35,530 | 34.7% |
| Namaqua Sandgrouse | 31,812 | 42.8% |
| Ring-necked Dove | 22,119 | 48.4% |
| Black-winged Stilt | 19,521 | 53.4% |
| Cape Robin-Chat | 17,520 | 57.8% |
| Freckled Nightjar | 11,603 | 60.8% |
| Blacksmith Lapwing | 8,447 | 62.9% |
| Hadada Ibis | 7,378 | 64.8% |

Both leaders are persistent nocturnal callers next to a microphone (Scops-Owl
peaks 00–03h, Fiery-necked Nightjar 03–04h and 19–20h). A single owl calling
steadily for six hours produces ~7,000 detections. **Detection counts measure
vocal persistence and microphone proximity, not abundance** — any analysis that
ranks species or sites by raw count is measuring the wrong thing.

---

## Coverage: worker uptime is not data yield

Worker uptime is excellent — 94–100% across the board, with the Majete cams
lowest at ~94% (they drop and reconnect most often). That number is close to
meaningless on its own, because a worker can be perfectly healthy while the
stream it is attached to delivers nothing usable.

Per source, uptime-normalised, sorted by useful yield:

| Source | Up (h) | Up % | dets/h | ≥0.7/h | spp@0.7 | Audio score | Issue |
|---|---|---|---|---|---|---|---|
| Tau Game Lodge | 1441 | 98.9 | 78.8 | **48.3** | 120 | 100 | good |
| Elephant Pan | 1323 | 99.0 | 99.4 | **47.4** | 118 | 73 | good |
| Namib Desert | 1107 | 98.9 | 71.6 | 31.5 | 48 | 62 | band-limited |
| Mpala Watering Hole | 1324 | 99.1 | 66.8 | 30.6 | **152** | 95 | good |
| Tembe | 1828 | 99.3 | 61.3 | 19.9 | 125 | 97 | good |
| JHB – Hyde Park (own mic) | 1029 | 100.0 | 25.5 | 16.2 | 52 | 100 | good |
| Twin Pan | 1824 | 99.1 | 40.0 | 15.0 | 125 | 100 | good |
| Majete Cam 1 | 789 | 94.6 | 38.7 | 13.8 | 92 | 63 | band-limited |
| tbb-test (Pi Zero) | 737 | 100.0 | 22.6 | 13.2 | 41 | – | – |
| Majete Cam 2 | 786 | 94.2 | 36.5 | 12.6 | 95 | 63 | band-limited |
| Mara River | 1447 | 99.1 | 38.6 | 12.3 | **148** | 100 | good |
| Olifants (Naledi) | 1821 | 98.9 | 26.1 | 10.5 | 122 | 100 | good |
| Timbavati | 1810 | 98.3 | 25.2 | 9.2 | 123 | 100 | good |
| Safarihoek | 431 | 99.8 | 47.0 | 9.1 | 42 | 89 | good |
| Kalahari | 1090 | 98.3 | 28.8 | 7.0 | 78 | 60 | band-limited |
| Majete Cam 4 | 785 | 94.2 | 19.0 | 5.3 | 88 | 77–93 | borderline |
| Majete Cam 5 | 789 | 94.7 | 13.8 | 2.5 | 52 | 63 | band-limited |
| Majete Cam 3 | 789 | 94.7 | 8.8 | 2.1 | 49 | 63 | band-limited |
| tbb-mems (Codec Zero) | 47 | 100.0 | 1.9 | 1.3 | 7 | – | – |
| Tortilis Camp | 1445 | 99.0 | 5.7 | 0.7 | 64 | 100 | good |
| Okaukuejo | 1109 | 98.9 | 2.7 | **0.3** | 19 | 40 | mostly silent |
| Stony Point | — | — | **0** | 0 | 0 | 24 | noise-masked |
| Majete Thawale Lodge | — | — | **0** | 0 | 0 | 0 | mostly silent |

The spread in useful yield is **160×** between Tau Game Lodge (48.3 ≥0.7
detections/hour) and Okaukuejo (0.3) — and infinite against the two sources
that produced literally zero detections over their whole run.

Audio scores are exponential moving averages and drift between reads — the
healthy sources wander a point or two (Tembe 97→99, Mpala 95→98 within minutes
of each other), and **Majete Cam 4 flips across the `band-limited` boundary**
(score 77–93, ceiling 6.6–8.6 kHz). Treat any single score as ±3 and the
issue label on a borderline source as unstable.

Modulo that, the per-source `audio_quality_metrics` score is the single best
predictor of yield, and it is what makes this diagnosable:

- **Okaukuejo** ran 1,109 healthy worker-hours and returned 19 species. Its
  audio is silent 99.6% of the time (`fraction_good = 0.004`, level −74.8
  dBFS). The mic is effectively dead; the worker never noticed.
- **Stony Point** (coastal) produced **zero** detections before being
  disabled — score 24, `noise-masked`. Continuous surf masks everything.
- **Majete Thawale Lodge** — score 0, −120 dBFS, no audio at all.
- **Band-limited sources** cap out at 4.3–5.1 kHz (Namib 4.29 kHz, Kalahari
  5.09 kHz, Majete Cams 1/2/3/5 ~4.4 kHz) against 12–15 kHz on the healthy
  cams. Everything above the cap is invisible, which structurally excludes
  most small passerines: Namib has the second-highest raw yield in the fleet
  (71.6 dets/h) but only 48 species at the floor, because sandgrouse-and-below
  is all it can hear.

**This is the single most actionable finding in the report.** Roughly 30% of
fleet-hours ran against audio that was dead, masked, or band-limited. Audio
quality should gate a source before its detections enter any analysis, and
`audio_quality_metrics` already computes everything needed to do that.

---

## What it gets right

**Diel structure emerges cleanly and unprompted.** Aggregated across all
sources by UTC hour, ≥0.7 detections and distinct species per hour:

- Species richness runs 198–214/hour through the night, then steps to
  **428–442 species/hour across 04–08h** — a dawn chorus, ~2.1× the nocturnal
  species count, appearing without any temporal prior in the model.
- Detection *volume* peaks separately at 04h and 07h, and again at 15h.
- Species richness collapses back below 215 after 16h while volume stays high —
  night is a few individuals calling a lot, day is many species calling
  briefly. That distinction is exactly what a passive acoustic monitor should
  be able to show, and it falls out of the raw table.

**Site acoustic fingerprints are real and stable.** Species distributions are
strongly site-locked in ways that match the habitat rather than the sampling:
Namaqua Sandgrouse is 97% Namib Desert, Freckled Nightjar 100% Mpala,
Four-coloured Bushshrike 100% Tembe, Sociable Weaver 78% Kalahari,
Southern Yellow-billed Hornbill 98% Elephant Pan. Site-locking on its own is
therefore *not* a false-positive signal — it is usually the system working.
What distinguishes an FP is site-locking **plus** a range mismatch **plus**
solo firing (below).

**Genuine multi-species windows.** 4,969 windows (≥0.5) contain more than one
species, 28 contain three. Cross-family pairs — kingfisher + silverbill at Mara
River, wagtail + goose at Olifants, goose + nightjar at Tembe — are real
simultaneous vocalisations, and the pipeline logs each species as its own row
rather than collapsing to a single winner.

**Infrastructure held up.** 23,750 stream-hours on one Raspberry Pi 5 with
94–100% per-worker uptime, plus a Pi Zero 2 W (`tbb-test`) sustaining 22.6
detections/hour co-located with the Pi 5 mic. Recurring operational failures
were external or upstream, not model-related: YouTube bot-blocking on mass
re-resolution, yt-dlp player-client breakage (2026-07), leaked ffmpeg processes
reconnecting to expired manifests, and a metered API key running dry which
silently killed AI notes for ~10 days.

---

## Failure modes

### 1. Acoustic collisions — coastal species inland, at high confidence

The characteristic failure. Marine and coastal waders fire hard at inland
savanna sites:

| Species | ≥0.7 | Max conf | Top site |
|---|---|---|---|
| Common Sandpiper | 739 | 1.00 | Mara River |
| Black-bellied (Grey) Plover | 716 | 0.99 | Tau Game Lodge (652) |
| Whimbrel | 251 | 0.99 | Elephant Pan |
| Caspian Tern | 129 | 0.95 | Tau Game Lodge |
| Little Tern | 125 | 0.99 | Elephant Pan |
| Sandwich Tern | 94 | 0.98 | Tau Game Lodge |
| Ruddy Turnstone | 30 | 0.95 | Tau Game Lodge |
| Cory's Shearwater | 7 | 0.96 | Kalahari |

Grey Plover at Tau is the poster child. 716 detections at up to 0.99, 91% at
one site, peaking **18h–01h** (113 detections at 20h) — and **solo in 711 of
716 windows**: no other species fires alongside it. That signature (site-locked
+ time-locked + solo + high-confidence) is a persistent site-specific *sound*,
not a bird.

Root cause, established by probing the model with clean Xeno-canto reference
recordings: the model genuinely knows these species (5–6/6 recognition at
0.99–1.00). So this is **not** a weak label. It is a real acoustic collision —
some nocturnal resident sound closely matches the plover's plaintive whistle,
fires a classifier that legitimately knows that whistle, and passes a range
filter that is on but too coarse to veto it (these species do occur on African
coasts and on passage). Leading hypothesis for the Tau sound is **Spotted
Thick-knee** (*Burhinus capensis*), nocturnal, plaintive, right habitat.
Unconfirmed — a targeted congener-confusion probe and a listen to a Tau
nocturnal clip are the outstanding work.

Mitigation built: a `species_suppressions` table with per-site and all-sites
(`*`) rules, an `/admin/suppressions` page with data-driven suggestions, and
pipeline polling on the 60-second species-floor cadence. **The table is
currently empty and the pipeline filter is not yet active** — activation needs
a pipeline restart, which itself risks tripping YouTube's bot-block by
re-resolving every source at once.

### 2. Non-bird sound classified as bird

From the hand-labelled `bad` set, with the operator's note on what it actually
was:

| Detection | Species emitted | Conf | Actually |
|---|---|---|---|
| 24051 | African Scops-Owl | 0.78 | Insects |
| 7976 | African Scops-Owl | 0.52 | Insects |
| 150297 | Eurasian Hoopoe | 0.82 | Human voice |
| 41369 | Narina Trogon | 0.44 | Human |
| 270440 | Small Buttonquail | 0.52 | Vehicle |
| 313702 | Mallard | 0.53 | Human |
| 194613 | Speckled Pigeon | 0.35 | Hippo |
| 325220 | Black Cuckoo | 0.75 | Animal (non-bird) |
| 284981 | African Wood-Owl | 0.89 | Animal (non-bird) |

Low-frequency, broadband, or tonal non-bird sounds — insects, engines, human
speech, hippo — map onto low-frequency bird calls. Insect stridulation → owl is
the most consistent case, and it reaches 0.78. BirdNET has no "not a bird"
class, so every window gets forced onto a bird label.

Note the collateral damage: African Scops-Owl is also the fleet's
highest-volume species at 50,387 detections. Some unknown fraction of that is
insects. That is not a claim the label is mostly wrong — it is a statement that
we cannot currently tell.

### 3. Range filter is a coarse gate, not a check

Detection **397737** — Long-billed Tailorbird (*Artisornis moreaui*, **CR**) at
Majete Cam 3, confidence 0.30. Majete is within the coarse range polygon, so
the filter passed it. The spectrogram has **zero energy above 3 kHz**; the real
song sits at 4–8 kHz. The actual source was a melodic passerine at 2–3 kHz,
likely a Collared Palm-Thrush or robin-chat. Compounding it, Majete Cam 3 is
one of the band-limited sources (~4.4 kHz ceiling) — it *cannot* record the
frequency band that would confirm or refute the ID.

Any conservation-notable hit needs a spectrogram check before it is believed.
See [`detection-examples.md`](detection-examples.md) for the full worked case.

### 4. Same-genus hedging read as co-occurrence

Of 4,969 multi-species windows, **2,422 contain a same-genus pair** and 2,409
are entirely a single genus. These are almost never two congeners calling
together; they are the model hedging between lookalikes — e.g. the Timbavati
francolin stacks (Natal Francolin, 72% Timbavati). Multi-species windows are a
genuine strength of the pipeline (§ What it gets right), but roughly half of
them are one bird with two names, and downstream co-occurrence analysis must
collapse same-genus stacks first.

### 5. Nocturnal high-volume callers dominate everything downstream

Not a bug, but it breaks naive aggregation. Two species are 26% of the
high-confidence corpus. Daily briefs, site notes, and any "most active species"
ranking will be about microphone placement unless volume is normalised per
species per site.

---

## Limits of this evaluation

- **No recall, at all.** No independent ground truth for what was calling.
  Everything here is precision and yield.
- **Precision rests on 292 non-random labels** (0.03% of the corpus), chosen by
  one operator who was looking for interesting and suspicious hits. The
  per-bin figures are indicative only. A randomly-sampled labelling round — say
  100 windows stratified across confidence bins and audio-quality tiers — would
  turn this section from indicative into measured, and is the highest-value
  next piece of work.
- **`unsure` is 49/292 (17%)** of labels. A meaningful share of detections are
  not adjudicable from the clip by a human either, which caps how good any
  ground truth from this method can get.
- **Uptime is worker-uptime**, not audio-availability. Audio availability is
  measured separately and is much worse (§ Coverage).
- **Site metadata is inferred** for the multisite Africam cams (OCR + resolver),
  so per-site attributions carry that error too.
- Numbers are a single snapshot of a live, growing database.

---

## Bottom line

**Trustworthy today**

- Presence of a common, vocal, in-range species at **≥0.9 confidence and ≥2
  sites** — the ~282-species corroborated list.
- Diel activity patterns and dawn-chorus structure, aggregated.
- Relative acoustic activity *between times of day at the same site*.
- Per-site audio quality as an engineering signal — it works and it is honest.

**Not trustworthy today**

- Any single detection below 0.9 without a spectrogram check.
- Any rarity or conservation-notable hit, unconditionally. The two
  most notable "records" in the corpus (Grey Plover ×716, Long-billed
  Tailorbird CR) are both false.
- Raw detection counts as abundance, or as a comparison between sites.
- Species inventories for band-limited, silent, or noise-masked sources —
  ~30% of fleet-hours.
- Co-occurrence inference without collapsing same-genus stacks.

**Ranked next steps**

1. **Gate on audio quality.** ~30% of fleet-hours are unusable and it is
   already measurable. Exclude score <50 from analyses; fix or drop Okaukuejo,
   keep Stony Point and Thawale off.
2. **Activate the suppression filter.** Built, deployed, empty, inert. Needs a
   restart strategy that doesn't trip YouTube's bot-block — per-source recycle
   rather than a full restart.
3. **Randomly-sampled labelling round.** Converts the precision section from
   indicative to measured.
4. **Close out the Grey Plover mechanism** — congener-confusion probe on
   Spotted Thick-knee, plus a human listen to a Tau nocturnal clip.
5. **Spectrogram/band sanity check on notable hits** — reject any detection
   whose energy distribution doesn't overlap the species' known band. This
   alone would have caught 397737.
6. **Prefer new sites over more time on existing ones** for inventory growth —
   discovery on established cams has saturated at ~10 new labels/week.

---

*Related: [`detection-examples.md`](detection-examples.md) — curated worked
examples, showcases and failure modes, each citing a real detection id.*
