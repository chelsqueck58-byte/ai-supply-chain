"""Publish generated data to GitHub Pages without clobbering the front end.

The problem this fixes: the refresh job used to stage everything (`git add -A`),
so it pushed its own copy of supply-chain.html alongside the data. Any front-end
edit made on another machine was silently reverted on the next refresh - and a
plain `git merge` does not save you, because the revert is a real diff on those
lines and wins every hunk it touches.

Two rules enforced here:
  1. Only GENERATED files are ever staged. The front end is never pushed by this
     job, whatever state the working copy is in.
  2. Local drift in front-end files is discarded before pushing, and the branch
     is rebased onto origin, so the mini always publishes on top of the newest
     front end instead of over it.

Run:  .venv/bin/python3 scripts/publish.py -m "data refresh"
      .venv/bin/python3 scripts/publish.py --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
BRANCH = "main"
HKT = dt.timezone(dt.timedelta(hours=8))

# Everything this job is allowed to publish. Anything tracked and not listed
# here belongs to the front end and is restored, never staged.
GENERATED = [
    "catalyst-calendar.json", "catalyst-deep.json", "circular-financing.json",
    "data.json", "deal-map.json", "deep-financials.json", "deep-fundamentals.json",
    "delivery-war.json", "earnings-dates.json", "forward-pe.json", "fundamentals.json",
    "fx-rates.json", "geo-mix.json", "historical-pe.json", "model-launches.json",
    "moves.json", "nvda-debt-financing.json", "nvda-partnerships.json",
    "off-bs-liabilities.json", "revenue-breakdown.json", "roic.json",
    "stock-page-extras.json", "supply-chain-map.json", "weekly-news.json",
]


def log(message: str) -> None:
    stamp = dt.datetime.now(HKT).strftime("%H:%M:%S")
    print(f"[publish {stamp}] {message}", file=sys.stderr, flush=True)


def git(*args: str, check: bool = True) -> str:
    done = subprocess.run(
        ["git", "-C", str(REPO), *args], capture_output=True, text=True
    )
    if check and done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {done.stderr.strip()[:300]}")
    return done.stdout.strip()


def tracked_files() -> list[str]:
    return [line for line in git("ls-files").splitlines() if line]


def restore_frontend() -> None:
    """Throw away this machine's copy of anything it does not generate.

    Uses `git diff --name-only` rather than `status --porcelain`: porcelain
    lines carry a two-column status prefix, and stripping the output shifts the
    first line's path by one character, so the guard silently misses the very
    file it exists to protect.
    """
    protected = set(tracked_files()) - set(GENERATED)
    unstaged = [f for f in git("diff", "--name-only").splitlines() if f in protected]
    staged = [f for f in git("diff", "--name-only", "--cached").splitlines() if f in protected]
    dirty = sorted(set(unstaged) | set(staged))
    if not dirty:
        return
    log(f"discarding local drift in front-end file(s): {', '.join(dirty)}")
    if staged:
        git("reset", "--quiet", "HEAD", "--", *staged)
    git("checkout", "--", *dirty)


def sync_with_remote() -> None:
    git("fetch", "origin", BRANCH)
    behind = git("rev-list", "--count", f"HEAD..origin/{BRANCH}")
    if behind == "0":
        return
    log(f"{behind} new commit(s) on origin/{BRANCH} - rebasing onto them")
    git("rebase", f"origin/{BRANCH}")


def stage_generated() -> list[str]:
    present = [f for f in GENERATED if (REPO / f).exists()]
    if present:
        git("add", "--", *present)
    changed = git("diff", "--cached", "--name-only")
    return [line for line in changed.splitlines() if line]


def publish(message: str, dry_run: bool) -> int:
    restore_frontend()
    sync_with_remote()
    staged = stage_generated()
    if not staged:
        log("no data changes to publish")
        return 0

    log(f"staged {len(staged)} file(s): {', '.join(staged)}")
    if dry_run:
        log("dry run - not committing or pushing")
        git("reset", check=False)
        return 0

    git("commit", "-m", message)
    try:
        git("push", "origin", BRANCH)
    except RuntimeError:
        # Someone pushed between the fetch and the push. Rebase once and retry;
        # a second failure is a real problem and should page a human.
        log("push rejected - rebasing once and retrying")
        git("fetch", "origin", BRANCH)
        git("rebase", f"origin/{BRANCH}")
        git("push", "origin", BRANCH)
    log(f"pushed to origin/{BRANCH}: {git('rev-parse', '--short', 'HEAD')}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-m", "--message", default="data refresh", help="commit message")
    parser.add_argument("--dry-run", action="store_true", help="stage and report, do not push")
    args = parser.parse_args()
    try:
        return publish(args.message, args.dry_run)
    except RuntimeError as err:
        log(f"fatal: {err}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
