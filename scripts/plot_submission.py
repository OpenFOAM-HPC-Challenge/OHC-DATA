#!/usr/bin/env python3
"""Preview plot: where a submission lands among the published results.

    ./scripts/plot_submission.py submissions/42_coarse.json --output preview.svg

Draws time-to-solution against energy-to-solution on log-log axes -- the
``TTS_ETS`` scatter of the challenge notebooks -- with everything already in
data/data.json as a grey cloud and the submitted runs in red on top, so a pull
request shows at a glance whether a new machine lands inside the field or
extends it.

Both axes are normalised quantities, not measurements (see ohc_schema.py):

    Time-To-Solution [h]     = 4000 x time per iteration / 3600
    Energy-To-Solution [kWh] = TDP x Time-To-Solution / 1000

The reference cloud is restricted to the meshes the submission uses, since a
236M-cell run is nowhere near a 65M-cell one and plotting both would only
compress the interesting part.

Output is a self-contained SVG -- standard library only, like the rest of
scripts/, no plotting package to install.  ``--png`` additionally rasterises it
with rsvg-convert when that is available, which is what the pull-request bot
embeds.
"""

import argparse
import math
import os
import subprocess
import sys
from xml.sax import saxutils

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ohc_schema as schema
import validate_submission as validator

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATASET = os.path.join(REPO, "data", "data.json")

# One subplot per mesh, drawn left-to-right.  Each panel is sized 3:1.5
# (height:width), and the three share log axes so a 65M-cell coarse run and a
# 236M-cell fine run stay directly comparable instead of being squashed
# together into one cloud.
MESHES = ["coarse", "medium", "fine"]
PANEL_W = 210
PANEL_H = 420  # 3:1.5 height-to-width
PANEL_GAP = 34
PAD = {"left": 86, "right": 210, "top": 76, "bottom": 64}
WIDTH = (PAD["left"] + len(MESHES) * PANEL_W
         + (len(MESHES) - 1) * PANEL_GAP + PAD["right"])
HEIGHT = PAD["top"] + PANEL_H + PAD["bottom"]

# One neutral ink for the field, one saturated hue for what is new.  Nothing
# else carries colour, so the eye goes straight to the submission.
INK_EXISTING = "#9aa0a6"
INK_NEW = "#d93025"
INK_AXIS = "#3c4043"
INK_GRID = "#e8eaed"
INK_PARETO = "#5f6368"
SURFACE = "#ffffff"

X_FIELD = "Energy-To-Solution [kWh]"
Y_FIELD = "Time-To-Solution [h]"


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #

def snap(value, up):
    """Round `value` out to the nearest 1-2-5 step of its decade."""
    exponent = math.floor(math.log10(value))
    mantissa = value / 10.0 ** exponent
    steps = [1.0, 2.0, 5.0, 10.0]
    if up:
        step = next(s for s in steps if s >= mantissa * 0.99999)
    else:
        step = [s for s in steps if s <= mantissa * 1.00001][-1]
    return step * 10.0 ** exponent


def nice_log_bounds(values):
    """Bounds containing `values`, snapped out to the next 1-2-5 step.

    Snapping to whole decades instead would be simpler, but with a field that
    spans .08 to 60 it wastes most of the axis on empty space -- and the point
    of the picture is where one submission sits inside the cloud.
    """
    positive = [v for v in values if v and v > 0]
    if not positive:
        return 1.0, 10.0
    lo, hi = snap(min(positive), False), snap(max(positive), True)
    return (lo, hi) if hi > lo else (lo, lo * 10.0)


def log_ticks(lo, hi):
    """1-2-5 ticks across the decades, thinned when the range is wide."""
    decades = round(math.log10(hi / lo))
    steps = [1, 2, 5] if decades <= 3 else ([1, 3] if decades <= 6 else [1])
    ticks = []
    decade = lo
    while decade <= hi * 1.0000001:
        for step in steps:
            value = decade * step
            if lo * 0.9999 <= value <= hi * 1.0001:
                ticks.append(value)
        decade *= 10.0
    return ticks


