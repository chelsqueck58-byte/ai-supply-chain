"""Daily 7-day news refresh for the ai-supply-chain stock pages.

Writes weekly-news.json, which supply-chain.html renders as the "This week"
section at the top of every stock page. Research runs through headless Claude
Code (`claude -p`) so it stays on the subscription rather than an API key.

Contract (also documented in weekly-news.json's own _schema key):

    {"generated": "<ISO ts>", "tickers": {"NVDA": [{...item...}], ...}}
    item = {"date": "YYYY-MM-DD", "headline": str, "why": str|absent, "src": str}

Run:  .venv/bin/python3 scripts/news_refresh.py
      .venv/bin/python3 scripts/news_refresh.py --only NVDA,AAPL   # subset
      .venv/bin/python3 scripts/news_refresh.py --dry-run          # no write
      .venv/bin/python3 scripts/news_refresh.py --jobs 1            # serialise
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
NEWS_PATH = REPO / "weekly-news.json"

WINDOW_DAYS = 7
MAX_ITEMS_PER_TICKER = 4
CLAUDE_TIMEOUT_S = 420
DEFAULT_JOBS = 4
HKT = dt.timezone(dt.timedelta(hours=8))

# Must stay in sync with STOCK_PAGES in supply-chain.html. Names are passed to
# the researcher so it searches the company, not the bare symbol - "3690" alone
# returns nothing useful.
STOCK_PAGES = {
    "META": "Meta Platforms",
    "NVDA": "NVIDIA",
    "AAPL": "Apple",
    "MSFT": "Microsoft",
    "GOOGL": "Alphabet / Google",
    "AMZN": "Amazon",
    "AMD": "Advanced Micro Devices",
    "AVGO": "Broadcom",
    "INTC": "Intel",
    "TSM": "TSMC (Taiwan Semiconductor)",
    "ASML": "ASML Holding",
    "MU": "Micron Technology",
    "MRVL": "Marvell Technology",
    "9988": "Alibaba Group (HK 9988)",
    "0700": "Tencent Holdings (HK 0700)",
    "3690": "Meituan (HK 3690)",
    "9618": "JD.com (HK 9618)",
    "6181": "Laopu Gold (HK 6181)",
}

SCHEMA_BLOCK = {
    "generated": "ISO timestamp of the daily refresh; the page shows it and flags >=2d stale",
    "tickers": "map of ticker id -> array of news items, any order (the page sorts newest-first and filters to the last 7 days)",
    "item": {
        "date": "YYYY-MM-DD, the day the news broke (the page joins this to own price data for the 1d move)",
        "headline": "one line, what happened",
        "why": "optional, one line on why it matters for this name",
        "src": "publication or filing the item came from - required for anything asserted as fact",
    },
}


def log(message: str) -> None:
    stamp = dt.datetime.now(HKT).strftime("%H:%M:%S")
    print(f"[news_refresh {stamp}] {message}", file=sys.stderr, flush=True)


def claude_bin() -> str:
    found = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not found:
        raise RuntimeError("claude CLI not found - set CLAUDE_BIN or add it to PATH")
    return found


def build_prompt(tid: str, name: str, start: dt.date, end: dt.date) -> str:
    return f"""Research news about {name} (ticker {tid}) published between {start:%Y-%m-%d} and {end:%Y-%m-%d} inclusive. Today is {end:%Y-%m-%d}.

Use web search. Return the at most {MAX_ITEMS_PER_TICKER} most material items for an equity investor: earnings, guidance, major products, deals, regulatory action, large analyst moves. Skip routine price commentary and listicles.

Hard rules:
- Every item's date must fall inside {start:%Y-%m-%d}..{end:%Y-%m-%d}. Drop anything older, however interesting. Check the publication date - do not trust an article's framing of "recent".
- The date is the day the news broke, not the day you read it. If a company reported after the market close, the date is the release day, not the reaction day.
- Every item needs a named source (publication or filing). No source, no item.
- State only what the source states. Do not assert that one fact caused another unless the source does.
- If nothing qualifies, return an empty array. An empty array is a correct answer and is much better than a padded one.

Keep the headline under 140 characters - it renders as one line in a table. Put the detail in "why".

