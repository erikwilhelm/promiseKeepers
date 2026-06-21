# Corporate Promise Extractor

Local, LLM-free extraction of corporate **promises** — forward-looking commitments
("we will… by 20XX", "we are committed to reducing… by N%") — from a large EDGAR
corpus. The output is one structured, provenance-tagged record per commitment,
designed to feed a longitudinal "was the promise kept?" tracker downstream.

No LLM is required to start. Forward-looking commitments are linguistically
regular, so a rule-based pass yields high-precision candidates with full
provenance. An LLM (or a fine-tuned classifier) is an optional later stage, used
to *normalize* survivors — never to *find* them.

---

## Quick start

The extractor lives at [src/extract_promises.py](src/extract_promises.py). Run it
directly with `python3 src/extract_promises.py …`, or use the `run.sh` / `run.ps1`
wrappers, which forward every argument to the extractor so the commands below work
verbatim (just swap `python3 extract_promises.py` → `./run.sh`).

```bash
# 0. (optional) isolate deps
python3 -m venv .venv && source .venv/bin/activate

# 1. offline smoke test: demo + extraction over the bundled sample corpus
./run.sh                       # Windows: .\run.ps1

# 1b. just the built-in samples (no data, no network)
python3 src/extract_promises.py --demo

# 2. install the corpus downloader + parquet reader
pip install -r requirements.txt        # huggingface_hub + pyarrow

# 3. download one year of 10-Ks, build the CIK->name map, extract, analyze
python3 src/download_corpus.py --year 2020
python3 src/cik_lookup.py
python3 src/extract_promises.py \
  --cik-map data/cik_names.json \
  --source parquet:data/edgar-corpus/year_2020/train \
  --sections section_1 section_7 \
  --out output/promises_2020.jsonl --min-score 5
python3 src/analyze_promises.py output/promises_2020.jsonl summary
```

The repo ships a tiny synthetic corpus, [data/sample_rows.jsonl](data/sample_rows.jsonl),
so the local pipeline (and `run.sh` with no args) works with no network or optional
deps. Dependencies are all optional and listed in
[requirements.txt](requirements.txt) — the extractor itself runs on the standard
library alone:

```
huggingface_hub  # src/download_corpus.py (fetch EDGAR parquet into ./data)
pyarrow          # --source parquet: (read .parquet corpora)
datasets         # only for the legacy --source hf: path
spacy            # only for --use-spacy (NER for dates/quantities)
```

(`cik_lookup.py` and `analyze_promises.py` use the standard library only.)

---

## 1. Acquiring the data

The default corpus is **`eloukas/edgar-corpus`** on Hugging Face: every public
company's Form 10-K, **1993–2020**, already split into the item sections we care
about (Item 1 Business, Item 1A Risk Factors, Item 7 MD&A, …), licensed Apache-2.0.
Pre-sectioning matters — commitments concentrate in Item 1 and Item 7, while
Item 1A is mostly hedged "may/could" language we want to skip.

### Option A — download the corpus into `./data` (recommended)

`eloukas/edgar-corpus` ships a dataset *loading script* (`edgar-corpus.py`), and
recent `datasets` releases dropped `trust_remote_code`, so
`--source hf:eloukas/edgar-corpus` **no longer works** (you'll get
*"Dataset scripts are no longer supported"*). Instead, download the Hub's
auto-converted **Parquet** copy and point the extractor at the files:

```bash
pip install huggingface_hub pyarrow

# one year, train split -> data/edgar-corpus/year_2020/train/*.parquet
python3 src/download_corpus.py --year 2020

# quick test: grab a single shard first
python3 src/download_corpus.py --year 2020 --split validation --max-files 1

# then extract:
python3 src/extract_promises.py \
  --source parquet:data/edgar-corpus/year_2020/train \
  --out output/promises_2020.jsonl --min-score 5
```

The download lands under `./data` (git-ignored except the bundled
`sample_rows.jsonl`). [download_corpus.py](src/download_corpus.py) accepts
`--year` (a single year `2020` **or an inclusive range `2016-2020`** for a
longitudinal study), `--split {train,validation,test}`, `--max-files N`, and
`--repo`. The `parquet:` source reads directories recursively, so after a
multi-year download you can extract everything in one pass with
`--source parquet:data/edgar-corpus`.

