#!/usr/bin/env python3
"""Overview figures for the dataset as a whole.

    ./scripts/plot_overview.py --outdir assets

Draws the two Hardware Track overview plots of the challenge notebooks
(``HWTrack.ipynb``) straight from ``data/data.json``, so the pictures in the
README follow the data instead of being redrawn by hand:

``submissions_overview``
    Time-to-solution against core count, log-log, marker shape per mesh --
    the shape of the field, and how far down it reaches.
``submissions_overview_cpu``
    How many runs each CPU family contributed, stacked by generation.

Both are restricted to the Hardware Track, as in the notebook: the Software
Track measures optimisation work on fixed hardware, and mixing the two would
make neither picture mean anything.

Time-To-Solution is a normalised quantity, not a measurement (see
ohc_schema.py):

    Time-To-Solution [h] = 4000 x time per iteration / 3600

Output is self-contained SVG -- standard library only, like the rest of
scripts/, no plotting package to install.  ``--png`` additionally rasterises
each figure with rsvg-convert when that is available.
"""

import argparse
import collections
import math
import os
import sys
from xml.sax import saxutils

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ohc_schema as schema
from plot_submission import fmt_tick, log_ticks, nice_log_bounds, rasterise

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATASET = os.path.join(REPO, "data", "data.json")

HARDWARE_TRACK = "Hardware Track"

# Mesh -> (marker shape, label).  Shape carries the mesh, as in the notebook,
# so the three resolutions stay legible in one panel without colour.
MESH_MARKERS = [
    ("coarse", "circle", "coarse (65M)"),
    ("medium", "square", "medium (110M)"),
    ("fine", "triangle", "fine (236M)"),
]

INK_AXIS = "#3c4043"
INK_GRID = "#e8eaed"
INK_TEXT = "#202124"
INK_MUTED = "#5f6368"
SURFACE = "#ffffff"

# One hue per CPU generation, ordered so neighbouring generations of the same
# family stay distinguishable.  Colour-blind-safe qualitative set.
PALETTE = ["#4477aa", "#ee6677", "#228833", "#ccbb44",
           "#66ccee", "#aa3377", "#bbbbbb", "#ee8866", "#44bb99"]

FONT = ("-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, "
        "'Helvetica Neue', Arial, sans-serif")


def esc(text):
    return saxutils.escape(str(text))


def text(x, y, body, size=12, fill=INK_TEXT, anchor="start", weight="normal"):
    return ('<text x="%.1f" y="%.1f" font-family="%s" font-size="%d" '
            'fill="%s" text-anchor="%s" font-weight="%s">%s</text>'
            % (x, y, FONT, size, fill, anchor, weight, esc(body)))


def marker(shape, cx, cy, r, fill, opacity=0.85):
    """One data point, drawn as `shape`."""
    common = 'fill="%s" fill-opacity="%.2f" stroke="%s" stroke-width="0.8"' % (
        fill, opacity, INK_AXIS)
    if shape == "circle":
        return '<circle cx="%.2f" cy="%.2f" r="%.2f" %s/>' % (cx, cy, r, common)
    if shape == "square":
        s = r * 1.8
        return '<rect x="%.2f" y="%.2f" width="%.2f" height="%.2f" %s/>' % (
            cx - s / 2, cy - s / 2, s, s, common)
    # triangle
    h = r * 1.9
    pts = " ".join("%.2f,%.2f" % p for p in (
        (cx, cy - h * 0.62), (cx + h * 0.55, cy + h * 0.44),
        (cx - h * 0.55, cy + h * 0.44)))
    return '<polygon points="%s" %s/>' % (pts, common)


def document(width, height, body):
    return "\n".join([
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
        'viewBox="0 0 %d %d" role="img">' % (width, height, width, height),
        '<rect width="%d" height="%d" fill="%s"/>' % (width, height, SURFACE),
        body,
        '</svg>',
        ''])


# --------------------------------------------------------------------------- #
# Figure 1: time-to-solution against core count
# --------------------------------------------------------------------------- #

X_FIELD = "Number of CPU Cores"
Y_FIELD = "Time-To-Solution [h]"


def scatter_points(records):
    """(cores, time, mesh) for every Hardware Track record that has both."""
    points = []
    for record in records:
        if record.get("Track") != HARDWARE_TRACK:
            continue
        x = schema.coerce_number(record.get(X_FIELD))
        y = schema.coerce_number(record.get(Y_FIELD))
        if x and y and x > 0 and y > 0:
            points.append((x, y, record.get("Mesh")))
    return points


