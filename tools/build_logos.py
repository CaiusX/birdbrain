#!/usr/bin/env python3
"""Generate ten Pythagoras-tree logo variants for /logo-test.

All variants share the same colour palette (trunk emerald-600 → mid
teal-600 → outer amber-500) and leaf-tip dots drawn from the network's
SOURCE_COLORS, rotated through. Only the geometry/visual emphasis varies:
each variant lives in its own builder function below.

Run from the repo root:

    python tools/build_logos.py

Writes ten SVG files into src/africam/web/static/logo-test/.
"""
from __future__ import annotations

import math
from pathlib import Path

# ---- Palette ---------------------------------------------------------------

# Mirror of SOURCE_COLORS in src/africam/web/app.py (11 entries, same order).
# Kept in sync by hand — the generator is a build-time tool, importing the
# web module would drag in FastAPI + SQLAlchemy for no good reason.
SITE_COLORS = [
    "#059669",  # Tembe — emerald-600
    "#84cc16",  # Olifants (Naledi) — lime-500
    "#b91c1c",  # Timbavati — red-700
    "#a8a29e",  # Twin Pan — stone-400
    "#ea580c",  # Safarihoek — orange-600
    "#b45309",  # Tau Game Lodge — amber-700
    "#eab308",  # Tortilis Camp — yellow-500
    "#0891b2",  # Mara River — cyan-600
    "#4d7c0f",  # Mpala Watering Hole — lime-700
    "#1d4ed8",  # Stony Point — blue-700
    "#7c2d12",  # Elephant Pan — orange-900
]

TRUNK_COLOR = "#059669"  # emerald-600
MID_COLOR   = "#0d9488"  # teal-600
OUTER_COLOR = "#f59e0b"  # amber-500


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def _lerp(c1: str, c2: str, t: float) -> str:
    r1, g1, b1 = _hex_to_rgb(c1)
    r2, g2, b2 = _hex_to_rgb(c2)
    return _rgb_to_hex((
        int(r1 + (r2 - r1) * t),
        int(g1 + (g2 - g1) * t),
        int(b1 + (b2 - b1) * t),
    ))


def depth_color(depth: int, max_depth: int) -> str:
    """3-stop gradient TRUNK → MID → OUTER as depth grows from 0 to
    max_depth. Pure mid colour at the half-way generation."""
    if max_depth == 0:
        return TRUNK_COLOR
    t = depth / max_depth
    if t < 0.5:
        return _lerp(TRUNK_COLOR, MID_COLOR, t * 2)
    return _lerp(MID_COLOR, OUTER_COLOR, (t - 0.5) * 2)


def stroke_width(depth: int, max_depth: int,
                 max_w: float = 8.0, min_w: float = 1.2) -> float:
    if max_depth == 0:
        return max_w
    return max_w - (max_w - min_w) * (depth / max_depth)


# ---- SVG fragment writer ---------------------------------------------------


class SVG:
    """Minimal SVG accumulator — no DOM, just an ordered list of element
    strings. Keeps output small and deterministic so diffs read clean."""

    def __init__(self, w: int, h: int) -> None:
        self.w = w
        self.h = h
        self.parts: list[str] = []

    def line(self, x1: float, y1: float, x2: float, y2: float,
             stroke: str, width: float) -> None:
        self.parts.append(
            f'<line x1="{x1:.2f}" y1="{y1:.2f}" '
            f'x2="{x2:.2f}" y2="{y2:.2f}" '
            f'stroke="{stroke}" stroke-width="{width:.2f}" '
            f'stroke-linecap="round"/>'
        )

    def circle(self, cx: float, cy: float, r: float,
               fill: str, opacity: float = 1.0) -> None:
        op = f' opacity="{opacity:.2f}"' if opacity < 1 else ""
        self.parts.append(
            f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r:.2f}" '
            f'fill="{fill}"{op}/>'
        )

    def polyline(self, points: list[tuple[float, float]],
                 stroke: str, width: float, opacity: float = 1.0) -> None:
        pts = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
        op = f' opacity="{opacity:.2f}"' if opacity < 1 else ""
        self.parts.append(
            f'<polyline points="{pts}" fill="none" stroke="{stroke}" '
            f'stroke-width="{width:.2f}" stroke-linecap="round"{op}/>'
        )

    def quad(self, x1: float, y1: float, cx: float, cy: float,
             x2: float, y2: float, stroke: str,
             width: float, opacity: float = 1.0) -> None:
        """Single quadratic Bézier — useful for the dense-network arcs."""
        op = f' opacity="{opacity:.2f}"' if opacity < 1 else ""
        self.parts.append(
            f'<path d="M {x1:.2f} {y1:.2f} Q {cx:.2f} {cy:.2f} {x2:.2f} {y2:.2f}" '
            f'fill="none" stroke="{stroke}" stroke-width="{width:.2f}" '
            f'stroke-linecap="round"{op}/>'
        )

    def render(self) -> str:
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {self.w} {self.h}">\n'
            + "\n".join(self.parts)
            + "\n</svg>\n"
        )


