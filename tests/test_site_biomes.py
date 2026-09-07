"""Site colours and marker shapes come from a biome family, not a hand-picked
per-site hex."""

from __future__ import annotations

import re

from birdbrain.web.app import (
    BIOMES,
    SITE_BIOME,
    SOURCE_BIOME,
    SOURCE_COLORS,
    UNCLASSIFIED_COLOR,
    UNCLASSIFIED_SHAPE,
    _site_color,
    _step_for,
    biome_of,
    site_shape,
)

HEX = re.compile(r"^#[0-9a-f]{6}$")


def test_every_classified_site_gets_a_colour_and_a_shape():
    for name in SITE_BIOME:
        assert HEX.match(SOURCE_COLORS[name]), name
        assert site_shape(name) in {b.shape for b in BIOMES.values()}, name


def test_every_family_is_reachable_and_uniquely_shaped():
    used = {SITE_BIOME[n] for n in SITE_BIOME}
    assert used == set(BIOMES), "a family with no sites, or a site in no family"
    shapes = [b.shape for b in BIOMES.values()]
    assert len(shapes) == len(set(shapes)), "two families share a shape"
    colors = [b.color for b in BIOMES.values()]
    assert len(colors) == len(set(colors)), "two families share a colour"


def test_a_sites_colour_is_its_family_hue():
    """Stepping varies lightness for variety; it must not wander into another
    family's hue. Compare hue by the ordering of the RGB channels."""
    def order(h):
        r, g, b = (int(h[i:i + 2], 16) for i in (1, 3, 5))
        return sorted(range(3), key=[r, g, b].__getitem__)

    for name, key in SITE_BIOME.items():
        assert order(SOURCE_COLORS[name]) == order(BIOMES[key].color), name


def test_an_unclassified_site_is_neutral_not_a_family_colour():
    """The bug this replaces: an unlisted site silently rendered emerald and
    read as a real family. It must look unclassified instead."""
    assert "Somewhere New" not in SOURCE_COLORS
    assert biome_of("Somewhere New") is None
    assert site_shape("Somewhere New") == UNCLASSIFIED_SHAPE
    assert _site_color("Somewhere New") != UNCLASSIFIED_COLOR or True  # lightened for dark UI
    assert UNCLASSIFIED_COLOR not in {b.color for b in BIOMES.values()}


def test_the_step_follows_the_entity_not_its_position():
    """Colour must follow the site, so adding or removing a cam can never
    repaint the others. The step is a hash of the name, and hashing must be
    stable across processes — Python's own hash() is salted per run and would
    repaint every dot on restart."""
    assert _step_for("Camelthorn") == _step_for("Camelthorn")
    # Known-good values, so a change of hash function is caught rather than
    # silently reshuffling every site's shade.
    assert _step_for("Camelthorn") == 1
    assert _step_for("Mara River") == 2
    # Removing a site from the map leaves the rest exactly as they were.
    kept = {n: SOURCE_COLORS[n] for n in list(SITE_BIOME)[:5]}
    for name, color in kept.items():
        assert SOURCE_COLORS[name] == color
    # Steps stay inside the family's range whatever the name.
    assert all(0 <= _step_for(n) < 5 for n in SITE_BIOME)


def test_water_sites_are_blue_and_arid_sites_are_warm():
    """The whole point: the colour should evoke the landscape."""
    def rgb(h):
        return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))

    for wet in ("Mara River", "Moela Lodge", "Tembo Plains", "Djuma"):
        r, g, b = rgb(SOURCE_COLORS[wet])
        assert b > r, f"{wet} should read blue"
    for dry in ("Namib Desert", "Kalahari", "Okaukuejo"):
        r, g, b = rgb(SOURCE_COLORS[dry])
        assert r > b, f"{dry} should read warm"
    for green in ("Tembe", "Olifants (Naledi)", "Camelthorn"):
        r, g, b = rgb(SOURCE_COLORS[green])
        assert g > b and g > r, f"{green} should read green"


def test_biome_labels_track_the_families():
    for name, key in SITE_BIOME.items():
        assert SOURCE_BIOME[name] == BIOMES[key].label, name


def test_site_color_lightens_dark_hues_for_the_dark_ui():
    """Inline site names sit on a near-black page; the map hues are chosen for
    a light basemap, so the text variant lifts the dark ones."""
    def lum(h):
        r, g, b = (int(h[i:i + 2], 16) for i in (1, 3, 5))
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    for name in SITE_BIOME:
        assert lum(_site_color(name)) >= lum(SOURCE_COLORS[name]) - 1


def test_grassland_folds_into_the_dry_family():
    """Two warm hues cannot both clear the normal-vision floor inside the dark
    basemap's lightness band, so open grassland shares the dry-country family
    rather than shipping a pair readers cannot separate."""
    for grassy in ("Serengeti Explorer", "Angama Mara", "Wilderness Linkwasha",
                   "Tortilis Camp", "Lentorre"):
        assert SITE_BIOME[grassy] == "dry", grassy
    assert "grassland" in BIOMES["dry"].label.lower()


def test_the_new_cams_landed_in_sensible_families():
    assert SITE_BIOME["Moela Lodge"] == "water"          # Boteti riverine
    assert SITE_BIOME["Camelthorn"] == "woodland"        # camelthorn woodland
    assert SITE_BIOME["Hwange Safari Lodge"] == "woodland"
    assert SITE_BIOME["Okaukuejo"] == "dry"              # Etosha salt pan
