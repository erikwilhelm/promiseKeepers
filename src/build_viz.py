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
import re

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "viz_template.html")

CLIMATE = {"ghg_emissions", "renewable_energy", "water", "waste"}
NETZERO_RE = re.compile(r"net[\s-]?zero|carbon[\s-]?neutral|carbon[\s-]?free", re.I)
PCT_RE = re.compile(r"\d{1,3}(?:\.\d+)?\s?%")


def build_esg(path):
    """Climate/net-zero pledges from the extracted promises (not score-able vs XBRL,
    so shown as commitments-over-time): keep genuine climate targets with a year."""
    out, seen = [], set()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("metric_category") not in CLIMATE:
                continue
            dy = r.get("deadline_year") or ""
            if not dy.isdigit() or not (2020 <= int(dy) <= 2055):
                continue
            text = r.get("text", "")
            netzero = bool(NETZERO_RE.search(text))
            pct = PCT_RE.search(text) or any("%" in str(t) for t in r.get("targets", []))
            if not (netzero or pct):
                continue
            key = (r.get("cik"), r["metric_category"], dy, text[:80])
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "company": r.get("company") or f"CIK {r.get('cik')}",
                "ticker": r.get("ticker") or "",
                "cik": r.get("cik"),
                "metric": r["metric_category"],
                "year": int(dy),
                "filed": r.get("year"),
                "netzero": netzero,
                "score": r.get("score"),
                "text": text.strip(),
            })
    return out


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
    ap.add_argument("--esg", help="extracted promises .jsonl to mine climate/net-zero "
                    "pledges from (e.g. output/promises_all.jsonl)")
    args = ap.parse_args()

    with open(args.file, encoding="utf-8") as fh:
        rows = [json.loads(l) for l in fh if l.strip()]

    plot, n_unverif, n_noise, n_dup, n_ceiling = [], 0, 0, 0, 0
    seen = set()
    for r in rows:
        tv, av = r.get("target_value"), r.get("actual_value")
        if r.get("status") not in ("kept", "exceeded", "missed"):
            n_unverif += 1
            continue
        if r.get("metric_category") == "margin":  # fractions, not dollars
            n_unverif += 1
            continue
        if r.get("direction") == "at_most":
            # ceilings / authorizations ("up to $X", buyback limits): not a promise
            # to *reach* $X, and their kept/missed inverts on this axis -> exclude.
            n_ceiling += 1
            continue
        if not isinstance(tv, (int, float)) or not isinstance(av, (int, float)):
            continue
        if tv <= 0 or av <= 0:
            continue
        ratio = av / tv
        if tv < args.min_promised or ratio > args.max_ratio or ratio < 1 / args.max_ratio:
            n_noise += 1  # implausible -> almost certainly a parse/scope error
            continue
        # colour strictly by position so it can never disagree with the diagonal
        status = "exceeded" if ratio > 1.15 else ("missed" if ratio < 0.85 else "kept")
        # same promise can recur across consecutive filings -> keep one
        key = (r.get("cik"), r.get("metric_category"),
               r.get("actual_year") or r.get("deadline_year"),
               round(float(tv)), (r.get("text") or "")[:80])
        if key in seen:
            n_dup += 1
            continue
        seen.add(key)
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
            "status": status,
            "direction": r.get("direction"),
            "score": r.get("score"),
            "concept": r.get("actual_concept") or "",
            "currency": r.get("currency") or "USD",
            "text": (r.get("text") or "").strip(),
        })

    if not plot:
        raise SystemExit(
            f"No plottable rows in {args.file} (need kept/exceeded/missed with "
            "dollar target+actual). Run check_promises.py over more promises first.")

    esg = build_esg(args.esg) if args.esg and os.path.exists(args.esg) else []

    with open(TEMPLATE, encoding="utf-8") as fh:
        html = fh.read()
    payload = json.dumps(plot, ensure_ascii=False)
    html = (html.replace("__TITLE__", args.title)
                .replace("__N_PLOT__", str(len(plot)))
                .replace("__N_UNVERIF__", str(n_unverif))
                .replace("__N_ESG__", str(len(esg)))
                .replace('"__ESG_DATA__"', json.dumps(esg, ensure_ascii=False))
                .replace('"__DATA__"', payload))

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(html)

    if esg:
        nz = sum(1 for p in esg if p["netzero"])
        print(f"climate pledges: {len(esg)} ({nz} net-zero) from {args.esg}")
    kept = sum(1 for p in plot if p["status"] in ("kept", "exceeded"))
    print(f"plotted {len(plot)} verified promises "
          f"({kept} met / {len(plot)-kept} missed); "
          f"{n_ceiling} ceiling/authorization promises excluded, "
          f"{n_dup} duplicate filings merged, {n_noise} dropped as parse/scope noise, "
          f"{n_unverif} not plottable")
    print(f"wrote {args.out}  — open it in a browser")


if __name__ == "__main__":
    main()
