#!/usr/bin/env python3
"""Fast manual-only football prediction pipeline: maximum five matches."""

import fcntl
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PYTHON = sys.executable
DB_FILE = BASE_DIR / "football.db"
REPORT_FILE = BASE_DIR / "latest_report.txt"
LOCK_FILE = BASE_DIR / ".pipeline.lock"

TOTAL_BUDGET = 55

def notify(callback, message):
    if callback:
        try:
            callback(message)
        except Exception:
            pass

def upcoming_fixture_count():
    """Check whether the database already has five upcoming fixtures."""
    if not DB_FILE.exists():
        return 0

    try:
        with sqlite3.connect(DB_FILE, timeout=3) as con:
            columns = {
                row[1] for row in con.execute(
                    "PRAGMA table_info(fixtures)"
                ).fetchall()
            }

            if "kickoff_utc" not in columns:
                return 0

            row = con.execute("""
                SELECT COUNT(*)
                FROM fixtures
                WHERE julianday(kickoff_utc) >= julianday('now')
                  AND julianday(kickoff_utc) <= julianday('now', '+7 days')
            """).fetchone()

            return min(int(row[0] or 0), 5)
    except Exception:
        return 0

def run_command(title, command, timeout, callback, deadline):
    remaining = deadline - time.monotonic()
    allowed = min(timeout, max(0, remaining - 1))

    if allowed < 1:
        return False, f"{title}: skipped; time budget exhausted."

    notify(callback, f"{title} — processing (maximum five matches)...")
    started = time.monotonic()

    process = None

    try:
        process = subprocess.Popen(
            [PYTHON] + command,
            cwd=BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            start_new_session=True,
        )

        output, _ = process.communicate(timeout=allowed)
        elapsed = round(time.monotonic() - started, 1)

        if process.returncode == 0:
            return True, f"{title}: completed in {elapsed}s.\n{output[-2500:]}"

        return False, (
            f"{title}: exited with code {process.returncode} "
            f"after {elapsed}s.\n{output[-1500:]}"
        )

    except subprocess.TimeoutExpired:
        if process:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                output, _ = process.communicate(timeout=2)
            except Exception:
                output = ""

        elapsed = round(time.monotonic() - started, 1)
        return False, (
            f"{title}: stopped after its {allowed:.0f}s time limit "
            f"({elapsed}s elapsed). Using available stored data.\n"
            f"{str(output)[-1000:]}"
        )

    except Exception as exc:
        return False, f"{title}: {exc}"

def run_pipeline(progress_callback=None):
    started = time.monotonic()
    deadline = started + TOTAL_BUDGET
    report = []

    with open(LOCK_FILE, "w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "An analysis is already running. Please wait."

        report.append("FOOTBALL INTELLIGENCE — FAST PREDICTION")
        report.append("Maximum matches: 5")
        report.append("Execution budget: 55 seconds")
        report.append("")

        # 1. Avoid unnecessary external fixture collection.
        count = upcoming_fixture_count()

        if count >= 5:
            message = (
                "Fixture collection: skipped; five upcoming fixtures "
                "are already available locally."
            )
            report.append(message)
            notify(progress_callback, message)
        else:
            ok, result = run_command(
                "Fixture collection",
                ["fixtures.py", "--days", "7", "--limit", "5"],
                10,
                progress_callback,
                deadline,
            )
            report.append(result)

        # 2. Local team analysis only; no historical data downloads.
        ok, result = run_command(
            "Team performance analysis",
            ["team_analysis.py", "--fixtures", "--limit", "5"],
            5,
            progress_callback,
            deadline,
        )
        report.append(result)

        # 3. News is restricted to five fixtures and given a short time limit.
        ok, result = run_command(
            "Team news collection",
            ["news_analysis.py", "--collect", "--limit", "5"],
            8,
            progress_callback,
            deadline,
        )
        report.append(result)

        # 4. Generate predictions for no more than five fixtures.
        ok, result = run_command(
            "Prediction engine",
            ["prediction_engine.py", "--days", "7", "--limit", "5"],
            24,
            progress_callback,
            deadline,
        )
        report.append(result)

        elapsed = round(time.monotonic() - started, 1)
        report.append("")
        report.append(f"Total execution time: {elapsed}s")

        if elapsed >= TOTAL_BUDGET:
            report.append("Execution budget reached.")

        final_report = "\n\n".join(report)

        REPORT_FILE.write_text(final_report, encoding="utf-8")
        notify(progress_callback, f"Prediction process finished in {elapsed}s.")

        return final_report

if __name__ == "__main__":
    print(run_pipeline())