def render_scatter(points):
    """Log-log scatter of time-to-solution against core count."""
    pad = {"left": 78, "right": 156, "top": 52, "bottom": 62}
    plot_w, plot_h = 470, 400
    width = pad["left"] + plot_w + pad["right"]
    height = pad["top"] + plot_h + pad["bottom"]

    x_lo, x_hi = nice_log_bounds([p[0] for p in points])
    y_lo, y_hi = nice_log_bounds([p[1] for p in points])

    def sx(value):
        return pad["left"] + plot_w * (math.log10(value) - math.log10(x_lo)) \
            / (math.log10(x_hi) - math.log10(x_lo))

    def sy(value):
        return pad["top"] + plot_h * (1.0 - (math.log10(value) - math.log10(y_lo))
                                      / (math.log10(y_hi) - math.log10(y_lo)))

    out = []

    # Grid and ticks.
    for value in log_ticks(x_lo, x_hi):
        x = sx(value)
        out.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
                   'stroke-width="1"/>' % (x, pad["top"], x,
                                           pad["top"] + plot_h, INK_GRID))
        out.append(text(x, pad["top"] + plot_h + 18, fmt_tick(value), 11,
                        INK_MUTED, "middle"))
    for value in log_ticks(y_lo, y_hi):
        y = sy(value)
        out.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
                   'stroke-width="1"/>' % (pad["left"], y,
                                           pad["left"] + plot_w, y, INK_GRID))
        out.append(text(pad["left"] - 10, y + 4, fmt_tick(value), 11,
                        INK_MUTED, "end"))

    # Frame.
    out.append('<rect x="%.1f" y="%.1f" width="%d" height="%d" fill="none" '
               'stroke="%s" stroke-width="1.2"/>'
               % (pad["left"], pad["top"], plot_w, plot_h, INK_AXIS))

    # Points, drawn mesh by mesh so the shapes stack predictably.
    shapes = {mesh: shape for mesh, shape, _ in MESH_MARKERS}
    colours = {mesh: PALETTE[i] for i, (mesh, _, _) in enumerate(MESH_MARKERS)}
    for mesh, shape, _ in MESH_MARKERS:
        for cores, tts, point_mesh in points:
            if point_mesh == mesh:
                out.append(marker(shape, sx(cores), sy(tts), 4.2,
                                  colours[mesh]))
    # Anything with an unrecognised mesh still gets drawn, in grey.
    for cores, tts, point_mesh in points:
        if point_mesh not in shapes:
            out.append(marker("circle", sx(cores), sy(tts), 4.2, "#9aa0a6"))

    # Axis labels and title.
    out.append(text(pad["left"] + plot_w / 2.0, height - 18,
                    "Number of CPU cores", 13, INK_TEXT, "middle"))
    out.append('<g transform="translate(%.1f,%.1f) rotate(-90)">%s</g>'
               % (22, pad["top"] + plot_h / 2.0,
                  text(0, 0, "Time-to-solution [h]", 13, INK_TEXT, "middle")))
    out.append(text(pad["left"], 26, "OpenFOAM HPC Challenge -- Hardware Track",
                    15, INK_TEXT, "start", "600"))
    out.append(text(pad["left"], 42, "%d published runs" % len(points), 11,
                    INK_MUTED))

    # Legend.
    lx = pad["left"] + plot_w + 26
    ly = pad["top"] + 14
    out.append(text(lx, ly, "Mesh", 12, INK_TEXT, "start", "600"))
    counts = collections.Counter(p[2] for p in points)
    for i, (mesh, shape, label) in enumerate(MESH_MARKERS):
        y = ly + 22 + i * 22
        out.append(marker(shape, lx + 7, y - 4, 4.2, colours[mesh]))
        out.append(text(lx + 20, y, "%s -- %d" % (label, counts.get(mesh, 0)),
                        11, INK_MUTED))

    return document(width, height, "\n".join(out))


# --------------------------------------------------------------------------- #
# Figure 2: contributions per CPU family, stacked by generation
# --------------------------------------------------------------------------- #

def cpu_counts(records):
    """{family: {generation: count}} over the Hardware Track."""
    counts = collections.defaultdict(collections.Counter)
    for record in records:
        if record.get("Track") != HARDWARE_TRACK:
            continue
        family = record.get("CPU Family") or "unknown"
        # "Turin (5th-gen)" -> "Turin (5th)", as the notebook does, so the
        # legend does not run away with the width.
        generation = (record.get("CPU Generation") or "unknown")
        generation = generation.replace("-gen)", ")")
        counts[family][generation] += 1
    return counts


