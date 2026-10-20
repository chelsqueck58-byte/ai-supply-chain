"""Investor focus refresh for the ai-supply-chain stock cards.

Writes investor-focus.json, which supply-chain.html renders at the TOP of
each Stock Card - the single specific thing investors are watching/debating
for that name right now, not a generic theme. Research runs through headless
Claude Code (`claude -p`) so it stays on the subscription rather than an API
key. Follows news_refresh.py's pattern (same repo, same hygiene bar).

Contract (also documented in investor-focus.json's own _schema key):

    {"generated": "<ISO ts>", "tickers": {"NVDA": {...focus...}, ...}}
    focus = {"focus": str, "source": str, "as_of": "YYYY-MM-DD"}

Run:  .venv/bin/python3 scripts/investor_focus.py
      .venv/bin/python3 scripts/investor_focus.py --only NVDA,AAPL
      .venv/bin/python3 scripts/investor_focus.py --dry-run
      .venv/bin/python3 scripts/investor_focus.py --jobs 1
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
OUT_PATH = REPO / "investor-focus.json"

CLAUDE_TIMEOUT_S = 420
DEFAULT_JOBS = 4
STALE_DAYS = 3  # a ticker researched within this window is skipped, not re-spent
HKT = dt.timezone(dt.timedelta(hours=8))

# Must stay in sync with STOCK_PAGES in supply-chain.html / news_refresh.py.
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
    "generated": "ISO timestamp of the refresh",
    "tickers": "map of ticker id -> focus object",
    "focus": {
        "focus": "one specific, concrete sentence on what investors are watching/debating for this name right now - a number, a date, a named risk, not a generic theme",
        "source": "publication, analyst note, or filing - required",
        "as_of": "YYYY-MM-DD, the date of the source material",
    },
}


def log(message: str) -> None:
    stamp = dt.datetime.now(HKT).strftime("%H:%M:%S")
    print(f"[investor_focus {stamp}] {message}", file=sys.stderr, flush=True)


def claude_bin() -> str:
    found = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not found:
        raise RuntimeError("claude CLI not found - set CLAUDE_BIN or add it to PATH")
    return found


def build_prompt(tid: str, name: str, today: dt.date) -> str:
    return f"""Today is {today:%Y-%m-%d}. You are an equity analyst covering {name} (ticker {tid}).

Use web search (recent earnings call, analyst notes, financial press - last ~30 days preferred) to find the SINGLE most specific thing investors are currently watching or debating about this stock. Not a generic theme ("AI growth", "China risk") - a CONCRETE, SPECIFIC point: a number to watch next print, a named risk with a date attached, a live debate with two sides, a metric that just changed direction. The kind of thing a trader would say in one breath to explain why the stock might move next.

Hard rules:
- Must be grounded in a real, named, dated source. No source, no answer.
- State only what the source states. Do not invent a number or date.
- Be specific: cite the actual figure, date, or named party where the source gives one.
- Keep "focus" under 220 characters - it renders as 1-2 lines at the top of a card.
- "source" should be short and concrete (e.g. "Reuters, Sep 12" or "Morgan Stanley note, Sep 10"), not a URL.

Output a JSON object and nothing else - no prose, no markdown fence:
{{"focus": "...", "source": "...", "as_of": "YYYY-MM-DD"}}
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
    try:
        return str(json.loads(done.stdout).get("result", ""))
    except (json.JSONDecodeError, AttributeError):
        return done.stdout


def extract_json_object(text: str) -> dict:
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(parsed, dict):
                        return parsed
                    break
        start = text.find("{", start + 1)
    raise ValueError("no JSON object in model output")


def clean_focus(tid: str, raw: dict, today: dt.date) -> dict | None:
    focus = str(raw.get("focus", "")).strip()
    source = str(raw.get("source", "")).strip()
    as_of = str(raw.get("as_of", "")).strip()
    if not focus or not source:
        log(f"  {tid}: dropped, missing focus or source")
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of):
        as_of = ""
    elif dt.date.fromisoformat(as_of) > today:
        log(f"  {tid}: dropped as_of date {as_of!r} in the future")
        as_of = ""
    return {"focus": focus[:260], "source": source[:100], "as_of": as_of}


