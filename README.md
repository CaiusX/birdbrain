# africam-bird

Real-time bird species recognition from live African wildlife cams. Pulls
audio from public [Africam](https://africam.com/) YouTube streams, runs
[BirdNET](https://github.com/kahst/BirdNET-Analyzer) on rolling 3-second
chunks, and surfaces detections on a small self-hosted dashboard. Runs on a
single Raspberry Pi 5.

The live playtest is at **<https://birds.vcexl.com>** — read-only public
view; admin actions are LAN-only.

## What's on the dashboard

- **Activity map** — each site is a 24-hour clock dial sized by unique-
  species count over the last 24 h.
- **Recent detections feed** — live, 5-second refresh, with per-detection
  spectrograms.
- **Per-species page** with a Wikipedia photo, natural-range map, sample
  clips, hour-of-day / daily / confidence histograms, and AI commentary.
- **Per-site page** with the live YouTube embed, recent unique species,
  and AI site commentary.
- **Daily soundscape brief** — Claude-generated overall + per-site bullets
  written once per UTC day.

## Running it

**Setting it up yourself?** See [`INSTALL.md`](INSTALL.md) for step-by-step
instructions (Raspberry Pi or desktop — from clone to always-on services).

Deploy is bare systemd user services on a Raspberry Pi 5 (Debian Bookworm,
Python 3.12, `uv`). Two services:

- **`africam-pipeline`** — one worker per source; yt-dlp → ffmpeg → BirdNET.
- **`africam-web`** — FastAPI + Jinja + HTMX dashboard on `:8765`.

Sources live in [`sources.toml`](sources.toml) (file-managed) or can be
added at runtime via `/admin`. Either kind can be toggled on/off from
`/admin` without a restart — the supervisor reconciles every 15 s.

Background workers (in the web process) need an `ANTHROPIC_API_KEY`
(loaded from `/etc/africam/secrets.env`) to write the per-species,
per-site and daily AI commentary. The detection pipeline does not need
it.

Public exposure is via Cloudflare Tunnel (`cloudflared` user service);
`/admin` and all mutating endpoints return 404 over the tunnel, so the
public side is effectively read-only without app-level auth.

## Acknowledgements

This project would not exist without:

- **[BirdNET-Analyzer](https://github.com/kahst/BirdNET-Analyzer)** —
  Cornell Lab of Ornithology. Model + library that does the actual
  species recognition. **Source code MIT, models CC BY-NC-SA 4.0.** See
  citation below.
- **Patrick McGuire / [BirdNET-Pi](https://github.com/mcguirepr89/BirdNET-Pi)** —
  the original Raspberry-Pi-hosted BirdNET project. The "run BirdNET 24/7
  on a small box, give the operator a useful UI" pattern was figured out
  there first, by hand, long before AI-assisted refactoring made the next
  iteration cheap. This project owes a lot to that prior work.
- **[Africam](https://africam.com/)** — supplies the live wildlife video
  streams (via YouTube) that this project listens to.
- **[Wikipedia / Wikimedia Commons](https://commons.wikimedia.org/) and
  [Wikidata](https://www.wikidata.org/)** — species photos, range maps
  (`P181` fallback), and conservation status icons. CC-BY-SA.
- **[Open-Meteo](https://open-meteo.com/)** — historical weather context
  woven into AI commentary.
- **[Xeno-Canto](https://www.xeno-canto.org/)** — reference bird
  recordings shown in the audition modal.
- **[OpenStreetMap](https://www.openstreetmap.org/) contributors** — map
  tiles (ODbL).
- **[Claude](https://www.anthropic.com/) (Anthropic)** — writes the
  per-species, per-site, and daily AI commentary.
- **[yt-dlp](https://github.com/yt-dlp/yt-dlp), [ffmpeg](https://ffmpeg.org/),
  [Leaflet](https://leafletjs.com/), [HTMX](https://htmx.org/),
  [Tailwind](https://tailwindcss.com/)** — the wiring.

## Citation

If you reference or build on this, please cite the underlying BirdNET work:

```bibtex
@article{kahl2021birdnet,
  title   = {BirdNET: A deep learning solution for avian diversity monitoring},
  author  = {Kahl, Stefan and Wood, Connor M and Eibl, Maximilian and Klinck, Holger},
  journal = {Ecological Informatics},
  volume  = {61},
  pages   = {101236},
  year    = {2021},
  publisher = {Elsevier}
}
```

## License

The code in this repository is licensed under the **MIT License** — see
[`LICENSE`](LICENSE).

The **BirdNET-Analyzer models** this project loads at runtime are licensed
separately under **CC BY-NC-SA 4.0** (non-commercial, share-alike). That
binds any deployment that actually runs detection — including this one. The
project's authors state that *educational and research use is considered
non-commercial*.

Wildlife video streams remain the property of Africam and their partner
lodges; we consume them as YouTube embeds, do not redistribute them, and
analyse only the audio.
