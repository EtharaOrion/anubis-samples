#!/usr/bin/env python3
"""Regenerate the chart SVGs in images/ from the shipped trajectory bytes.

Everything this script draws is read out of the task bundles themselves:
task.toml for the declared tier and category, trajectories/<model>/pass_summary.json
for the headline rate, the per-run report.json files for the two scoring channels,
and agent/trajectory.json for cost and step counts. Nothing is hard-coded, and the
loader refuses to aggregate a task that is missing any part of the 2 x 8 grid.

It emits SVG only. It does NOT rasterize PNGs: the matching .png fallbacks are
separate, browser-rendered exports of these exact SVGs, not output of this script.

The script also prints a plain-text summary of every number it computed. Run it and
diff the printed figures against README.md - they are the same numbers, computed the
same way, from the same files.

Usage:  python3 scripts/generate_charts.py
"""

import glob
import json
import math
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGES_DIR = os.path.join(REPO_ROOT, "images")

MODELS = ["claude-opus-5", "glm-5.3"]
MODEL_LABEL = {"claude-opus-5": "opus-5", "glm-5.3": "glm-5.3"}

EXPECTED_RUNS = 8
EXPECTED_TASKS = 3

# The three score fields ship rounded to two decimals, so a recomputed value can
# legitimately sit half a hundredth away from the stored one. Anything beyond that
# is a real inconsistency and should stop the build.
RECOMPUTE_TOLERANCE = 0.01

# Tiers are cut on claude-opus-5's mean combined score. Ordered hardest-last so the
# charts read left to right as difficulty increases.
TIER_ORDER = ["medium", "hard", "expert"]

CATEGORY_ORDER = ["Debugging", "Games", "System Administration"]
CATEGORY_LABEL = {
    "Debugging": "Debugging",
    "Games": "Games",
    "System Administration": "Sys. Admin.",
}
CATEGORY_COLORS = {
    "Debugging": ("#0e8a78", "#35d0ba"),
    "Games": ("#b9781f", "#e2ac52"),
    "System Administration": ("#6a5acd", "#9d8df1"),
}

FONT_SERIF = "Georgia, 'Iowan Old Style', 'Times New Roman', ui-serif, serif"
FONT_MONO = "'JetBrains Mono','SF Mono','Berkeley Mono',ui-monospace,'Courier New',monospace"


def theme(dark):
    if dark:
        return {
            "bg0": "#050c0c", "bg1": "#0a1e1c", "bg2": "#071313",
            "text": "#eef2ea", "muted": "#9fc4bc",
            "grid": "rgba(190,225,218,0.12)", "axis": "rgba(190,225,218,0.38)",
            "teal": "#35d0ba", "gold": "#e2ac52",
            "marker_ring": "#0f2723", "hatch_line": "rgba(5,12,12,0.55)",
        }
    return {
        "bg0": "#faf8f4", "bg1": "#f3efe6", "bg2": "#faf8f4",
        "text": "#0a1414", "muted": "#4a6560",
        "grid": "rgba(10,30,28,0.10)", "axis": "rgba(10,30,28,0.35)",
        "teal": "#0e8a78", "gold": "#b9781f",
        "marker_ring": "#ffffff", "hatch_line": "rgba(255,255,255,0.55)",
    }


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def parse_toml_flat(path):
    """Minimal reader for the handful of scalars we need.

    tomllib only exists from Python 3.11 and this script is meant to run on a bare
    system interpreter, so the few keys we care about are pulled out directly.
    """
    out = {}
    section = None
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if val.startswith('"') and val.endswith('"') and len(val) >= 2:
                out[f"{section}.{key}" if section else key] = val[1:-1]
    return out


def discover_task_uuids():
    uuids = []
    for name in sorted(os.listdir(REPO_ROOT)):
        path = os.path.join(REPO_ROOT, name)
        if not os.path.isdir(path) or name in ("images", "scripts", ".git"):
            continue
        if not os.path.isfile(os.path.join(path, "task.toml")):
            continue
        # A task only counts once both cohorts are present, so a partially copied
        # bundle can never quietly shrink an aggregate.
        if all(os.path.isdir(os.path.join(path, "trajectories", m)) for m in MODELS):
            uuids.append(name)
    return uuids


