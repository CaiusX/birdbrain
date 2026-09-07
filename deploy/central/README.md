# Central's timers

User units for the central Pi (the one that runs `birdbrain web`). Install:

    cp deploy/central/birdbrain-prune.* ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now birdbrain-prune.timer

`birdbrain-prune` applies the clip retention policy in `birdbrain/retention.py`
nightly: clips older than 30 days go, except anything a person audited and the
newest low/mid/high-confidence clip of every species at every source. Give
loud, common species a shorter window with `birdbrain clip-retention`:

    birdbrain clip-retention "Egyptian Goose" --days 2
    birdbrain clip-retention            # list overrides

Dry-run the sweep any time with `birdbrain prune --dry-run`.

## Muted cams

Some cams stream a real audio track carrying pure digital silence, usually a
sanctuary that mutes for privacy. They are worth nothing to BirdNET, so they
get disabled and recorded in the `muted_cams` setting:

    uv run python scripts/check-muted-cams.py --add "GRACE Gorilla Sanctuary"

`birdbrain-muted-cams.timer` then samples each one weekly and puts it back on
the roster by itself the moment it carries sound again. Run it by hand with
`--dry-run` to measure without changing anything, or `--list` to see what is
currently muted. It only ever touches names on that list, so a cam paused for
some other reason (a YouTube IP block, see `scripts/youtube-resume.py`) is
never disturbed.

Install these on whichever box runs the cams' pipeline — that is the ingest
node `bne`, not central, for every YouTube cam.