# ---- Recursive Pythagoras builder ------------------------------------------


def build_tree(svg: SVG, x0: float, y0: float,
               init_angle_deg: float, init_length: float,
               max_depth: int, split_angle: float, ratio: float,
               *,
               max_stroke: float = 8.0, min_stroke: float = 1.2,
               left_angle: float | None = None,
               right_angle: float | None = None,
               return_nodes: bool = False
               ) -> tuple[list[tuple[float, float]], dict[int, list]]:
    """Recurse the Pythagoras tree. Draws into ``svg`` and returns
    (tips, nodes_by_depth) for downstream effects (leaf dots, skip
    connections, head accents)."""
    tips: list[tuple[float, float]] = []
    nodes: dict[int, list[tuple[float, float]]] = {
        d: [] for d in range(max_depth + 1)
    }

    def recurse(x: float, y: float, angle_deg: float,
                length: float, depth: int) -> None:
        rad = math.radians(angle_deg)
        x2 = x + length * math.cos(rad)
        y2 = y - length * math.sin(rad)  # SVG y-down
        col = depth_color(depth, max_depth)
        w = stroke_width(depth, max_depth, max_stroke, min_stroke)
        svg.line(x, y, x2, y2, stroke=col, width=w)
        nodes[depth].append((x2, y2))
        if depth >= max_depth:
            tips.append((x2, y2))
            return
        la = left_angle if left_angle is not None else split_angle
        ra = right_angle if right_angle is not None else split_angle
        recurse(x2, y2, angle_deg + la, length * ratio, depth + 1)
        recurse(x2, y2, angle_deg - ra, length * ratio, depth + 1)

    recurse(x0, y0, init_angle_deg, init_length, 0)
    return tips, nodes


def add_leaves(svg: SVG, tips: list[tuple[float, float]],
               palette: list[str], r: float = 3.0) -> None:
    """Drop a coloured circle on every terminal tip, rotating through the
    palette. With 64 tips and an 11-colour palette each colour appears 5–6×
    and the rotation cycle (gcd 1) ensures no two siblings share a hue."""
    for i, (x, y) in enumerate(tips):
        svg.circle(x, y, r=r, fill=palette[i % len(palette)])


# ---- Ten variants ----------------------------------------------------------

WIDTH = 240
HEIGHT = 200
ROOT_X = 120
ROOT_Y = 200
TRUNK_LEN = 50

# Each builder returns the rendered SVG string.

def v01_classic() -> str:
    """Birdbrain Classic — symmetric, depth 6, angle 30°, ratio 0.65."""
    svg = SVG(WIDTH, HEIGHT)
    tips, _ = build_tree(svg, ROOT_X, ROOT_Y, 90, TRUNK_LEN,
                         max_depth=6, split_angle=30, ratio=0.65)
    add_leaves(svg, tips, SITE_COLORS)
    return svg.render()


def v02_heart() -> str:
    """Heart Canopy — angle 35°, ratio 0.60 → squat, heart-shaped."""
    svg = SVG(WIDTH, HEIGHT)
    tips, _ = build_tree(svg, ROOT_X, ROOT_Y, 90, TRUNK_LEN,
                         max_depth=6, split_angle=35, ratio=0.60)
    add_leaves(svg, tips, SITE_COLORS)
    return svg.render()


def v03_tilted() -> str:
    """Tilted Perch — classic geometry rotated 12° clockwise."""
    svg = SVG(WIDTH, HEIGHT)
    tips, _ = build_tree(svg, ROOT_X, ROOT_Y, 90 - 12, TRUNK_LEN,
                         max_depth=6, split_angle=30, ratio=0.65)
    add_leaves(svg, tips, SITE_COLORS)
    return svg.render()