def load_task_meta(uuid):
    meta = parse_toml_flat(os.path.join(REPO_ROOT, uuid, "task.toml"))
    name = meta.get("task.name", "")
    assert name.endswith(uuid), f"{uuid}: task.name {name!r} does not carry its uuid"
    tier = meta.get("metadata.difficulty")
    category = meta.get("metadata.category")
    assert tier in TIER_ORDER, f"{uuid}: unexpected difficulty {tier!r}"
    assert category in CATEGORY_ORDER, f"{uuid}: unexpected category {category!r}"
    return {"tier": tier, "category": category}


def load_model_runs(uuid, model):
    base = os.path.join(REPO_ROOT, uuid, "trajectories", model)
    reports = sorted(glob.glob(os.path.join(base, "run_*", "report.json")))
    assert len(reports) == EXPECTED_RUNS, \
        f"{uuid}/{model}: {len(reports)} runs, expected {EXPECTED_RUNS}"

    ids = sorted(int(os.path.basename(os.path.dirname(p)).split("_")[1]) for p in reports)
    assert ids == list(range(1, EXPECTED_RUNS + 1)), f"{uuid}/{model}: run ids {ids}"

    combined, pytest_ch, rubric_ch, judges = [], [], [], set()
    for path in reports:
        with open(path, encoding="utf-8") as fh:
            r = json.load(fh)
        t = r["test_weights_percentage"]
        b = r["rubric_weights_percentage"]
        f = r["final_reward"]
        # The headline number is the mean of the two channels. Check it against the
        # stored value rather than trusting either in isolation.
        assert abs((t + b) / 2.0 - f) <= RECOMPUTE_TOLERANCE, \
            f"{path}: ({t} + {b}) / 2 != {f}"
        combined.append(f)
        pytest_ch.append(t)
        rubric_ch.append(b)
        judges.add(r["judge_model"])

    assert len(judges) == 1, f"{uuid}/{model}: multiple judges {sorted(judges)}"

    summary_path = os.path.join(base, "pass_summary.json")
    with open(summary_path, encoding="utf-8") as fh:
        summary = json.load(fh)
    assert summary["runs"] == EXPECTED_RUNS, f"{summary_path}: runs != {EXPECTED_RUNS}"
    stored = summary["average_combined_score"]
    assert abs(sum(combined) / len(combined) - stored) <= RECOMPUTE_TOLERANCE, \
        f"{summary_path}: stored {stored} != recomputed {sum(combined) / len(combined)}"

    cost, steps = 0.0, 0
    for path in sorted(glob.glob(os.path.join(base, "run_*", "agent", "trajectory.json"))):
        with open(path, encoding="utf-8") as fh:
            m = json.load(fh)["final_metrics"]
        cost += m["total_cost_usd"]
        steps += m["total_steps"]

    return {
        # The stored summary value is authoritative; the recompute above only guards it.
        "pass_rate": stored,
        "pytest": sum(pytest_ch) / len(pytest_ch),
        "rubric": sum(rubric_ch) / len(rubric_ch),
        "cost_per_run": cost / EXPECTED_RUNS,
        "steps_per_run": steps / EXPECTED_RUNS,
        "judge": judges.pop(),
    }


def build_dataset():
    uuids = discover_task_uuids()
    assert len(uuids) == EXPECTED_TASKS, f"found {len(uuids)} tasks, expected {EXPECTED_TASKS}"

    tasks = []
    for uuid in uuids:
        entry = {"uuid": uuid}
        entry.update(load_task_meta(uuid))
        for model in MODELS:
            entry[model] = load_model_runs(uuid, model)
        tasks.append(entry)

    tiers = {t["tier"] for t in tasks}
    assert tiers == set(TIER_ORDER), f"tiers {sorted(tiers)} != {TIER_ORDER}"
    cats = {t["category"] for t in tasks}
    assert cats == set(CATEGORY_ORDER), f"categories {sorted(cats)} != {CATEGORY_ORDER}"

    tasks.sort(key=lambda t: TIER_ORDER.index(t["tier"]))
    return tasks


