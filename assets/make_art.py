#!/usr/bin/env python3
"""Draw the hand-drawn (Excalidraw-style) banner and logo as standalone SVGs.

    uv run --with fonttools --with brotli python assets/make_art.py --font /path/to/Virgil.woff2

Virgil is Excalidraw's handwriting font (OFL-1.1, https://excalidraw.com/Virgil.woff2).
Lettering is written into the SVG as outlines, so the files need no font at view time
and look the same on GitHub, in a marketplace listing, or as a rasterized icon.

Shapes are sketched the way a hand would: each outline is drawn twice, each pass pushed
off the true line by slow random drift. The random seed is fixed, so output is stable
from run to run; change SEED for a different "take" of the same drawing.
"""

from __future__ import annotations

import argparse
import math
import random
from pathlib import Path

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

SEED = 7
INK = "#1e1e1e"
MUTED = "#6f6a60"
PAPER = "#fffdf7"
# Excalidraw's default pastel palette.
ORANGE, PINK, GREEN, PURPLE, BLUE, YELLOW = "#ffd8a8", "#ffc9c9", "#b2f2bb", "#d0bfff", "#a5d8ff", "#ffec99"

Point = tuple[float, float]


def n(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".")


# --- geometry: every shape becomes a dense list of points -------------------------------------

def line(p: Point, q: Point, step: float = 6) -> list[Point]:
    count = max(2, int(math.dist(p, q) / step))
    return [(p[0] + (q[0] - p[0]) * i / count, p[1] + (q[1] - p[1]) * i / count) for i in range(count + 1)]


def polygon(points: list[Point], closed: bool = True) -> list[Point]:
    pts: list[Point] = []
    edges = list(zip(points, points[1:] + points[:1])) if closed else list(zip(points, points[1:]))
    for p, q in edges:
        pts += line(p, q)[:-1]
    return pts + [points[0] if closed else points[-1]]


def arc(cx: float, cy: float, r: float, a0: float, a1: float, step: float = 4) -> list[Point]:
    count = max(3, int(abs(a1 - a0) * r / step))
    return [(cx + r * math.cos(a0 + (a1 - a0) * i / count), cy + r * math.sin(a0 + (a1 - a0) * i / count))
            for i in range(count + 1)]


def rrect(x: float, y: float, w: float, h: float, r: float) -> list[Point]:
    hp = math.pi / 2
    return (line((x + r, y), (x + w - r, y))[:-1] + arc(x + w - r, y + r, r, -hp, 0)[:-1]
            + line((x + w, y + r), (x + w, y + h - r))[:-1] + arc(x + w - r, y + h - r, r, 0, hp)[:-1]
            + line((x + w - r, y + h), (x + r, y + h))[:-1] + arc(x + r, y + h - r, r, hp, 2 * hp)[:-1]
            + line((x, y + h - r), (x, y + r))[:-1] + arc(x + r, y + r, r, 2 * hp, 3 * hp))


def circle(cx: float, cy: float, r: float) -> list[Point]:
    return arc(cx, cy, r, 0, 2 * math.pi)


def gear(cx: float, cy: float, r_out: float, r_in: float, teeth: int, phase: float = 0.0) -> list[Point]:
    """Outline of a cog: flat-topped teeth around a root circle."""
    pitch = 2 * math.pi / teeth
    corners: list[Point] = []
    for i in range(teeth):
        a = phase + i * pitch
        for r, half in ((r_in, 0.30), (r_out, 0.17), (r_out, -0.17), (r_in, -0.30)):
            corners.append((cx + r * math.cos(a - half * pitch), cy + r * math.sin(a - half * pitch)))
    return polygon(corners)


def squiggle(x: float, y_bottom: float, y_top: float, sway: float, waves: float = 1.5,
             drift: float = 0.0) -> list[Point]:
    """A rising wavy line: the usual shorthand for steam. `drift` leans it sideways as it rises."""
    count = max(12, int((y_bottom - y_top) / 3))
    return [(x + drift * i / count + sway * math.sin(waves * 2 * math.pi * i / count),
             y_bottom - (y_bottom - y_top) * i / count) for i in range(count + 1)]


def bezier(p0: Point, p1: Point, p2: Point, p3: Point, count: int = 28) -> list[Point]:
    out = []
    for i in range(count + 1):
        t = i / count
        a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t ** 2, t ** 3
        out.append((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0], a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]))
    return out