> **Why not `--source hf:`?** The old script-based loader is unmaintained.
> `download_corpus.py` reads the same data from the Hub's `refs/convert/parquet`
> revision, which needs no remote code.

### Option B — any other parquet corpus

The `parquet:` source reads any directory of `.parquet` files. Rows with
`section_*` columns are scanned per section; rows with only a `text`/`section_full`
column are scanned whole. For example **`PleIAs/SEC`** (10-Ks 1993–2024, full text,
no loading script):

```bash
huggingface-cli download PleIAs/SEC --repo-type dataset --local-dir ./data/pleias
python3 src/extract_promises.py --source parquet:./data/pleias --sections section_full ...
```

Scanning whole-document text (Option B) is noisier than the pre-sectioned corpus
(Option A) because you also sweep risk factors and legal boilerplate — the
safe-harbor filter handles most of it, but expect lower precision.

### Sizing & scope

- A **single year** is a few GB; the **full 1993–2020** set is tens of GB. Start
  with one year, validate precision, then widen with `--year` or the `full` config.
- This corpus yields **financial / operational / governance** promises (capex,
  store openings, buybacks, margin targets, hiring). **ESG / climate** pledges
  mostly live in standalone sustainability reports, not 10-Ks — see *Extending
  the corpus*.

### Fresh filings beyond 2020 (official SEC, free, self-hosted)

The SEC's own access is free with no key — sidestep rate limits by pulling the
nightly bulk archives and hosting them yourself:

- `https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip` — all XBRL facts
- `https://www.sec.gov/Archives/edgar/daily-index/xbrl/submissions.zip` — all filing histories
- `https://www.sec.gov/Archives/edgar/full-index/{YYYY}/QTR{1-4}/master.idx` — filing index

Live requests are limited to **10 req/s** and **require a descriptive
`User-Agent` header** (e.g. `Spare Energy research@example.com`) or the SEC
returns 403. To get item sections from raw filings, run them through
[`edgar-crawler`](https://github.com/lefterisloukas/edgar-crawler) (the same
toolkit that produced `eloukas/edgar-corpus`), then feed its JSON to this script.

### Extending the corpus (ESG / open web)

| Source | What it gives you | Coverage | Access | License |
|---|---|---|---|---|
| `eloukas/edgar-corpus` (HF) | 10-K item sections | 1993–2020 | `datasets` | Apache-2.0 |
| `PleIAs/SEC` (HF) | 10-K full text (parquet) | 1993–2024 | `hf download` | see card |
| SEC EDGAR bulk | all filings + XBRL | 1993–present | free, 10 req/s | US public domain |
| ResponsibilityReports / SustainabilityReports | CSR/ESG report PDFs | 2000–present | free download | site terms |
| Common Crawl | open-web pages (company sites) | 2008–present | free (AWS S3) | CC terms |
| Net Zero Tracker (via Our World in Data) | structured net-zero targets | current | CSV | CC BY |
| WikiRate | ESG commitments + linked outcomes | current | REST API | CC BY 4.0 |

For ESG promises, parse sustainability-report PDFs (e.g. with the report
archives or Common Crawl) to plain text and feed them to the **same extractor**
via `--source jsonl:` or `--source parquet:`, mapping each report to a
`section_*` field.

---

## 2. The processing flow

```mermaid
flowchart TD
    A["Acquire corpus<br/>HF eloukas/edgar-corpus"] --> B["Select sections<br/>Item 1 Business · Item 7 MD&A"]
    B --> C["Sentence split<br/>regex, or spaCy if --use-spacy"]
    C --> D{"Commitment-shaped?<br/>subject + commissive verb<br/>OR hard deadline + target"}
    D -- no --> X["discard"]
    D -- yes --> E["Extract fields<br/>modality · metric · target · deadline"]
    E --> F{"Safe-harbor / pure-hedge<br/>boilerplate?"}
    F -- yes --> X
    F -- no --> G[("promises<br/>JSONL / SQLite<br/>+ full provenance")]
    G -.optional.-> H["SEC-BERT classifier<br/>re-score candidates"]
    H -.optional.-> I["LLM<br/>normalize survivors only"]
```