def v04_asymmetric() -> str:
    """Asymmetric Branches — 35° left split, 25° right split."""
    svg = SVG(WIDTH, HEIGHT)
    tips, _ = build_tree(
        svg, ROOT_X, ROOT_Y, 90, TRUNK_LEN,
        max_depth=6, split_angle=30, ratio=0.65,
        left_angle=35, right_angle=25,
    )
    add_leaves(svg, tips, SITE_COLORS)
    return svg.render()


def v05_dense() -> str:
    """Dense Network — classic + quad-bezier skip connections between
    every same-depth pair within radius. Neural-net read dialled up."""
    svg = SVG(WIDTH, HEIGHT)
    tips, nodes = build_tree(svg, ROOT_X, ROOT_Y, 90, TRUNK_LEN,
                             max_depth=6, split_angle=30, ratio=0.65)
    # Skip connections at depths 4..6 only — adding them at depths 1-3 makes
    # the whole canopy turn into a tangle.
    for d in (4, 5, 6):
        pts = nodes[d]
        for i in range(len(pts)):
            for j in range(i + 1, len(pts)):
                ax, ay = pts[i]
                bx, by = pts[j]
                dx, dy = bx - ax, by - ay
                dist = (dx * dx + dy * dy) ** 0.5
                if dist < 18 + d * 2:  # depth-aware radius
                    # Arc that bows slightly downward — visible without
                    # tangling with the branch lines above.
                    cx = (ax + bx) / 2
                    cy = (ay + by) / 2 + 4
                    svg.quad(ax, ay, cx, cy, bx, by,
                             stroke="#94a3b8", width=0.6, opacity=0.25)
    add_leaves(svg, tips, SITE_COLORS)
    return svg.render()


def v06_minimal() -> str:
    """Minimal Mark — depth 4, thicker strokes. Favicon-friendly."""
    svg = SVG(WIDTH, HEIGHT)
    tips, _ = build_tree(svg, ROOT_X, ROOT_Y, 90, TRUNK_LEN * 1.2,
                         max_depth=4, split_angle=30, ratio=0.65,
                         max_stroke=12, min_stroke=4)
    add_leaves(svg, tips, SITE_COLORS, r=4.5)
    return svg.render()


def v07_deep() -> str:
    """Deep Detail — depth 7, fine strokes. Intricate canopy."""
    svg = SVG(WIDTH, HEIGHT)
    tips, _ = build_tree(svg, ROOT_X, ROOT_Y, 90, TRUNK_LEN * 0.85,
                         max_depth=7, split_angle=28, ratio=0.66,
                         max_stroke=6, min_stroke=0.8)
    add_leaves(svg, tips, SITE_COLORS, r=2.0)
    return svg.render()


def _add_head_accent(svg: SVG, all_nodes: list[tuple[float, float]],
                     chevron_color: str = "#a7f3d0",
                     beak_color: str = "#fbbf24",
                     scale: float = 1.0) -> None:
    """Place a small head chevron + beak dot at the highest node in
    ``all_nodes``. Used by v08 (Head Accent) and the dual-reading variants
    11/14/15 below."""
    top_x, top_y = min(all_nodes, key=lambda p: p[1])
    s = scale
    svg.polyline(
        [(top_x - 5 * s, top_y - 4 * s),
         (top_x,         top_y - 11 * s),
         (top_x + 5 * s, top_y - 4 * s)],
        stroke=chevron_color, width=2.2 * s,
    )
    svg.circle(top_x, top_y - 13 * s, r=2.2 * s, fill=beak_color)


def v08_head_accent() -> str:
    """Head Accent — classic + small emerald chevron at apex (subtle bird
    head emerging from the canopy)."""
    svg = SVG(WIDTH, HEIGHT)
    tips, nodes = build_tree(svg, ROOT_X, ROOT_Y, 90, TRUNK_LEN,
                             max_depth=6, split_angle=30, ratio=0.65)
    add_leaves(svg, tips, SITE_COLORS)
    _add_head_accent(svg, [p for d in nodes.values() for p in d])
    return svg.render()


def v09_wing_spread() -> str:
    """Wing Spread — angle 45°, ratio 0.70 → wide canopy, wings extended."""
    svg = SVG(WIDTH, HEIGHT)
    tips, _ = build_tree(svg, ROOT_X, ROOT_Y, 90, TRUNK_LEN * 0.85,
                         max_depth=6, split_angle=45, ratio=0.70)
    add_leaves(svg, tips, SITE_COLORS)
    return svg.render()


