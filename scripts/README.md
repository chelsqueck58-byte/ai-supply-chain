# ai-supply-chain refresh scripts

Three files, all meant to run on the Mac mini from the repo checkout:

| script | what it does |
|---|---|
| `news_refresh.py` | researches the last 7 days per ticker via headless `claude -p` and writes `weekly-news.json` |
| `publish.py` | commits and pushes **only** generated data, never the front end |
| `install_news_job.sh` | one-shot: installs the daily launchd job for the two above |

## 1. One-time fix on the mini

The refresh job was staging everything (`git add -A`), so it pushed its own copy
of `supply-chain.html` alongside the data and silently reverted front-end edits
made anywhere else. Observed 2026-09-08: two `data refresh` commits restored the
file byte-for-byte to its pre-edit state and reverted the live site.

A plain `git merge` does **not** protect you here — the stale copy is a real diff
on those lines, so it wins every hunk it touches and only edits on untouched
lines survive. The fix is to stop the job publishing the front end at all.

```bash
cd ~/ai-supply-chain            # wherever the mini's checkout lives

# 1. take the current front end from GitHub, discarding the mini's stale copy
git fetch origin main
git checkout origin/main -- supply-chain.html index.html catalysts.html

# 2. get the new scripts
git pull --rebase origin main

# 3. confirm the guard works before trusting it
.venv/bin/python3 scripts/publish.py --dry-run
```

Then in `orchestrate.py`, **delete every `git add -A` / `git add .` / `git commit`
/ `git push`** and call `publish.py` instead (see §3).

## 2. Wiring in the daily news refresh

One command, on the mini, from the repo checkout:

```bash
git pull --rebase origin main
bash scripts/install_news_job.sh
```

That installs a launchd agent running `news_refresh.py` then `publish.py` daily
at 07:30 HKT (launchd fires in local time, so no UTC conversion — that is only
needed for GitHub Actions cron). It is idempotent: re-running unloads the old
copy first. It refuses to install if the venv, `claude`, or the scripts are
missing, because a launchd job that fails at 07:30 fails almost silently.

`CLAUDE_BIN` and `PATH` are written into the plist explicitly — launchd jobs get
no login shell, so an interactive-only PATH entry would leave `claude`
unfindable at run time.

```bash
launchctl start com.chels.ai-supply-chain.news     # run it now
tail -f ~/Library/Logs/ai-supply-chain-news.log    # watch it
```

This deliberately does **not** touch `orchestrate.py`. The news refresh runs on
its own timer and publishes through `publish.py`, so it cannot clobber the front
end. §3 is only needed if you would rather fold it into the existing pipeline.

## 3. Calling from orchestrate.py

If the news refresh should run as a stage of the existing pipeline rather than
on its own timer:

```python
import subprocess, sys, pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent
PY = str(REPO / ".venv" / "bin" / "python3")


def refresh_news() -> bool:
    """Research the last 7 days per ticker into weekly-news.json."""
    done = subprocess.run([PY, str(REPO / "scripts" / "news_refresh.py")])
    if done.returncode != 0:
        print("news refresh failed - keeping yesterday's items", file=sys.stderr)
    return done.returncode == 0


def publish(message: str) -> None:
    """Push generated data only. Never stages the front end."""
    subprocess.run([PY, str(REPO / "scripts" / "publish.py"), "-m", message], check=True)
```

Call `refresh_news()` alongside the other data stages, then `publish("data refresh")`
once at the end. A news failure is deliberately non-fatal: the previous run's
items stay in place and age out of the 7-day window on their own.

## 4. Telegram research needs a date

`data.json` carries forwarded research per instrument as `inst.tele`, currently
`{catalysts, fundamentals, historicals}` — with no date and no source. The page
renders it directly under a live price, so an undated note reads as today's
view even when it is months old.

Concrete case (2026-09-10): META's block cites "Muse Spark 1.1 API Launch" and
"Current $666, PT $775". Muse Spark 1.3 shipped 2026-09-02, and META last closed
near $666 on 2026-07-16 — so the note is roughly two months old and correct as
written. Rewriting "1.1" to "1.3" would falsify the analyst; dating it fixes the
problem properly.

The front end now reads two optional fields and labels the note undated when
they are missing:

    inst.tele = {
      "catalysts": "...", "fundamentals": "...", "historicals": "...",
      "date": "YYYY-MM-DD",   # Telegram message date, not the ingest date
      "src":  "Morgan Stanley"
    }

Notes older than 30 days get a red "56d old" flag. The ingestion step just needs
to carry the Telegram message date through — it already has it — plus the house
name where the note states one.

## 5. Notes

- `news_refresh.py --only NVDA,AAPL` re-runs a subset; `--dry-run` prints without writing.
- Validation is deliberately strict: an item is dropped unless its date parses,
  falls inside the 7-day window, and carries a named source. A thin section
  beats a wrong one on a public page.
- If every ticker fails, `weekly-news.json` is left untouched and the exit code
  is 1, so the section keeps showing the last good data and flags itself stale
  rather than going blank.
- `STOCK_PAGES` in `news_refresh.py` must stay in sync with `STOCK_PAGES` in
  `supply-chain.html`.