1. **Acquire** — one year of the corpus, or local `jsonl`/`parquet`.
2. **Select sections** — scan Item 1 and Item 7; skip Item 1A (hedged noise).
3. **Split + cheap prefilter** — sentence-split, then keep only
   commitment-shaped sentences (corporate subject + commissive verb, *or* a hard
   "by 20XX" deadline plus a quantified target). Nothing expensive runs on the rest.
4. **Extract fields** — modality, metric category, target value(s), deadline
   year, the verbatim span, a stable `promise_id`, and a heuristic score.
5. **Filter boilerplate** — drop forward-looking-statement / safe-harbor
   disclaimers and pure-hedge sentences. These are the dominant false-positive
   sources; handling them explicitly is what makes the pass precision-first.
6. **Emit** — JSONL or SQLite, one row per candidate with full provenance.

---

## 3. Usage

```
--source     hf:NAME | jsonl:PATH | parquet:PATH_OR_DIR   (required unless --demo)
--year       corpus year, e.g. 2020 (selects hf config year_2020)
--sections   section fields to scan        (default: section_1 section_7)
--out        output path                    (default: output/promises.jsonl)
--format     jsonl | sqlite                 (default: jsonl)
--cik-map    JSON from cik_lookup.py to fill company/ticker from CIK
--min-score  minimum score to keep          (default: 4)
--limit      stop after N rows (0 = all)
--use-spacy  use spaCy for sentence split + NER if installed
--demo       run on built-in samples and exit
```

```bash
# local files you already have
python3 src/extract_promises.py --source jsonl:rows.jsonl --out output/promises.jsonl
python3 src/extract_promises.py --source parquet:./data --format sqlite --out output/promises.db

# higher recall on dates/quantities
pip install spacy && python -m spacy download en_core_web_sm
python3 src/extract_promises.py --source parquet:data/edgar-corpus/year_2020/train --use-spacy ...
```

### Filling in company names (`--cik-map`)

The EDGAR corpus rows carry only a numeric `cik`, **no company name**, so
`company` comes out `null`. [cik_lookup.py](src/cik_lookup.py) downloads SEC's
public `company_tickers.json` and builds a `cik -> {name, ticker}` map; pass it to
the extractor with `--cik-map`:

```bash
python3 src/cik_lookup.py                     # -> data/cik_names.json (~8k filers)

python3 src/extract_promises.py \
  --cik-map data/cik_names.json \
  --source parquet:data/edgar-corpus/year_2020/train \
  --out output/promises_2020.jsonl --min-score 5
```

Coverage is limited to filers that have a ticker (~8k public companies), so some
CIKs (foreign issuers, funds, delisted names) stay `null` — those rows still keep
their `cik`. For full coverage, swap in SEC's `submissions` data set.

### Analyzing & tracing the output

[analyze_promises.py](src/analyze_promises.py) reads the extractor output and
answers the common questions (stdlib only). **Every command both prints a report
and writes a file** under `output/analysis/` — override the path with `--csv PATH`,
change the directory with `--out-dir`, or suppress with `--no-save`.

The input can be a single file, a glob, a directory, or a comma-separated list —
so pointing it at several years' output gives a longitudinal view:

```bash
# the landscape: totals, top companies, breakdown by metric / deadline / filing year
python3 src/analyze_promises.py output/promises_2020.jsonl summary

# everything one company committed to, across every year (match name/ticker/CIK)
python3 src/analyze_promises.py output/promises_2016_2020.jsonl company "delta air lines"

# trace one theme ACROSS companies, sorted by deadline -> metric_ghg_emissions.csv
python3 src/analyze_promises.py output/promises_2016_2020.jsonl metric ghg_emissions

# generic filtered list
python3 src/analyze_promises.py output/promises_2020.jsonl list \
  --metric water --deadline 2030 --min-score 7 --sort deadline
```

### Longitudinal study (`trend`)

`trend` pivots promise counts by **filing year** so you can see how commitment
patterns move over time. Feed it a multi-year extraction (one combined file, or a
glob/list of per-year files):

```bash
# metric x year matrix (overall) -> output/analysis/trend_metric_by_year.csv
python3 src/analyze_promises.py "output/promises_*.jsonl" trend

# one theme, company x year: which companies keep promising it, year over year
python3 src/analyze_promises.py output/promises_2016_2020.jsonl trend --metric ghg_emissions

# one company, metric x year: how its promise mix shifts over time
python3 src/analyze_promises.py output/promises_2016_2020.jsonl trend --company "delta"
```

