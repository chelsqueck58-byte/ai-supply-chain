# ai-supply-chain refresh scripts

Two files, both meant to run on the Mac mini from the repo checkout:

| script | what it does |
|---|---|
| `news_refresh.py` | researches the last 7 days per ticker via headless `claude -p` and writes `weekly-news.json` |
| `publish.py` | commits and pushes **only** generated data, never the front end |

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

## 2. Scheduling

`launchd` fires in the mini's local time, so HKT goes in directly — no UTC
conversion (that conversion is only needed for GitHub Actions cron).

`~/Library/LaunchAgents/com.chels.ai-supply-chain.news.plist`, daily 07:30 HKT:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>              <string>com.chels.ai-supply-chain.news</string>
  <key>WorkingDirectory</key>   <string>/Users/chels/ai-supply-chain</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-lc</string>
    <string>.venv/bin/python3 scripts/news_refresh.py &amp;&amp; .venv/bin/python3 scripts/publish.py -m "news refresh"</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>7</integer><key>Minute</key><integer>30</integer></dict>
  <key>StandardOutPath</key>    <string>/Users/chels/Library/Logs/ai-supply-chain-news.log</string>
  <key>StandardErrorPath</key>  <string>/Users/chels/Library/Logs/ai-supply-chain-news.log</string>
</dict>
</plist>
```

```bash
launchctl unload ~/Library/LaunchAgents/com.chels.ai-supply-chain.news.plist 2>/dev/null
launchctl load  ~/Library/LaunchAgents/com.chels.ai-supply-chain.news.plist
launchctl start com.chels.ai-supply-chain.news        # test it now
tail -f ~/Library/Logs/ai-supply-chain-news.log
```

`-lc` matters: it loads the login shell so `claude` is on `PATH`. If it still
isn't, set `CLAUDE_BIN=/Users/chels/.local/bin/claude` in the plist's
`EnvironmentVariables`.

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

## 4. Notes

- `news_refresh.py --only NVDA,AAPL` re-runs a subset; `--dry-run` prints without writing.
- Validation is deliberately strict: an item is dropped unless its date parses,
  falls inside the 7-day window, and carries a named source. A thin section
  beats a wrong one on a public page.
- If every ticker fails, `weekly-news.json` is left untouched and the exit code
  is 1, so the section keeps showing the last good data and flags itself stale
  rather than going blank.
- `STOCK_PAGES` in `news_refresh.py` must stay in sync with `STOCK_PAGES` in
  `supply-chain.html`.