def clean_path(pts: list[Point], closed: bool = False) -> str:
    return "M" + " L".join(f"{n(x)} {n(y)}" for x, y in pts) + (" Z" if closed else "")


# --- the sketching hand ------------------------------------------------------------------------

class Hand:
    def __init__(self, seed: int = SEED) -> None:
        self.rnd = random.Random(seed)
        self.clip_count = 0

    def _drift(self, pts: list[Point], amp: float) -> list[Point]:
        """Push points off the true line by slow drift: two sine waves of random phase per axis."""
        r = self.rnd
        waves = [(r.uniform(70, 130), r.uniform(0, 6.3), r.uniform(28, 46), r.uniform(0, 6.3)) for _ in range(2)]
        out, travelled, prev = [], 0.0, pts[0]
        for p in pts:
            travelled += math.dist(prev, p)
            prev = p
            dx, dy = (amp * (0.65 * math.sin(travelled / w1 + p1) + 0.35 * math.sin(travelled / w2 + p2))
                      for w1, p1, w2, p2 in waves)
            out.append((p[0] + dx, p[1] + dy))
        return out

    def stroke(self, pts: list[Point], width: float = 1.8, color: str = INK, amp: float = 1.3,
               passes: int = 2) -> str:
        """The outline, drawn `passes` times. Smoothed through midpoints so corners soften a little."""
        paths = []
        for _ in range(passes):
            d = self._drift(pts, amp)
            cmds = [f"M{n(d[0][0])} {n(d[0][1])}"]
            for a, b in zip(d[1:], d[2:]):
                cmds.append(f"Q{n(a[0])} {n(a[1])} {n((a[0] + b[0]) / 2)} {n((a[1] + b[1]) / 2)}")
            cmds.append(f"L{n(d[-1][0])} {n(d[-1][1])}")
            paths.append(f'<path d="{" ".join(cmds)}" fill="none" stroke="{color}" stroke-width="{n(width)}" '
                         f'stroke-linecap="round" stroke-linejoin="round"/>')
        return "".join(paths)

    def solid(self, pts: list[Point], fill: str) -> str:
        return f'<path d="{clean_path(pts, True)}" fill="{fill}"/>'

    def hachure(self, pts: list[Point], color: str, gap: float = 7, width: float = 1.4) -> str:
        """Diagonal pencil shading, clipped to the shape."""
        self.clip_count += 1
        cid = f"clip{self.clip_count}"
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        lines, x = [], x0 - (y1 - y0)
        while x < x1:
            lines.append(self.stroke(line((x, y1), (x + (y1 - y0), y0), 12), width, color, amp=0.8, passes=1))
            x += gap + self.rnd.uniform(-1, 1)
        return (f'<clipPath id="{cid}"><path d="{clean_path(pts, True)}"/></clipPath>'
                f'<g clip-path="url(#{cid})">{"".join(lines)}</g>')

    def arrowhead(self, tip: Point, angle: float, size: float = 11, width: float = 1.8, color: str = INK) -> str:
        out = ""
        for side in (-0.45, 0.45):
            a = angle + math.pi + side
            out += self.stroke(line(tip, (tip[0] + size * math.cos(a), tip[1] + size * math.sin(a)), 3),
                               width, color, amp=0.5)
        return out


# --- lettering as outlines ---------------------------------------------------------------------

class Lettering:
    def __init__(self, font_path: Path) -> None:
        font = TTFont(str(font_path))
        self.glyphs, self.cmap, self.upm = font.getGlyphSet(), font.getBestCmap(), font["head"].unitsPerEm

    def text(self, s: str, x: float, y: float, size: float, fill: str = INK, anchor: str = "middle") -> str:
        scale = size / self.upm
        names = [self.cmap.get(ord(ch)) or self.cmap[ord("-")] for ch in s]
        width = sum(self.glyphs[g].width for g in names) * scale
        cursor = x - width / 2 if anchor == "middle" else x
        pen = SVGPathPen(self.glyphs, ntos=n)
        for g in names:
            self.glyphs[g].draw(TransformPen(pen, (scale, 0, 0, -scale, cursor, y)))
            cursor += self.glyphs[g].width * scale
        return f'<path d="{pen.getCommands()}" fill="{fill}"/>'


# --- banner ------------------------------------------------------------------------------------