def load_existing() -> dict:
    if not OUT_PATH.exists():
        return {}
    try:
        doc = json.loads(OUT_PATH.read_text())
    except json.JSONDecodeError as err:
        log(f"existing investor-focus.json is not valid JSON ({err}) - starting clean")
        return {}
    return doc.get("tickers", {}) if isinstance(doc, dict) else {}


def write_atomic(doc: dict) -> None:
    with tempfile.NamedTemporaryFile(
        "w", dir=str(OUT_PATH.parent), suffix=".tmp", delete=False
    ) as handle:
        json.dump(doc, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        temp = pathlib.Path(handle.name)
    temp.replace(OUT_PATH)


def research_one(tid: str, today: dt.date) -> tuple[str, dict | None]:
    try:
        text = run_claude(build_prompt(tid, STOCK_PAGES[tid], today))
        cleaned = clean_focus(tid, extract_json_object(text), today)
    except (RuntimeError, ValueError) as err:
        log(f"{tid}: FAILED - {err}")
        return tid, None
    log(f"{tid}: {'ok' if cleaned else 'empty'}")
    return tid, cleaned


def refresh(only: list[str] | None, dry_run: bool, jobs: int, force: bool) -> int:
    today = dt.datetime.now(HKT).date()
    tickers = load_existing()

    def is_fresh(tid: str) -> bool:
        if force:
            return False
        entry = tickers.get(tid)
        if not entry or not entry.get("_fetched"):
            return False
        try:
            age = (today - dt.date.fromisoformat(entry["_fetched"])).days
        except ValueError:
            return False
        return age < STALE_DAYS

    all_targets = [t for t in STOCK_PAGES if not only or t in only]
    targets = [t for t in all_targets if not is_fresh(t)]
    skipped = len(all_targets) - len(targets)
    if not all_targets:
        log(f"no matching tickers in --only {only}")
        return 2
    if not targets:
        log(f"all {len(all_targets)} target(s) still fresh (<{STALE_DAYS}d) - nothing to do")
        return 0

    log(f"researching {len(targets)} ticker(s) ({skipped} still fresh), {jobs} at a time")
    with cf.ThreadPoolExecutor(max_workers=jobs) as pool:
        results = list(pool.map(lambda t: research_one(t, today), targets))

    ok, failed = [], []
    for tid, cleaned in results:
        if cleaned is None:
            failed.append(tid)
            continue
        cleaned["_fetched"] = today.isoformat()
        tickers[tid] = cleaned
        ok.append(tid)

    if not ok and not skipped:
        log("every ticker failed - leaving investor-focus.json untouched")
        return 1

    doc = {
        "_schema": SCHEMA_BLOCK,
        "generated": dt.datetime.now(HKT).replace(microsecond=0).isoformat(),
        "tickers": {t: tickers[t] for t in STOCK_PAGES if tickers.get(t)},
    }
    if dry_run:
        log(f"dry run - would write {len(doc['tickers'])} ticker(s)")
        print(json.dumps(doc, indent=2, ensure_ascii=False))
        return 0

    write_atomic(doc)
    log(f"wrote {OUT_PATH.name}: {len(doc['tickers'])} ticker(s)")
    if failed:
        log(f"kept previous focus for failed ticker(s): {', '.join(failed)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", help="comma-separated ticker subset")
    parser.add_argument("--dry-run", action="store_true", help="print, do not write")
    parser.add_argument("--force", action="store_true", help="ignore freshness, re-research everyone in scope")
    parser.add_argument("--jobs", type=int, default=DEFAULT_JOBS,
                        help=f"tickers researched concurrently (default {DEFAULT_JOBS})")
    args = parser.parse_args()
    only = [t.strip().upper() for t in args.only.split(",")] if args.only else None
    try:
        return refresh(only, args.dry_run, max(1, args.jobs), args.force)
    except RuntimeError as err:
        log(f"fatal: {err}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