def fmt_tick(value):
    if value >= 1000:
        return "%g" % value
    if value >= 1:
        return ("%.0f" if value == int(value) else "%.1f") % value
    return ("%g" % value).replace("0.", ".")


def pareto_front(points):
    """Non-dominated set for (min energy, min time), ordered by energy."""
    front, best_time = [], float("inf")
    for x, y in sorted(points):
        if y < best_time:
            front.append((x, y))
            best_time = y
    return front


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #

def points_of(records, meshes=None):
    """(energy, time, mesh) for every record that has both coordinates."""
    points = []
    for record in records:
        mesh = record.get("Mesh")
        if meshes and mesh not in meshes:
            continue
        x = schema.coerce_number(record.get(X_FIELD))
        y = schema.coerce_number(record.get(Y_FIELD))
        if x and y and x > 0 and y > 0:
            points.append((x, y, mesh))
    return points


# --------------------------------------------------------------------------- #
# SVG
# --------------------------------------------------------------------------- #

def render(existing, new, title, subtitle, join=True):
    """Return the finished SVG document as a string.

    One subplot per mesh (coarse/medium/fine), all sharing the same log axes so
    a point's position means the same thing in every panel.
    """
    parts = []
    add = parts.append

    y0 = PAD["top"]
    w, h = PANEL_W, PANEL_H

    everything = existing + new
    x_lo, x_hi = nice_log_bounds([p[0] for p in everything])
    y_lo, y_hi = nice_log_bounds([p[1] for p in everything])

    def sx(v, x0):
        return x0 + w * (math.log(v) - math.log(x_lo)) / (math.log(x_hi) - math.log(x_lo))

    def sy(v):
        return y0 + h * (1 - (math.log(v) - math.log(y_lo)) / (math.log(y_hi) - math.log(y_lo)))

    def panel_x0(index):
        return PAD["left"] + index * (w + PANEL_GAP)

    add('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
        'viewBox="0 0 %d %d" font-family="Helvetica,Arial,sans-serif">'
        % (WIDTH, HEIGHT, WIDTH, HEIGHT))
    add('<rect width="%d" height="%d" fill="%s"/>' % (WIDTH, HEIGHT, SURFACE))

    add('<text x="%d" y="26" font-size="17" font-weight="600" fill="%s">%s</text>'
        % (PAD["left"] - 4, INK_AXIS, saxutils.escape(title)))
    add('<text x="%d" y="46" font-size="12.5" fill="%s">%s</text>'
        % (PAD["left"] - 4, INK_PARETO, saxutils.escape(subtitle)))

    # Split both clouds by mesh once, so each panel only draws its own runs.
    existing_by_mesh = {m: [] for m in MESHES}
    new_by_mesh = {m: [] for m in MESHES}
    for x, y, mesh in existing:
        existing_by_mesh.setdefault(mesh, []).append((x, y))
    for x, y, mesh in new:
        new_by_mesh.setdefault(mesh, []).append((x, y))

    x_ticks = log_ticks(x_lo, x_hi)
    y_ticks = log_ticks(y_lo, y_hi)

    any_front = False
    for index, mesh in enumerate(MESHES):
        x0 = panel_x0(index)
        panel_existing = existing_by_mesh.get(mesh, [])
        panel_new = new_by_mesh.get(mesh, [])

        # --- grid and axes (y labels only on the leftmost panel) ---------- #
        for value in x_ticks:
            x = sx(value, x0)
            add('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s"/>'
                % (x, y0, x, y0 + h, INK_GRID))
            add('<text x="%.1f" y="%.1f" font-size="11.5" text-anchor="middle" '
                'fill="%s">%s</text>' % (x, y0 + h + 18, INK_AXIS, fmt_tick(value)))
        for value in y_ticks:
            y = sy(value)
            add('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s"/>'
                % (x0, y, x0 + w, y, INK_GRID))
            if index == 0:
                add('<text x="%.1f" y="%.1f" font-size="11.5" text-anchor="end" '
                    'fill="%s">%s</text>' % (x0 - 8, y + 4, INK_AXIS, fmt_tick(value)))
        add('<rect x="%d" y="%d" width="%d" height="%d" fill="none" '
            'stroke="%s"/>' % (x0, y0, w, h, INK_AXIS))

        # --- panel heading ------------------------------------------------- #
        add('<text x="%.1f" y="%.1f" font-size="13.5" font-weight="600" '
            'text-anchor="middle" fill="%s">%s</text>'
            % (x0 + w / 2.0, y0 - 12, INK_AXIS, mesh))

        # --- the field it is being placed against ------------------------- #
        for x, y in panel_existing:
            add('<circle cx="%.1f" cy="%.1f" r="3.2" fill="none" stroke="%s" '
                'stroke-width="1.3" opacity="0.75"/>'
                % (sx(x, x0), sy(y), INK_EXISTING))

        front = pareto_front(panel_existing)
        any_front = any_front or len(front) > 1
        if len(front) > 1:
            path = " ".join("%s%.1f,%.1f" % ("M" if i == 0 else "L", sx(x, x0), sy(y))
                            for i, (x, y) in enumerate(front))
            add('<path d="%s" fill="none" stroke="%s" stroke-width="1.4" '
                'stroke-dasharray="5 4" opacity="0.85"/>' % (path, INK_PARETO))

        # --- the submission ----------------------------------------------- #
        for x, y in panel_new:
            add('<circle cx="%.1f" cy="%.1f" r="5.4" fill="%s" stroke="%s" '
                'stroke-width="1.6"/>' % (sx(x, x0), sy(y), SURFACE, INK_NEW))
            add('<circle cx="%.1f" cy="%.1f" r="2.4" fill="%s"/>'
                % (sx(x, x0), sy(y), INK_NEW))
        if join and len(panel_new) > 1:
            path = " ".join("%s%.1f,%.1f" % ("M" if i == 0 else "L", sx(x, x0), sy(y))
                            for i, (x, y) in enumerate(sorted(panel_new)))
            add('<path d="%s" fill="none" stroke="%s" stroke-width="1.5" '
                'opacity="0.55"/>' % (path, INK_NEW))

        if not panel_existing and not panel_new:
            add('<text x="%.1f" y="%.1f" font-size="12" text-anchor="middle" '
                'fill="%s">%s</text>'
                % (x0 + w / 2.0, y0 + h / 2.0, INK_EXISTING, "no runs"))

    # --- shared axis labels ------------------------------------------------ #
    row_mid = (panel_x0(0) + panel_x0(len(MESHES) - 1) + w) / 2.0
    add('<text x="%.1f" y="%.1f" font-size="12.5" text-anchor="middle" '
        'fill="%s">%s</text>'
        % (row_mid, HEIGHT - 16, INK_AXIS, "Energy-To-Solution [kWh]"))
    add('<text x="18" y="%.1f" font-size="12.5" text-anchor="middle" '
        'fill="%s" transform="rotate(-90 18 %.1f)">%s</text>'
        % (y0 + h / 2.0, INK_AXIS, y0 + h / 2.0, "Time-To-Solution [h]"))

    # --- legend ------------------------------------------------------------ #
    lx, ly = panel_x0(len(MESHES) - 1) + w + 26, y0 + 8
    rows = [(INK_NEW, True, "this submission (%d)" % len(new)),
            (INK_EXISTING, False, "already published (%d)" % len(existing))]
    for index, (colour, filled, text) in enumerate(rows):
        y = ly + index * 22
        add('<circle cx="%.1f" cy="%.1f" r="%.1f" fill="%s" stroke="%s" '
            'stroke-width="1.5"/>'
            % (lx + 6, y - 4, 5.0 if filled else 3.2,
               colour if filled else "none", colour))
        add('<text x="%.1f" y="%.1f" font-size="12" fill="%s">%s</text>'
            % (lx + 20, y, INK_AXIS, saxutils.escape(text)))
    if any_front:
        y = ly + len(rows) * 22
        add('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
            'stroke-width="1.4" stroke-dasharray="5 4"/>'
            % (lx, y - 4, lx + 13, y - 4, INK_PARETO))
        add('<text x="%.1f" y="%.1f" font-size="12" fill="%s">%s</text>'
            % (lx + 20, y, INK_AXIS, "Pareto front"))
        add('<text x="%.1f" y="%.1f" font-size="11" fill="%s">%s</text>'
            % (lx, y + 20, INK_PARETO, "lower left is better"))

    add("</svg>")
    return "\n".join(parts) + "\n"