Example `trend` output (2019→2020) — note ESG themes climbing:

```
metric                2019   2020  TOTAL
renewable_energy        10     47     57
diversity                3     20     23
ghg_emissions           19     23     42
```

> **Getting 5 years of corpus.** Download a range and extract it in one pass
> (each promise is tagged with its filing `year`):
> ```bash
> python3 src/download_corpus.py --year 2016-2020
> python3 src/extract_promises.py --cik-map data/cik_names.json \
>   --source parquet:data/edgar-corpus \
>   --out output/promises_2016_2020.jsonl --min-score 5
> python3 src/analyze_promises.py output/promises_2016_2020.jsonl trend
> ```
> The `parquet:` source scans the directory recursively, so one `--source
> parquet:data/edgar-corpus` sweeps every year you've downloaded.
>
> The corpus ends at **2020**, so "5 years back" means **2016–2020**. A
> longitudinal trace is only meaningful for companies that appear in multiple
> years — large, continuously-listed filers (use the `train` split, not the small
> `validation`/`test` splits, for real coverage).

### Was the promise kept? (actuals via SEC XBRL)

[check_promises.py](src/check_promises.py) closes the loop on the *quantified
financial* promises. The "actuals" proxy is **SEC XBRL company facts**
(`data.sec.gov`) — the real numbers companies report, free and authoritative. For
a promise that names a dollar amount (or a margin %) and a target year, it fetches
the matching `us-gaap` concept for that fiscal year and compares promised vs actual:

```bash
python3 src/check_promises.py output/promises_2016_2020.jsonl --out output/checked.jsonl
python3 src/check_promises.py output/promises_2016_2020.jsonl --metric capex
```

| metric | XBRL concept(s) used as the actual |
|---|---|
| `capex` | `PaymentsToAcquirePropertyPlantAndEquipment` |
| `growth` | `Revenues` / `RevenueFromContractWithCustomerExcludingAssessedTax` |
| `shareholder_returns` | dividends **+** share repurchases (summed) |
| `margin` | `OperatingIncomeLoss / Revenues` |

Each checked record gains `target_value`, `actual_value`, `actual_concept`,
`ratio`, `direction` (parsed from "at least" / "no more than" / "approximately"),
and a `status` ∈ `kept · exceeded · missed · no_actual · unverifiable`. The tool
caches each company's facts under `data/sec_facts/` (git-ignored) so re-runs are
offline; `--max-companies` and `--throttle` keep it under SEC's ~10 req/s limit
(`--offline` uses cache only).

> **What this proxy can and can't do.** It is authoritative for company-wide
> dollar promises (e.g. *"capital expenditures of approximately $550 million"* →
> verified against reported capex). It is **out of scope** for ESG pledges
> (net-zero, water, emissions are not in XBRL — those need CDP / Net Zero Tracker)
> and is only as good as the extraction: a sentence that mentions a *segment* or a
> *cash balance* rather than the company-wide target will compare against the
> consolidated XBRL figure and look spuriously "missed"/"exceeded". Treat the
> verdict as a screen, not a judgment — `actual_concept` and `text` are kept on
> every row so each call is auditable.

### Visualize it (Gapminder-style)

[build_viz.py](src/build_viz.py) turns the checked output into a single
self-contained, interactive HTML — a **promised-vs-actual** bubble chart on log-log
axes with a diagonal "kept the promise" line: bubbles above it over-delivered,
below it fell short. Colour is the verdict, size is the size of the promise, and a
**play button animates through target years**. Click any bubble to open a detail
panel with the company, the numbers, the SEC concept, and the verbatim sentence —
plus a link to the company's EDGAR filings.

```bash
python3 src/build_viz.py output/checked.jsonl --out output/promises.html
# then open output/promises.html in a browser
```

Filter by metric, toggle kept/exceeded/missed, search a company, or click
*"Show all its promises"* to focus one filer. Only verdicts with a dollar target
**and** a reported actual are plotted; `--min-promised` and `--max-ratio` drop
implausible rows (per-share figures, segment-vs-total scope mismatches). The page
uses Plotly from a CDN (needs network to load the chart library; the data itself is
embedded, nothing is uploaded).