def v10_spectrogram() -> str:
    """Spectrogram Rotation — trunk on left, branches fanning right.
    Trunk start at left-centre; tree extends horizontally."""
    svg = SVG(WIDTH, HEIGHT)
    # Initial angle = 0° (right). Move root to left margin.
    tips, _ = build_tree(svg, 25, HEIGHT / 2, 0, TRUNK_LEN * 1.1,
                         max_depth=6, split_angle=30, ratio=0.65)
    add_leaves(svg, tips, SITE_COLORS)
    return svg.render()


def v11_crested_heart() -> str:
    """Crested Heart — heart-canopy geometry (v2) + apex chevron and beak
    dot from v8. The brain hemispheres of v2 with an explicit bird head."""
    svg = SVG(WIDTH, HEIGHT)
    tips, nodes = build_tree(svg, ROOT_X, ROOT_Y, 90, TRUNK_LEN,
                             max_depth=6, split_angle=35, ratio=0.60)
    add_leaves(svg, tips, SITE_COLORS)
    _add_head_accent(svg, [p for d in nodes.values() for p in d])
    return svg.render()


def v12_heart_on_neck() -> str:
    """Heart on Neck — v2 canopy on top of an S-curved trunk drawn as a
    Bézier path. The S gives the bird an explicit neck; the canopy still
    reads as brain hemispheres."""
    svg = SVG(WIDTH, HEIGHT)
    # S-curve neck from foot (120, 200) up to canopy root (120, 140).
    # Drawn as a thick path with a slight forward S — bird-neck shape.
    svg.parts.append(
        '<path d="M 120 200 C 132 188 108 162 120 140" '
        'fill="none" stroke="#059669" stroke-width="8" '
        'stroke-linecap="round"/>'
    )
    # Build the canopy starting from the top of the S-curve.
    tips, _ = build_tree(svg, 120, 140, 90, TRUNK_LEN * 0.85,
                         max_depth=6, split_angle=35, ratio=0.60)
    add_leaves(svg, tips, SITE_COLORS)
    return svg.render()


def v13_synaptic_heart() -> str:
    """Synaptic Heart — v2 + sparse arc skip-connections drawn ONLY at
    depths 3 and 4 within a tight radius, each with a small midpoint dot
    that reads as a synaptic bouton. Dendritic brain reading without v5's
    visual noise."""
    svg = SVG(WIDTH, HEIGHT)
    tips, nodes = build_tree(svg, ROOT_X, ROOT_Y, 90, TRUNK_LEN,
                             max_depth=6, split_angle=35, ratio=0.60)
    # Skip-connections only at mid-depths, tighter radius than v5.
    for d in (3, 4):
        pts = nodes[d]
        for i in range(len(pts)):
            for j in range(i + 1, len(pts)):
                ax, ay = pts[i]
                bx, by = pts[j]
                dx, dy = bx - ax, by - ay
                dist = (dx * dx + dy * dy) ** 0.5
                if dist < 14:  # tight — picks up only nearby pairs
                    cx = (ax + bx) / 2
                    cy = (ay + by) / 2 + 3
                    svg.quad(ax, ay, cx, cy, bx, by,
                             stroke="#a7f3d0", width=0.5, opacity=0.35)
                    # Synaptic bouton at midpoint.
                    svg.circle(cx, cy - 0.5, r=0.9,
                               fill="#a7f3d0", opacity=0.55)
    add_leaves(svg, tips, SITE_COLORS)
    return svg.render()