def rasterise(svg_path, png_path, scale=2):
    """Rasterise with rsvg-convert; returns False when it is not installed."""
    try:
        subprocess.check_call(["rsvg-convert", "-z", str(scale),
                               "-o", png_path, svg_path])
        return True
    except (OSError, subprocess.CalledProcessError) as exc:
        print("note: no PNG written (%s)" % exc, file=sys.stderr)
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+", metavar="FILE",
                        help="submission files to highlight")
    parser.add_argument("--dataset", default=DATASET, metavar="FILE",
                        help="published data drawn in grey (default: %(default)s)")
    parser.add_argument("--output", default="preview.svg", metavar="FILE",
                        help="SVG to write (default: %(default)s)")
    parser.add_argument("--png", metavar="FILE", nargs="?", const="",
                        help="also rasterise to PNG (default: the SVG path "
                             "with a .png suffix)")
    parser.add_argument("--all-meshes", action="store_true", dest="all_meshes",
                        help="accepted for backwards compatibility; the plot is "
                             "now always faceted into coarse/medium/fine panels")
    args = parser.parse_args(argv)

    report = validator.validate_files(args.paths, args.dataset)
    new_records = [r for f in report["files"] for r in f["accepted"]]
    if not new_records:
        print("error: no valid records to plot", file=sys.stderr)
        return 1

    # Each panel draws its own mesh, so the whole published field is loaded and
    # split by mesh inside render() rather than pre-filtered here.
    sub_meshes = {r.get("Mesh") for r in new_records}
    new = points_of(new_records)
    existing = points_of(schema.load_dataset(args.dataset))
    if not existing:
        print("note: nothing published yet", file=sys.stderr)

    # One contributor's scaling sweep is a curve worth joining up; a bulk
    # submission of many machines is a cloud, and joining it draws spaghetti.
    # Meshes no longer gate this -- each mesh lands in its own panel, so a sweep
    # spanning coarse/medium/fine is joined within each panel independently.
    def uniform(field):
        return len({str(r.get(field)) for r in new_records}) == 1

    contributors = sorted({str(r.get("Contributor ID")) for r in new_records})
    join = (len(new) <= 12 and uniform("Contributor ID") and uniform("Track"))

    if len(contributors) <= 3:
        who = "submission %s" % ", ".join(contributors)
    else:
        who = "%d runs from %d contributors" % (len(new), len(contributors))
    subtitle = "%s mesh; %d published run%s for comparison" % (
        ", ".join(sorted(m for m in sub_meshes if m)) or "no",
        len(existing), "" if len(existing) == 1 else "s")
    title = "OpenFOAM HPC Challenge -- %s" % who

    with open(args.output, "w") as handle:
        handle.write(render(existing, new, title, subtitle, join))
    print("wrote %s (%d new, %d published)"
          % (args.output, len(new), len(existing)))

    if args.png is not None:
        png = args.png or os.path.splitext(args.output)[0] + ".png"
        if rasterise(args.output, png):
            print("wrote %s" % png)
    return 0


if __name__ == "__main__":
    sys.exit(main())
