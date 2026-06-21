#!/usr/bin/env python3
"""
check_promises.py — was the promise kept? Score quantified financial promises
against actuals from SEC XBRL.

The "actuals" proxy: companies file their real numbers as structured XBRL with the
SEC, free and authoritative (data.sec.gov). For a promise that names a dollar
amount and a target year (capex, revenue, dividends+buybacks) or a margin %, we
fetch the company's reported value for the matching us-gaap concept in that fiscal
year and compare promised vs actual.

    python3 src/check_promises.py output/promises_2016_2020.jsonl \
        --out output/checked.jsonl

What it can verify (clean XBRL concepts):
    capex                PaymentsToAcquirePropertyPlantAndEquipment
    growth (revenue)     Revenues / RevenueFromContractWithCustomerExcludingAssessedTax
    shareholder_returns  dividends + share repurchases (summed)
    margin (%)           OperatingIncomeLoss / Revenues

What it can't (marked unverifiable): ESG pledges (net-zero, water, emissions —
not in XBRL; would need CDP / Net Zero Tracker), store counts, headcount, and any
promise without a numeric target or target year.

SEC requires a descriptive User-Agent and ~10 req/s; this caches every company's
facts under data/sec_facts/ so re-runs are offline. Be polite with --max-companies.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from collections import Counter

FACTS_DIR = "data/sec_facts"
UA = "promiseKeepers research erik.wilhelm@gmail.com"

# metric_category -> XBRL concepts. A list of lists = sum across the inner groups
# (each inner group tries its synonyms in order). Single list = first match wins.
CONCEPTS = {
    "capex": [["PaymentsToAcquirePropertyPlantAndEquipment",
               "PaymentsToAcquireProductiveAssets"]],
    "growth": [["RevenueFromContractWithCustomerExcludingAssessedTax",
                "Revenues", "SalesRevenueNet"]],
    "shareholder_returns": [["PaymentsOfDividends", "PaymentsOfDividendsCommonStock"],
                            ["PaymentsForRepurchaseOfCommonStock"]],
}
MARGIN_NUM = ["OperatingIncomeLoss"]
MARGIN_DEN = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
              "SalesRevenueNet"]

SCALE = {"billion": 1e9, "bn": 1e9, "b": 1e9, "million": 1e6, "mn": 1e6,
         "m": 1e6, "thousand": 1e3, "k": 1e3}


# --- parsing the promised quantity ----------------------------------------

def parse_money(s: str):
    # require a '$' (optionally with a scale word) or a number + spelled scale word,
    # so bare years ("2020") and percents ("4.0%") are NOT read as dollars.
    m = re.search(r"\$\s*([\d][\d,]*\.?\d*)\s*(billion|bn|million|mn|thousand|b|m|k)?\b",
                  s, re.I)
    if not m:
        m = re.search(r"\b([\d][\d,]*\.?\d*)\s+(billion|bn|million|mn|thousand)\b", s, re.I)
    if not m:
        return None
    val = float(m.group(1).replace(",", ""))
    return val * SCALE.get((m.group(2) or "").lower(), 1.0)


def parse_percent(s: str):
    m = re.search(r"([\d]{1,3}(?:\.\d+)?)\s?%", s)
    return float(m.group(1)) / 100.0 if m else None


def pick(targets, text, parser):
    for t in (targets or []):
        v = parser(t)
        if v is not None:
            return v
    return parser(text)


def direction(text: str) -> str:
    # Only strong, unambiguous cues flip the test. Generic words like "under",
    # "below", "less/more than", "exceed" appear constantly in unrelated context
    # (credit facilities, comparisons) and must NOT decide direction.
    t = text.lower()
    if re.search(r"\bno more than\b|\bup to\b|\bnot to exceed\b|\bat most\b"
                 r"|\bcapped at\b|\bmaximum of\b|\bceiling of\b", t):
        return "at_most"
    if re.search(r"\bat least\b|\bno less than\b|\bminimum of\b|\bin excess of\b", t):
        return "at_least"
    return "approx"  # default: treat a target as a soft point estimate (±15%)


def verdict(direction_: str, ratio: float) -> str:
    if direction_ == "at_least":
        return "kept" if ratio >= 0.98 else "missed"
    if direction_ == "at_most":
        return "kept" if ratio <= 1.02 else "missed"
    if ratio > 1.15:
        return "exceeded"
    if ratio < 0.85:
        return "missed"
    return "kept"


# --- SEC XBRL actuals ------------------------------------------------------

def fetch_facts(cik: str, *, offline: bool, throttle: float) -> dict:
    pad = f"CIK{int(cik):010d}"
    os.makedirs(FACTS_DIR, exist_ok=True)
    path = os.path.join(FACTS_DIR, f"{pad}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    if offline:
        return {}
    url = f"https://data.sec.gov/api/xbrl/companyfacts/{pad}.json"
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept-Encoding": "gzip, deflate"})
    try:
        raw = urllib.request.urlopen(req, timeout=30)
        data = raw.read()
        if raw.info().get("Content-Encoding") == "gzip":
            import gzip
            data = gzip.decompress(data)
        facts = json.loads(data)
    except Exception:
        facts = {}  # 404 / no XBRL — cache the miss so we don't refetch
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(facts, fh)
    time.sleep(throttle)
    return facts


def annual_value(facts: dict, concepts: list, year: str):
    """Latest annual (10-K, fp=FY) USD value whose period ends in `year`."""
    gaap = (facts.get("facts") or {}).get("us-gaap") or {}
    for c in concepts:
        node = gaap.get(c)
        if not node:
            continue
        cand = []
        for u in node.get("units", {}).get("USD", []):
            end = str(u.get("end", ""))
            if end[:4] == str(year) and u.get("form", "").startswith("10-K") \
                    and u.get("fp") == "FY":
                cand.append(u)
        if cand:
            # prefer a calendar-frame entry, else the most recently filed
            cand.sort(key=lambda u: (bool(u.get("frame")), u.get("filed", "")))
            return cand[-1].get("val"), c
    return None, None


def sum_groups(facts, groups, year):
    total, used = 0.0, []
    for g in groups:
        v, c = annual_value(facts, g, year)
        if v is not None:
            total += v
            used.append(c)
    return (total, "+".join(used)) if used else (None, None)


# --- driver ----------------------------------------------------------------

def is_candidate(p) -> bool:
    return (p.get("cik", "?") not in ("", "?", None)
            and (p.get("deadline_year") or "").isdigit()
            and (p.get("metric_category") in CONCEPTS or p.get("metric_category") == "margin"))


def check_one(p, facts):
    metric = p["metric_category"]
    year = p["deadline_year"]
    text = p.get("text", "")
    targets = p.get("targets", [])

    if metric == "margin":
        target = pick(targets, text, parse_percent)
        if target is None:
            return dict(status="unverifiable", note="no percent target")
        num, _ = annual_value(facts, MARGIN_NUM, year)
        den, _ = annual_value(facts, MARGIN_DEN, year)
        if num is None or not den:
            return dict(status="no_actual", note=f"no operating margin for FY{year}")
        actual = num / den
        ratio = actual / target if target else 0
        return dict(status=verdict("approx", ratio), target_value=target,
                    actual_value=round(actual, 4), actual_concept="OperatingIncome/Revenue",
                    actual_year=year, ratio=round(ratio, 3), direction="approx",
                    note="margin compared as ratio of fractions")

    target = pick(targets, text, parse_money)
    if target is None:
        return dict(status="unverifiable", note="no dollar target parsed")
    actual, concept = sum_groups(facts, CONCEPTS[metric], year)
    if actual is None:
        return dict(status="no_actual", note=f"no XBRL {metric} for FY{year}")
    d = direction(text)
    ratio = actual / target if target else 0
    return dict(status=verdict(d, ratio), target_value=target, actual_value=actual,
                actual_concept=concept, actual_year=year, ratio=round(ratio, 3),
                direction=d, note="")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", help="promises .jsonl from extract_promises.py")
    ap.add_argument("--out", default="output/checked.jsonl")
    ap.add_argument("--metric", help="restrict to one metric_category")
    ap.add_argument("--min-score", type=int, default=6)
    ap.add_argument("--max-companies", type=int, default=50,
                    help="cap distinct CIKs fetched from SEC (politeness/time)")
    ap.add_argument("--throttle", type=float, default=0.15, help="seconds between fetches")
    ap.add_argument("--offline", action="store_true", help="use cached facts only")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    with open(args.file, encoding="utf-8") as fh:
        promises = [json.loads(l) for l in fh if l.strip()]

    cands = [p for p in promises
             if is_candidate(p) and int(p.get("score") or 0) >= args.min_score
             and (not args.metric or p.get("metric_category") == args.metric)]
    print(f"{len(promises)} promises -> {len(cands)} financially-verifiable candidates "
          f"(metric in {sorted(set(CONCEPTS) | {'margin'})}, has $/% target + target year)\n")

    facts_cache, seen_order = {}, []
    results = []
    for p in cands:
        cik = p["cik"]
        if cik not in facts_cache:
            if len(seen_order) >= args.max_companies and not args.offline \
                    and not os.path.exists(os.path.join(FACTS_DIR, f"CIK{int(cik):010d}.json")):
                continue  # over the network budget and not cached
            facts_cache[cik] = fetch_facts(cik, offline=args.offline, throttle=args.throttle)
            seen_order.append(cik)
        out = dict(p)
        out.update(check_one(p, facts_cache[cik]))
        results.append(out)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    by_status = Counter(r["status"] for r in results)
    print("verdicts:")
    for s in ("kept", "exceeded", "missed", "no_actual", "unverifiable"):
        if by_status.get(s):
            print(f"  {by_status[s]:5d}  {s}")
    scored = [r for r in results if r["status"] in ("kept", "exceeded", "missed")]
    if scored:
        kept = sum(1 for r in scored if r["status"] in ("kept", "exceeded"))
        print(f"\nkept-rate (of {len(scored)} scored): {kept/len(scored):.0%}")
        print("\nexamples:")
        fv = lambda v: (f"{v:.1%}" if isinstance(v, (int, float)) and abs(v) < 1
                        else f"{v:,.0f}")
        for r in sorted(scored, key=lambda r: r["status"])[:8]:
            print(f"  [{r['status']:8}] {r.get('company') or 'CIK '+r['cik']}  "
                  f"{r['metric_category']} FY{r['actual_year']}  "
                  f"promised~{fv(r.get('target_value'))} actual~{fv(r.get('actual_value'))} "
                  f"(×{r.get('ratio')})")
            print(f"      {r['text'][:140]}")
    print(f"\nwrote {len(results)} checked promises -> {args.out}")


if __name__ == "__main__":
    main()
