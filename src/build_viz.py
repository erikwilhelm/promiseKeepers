#!/usr/bin/env python3
"""
build_viz.py — render checked promises as an interactive Gapminder-style HTML.

Reads check_promises.py output and emits a single self-contained .html: an
animated promised-vs-actual bubble chart (log-log, with a "kept the promise"
diagonal), filters by metric / verdict / company, a play button over target
years, and a click-through detail panel showing each promise's text and SEC
concept.

    python3 src/check_promises.py output/promises_2016_2020.jsonl --out output/checked.jsonl
    python3 src/build_viz.py output/checked.jsonl --out output/promises.html

Only verdicts with both a promised and an actual dollar value are plotted
(kept / exceeded / missed); margin (%) and unverifiable rows are summarized in
the header but not plotted on the dollar axes.
"""
from __future__ import annotations

import argparse
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "viz_template.html")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", default="output/checked.jsonl",
                    help="checked .jsonl from check_promises.py")
    ap.add_argument("--out", default="output/promises.html")
    ap.add_argument("--title", default="Corporate Promises — kept or broken?")
    ap.add_argument("--min-promised", type=float, default=1e5,
                    help="drop promises below this $ (parse/per-share noise)")
    ap.add_argument("--max-ratio", type=float, default=50.0,
                    help="drop actual/promised above this (scope/unit mismatch)")
    args = ap.parse_args()

    with open(args.file, encoding="utf-8") as fh:
        rows = [json.loads(l) for l in fh if l.strip()]

    plot, n_unverif, n_noise = [], 0, 0
    for r in rows:
        tv, av = r.get("target_value"), r.get("actual_value")
        if r.get("status") not in ("kept", "exceeded", "missed"):
            n_unverif += 1
            continue
        if r.get("metric_category") == "margin":  # fractions, not dollars
            n_unverif += 1
            continue
        if not isinstance(tv, (int, float)) or not isinstance(av, (int, float)):
            continue
        if tv <= 0 or av <= 0:
            continue
        ratio = av / tv
        if tv < args.min_promised or ratio > args.max_ratio or ratio < 1 / args.max_ratio:
            n_noise += 1  # implausible -> almost certainly a parse/scope error
            continue
        plot.append({
            "company": r.get("company") or f"CIK {r.get('cik')}",
            "ticker": r.get("ticker") or "",
            "cik": r.get("cik"),
            "metric": r.get("metric_category"),
            "year": r.get("actual_year") or r.get("deadline_year"),
            "filed": r.get("year"),
            "promised": float(tv),
            "actual": float(av),
            "ratio": r.get("ratio") or round(av / tv, 3),
            "status": r.get("status"),
            "direction": r.get("direction"),
            "score": r.get("score"),
            "concept": r.get("actual_concept") or "",
            "text": (r.get("text") or "").strip(),
        })

    if not plot:
        raise SystemExit(
            f"No plottable rows in {args.file} (need kept/exceeded/missed with "
            "dollar target+actual). Run check_promises.py over more promises first.")

    with open(TEMPLATE, encoding="utf-8") as fh:
        html = fh.read()
    payload = json.dumps(plot, ensure_ascii=False)
    html = (html.replace("__TITLE__", args.title)
                .replace("__N_PLOT__", str(len(plot)))
                .replace("__N_UNVERIF__", str(n_unverif))
                .replace('"__DATA__"', payload))

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(html)

    kept = sum(1 for p in plot if p["status"] in ("kept", "exceeded"))
    print(f"plotted {len(plot)} verified promises "
          f"({kept} met / {len(plot)-kept} missed); "
          f"{n_noise} dropped as parse/scope noise, {n_unverif} not plottable")
    print(f"wrote {args.out}  — open it in a browser")


if __name__ == "__main__":
    main()