Output a JSON array and nothing else - no prose, no markdown fence:
[{{"date": "YYYY-MM-DD", "headline": "one line, what happened", "why": "one line on why it matters for {tid}", "src": "publication or filing"}}]
"""


def run_claude(prompt: str) -> str:
    cmd = [
        claude_bin(),
        "-p",
        prompt,
        "--allowedTools",
        "WebSearch,WebFetch",
        "--output-format",
        "json",
    ]
    try:
        done = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT_S,
            cwd=str(REPO),
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"claude timed out after {CLAUDE_TIMEOUT_S}s")
    if done.returncode != 0:
        raise RuntimeError(f"claude exited {done.returncode}: {done.stderr.strip()[:300]}")
    # --output-format json wraps the answer; fall back to raw stdout if the
    # wrapper shape ever changes so a CLI update degrades instead of breaking.
    try:
        return str(json.loads(done.stdout).get("result", ""))
    except (json.JSONDecodeError, AttributeError):
        return done.stdout


def extract_json_array(text: str) -> list:
    start = text.find("[")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "[":
                depth += 1
            elif text[i] == "]":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(parsed, list):
                        return parsed
                    break
        start = text.find("[", start + 1)
    raise ValueError("no JSON array in model output")


def clean_items(tid: str, raw: list, start: dt.date, end: dt.date) -> list:
    """Drop anything unusable. A thin section beats a wrong one on a public page."""
    kept = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        date = str(entry.get("date", "")).strip()
        headline = str(entry.get("headline", "")).strip()
        src = str(entry.get("src", "")).strip()
        why = str(entry.get("why", "")).strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            log(f"  {tid}: dropped item, unparseable date {date!r}")
            continue
        try:
            parsed = dt.date.fromisoformat(date)
        except ValueError:
            log(f"  {tid}: dropped item, invalid date {date!r}")
            continue
        if not start <= parsed <= end:
            log(f"  {tid}: dropped item dated {date}, outside window")
            continue
        if not headline or not src:
            log(f"  {tid}: dropped item, missing headline or source")
            continue
        item = {"date": date, "headline": headline[:200]}
        if why:
            item["why"] = why[:400]
        item["src"] = src[:120]
        kept.append(item)
    kept.sort(key=lambda i: i["date"], reverse=True)
    return kept[:MAX_ITEMS_PER_TICKER]


def load_existing() -> dict:
    if not NEWS_PATH.exists():
        return {}
    try:
        doc = json.loads(NEWS_PATH.read_text())
    except json.JSONDecodeError as err:
        log(f"existing weekly-news.json is not valid JSON ({err}) - starting clean")
        return {}
    return doc.get("tickers", {}) if isinstance(doc, dict) else {}


def write_atomic(doc: dict) -> None:
    """Write via a temp file so a crash mid-write cannot truncate the live feed."""
    with tempfile.NamedTemporaryFile(
        "w", dir=str(NEWS_PATH.parent), suffix=".tmp", delete=False
    ) as handle:
        json.dump(doc, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        temp = pathlib.Path(handle.name)
    temp.replace(NEWS_PATH)


def research_one(tid: str, start: dt.date, end: dt.date) -> tuple[str, list | None]:
    """Returns (tid, items) or (tid, None) if this ticker could not be researched."""
    try:
        text = run_claude(build_prompt(tid, STOCK_PAGES[tid], start, end))
        items = clean_items(tid, extract_json_array(text), start, end)
    except (RuntimeError, ValueError) as err:
        log(f"{tid}: FAILED - {err}")
        return tid, None
    log(f"{tid}: {len(items)} item(s)")
    return tid, items


def refresh(only: list[str] | None, dry_run: bool, jobs: int) -> int:
    end = dt.datetime.now(HKT).date()
    start = end - dt.timedelta(days=WINDOW_DAYS)
    targets = [t for t in STOCK_PAGES if not only or t in only]
    if not targets:
        log(f"no matching tickers in --only {only}")
        return 2

    # Start from the previous run so one ticker's failure keeps yesterday's
    # items rather than blanking that page. Stale entries age out of the
    # 7-day window on their own.
    tickers = load_existing()
    ok, failed = [], []

    # Each ticker is an independent `claude -p` process taking ~2-3 minutes, so
    # the whole run is IO-bound - serially it is over 45 minutes for 18 names.
    # Keep the pool small; this shares a subscription with the other bots.
    log(f"researching {len(targets)} ticker(s) {jobs} at a time, window {start} to {end}")
    with cf.ThreadPoolExecutor(max_workers=jobs) as pool:
        results = list(pool.map(lambda t: research_one(t, start, end), targets))

    for tid, items in results:
        if items is None:
            failed.append(tid)
            continue
        if items:
            tickers[tid] = items
        else:
            tickers.pop(tid, None)
        ok.append(tid)

    if not ok:
        log("every ticker failed - leaving weekly-news.json untouched")
        return 1

    doc = {
        "_schema": SCHEMA_BLOCK,
        "generated": dt.datetime.now(HKT).replace(microsecond=0).isoformat(),
        "tickers": {t: tickers[t] for t in STOCK_PAGES if tickers.get(t)},
    }
    total = sum(len(v) for v in doc["tickers"].values())
    if dry_run:
        log(f"dry run - would write {total} item(s) across {len(doc['tickers'])} ticker(s)")
        print(json.dumps(doc, indent=2, ensure_ascii=False))
        return 0

    write_atomic(doc)
    log(f"wrote {NEWS_PATH.name}: {total} item(s) across {len(doc['tickers'])} ticker(s)")
    if failed:
        log(f"kept previous items for failed ticker(s): {', '.join(failed)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", help="comma-separated ticker subset")
    parser.add_argument("--dry-run", action="store_true", help="print, do not write")
    parser.add_argument("--jobs", type=int, default=DEFAULT_JOBS,
                        help=f"tickers researched concurrently (default {DEFAULT_JOBS})")
    args = parser.parse_args()
    only = [t.strip().upper() for t in args.only.split(",")] if args.only else None
    try:
        return refresh(only, args.dry_run, max(1, args.jobs))
    except RuntimeError as err:
        log(f"fatal: {err}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