def banner(letter: Lettering) -> str:
    hand = Hand()
    W, H = 840, 320
    out = [f'<path d="{clean_path(rrect(3, 3, W - 6, H - 6, 16), True)}" fill="{PAPER}"/>',
           hand.stroke(rrect(3, 3, W - 6, H - 6, 16), 1.4, "#d8d2c4", amp=1.0, passes=1)]

    sources = [("Hacker News", ORANGE), ("Reddit", PINK), ("arXiv + HF papers", GREEN),
               ("GitHub", PURPLE), ("Eng blogs", BLUE)]
    flows = []
    for i, (label, color) in enumerate(sources):
        cy = 60 + i * 50
        pill = rrect(34, cy - 18, 164, 36, 12)
        out += [hand.solid(pill, color), hand.stroke(pill), letter.text(label, 116, cy + 5, 14.5)]
        # Each arrow lands at its own height on the funnel's mouth.
        end = (336, 124 + i * 18)
        ctrl = ((250, cy), (270, end[1]))
        curve = bezier((204, cy), *ctrl, end)
        out += [hand.stroke(curve, 1.6, amp=1.0),
                hand.arrowhead(end, math.atan2(end[1] - ctrl[1][1], end[0] - ctrl[1][0]), 9, 1.6)]
        flows.append((f"M204 {cy} C{ctrl[0][0]} {ctrl[0][1]} {ctrl[1][0]} {ctrl[1][1]} {end[0]} {end[1]}", color, i))

    # The engine is a funnel lying on its side: many things in, one thing out.
    funnel = polygon([(346, 92), (452, 138), (496, 138), (496, 182), (452, 182), (346, 228)])
    # A pipe on top lets off steam; drawn before the funnel so the funnel's outline closes it.
    pipe = [(465, 138), (465, 116), (485, 116), (485, 138)]
    out += [hand.solid(polygon(pipe), "#ffffff"), hand.stroke(polygon(pipe, closed=False), 1.9, amp=0.6)]
    for x, top, sway, delay in ((470, 90, 2.5, 0), (481, 96, -2.5, 1.1)):
        out.append(f'<g>{hand.stroke(squiggle(x, 110, top, sway), 1.8, MUTED, amp=0.3, passes=1)}'
                   f'<animateTransform attributeName="transform" type="translate" values="0 3;0 -7" dur="2.2s" '
                   f'begin="{delay}s" repeatCount="indefinite"/>'
                   f'<animate attributeName="opacity" values="0;1;0" dur="2.2s" begin="{delay}s" '
                   f'repeatCount="indefinite"/></g>')
    out += [hand.solid(funnel, "#fff9db"), hand.hachure(funnel, "#ffd43b"), hand.stroke(funnel, 2.2, amp=1.5)]
    # Two meshing gears inside, turning opposite ways (8 and 6 teeth, so periods of 12s and 9s).
    for cx, cy, r_out, r_in, teeth, phase, color, spin in ((386, 160, 21, 15, 8, 0.0, "#74c0fc", ("0", "360", 12)),
                                                          (422, 153, 14, 10, 6, 0.52, "#ffa8a8", ("360", "0", 9))):
        cog = gear(cx, cy, r_out, r_in, teeth, phase)
        out.append(f'<g>{hand.solid(cog, color)}{hand.stroke(cog, 1.7, amp=0.5, passes=1)}'
                   f'{hand.solid(circle(cx, cy, r_in * 0.3), INK)}'
                   f'<animateTransform attributeName="transform" type="rotate" from="{spin[0]} {cx} {cy}" '
                   f'to="{spin[1]} {cx} {cy}" dur="{spin[2]}s" repeatCount="indefinite"/></g>')
    out += [letter.text("no keys, no tokens, no browser", 420, 62, 13.5, MUTED),
            letter.text("content-engine", 420, 268, 24),
            letter.text("normalize, dedupe, summarize", 420, 292, 13.5, MUTED)]

    out_arrow = line((504, 160), (584, 160))
    out += [hand.stroke(out_arrow, 2.0, amp=1.0), hand.arrowhead((584, 160), 0, 11, 2.0)]

    # The brief: a sheet with a heading and three items.
    sheet = rrect(598, 76, 164, 168, 8)
    out += [hand.solid(sheet, "#ffffff"), hand.stroke(sheet, 2.0),
            hand.stroke(line((616, 101), (702, 101)), 4.5, amp=0.8, passes=1)]
    for row, color in enumerate((ORANGE, GREEN, BLUE)):
        y = 126 + row * 38
        out += [hand.stroke(line((616, y), (642, y)), 5, color.replace("ffd8a8", "ffa94d").replace("b2f2bb", "69db7c")
                            .replace("a5d8ff", "4dabf7"), amp=0.6, passes=1),
                hand.stroke(line((650, y), (744, y)), 2.2, "#b9b3a6", amp=0.8, passes=1),
                hand.stroke(line((616, y + 13), (722 - row * 14, y + 13)), 2.2, "#b9b3a6", amp=0.8, passes=1)]
    out += [letter.text("brief.md + items.json", 680, 268, 15.5),
            letter.text("old runs cleaned after 4 days", 680, 290, 13, MUTED)]

    # Motion: one dot per source travels its arrow into the funnel; one leaves for the brief.
    for path, color, i in flows:
        out.append(f'<circle r="4.5" fill="{color}" stroke="{INK}" stroke-width="1.3">'
                   f'<animateMotion dur="2.8s" begin="{n(i * 0.55)}s" repeatCount="indefinite" path="{path}"/></circle>')
    out.append(f'<circle r="5" fill="#ffd43b" stroke="{INK}" stroke-width="1.3">'
               f'<animateMotion dur="1.4s" repeatCount="indefinite" path="M504 160 L580 160"/></circle>')

    label = ("Hand-drawn sketch: Hacker News, Reddit, arXiv, GitHub and engineering blogs flow into "
             "a funnel with gears and a steaming pipe, labelled content-engine, which writes one brief.")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
            f'role="img" aria-label="{label}">\n' + "\n".join(out) + "\n</svg>\n")