def svg_header(width, height, th, aria):
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" aria-label="{esc(aria)}">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="{th['bg0']}"/>
      <stop offset="60%" stop-color="{th['bg1']}"/>
      <stop offset="100%" stop-color="{th['bg2']}"/>
    </linearGradient>
    <pattern id="hatch" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
      <rect width="8" height="8" fill="{th['gold']}"/>
      <line x1="0" y1="0" x2="0" y2="8" stroke="{th['hatch_line']}" stroke-width="3"/>
    </pattern>
    <filter id="glow" x="-50%" y="-50%" width="200%" height="200%">
      <feGaussianBlur stdDeviation="6"/>
    </filter>
  </defs>
  <rect width="{width}" height="{height}" fill="url(#bg)"/>
"""


def diamond_points(cx, cy, r):
    return f"{cx},{cy - r} {cx + r},{cy} {cx},{cy + r} {cx - r},{cy}"


def make_line_chart(path, dark, title, subtitle, categories, series_a, series_b,
                    counts, y_max, y_step, axis_fmt, value_fmt, footnote):
    th = theme(dark)
    W, H = 1600, 900
    left, right, top, bottom = 110, 1540, 150, 770

    for v in list(series_a) + list(series_b):
        assert 0 <= v <= y_max, f"{path}: {v} outside axis range 0..{y_max}"

    n = len(categories)
    xs = [left + (right - left) * (i + 0.5) / n for i in range(n)]

    def y_of(v):
        return bottom - (v / y_max) * (bottom - top)

    out = [svg_header(W, H, th, f"{title}. {subtitle}")]
    a = out.append

    a(f'  <text x="{left}" y="72" font-family="{FONT_SERIF}" font-size="40" font-weight="bold" fill="{th["text"]}">{esc(title)}</text>')
    a(f'  <text x="{left}" y="108" font-family="{FONT_MONO}" font-size="19" fill="{th["muted"]}">{esc(subtitle)}</text>')

    lx, ly = 1180, 47
    a(f'  <line x1="{lx}" y1="{ly}" x2="{lx + 46}" y2="{ly}" stroke="{th["teal"]}" stroke-width="4"/>')
    a(f'  <circle cx="{lx + 23}" cy="{ly}" r="8" fill="{th["teal"]}" stroke="{th["marker_ring"]}" stroke-width="3"/>')
    a(f'  <text x="{lx + 58}" y="{ly + 7}" font-family="{FONT_MONO}" font-size="20" fill="{th["text"]}">opus-5</text>')
    a(f'  <line x1="{lx + 158}" y1="{ly}" x2="{lx + 204}" y2="{ly}" stroke="{th["gold"]}" stroke-width="3.5" stroke-dasharray="9 7"/>')
    a(f'  <polygon points="{diamond_points(lx + 181, ly, 9)}" fill="url(#hatch)" stroke="{th["marker_ring"]}" stroke-width="2.5"/>')
    a(f'  <text x="{lx + 216}" y="{ly + 7}" font-family="{FONT_MONO}" font-size="20" fill="{th["text"]}">glm-5.3</text>')

    steps = int(round(y_max / y_step))
    for i in range(steps + 1):
        v = i * y_step
        y = y_of(v)
        a(f'  <line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" stroke="{th["grid"]}" stroke-width="1"/>')
        a(f'  <text x="{left - 16}" y="{y + 7:.1f}" text-anchor="end" font-family="{FONT_MONO}" font-size="18" fill="{th["muted"]}">{esc(axis_fmt(v))}</text>')
    a(f'  <line x1="{left}" y1="{y_of(0):.1f}" x2="{right}" y2="{y_of(0):.1f}" stroke="{th["axis"]}" stroke-width="2"/>')

    for x in xs:
        a(f'  <line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{bottom}" stroke="{th["grid"]}" stroke-width="1" stroke-dasharray="4 8"/>')

    pts_a = " ".join(f"{x:.1f},{y_of(v):.1f}" for x, v in zip(xs, series_a))
    pts_b = " ".join(f"{x:.1f},{y_of(v):.1f}" for x, v in zip(xs, series_b))
    a(f'  <polyline points="{pts_b}" fill="none" stroke="{th["gold"]}" stroke-width="3.5" stroke-dasharray="9 7"/>')
    a(f'  <polyline points="{pts_a}" fill="none" stroke="{th["teal"]}" stroke-width="4"/>')

    for i, x in enumerate(xs):
        ya, yb = y_of(series_a[i]), y_of(series_b[i])

        a(f'  <circle cx="{x:.1f}" cy="{ya:.1f}" r="9" fill="{th["teal"]}" opacity="0.28" filter="url(#glow)"/>')
        a(f'  <polygon points="{diamond_points(x, yb, 9)}" fill="{th["gold"]}" opacity="0.28" filter="url(#glow)"/>')
        a(f'  <polygon points="{diamond_points(x, yb, 9)}" fill="url(#hatch)" stroke="{th["marker_ring"]}" stroke-width="2.5"/>')
        a(f'  <circle cx="{x:.1f}" cy="{ya:.1f}" r="8" fill="{th["teal"]}" stroke="{th["marker_ring"]}" stroke-width="3"/>')

        # Labels dodge each other when the two markers nearly coincide: both go above
        # the higher one, pushed toward whichever side has no line running through it.
        open_side = 1 if i == 0 else (-1 if i == n - 1 else 0)
        if abs(ya - yb) < 30:
            hi = min(ya, yb)
            dx = 26 * (open_side if open_side else 1)
            anchor = "start" if dx > 0 else "end"
            a(f'  <text x="{x + dx:.1f}" y="{hi - 40:.1f}" text-anchor="{anchor}" font-family="{FONT_MONO}" font-size="21" font-weight="bold" fill="{th["teal"]}">{esc(value_fmt(series_a[i]))}</text>')
            a(f'  <text x="{x + dx:.1f}" y="{hi - 16:.1f}" text-anchor="{anchor}" font-family="{FONT_MONO}" font-size="21" font-weight="bold" fill="{th["gold"]}">{esc(value_fmt(series_b[i]))}</text>')
        else:
            for y, v, col in ((ya, series_a[i], th["teal"]), (yb, series_b[i], th["gold"])):
                above = y == min(ya, yb)
                dy = -22 if above else 34
                if not above and y + dy > bottom:
                    dy = -22
                a(f'  <text x="{x:.1f}" y="{y + dy:.1f}" text-anchor="middle" font-family="{FONT_MONO}" font-size="21" font-weight="bold" fill="{col}">{esc(value_fmt(v))}</text>')

        a(f'  <text x="{x:.1f}" y="{bottom + 40}" text-anchor="middle" font-family="{FONT_SERIF}" font-size="26" fill="{th["text"]}">{esc(categories[i])}</text>')
        a(f'  <text x="{x:.1f}" y="{bottom + 68}" text-anchor="middle" font-family="{FONT_MONO}" font-size="18" fill="{th["muted"]}">n={counts[i]}</text>')

    a(f'  <text x="{left}" y="876" font-family="{FONT_MONO}" font-size="16" fill="{th["muted"]}">{esc(footnote)}</text>')
    a("</svg>")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")


def make_donut(path, dark, title, subtitle, segments, footnote):
    """Category coverage. segments is a list of (label, count, light, dark) tuples."""
    th = theme(dark)
    W, H = 1600, 1200
    cx, cy, R, r = 672, 672, 330, 190
    total = sum(s[1] for s in segments)
    assert total > 0, f"{path}: empty donut"

    out = [svg_header(W, H, th, f"{title}. {subtitle}")]
    a = out.append
    a(f'  <text x="110" y="96" font-family="{FONT_SERIF}" font-size="44" font-weight="bold" fill="{th["text"]}">{esc(title)}</text>')
    a(f'  <text x="110" y="136" font-family="{FONT_MONO}" font-size="20" fill="{th["muted"]}">{esc(subtitle)}</text>')

    angle = -90.0
    for label, count, col_l, col_d in segments:
        col = col_d if dark else col_l
        sweep = 360.0 * count / total
        a0, a1 = math.radians(angle), math.radians(angle + sweep)
        large = 1 if sweep > 180 else 0

        x0, y0 = cx + R * math.cos(a0), cy + R * math.sin(a0)
        x1, y1 = cx + R * math.cos(a1), cy + R * math.sin(a1)
        xi0, yi0 = cx + r * math.cos(a1), cy + r * math.sin(a1)
        xi1, yi1 = cx + r * math.cos(a0), cy + r * math.sin(a0)
        a(f'  <path d="M {x0:.2f} {y0:.2f} A {R} {R} 0 {large} 1 {x1:.2f} {y1:.2f} '
          f'L {xi0:.2f} {yi0:.2f} A {r} {r} 0 {large} 0 {xi1:.2f} {yi1:.2f} Z" '
          f'fill="{col}" stroke="{th["bg1"]}" stroke-width="3"/>')

        mid = math.radians(angle + sweep / 2.0)
        lx0, ly0 = cx + (R + 8) * math.cos(mid), cy + (R + 8) * math.sin(mid)
        lx1, ly1 = cx + (R + 55) * math.cos(mid), cy + (R + 55) * math.sin(mid)
        a(f'  <line x1="{lx0:.2f}" y1="{ly0:.2f}" x2="{lx1:.2f}" y2="{ly1:.2f}" stroke="{th["axis"]}" stroke-width="2"/>')
        a(f'  <circle cx="{lx1:.2f}" cy="{ly1:.2f}" r="5" fill="{col}"/>')

        rightish = math.cos(mid) >= 0
        tx = cx + (R + 90) * math.cos(mid)
        ty = cy + (R + 90) * math.sin(mid)
        anchor = "start" if rightish else "end"
        sw = tx if rightish else tx - 18
        a(f'  <rect x="{sw:.2f}" y="{ty - 34:.2f}" width="18" height="18" rx="4" fill="{col}"/>')
        lab_x = tx + 26 if rightish else tx - 26
        a(f'  <text x="{lab_x:.2f}" y="{ty - 18:.2f}" text-anchor="{anchor}" font-family="{FONT_SERIF}" font-size="30" font-weight="bold" fill="{th["text"]}">{esc(label)}</text>')
        pct = 100.0 * count / total
        noun = "task" if count == 1 else "tasks"
        a(f'  <text x="{tx:.2f}" y="{ty + 14:.2f}" text-anchor="{anchor}" font-family="{FONT_MONO}" font-size="20" fill="{th["muted"]}">{count} {noun} \u00b7 {pct:.0f}%</text>')

        angle += sweep

    a(f'  <circle cx="{cx}" cy="{cy}" r="184" fill="{th["bg1"]}"/>')
    a(f'  <text x="{cx}" y="{cy + 18}" text-anchor="middle" font-family="{FONT_SERIF}" font-size="86" font-weight="bold" fill="{th["text"]}">{total}</text>')
    a(f'  <text x="{cx}" y="{cy + 58}" text-anchor="middle" font-family="{FONT_MONO}" font-size="22" fill="{th["muted"]}" letter-spacing="4">TASKS</text>')
    a(f'  <text x="{cx}" y="{cy + 92}" text-anchor="middle" font-family="{FONT_MONO}" font-size="18" fill="{th["muted"]}">{len(segments)} categories</text>')
    a(f'  <text x="110" y="1160" font-family="{FONT_MONO}" font-size="16" fill="{th["muted"]}">{esc(footnote)}</text>')
    a("</svg>")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")


def print_summary(tasks):
    print("=" * 78)
    print("ANUBIS sample - figures recomputed from the shipped bytes")
    print("=" * 78)
    print()

    judges = {t[m]["judge"] for t in tasks for m in MODELS}
    print(f"tasks {len(tasks)}   models {len(MODELS)}   runs/model {EXPECTED_RUNS}   "
          f"rollouts {len(tasks) * len(MODELS) * EXPECTED_RUNS}   judge {sorted(judges)[0]}")
    print()

    print("per task")
    print(f"  {'uuid':10} {'tier':8} {'category':22} {'opus-5':>8} {'glm-5.3':>8}")
    for t in tasks:
        print(f"  {t['uuid'][:8]:10} {t['tier']:8} {t['category']:22} "
              f"{t['claude-opus-5']['pass_rate']:7.2f}% {t['glm-5.3']['pass_rate']:7.2f}%")
    print()

    print("channels (mean over 8 runs)")
    for t in tasks:
        for m in MODELS:
            d = t[m]
            print(f"  {t['uuid'][:8]} {MODEL_LABEL[m]:8} pytest {d['pytest']:6.2f}%   "
                  f"rubric {d['rubric']:6.2f}%   combined {d['pass_rate']:6.2f}%")
    print()

    print("effort (mean per run)")
    for t in tasks:
        for m in MODELS:
            d = t[m]
            print(f"  {t['uuid'][:8]} {MODEL_LABEL[m]:8} ${d['cost_per_run']:6.2f}   "
                  f"{d['steps_per_run']:6.1f} steps")
    print()

    print("corpus")
    for m in MODELS:
        rate = sum(t[m]["pass_rate"] for t in tasks) / len(tasks)
        cost = sum(t[m]["cost_per_run"] for t in tasks) / len(tasks)
        steps = sum(t[m]["steps_per_run"] for t in tasks) / len(tasks)
        pyt = sum(t[m]["pytest"] for t in tasks) / len(tasks)
        rub = sum(t[m]["rubric"] for t in tasks) / len(tasks)
        print(f"  {MODEL_LABEL[m]:8} pass rate {rate:6.2f}%   pytest {pyt:6.2f}%   "
              f"rubric {rub:6.2f}%   ${cost:5.2f}/run   {steps:5.1f} steps/run")
    total_cost = sum(t[m]["cost_per_run"] * EXPECTED_RUNS for t in tasks for m in MODELS)
    total_steps = sum(t[m]["steps_per_run"] * EXPECTED_RUNS for t in tasks for m in MODELS)
    print(f"  {'all':8} ${total_cost:.2f} across {len(tasks) * len(MODELS) * EXPECTED_RUNS} "
          f"rollouts, {int(round(total_steps))} steps")
    print()


def main():
    os.makedirs(IMAGES_DIR, exist_ok=True)
    tasks = build_dataset()
    print_summary(tasks)

    tiers = [t["tier"] for t in tasks]
    counts = [1] * len(tasks)
    rate_a = [t["claude-opus-5"]["pass_rate"] for t in tasks]
    rate_b = [t["glm-5.3"]["pass_rate"] for t in tasks]
    cost_a = [t["claude-opus-5"]["cost_per_run"] for t in tasks]
    cost_b = [t["glm-5.3"]["cost_per_run"] for t in tasks]

    segments = []
    for cat in CATEGORY_ORDER:
        n = sum(1 for t in tasks if t["category"] == cat)
        light, dark_col = CATEGORY_COLORS[cat]
        segments.append((CATEGORY_LABEL[cat], n, light, dark_col))

    written = []
    for dark in (False, True):
        suffix = "dark" if dark else "light"

        p = os.path.join(IMAGES_DIR, f"pass_rate_by_tier-{suffix}.svg")
        make_line_chart(
            p, dark,
            "Partial-credit pass rate by tier",
            "mean combined score over 8 runs per model, tiers cut on opus-5",
            tiers, rate_a, rate_b, counts,
            y_max=60, y_step=15,
            axis_fmt=lambda v: f"{v:.0f}%",
            value_fmt=lambda v: f"{v:.2f}%",
            footnote="one task per tier; lines are a visual guide across categories, not a continuous trend",
        )
        written.append(p)

        p = os.path.join(IMAGES_DIR, f"cost_by_tier-{suffix}.svg")
        make_line_chart(
            p, dark,
            "Mean agent cost per run by tier",
            "total_cost_usd from agent/trajectory.json, averaged over 8 runs",
            tiers, cost_a, cost_b, counts,
            y_max=12, y_step=3,
            axis_fmt=lambda v: f"${v:.0f}",
            value_fmt=lambda v: f"${v:.2f}",
            footnote="cost tracks the agent time budget, not the tier; the expert task carries a 7200 s timeout against 21600 s",
        )
        written.append(p)

        p = os.path.join(IMAGES_DIR, f"tasks_by_category-{suffix}.svg")
        make_donut(
            p, dark,
            "Task coverage by category",
            "three distinct categories, one task each",
            segments,
            footnote="every task is Python 3, standard library only",
        )
        written.append(p)

    print("wrote")
    for p in written:
        print(f"  images/{os.path.basename(p)}")
    print()
    print("SVG only. The .png fallbacks in images/ are browser-rendered exports of")
    print("these exact files and are not produced by this script.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