---

## 4. Output schema

One record per extracted commitment (JSONL field / SQLite column):

| Field | Type | Description |
|---|---|---|
| `promise_id` | str | `sha1(cik\|year\|section\|normalized_text)[:16]` — stable id for dedup |
| `cik` | str | SEC Central Index Key of the filer |
| `company` | str? | company name (from `--cik-map`; `null` if CIK not in the map) |
| `ticker` | str? | stock ticker (from `--cik-map`) |
| `year` | str? | filing year |
| `section` | str | source section (e.g. `section_7`) |
| `sent_idx` | int | sentence index within the section |
| `char_start` / `char_end` | int | character span of the sentence in the section |
| `text` | str | the **verbatim** commitment sentence (your evidence) |
| `modality` | str | `commissive` (explicit commitment) or `conditional` |
| `cues` | list | matched commitment phrases (e.g. `["we are committed to"]`) |
| `metric_category` | str? | `ghg_emissions`, `capex`, `shareholder_returns`, … |
| `targets` | list | quantified targets found (`["50%"]`, `["$1 billion"]`, `["net-zero"]`) |
| `deadline_year` | str? | extracted target year |
| `deadline_phrase` | str? | the raw deadline phrase (e.g. `by 2030`) |
| `score` | int | heuristic confidence (commissive +3, deadline +2, target +2, metric +1, first-person +1) |

This row is the **observation** record for the downstream tracker (see below):
provenance + verbatim text + parsed fields, keyed by a stable id.

---

## 5. Tuning

All precision lives in the lexicons at the top of `extract_promises.py`:

- `COMMISSIVE_RE` / `SUPPORTING_RE` — what counts as a commitment. Broaden to
  raise recall, tighten to raise precision.
- `HEDGE_RE` — uncertainty language that, *absent* a commissive cue, disqualifies
  a sentence.
- `FLS_RE` — safe-harbor / forward-looking-statement boilerplate, always dropped.
- `METRICS` — topic gazetteer mapping sentences to a `metric_category`.
- `--min-score` — the keep threshold; raise it to trade recall for precision.

Workflow: run a year, eyeball the top and bottom of the score range, and adjust.
Known gaps to expect — oddly-phrased commitments slip through (recall), and
relative deadlines like "over the next three years" currently resolve to
`deadline_year = null` (map them to `filing_year + N` if needed).

---

## 6. Roadmap — when rules plateau

1. **Label** a few hundred output rows (promise vs. not).
2. **Classifier stage** — train SEC-BERT (`nlpaueb/sec-bert-base`) on those labels
   as a real-promise-vs-boilerplate filter over candidates. This is where a
   domain-pretrained encoder earns its keep — representation and filtering, not
   generation.
3. **LLM normalization** — only on the survivors, to canonicalize the metric,
   baseline, and scope, and to resolve duplicate phrasings. Never to find promises.

## 7. How it fits the bigger picture

Each output row is an **observation**. The tracker clusters observations into
**canonical promises** (entity + normalized metric + target date → stable id),
so the same pledge restated across years becomes one promise with a time series.
A scheduler then re-checks each promise when its target date passes (or on a new
snapshot) and diffs the state — change-detection (a target year quietly sliding,
a commitment page removed) is the high-signal, defensible output, more reliable
than an automated "was it met" verdict.

[check_promises.py](src/check_promises.py) is the first cut at that re-check for
the financially-quantified subset: it scores promised-vs-actual against SEC XBRL
(see *Was the promise kept?* above). For ESG pledges the same pattern applies with
a different actuals source (CDP, Net Zero Tracker, company sustainability data).

## 8. Limitations

- Precision-first heuristic: tune the lexicons against your own sample rather
  than trusting it blindly.
- 10-K corpus covers operational/financial/governance promises; ESG/climate
  pledges require sustainability-report text (same extractor, different source).
- `eloukas/edgar-corpus` ends at 2020 — use the SEC bulk path for newer filings.
- spaCy is optional; enabling it improves date/quantity recall via NER.
- `check_promises.py` verifies only company-wide dollar/margin promises against
  SEC XBRL; ESG pledges and segment/qualitative figures are out of scope, and a
  verdict is only as reliable as the underlying extraction (audit via the kept
  `actual_concept` + `text`).