# --- logo --------------------------------------------------------------------------------------

def logo() -> str:
    """Square icon: a funnel with two meshing gears inside and a smokestack letting off
    steam (the engine), and two lines of brief coming out below.
    Few shapes and heavy strokes, so it still reads at 32 px."""
    hand = Hand(SEED + 1)
    tile = rrect(30, 30, 452, 452, 104)
    funnel = polygon([(88, 188), (424, 188), (298, 318), (298, 374), (214, 374), (214, 318)])
    stack = [(326, 188), (326, 140), (372, 140), (372, 188)]
    cap = rrect(314, 122, 70, 22, 8)
    out = [hand.solid(tile, "#ffe066"), hand.stroke(tile, 13, amp=3.0),
           hand.solid(polygon(stack), "#ffffff"), hand.stroke(polygon(stack, closed=False), 12, amp=1.4, passes=1),
           hand.solid(cap, "#ffffff"), hand.stroke(cap, 11, amp=1.0, passes=1),
           hand.solid(funnel, "#ffffff"), hand.stroke(funnel, 14, amp=2.6)]
    # Steam leans left as it rises, into the corner the smokestack leaves empty.
    for x, top, sway, drift in ((338, 50, 9, -34), (366, 70, -8, -16)):
        out.append(hand.stroke(squiggle(x, 104, top, sway, drift=drift), 11, amp=0.8, passes=1))
    for cx, cy, r_out, r_in, teeth, phase, color in ((224, 248, 47, 34, 8, 0.0, "#4dabf7"),
                                                    (300, 232, 31, 22, 6, 0.52, "#ff8787")):
        cog = gear(cx, cy, r_out, r_in, teeth, phase)
        hub = circle(cx, cy, r_in * 0.36)
        out += [hand.solid(cog, color), hand.stroke(cog, 8.5, amp=1.0, passes=1),
                hand.solid(hub, "#ffffff"), hand.stroke(hub, 6, amp=0.5, passes=1)]
    out += [hand.stroke(line((166, 420), (346, 420)), 16, amp=2.0, passes=1),
            hand.stroke(line((202, 454), (310, 454)), 16, amp=2.0, passes=1)]
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512" '
            'role="img" aria-label="content-engine logo: a hand-drawn funnel with gears and a steaming pipe, '
            'turning feeds into a brief">\n'
            + "\n".join(out) + "\n</svg>\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--font", type=Path, required=True, help="path to Virgil.woff2 (or .ttf)")
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent)
    args = ap.parse_args()
    (args.out / "banner.svg").write_text(banner(Lettering(args.font)))
    (args.out / "logo.svg").write_text(logo())
    print(args.out / "banner.svg", args.out / "logo.svg", sep="\n")


if __name__ == "__main__":
    main()