def v14_soaring_heart() -> str:
    """Soaring Heart (polished) — v9 wing-spread geometry with a properly
    centred head + triangular beak at the canopy apex, plus a subtle tail
    accent at the trunk root.

    Read as a soaring bird seen from above: wings spread wide, head + beak
    pointing forward (up the viewBox), body tapering down through the
    trunk, a small spread-feather hint at the tail. The fractal canopy
    still reads as paired hemispheres at the brain layer."""
    svg = SVG(WIDTH, HEIGHT)
    tips, nodes = build_tree(svg, ROOT_X, ROOT_Y, 90, TRUNK_LEN * 0.85,
                             max_depth=6, split_angle=45, ratio=0.70)
    add_leaves(svg, tips, SITE_COLORS)

    # ---- Head + beak, locked to the symmetry axis ----
    # Pick the apex y from the actual canopy nodes; lock x to 120 so the
    # symmetric tree always gets a centred head (the natural min(y) pick
    # lands on whichever apex node was added first, which is off-centre).
    apex_y = min(p[1] for d in nodes.values() for p in d)
    hx, hy = 120, apex_y - 3.0
    # Rounded chevron — bird crown.
    svg.polyline(
        [(hx - 6.5, hy),
         (hx - 2.5, hy - 7),
         (hx + 2.5, hy - 7),
         (hx + 6.5, hy)],
        stroke="#a7f3d0", width=2.6,
    )
    # Triangular beak pointing forward (up). Polygon — explicitly added
    # because SVG.polyline can't close; tiny inline path keeps the helper
    # surface area unchanged.
    svg.parts.append(
        f'<polygon points="{hx - 2.5:.2f},{hy - 7:.2f} '
        f'{hx + 2.5:.2f},{hy - 7:.2f} '
        f'{hx:.2f},{hy - 13:.2f}" fill="#fbbf24"/>'
    )

    # ---- Tail accent: splayed-feather chevron near the trunk root ----
    # Subtle (opacity 0.55) so it whispers "tail feathers" without
    # competing with the trunk's emerald stroke.
    tx, ty = 120, 197
    svg.polyline(
        [(tx - 9, ty + 2),
         (tx - 4, ty - 3),
         (tx,     ty),
         (tx + 4, ty - 3),
         (tx + 9, ty + 2)],
        stroke="#059669", width=1.6, opacity=0.55,
    )

    return svg.render()


def v15_wide_crested_heart() -> str:
    """Wide Crested Heart — geometric compromise between v2 (angle 35°) and
    v9 (angle 45°) at angle 40°, with a head chevron + beak. Hybrid bird
    with both wing-spread and visible brain-hemisphere lobes."""
    svg = SVG(WIDTH, HEIGHT)
    tips, nodes = build_tree(svg, ROOT_X, ROOT_Y, 90, TRUNK_LEN,
                             max_depth=6, split_angle=40, ratio=0.65)
    add_leaves(svg, tips, SITE_COLORS)
    _add_head_accent(svg, [p for d in nodes.values() for p in d])
    return svg.render()


VARIANTS = [
    (1,  "Birdbrain Classic",      "Symmetric · depth 6 · angle 30° · ratio 0.65",                v01_classic),
    (2,  "Heart Canopy",           "Squat · depth 6 · angle 35° · ratio 0.60",                    v02_heart),
    (3,  "Tilted Perch",           "Classic geometry rotated 12° clockwise",                       v03_tilted),
    (4,  "Asymmetric Branches",    "Left split 35°, right split 25°, depth 6",                    v04_asymmetric),
    (5,  "Dense Network",          "Classic + skip-connections at depths 4–6",                    v05_dense),
    (6,  "Minimal Mark",           "Depth 4, thicker strokes — favicon-optimised",                v06_minimal),
    (7,  "Deep Detail",            "Depth 7, fine strokes — large-display canopy",                v07_deep),
    (8,  "Head Accent",            "Classic + emerald chevron and amber beak dot at apex",        v08_head_accent),
    (9,  "Wing Spread",            "Angle 45°, ratio 0.70 — wings-extended canopy",               v09_wing_spread),
    (10, "Spectrogram Rotation",   "Classic rotated 90° CCW — trunk left, branches fan right",    v10_spectrogram),
    # ---- Dual bird+brain riffs on the user's picks (v2 and v9) ----
    (11, "Crested Heart",          "v2 heart canopy + head chevron + beak dot",                   v11_crested_heart),
    (12, "Heart on Neck",          "v2 canopy on an S-curved bird neck",                          v12_heart_on_neck),
    (13, "Synaptic Heart",         "v2 + sparse arc skip-connections with synaptic dots",          v13_synaptic_heart),
    (14, "Soaring Heart",          "v9 wing spread + head chevron + beak dot",                    v14_soaring_heart),
    (15, "Wide Crested Heart",     "Angle 40° (between v2 and v9) + head chevron + beak dot",     v15_wide_crested_heart),
]


# ---- Driver ----------------------------------------------------------------


def main() -> None:
    out_dir = Path(__file__).resolve().parent.parent / "src/africam/web/static/logo-test"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Writing 10 variants to {out_dir}")
    for n, name, _blurb, builder in VARIANTS:
        path = out_dir / f"logo-{n:02d}.svg"
        path.write_text(builder())
        print(f"  logo-{n:02d}.svg  ({path.stat().st_size:>5} bytes)  {name}")


if __name__ == "__main__":
    main()
