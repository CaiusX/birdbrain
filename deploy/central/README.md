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
