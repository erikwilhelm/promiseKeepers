#!/usr/bin/env python3
"""
extract_promises.py — local, LLM-free extraction of corporate "promises"
(forward-looking commitments) from a large EDGAR corpus.

Pipeline:
    corpus rows -> pick sections -> sentence split -> cheap regex prefilter
    -> modality / target / deadline / metric extraction (+ optional spaCy NER)
    -> forward-looking-statement boilerplate filtering
    -> scored records (JSONL or SQLite) with full provenance.

No LLM required. spaCy is OPTIONAL (only improves recall of dates/quantities);
the script runs fine on the regex path alone.

Quick sanity check with built-in samples (no download, no network, no spaCy):
    python3 extract_promises.py --demo

Run over a Hugging Face corpus (already split into 10-K item sections):
    pip install datasets
    python3 extract_promises.py --source hf:eloukas/edgar-corpus --year 2020 \
        --sections section_1 section_7 --out promises_2020.jsonl --min-score 5

Run over local files you already have:
    python3 extract_promises.py --source jsonl:/path/rows.jsonl --out out.jsonl
    python3 extract_promises.py --source parquet:/path/dir   --format sqlite --out out.db
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from typing import Iterator, Optional

# ---------------------------------------------------------------------------
# 1. Lexicons & patterns  (tune these first — this is where precision lives)
# ---------------------------------------------------------------------------

# A commitment = a corporate SUBJECT + a commissive VERB.
_SUBJECT = r"(?:we|our company|the company|the corporation)"
_VERB = (
    r"(?:will|shall"
    r"|plan(?:s|ned)? to|aim(?:s)? to|target(?:s|ing)?"
    r"|commit(?:s|ted)? to|are committed to|is committed to|have committed to"
    r"|intend(?:s)? to|expect(?:s)? to|anticipate(?:s)? to"
    r"|pledge(?:s|d)? to|seek(?:s)? to|are targeting|are working to"
    r"|are on track to|have pledged to|set(?:s)? a target|expect(?:s)?)"
)
COMMISSIVE_RE = re.compile(rf"\b{_SUBJECT}\s+{_VERB}\b", re.I)
# Supporting cues that signal a commitment even without a clean subject+verb.
SUPPORTING_RE = re.compile(
    r"\bcommitment to\b|\ba target of\b|\ba goal of\b"
    r"|\bour (?:target|goal|ambition|commitment|aspiration)\b",
    re.I,
)

# Pure-uncertainty language (risk factors). Used to drop non-commissive noise.
HEDGE_RE = re.compile(
    r"\bmay\b|\bmight\b|\bcould\b|\bno assurance\b|\bsubject to\b"
    r"|\bwe believe\b|\bwe estimate\b|\bpotential(?:ly)?\b",
    re.I,
)

# Forward-looking-statement / safe-harbor boilerplate — exclude outright.
FLS_RE = re.compile(
    r"forward-looking statements|private securities litigation reform act"
    r"|safe harbor|actual results (?:may|could) differ|no obligation to update"
    r"|undue reliance|within the meaning of section",
    re.I,
)

# A deadline anchored by futurity ("by/within/no later than ...").
DEADLINE_RE = re.compile(
    r"\b(?:by|before|no later than|by the end of|by fiscal|over the next|within)\s+"
    r"(?:the\s+)?(?:end of\s+)?(?:fiscal\s+)?(?:year\s+)?"
    r"(\d{4}|\d{1,2}\s+years?|the\s+decade)\b",
    re.I,
)
YEAR_RE = re.compile(r"\b(20\d{2})\b")

# Quantified targets (regex path; spaCy adds PERCENT/MONEY/QUANTITY when present).
PERCENT_RE = re.compile(r"\b\d{1,3}(?:\.\d+)?\s?%")
MONEY_RE = re.compile(r"\$\s?\d[\d,\.]*\s?(?:billion|million|thousand|bn|mn|m|k)?", re.I)
NUMUNIT_RE = re.compile(
    r"\b\d[\d,\.]*\s?(?:\w+\s){0,2}"
    r"(?:stores?|locations?|employees?|jobs?|MW|GW|tonnes?|tons?|units?|facilities)\b",
    re.I,
)
NETZERO_RE = re.compile(r"\bnet[\s-]?zero\b|\bcarbon[\s-]?neutral(?:ity)?\b|\bcarbon[\s-]?free\b", re.I)
FIRST_PERSON_RE = re.compile(r"\b(?:we|our|us|the company|the corporation)\b", re.I)

# Topic gazetteer -> category label.
METRICS = {
    "ghg_emissions": r"emission|greenhouse|\bghg\b|carbon|\bco2\b|scope\s?[123]|decarboni",
    "renewable_energy": r"renewable|clean energy|solar|wind|green electricity",
    "water": r"\bwater\b|freshwater",
    "waste": r"\bwaste\b|landfill|recycl|circular econom",
    "diversity": r"diversity|women|underrepresented|gender|inclusion",
    "safety": r"safety|injur|incident rate|recordable",
    "capex": r"capital expenditure|\bcapex\b|capital spending|invest(?:ment)? of",
    "shareholder_returns": r"dividend|buyback|repurchase|return.{0,20}shareholders",
    "growth": r"revenue|sales growth|net sales|top-line",
    "margin": r"\bmargin\b|profitability|operating income",
    "footprint": r"stores?|locations?|openings?|facilities|capacity|production",
    "workforce": r"headcount|employees|hiring|jobs|workforce",
}
METRICS = {k: re.compile(v, re.I) for k, v in METRICS.items()}

DEFAULT_SECTIONS = ["section_1", "section_7"]  # Item 1 (Business) + Item 7 (MD&A)

# ---------------------------------------------------------------------------
# 2. Record
# ---------------------------------------------------------------------------


@dataclass
class Promise:
    promise_id: str
    cik: str
    company: Optional[str]
    ticker: Optional[str]
    year: Optional[str]
    section: str
    sent_idx: int
    char_start: int
    char_end: int
    text: str
    modality: str
    cues: list = field(default_factory=list)
    metric_category: Optional[str] = None
    targets: list = field(default_factory=list)
    deadline_year: Optional[str] = None
    deadline_phrase: Optional[str] = None
    score: int = 0


# ---------------------------------------------------------------------------
# 3. Sentence splitting (spaCy if available, else regex)
# ---------------------------------------------------------------------------

_NLP = None


def get_spacy():
    global _NLP
    if _NLP is None:
        try:
            import spacy

            _NLP = spacy.load("en_core_web_sm", disable=["lemmatizer", "tagger"])
        except Exception:
            _NLP = False  # mark as unavailable; fall back to regex
    return _NLP or None


_SENT_RE = re.compile(r"(?<=[.!?;])\s+(?=[A-Z(])")


def split_sentences(text: str, use_spacy: bool):
    if use_spacy and get_spacy() is not None:
        for sent in get_spacy()(text).sents:
            s = sent.text.strip()
            if s:
                yield s, sent.start_char
        return
    pos = 0
    for chunk in _SENT_RE.split(text):
        s = chunk.strip()
        if s:
            start = text.find(chunk, pos)
            pos = start + len(chunk)
            yield s, start


# ---------------------------------------------------------------------------
# 4. Extraction core
# ---------------------------------------------------------------------------


def _matches(rx: re.Pattern, s: str) -> list:
    return [m.group(0).strip() for m in rx.finditer(s)]


def _stable_id(cik: str, year, section: str, text: str) -> str:
    norm = re.sub(r"\s+", " ", text.lower()).strip()
    h = hashlib.sha1(f"{cik}|{year}|{section}|{norm}".encode()).hexdigest()
    return h[:16]


def extract(sentence: str, *, cik="", company=None, ticker=None, year=None,
            section="", sent_idx=0, char_start=0) -> Optional[Promise]:
    """Return a Promise if the sentence reads as a forward-looking commitment."""
    if FLS_RE.search(sentence):
        return None  # safe-harbor boilerplate, not a real promise

    has_commissive = bool(COMMISSIVE_RE.search(sentence) or SUPPORTING_RE.search(sentence))
    has_deadline_strong = bool(DEADLINE_RE.search(sentence))

    targets = (
        _matches(PERCENT_RE, sentence)
        + _matches(MONEY_RE, sentence)
        + _matches(NUMUNIT_RE, sentence)
        + _matches(NETZERO_RE, sentence)
    )
    has_target = bool(targets)

    # Keep only sentences that are genuinely commitment-shaped.
    if not (has_commissive or (has_deadline_strong and has_target)):
        return None
    # Drop pure-hedge sentences that lack any commissive cue.
    if not has_commissive and HEDGE_RE.search(sentence):
        return None

    metric_category = next((k for k, rx in METRICS.items() if rx.search(sentence)), None)

    dl = DEADLINE_RE.search(sentence)
    yr = YEAR_RE.search(sentence)
    deadline_year = None
    if dl and re.fullmatch(r"\d{4}", dl.group(1)):
        deadline_year = dl.group(1)
    elif yr:
        deadline_year = yr.group(1)

    has_fp = bool(FIRST_PERSON_RE.search(sentence))
    score = (
        3 * has_commissive
        + 2 * has_deadline_strong
        + (1 if (yr and not has_deadline_strong) else 0)
        + 2 * has_target
        + 1 * bool(metric_category)
        + 1 * has_fp
    )

    cues = _matches(COMMISSIVE_RE, sentence) + _matches(SUPPORTING_RE, sentence)
    return Promise(
        promise_id=_stable_id(cik, year, section, sentence),
        cik=cik, company=company, ticker=ticker, year=year, section=section,
        sent_idx=sent_idx, char_start=char_start, char_end=char_start + len(sentence),
        text=sentence, modality="commissive" if has_commissive else "conditional",
        cues=cues, metric_category=metric_category, targets=targets,
        deadline_year=deadline_year, deadline_phrase=dl.group(0) if dl else None,
        score=score,
    )


def scan_text(text, *, cik, company, ticker, year, section, use_spacy) -> Iterator[Promise]:
    for i, (sent, start) in enumerate(split_sentences(text, use_spacy)):
        if len(sent) > 1200:  # paragraph blobs that escaped splitting
            continue
        p = extract(sent, cik=cik, company=company, ticker=ticker, year=year,
                    section=section, sent_idx=i, char_start=start)
        if p is not None:
            yield p


# ---------------------------------------------------------------------------
# 5. Corpus sources
# ---------------------------------------------------------------------------


def iter_rows(source: str, year: Optional[str]):
    """Yield (meta, {section_name: text}) from hf:/jsonl:/parquet: sources."""
    kind, _, ref = source.partition(":")

    if kind == "hf":
        try:
            from datasets import load_dataset
        except ImportError:
            sys.exit("Install the loader first:  pip install datasets")
        config = f"year_{year}" if year else "full"
        try:
            ds = load_dataset(ref, config, split="train", trust_remote_code=True)
        except Exception as e:
            sys.exit(f"Could not load {ref} ({config}): {e}\n"
                     "Tip: download the parquet files once with `huggingface-cli "
                     "download` and use --source parquet:/path instead.")
        for ex in ds:
            yield ex, {k: v for k, v in ex.items() if k.startswith("section_")}

    elif kind == "jsonl":
        with open(ref, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                yield row, {k: v for k, v in row.items() if k.startswith("section_")}

    elif kind == "parquet":
        try:
            import pyarrow.parquet as pq
            import glob
            import os
        except ImportError:
            sys.exit("Install pyarrow:  pip install pyarrow")
        files = (sorted(glob.glob(os.path.join(ref, "**", "*.parquet"), recursive=True))
                 if os.path.isdir(ref) else [ref])
        for f in files:
            for batch in pq.ParquetFile(f).iter_batches():
                for row in batch.to_pylist():
                    secs = {k: v for k, v in row.items() if k.startswith("section_")}
                    if not secs and "text" in row:        # generic text dataset
                        secs = {"section_full": row["text"]}
                    yield row, secs
    else:
        sys.exit(f"Unknown source kind '{kind}'. Use hf:, jsonl:, or parquet:.")


# ---------------------------------------------------------------------------
# 6. Output sinks
# ---------------------------------------------------------------------------


def _ensure_parent(path):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def write_jsonl(promises, path):
    _ensure_parent(path)
    n = 0
    with open(path, "w", encoding="utf-8") as fh:
        for p in promises:
            fh.write(json.dumps(asdict(p), ensure_ascii=False) + "\n")
            n += 1
    return n


def write_sqlite(promises, path):
    _ensure_parent(path)
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE IF NOT EXISTS promises ("
        "promise_id TEXT, cik TEXT, company TEXT, ticker TEXT, year TEXT, section TEXT,"
        "sent_idx INT, char_start INT, char_end INT, text TEXT, modality TEXT,"
        "cues TEXT, metric_category TEXT, targets TEXT, deadline_year TEXT,"
        "deadline_phrase TEXT, score INT,"
        "PRIMARY KEY (promise_id, section))"
    )
    n = 0
    for p in promises:
        d = asdict(p)
        d["cues"] = json.dumps(d["cues"])
        d["targets"] = json.dumps(d["targets"])
        con.execute(
            "INSERT OR IGNORE INTO promises VALUES "
            "(:promise_id,:cik,:company,:ticker,:year,:section,:sent_idx,:char_start,"
            ":char_end,:text,:modality,:cues,:metric_category,:targets,"
            ":deadline_year,:deadline_phrase,:score)", d)
        n += 1
    con.commit()
    con.close()
    return n


# ---------------------------------------------------------------------------
# 7. Driver
# ---------------------------------------------------------------------------

DEMO_SENTENCES = [
    "We are committed to achieving net-zero greenhouse gas emissions across our operations by 2040.",
    "We plan to open approximately 150 new stores in fiscal 2026.",
    "We expect capital expenditures of approximately $2.5 billion in 2026.",
    "The Company targets a 50% reduction in water withdrawal by 2030 compared to a 2020 baseline.",
    "We will return at least $1 billion to shareholders through dividends and share repurchases in 2025.",
    "Our actual results may differ materially from these expectations and we could incur additional costs.",
    "This report contains forward-looking statements within the meaning of the Private Securities "
    "Litigation Reform Act of 1995; actual results may differ.",
    "The Company operates 300 stores across the region.",
]


def run_demo(min_score):
    print("DEMO — running the extractor on built-in samples\n" + "-" * 60)
    kept = 0
    for i, s in enumerate(DEMO_SENTENCES):
        p = extract(s, cik="DEMO", year="2024", section="demo", sent_idx=i)
        verdict = "DROP"
        if p and p.score >= min_score:
            kept += 1
            verdict = f"PROMISE  score={p.score}  metric={p.metric_category}  " \
                      f"deadline={p.deadline_year}  targets={p.targets}"
        print(f"[{verdict}]\n  {s}\n")
    print("-" * 60)
    print(f"kept {kept} / {len(DEMO_SENTENCES)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", help="hf:NAME | jsonl:PATH | parquet:PATH_OR_DIR")
    ap.add_argument("--year", help="corpus year (for hf: configs like year_2020)")
    ap.add_argument("--sections", nargs="*", default=DEFAULT_SECTIONS,
                    help=f"section fields to scan (default: {DEFAULT_SECTIONS})")
    ap.add_argument("--out", default="output/promises.jsonl")
    ap.add_argument("--format", choices=["jsonl", "sqlite"], default="jsonl")
    ap.add_argument("--cik-map", help="JSON {cik: {name, ticker}} from cik_lookup.py "
                    "to fill company/ticker (corpus rows have no company name)")
    ap.add_argument("--min-score", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0, help="stop after N rows (0 = all)")
    ap.add_argument("--use-spacy", action="store_true",
                    help="use spaCy for sentence splitting + NER if installed")
    ap.add_argument("--demo", action="store_true", help="run on built-in samples and exit")
    args = ap.parse_args()

    try:  # filing text contains chars the Windows console can't encode
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if args.demo:
        run_demo(args.min_score)
        return
    if not args.source:
        ap.error("provide --source (or use --demo)")

    cikmap = {}
    if args.cik_map:
        from cik_lookup import load_cik_map, norm_cik
        cikmap = load_cik_map(args.cik_map)

    def gen():
        rows = 0
        for meta, sections in iter_rows(args.source, args.year):
            rows += 1
            if args.limit and rows > args.limit:
                break
            cik = str(meta.get("cik", "")) or "?"
            info = cikmap.get(norm_cik(cik)) if cikmap else None
            company = (meta.get("company") or meta.get("name")
                       or (info or {}).get("name"))
            ticker = (info or {}).get("ticker")
            year = str(meta.get("year") or args.year or "")
            for sec_name in args.sections:
                text = sections.get(sec_name)
                if not text:
                    continue
                for p in scan_text(text, cik=cik, company=company, ticker=ticker,
                                   year=year, section=sec_name, use_spacy=args.use_spacy):
                    if p.score >= args.min_score:
                        yield p

    writer = write_sqlite if args.format == "sqlite" else write_jsonl
    n = writer(gen(), args.out)
    print(f"wrote {n} promises -> {args.out}  (format={args.format}, "
          f"min_score={args.min_score})")


if __name__ == "__main__":
    main()