def render_cpu_histogram(records):
    """Stacked bar chart: runs per CPU family, split by generation."""
    counts = cpu_counts(records)
    families = sorted(counts, key=lambda f: -sum(counts[f].values()))

    # Generations ordered by total size, so the legend reads big-to-small and
    # the colours are assigned stably whatever the data does.
    totals = collections.Counter()
    for family in counts:
        totals.update(counts[family])
    generations = [g for g, _ in totals.most_common()]
    colour = {g: PALETTE[i % len(PALETTE)] for i, g in enumerate(generations)}

    pad = {"left": 62, "right": 188, "top": 52, "bottom": 58}
    plot_w, plot_h = 380, 340
    width = pad["left"] + plot_w + pad["right"]
    height = pad["top"] + plot_h + pad["bottom"]

    tallest = max((sum(counts[f].values()) for f in families), default=1)
    y_hi = max(5 * math.ceil(tallest / 5.0), 5)

    def sy(value):
        return pad["top"] + plot_h * (1.0 - value / float(y_hi))

    out = []

    # Horizontal grid in steps of 5, plus the count axis.
    step = 5 if y_hi <= 50 else 10
    for value in range(0, y_hi + 1, step):
        y = sy(value)
        out.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
                   'stroke-width="1"/>' % (pad["left"], y,
                                           pad["left"] + plot_w, y, INK_GRID))
        out.append(text(pad["left"] - 10, y + 4, str(value), 11, INK_MUTED,
                        "end"))

    out.append('<rect x="%.1f" y="%.1f" width="%d" height="%d" fill="none" '
               'stroke="%s" stroke-width="1.2"/>'
               % (pad["left"], pad["top"], plot_w, plot_h, INK_AXIS))

    # One stacked bar per family.
    slot = plot_w / float(max(len(families), 1))
    bar_w = min(slot * 0.55, 86)
    for i, family in enumerate(families):
        cx = pad["left"] + slot * (i + 0.5)
        bottom = 0
        for generation in generations:
            n = counts[family].get(generation, 0)
            if not n:
                continue
            y_top, y_bottom = sy(bottom + n), sy(bottom)
            out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" '
                       'fill="%s" stroke="%s" stroke-width="0.8"/>'
                       % (cx - bar_w / 2, y_top, bar_w, y_bottom - y_top,
                          colour[generation], INK_AXIS))
            bottom += n
        out.append(text(cx, pad["top"] + plot_h + 20, family, 12, INK_TEXT,
                        "middle"))
        out.append(text(cx, sy(bottom) - 7, str(bottom), 11, INK_MUTED,
                        "middle"))

    out.append('<g transform="translate(%.1f,%.1f) rotate(-90)">%s</g>'
               % (20, pad["top"] + plot_h / 2.0,
                  text(0, 0, "Number of runs", 13, INK_TEXT, "middle")))
    out.append(text(pad["left"], 26, "Contributed runs per CPU family", 15,
                    INK_TEXT, "start", "600"))
    out.append(text(pad["left"], 42, "Hardware Track, stacked by generation",
                    11, INK_MUTED))

    lx = pad["left"] + plot_w + 24
    ly = pad["top"] + 14
    out.append(text(lx, ly, "CPU generation", 12, INK_TEXT, "start", "600"))
    for i, generation in enumerate(generations):
        y = ly + 22 + i * 20
        out.append('<rect x="%.1f" y="%.1f" width="11" height="11" fill="%s" '
                   'stroke="%s" stroke-width="0.8"/>'
                   % (lx, y - 9, colour[generation], INK_AXIS))
        out.append(text(lx + 18, y, "%s -- %d" % (generation,
                                                  totals[generation]),
                        11, INK_MUTED))

    return document(width, height, "\n".join(out))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

FIGURES = ["submissions_overview", "submissions_overview_cpu"]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        epilog="Figures written: %s" % ", ".join(FIGURES),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", default=DATASET, metavar="FILE",
                        help="dataset to draw (default: %(default)s)")
    parser.add_argument("--outdir", default=os.path.join(REPO, "assets"),
                        metavar="DIR",
                        help="directory the figures are written to "
                             "(default: <repo>/assets)")
    parser.add_argument("--png", action="store_true",
                        help="also rasterise each figure to PNG, when "
                             "rsvg-convert is available")
    args = parser.parse_args(argv)

    records = schema.load_dataset(args.dataset)
    if not records:
        print("error: no records in %s" % args.dataset, file=sys.stderr)
        return 1

    points = scatter_points(records)
    if not points:
        print("error: no Hardware Track run has both %s and %s"
              % (X_FIELD, Y_FIELD), file=sys.stderr)
        return 1

    os.makedirs(args.outdir, exist_ok=True)
    figures = {
        "submissions_overview": render_scatter(points),
        "submissions_overview_cpu": render_cpu_histogram(records),
    }

    for name, svg in figures.items():
        path = os.path.join(args.outdir, name + ".svg")
        with open(path, "w") as handle:
            handle.write(svg)
        print("wrote %s" % os.path.relpath(path, REPO))
        if args.png:
            png = os.path.splitext(path)[0] + ".png"
            if rasterise(path, png):
                print("wrote %s" % os.path.relpath(png, REPO))

    hardware = sum(1 for r in records if r.get("Track") == HARDWARE_TRACK)
    print("%d record(s), %d on the Hardware Track, %d plotted"
          % (len(records), hardware, len(points)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
