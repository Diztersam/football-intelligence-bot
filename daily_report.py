#!/usr/bin/env python3
"""Manual-only pipeline: select five fixtures, analyse their teams/news, then predict."""
import fcntl
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent
PYTHON = sys.executable
REPORT_FILE = BASE_DIR / "latest_report.txt"
LOCK_FILE = BASE_DIR / ".pipeline.lock"
LAGOS = ZoneInfo("Africa/Lagos")
TIMEOUT = 1800

def notify(callback, message):
    if callback:
        try:
            callback(message)
        except Exception:
            pass

def run_step(title, command, callback, number, total):
    notify(callback, f"⚽ FOOTBALL INTELLIGENCE\n\n{number}/{total}  {title}\n\nProcessing…")
    started = time.monotonic()
    result = subprocess.run(
        [PYTHON] + command, cwd=BASE_DIR, capture_output=True,
        text=True, timeout=TIMEOUT, check=False
    )
    elapsed = round(time.monotonic() - started, 1)
    return {
        "title": title, "command": " ".join(command),
        "returncode": result.returncode, "elapsed": elapsed,
        "stdout": result.stdout.strip(), "stderr": result.stderr.strip()
    }

def run_pipeline(progress_callback=None):
    with open(LOCK_FILE, "w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "An analysis is already running. Please wait."

        started = datetime.now(LAGOS)
        steps = [
            ("Fixture shortlist", ["fixtures.py", "--days", "7", "--limit", "5"]),
            ("Team performance", ["team_analysis.py", "--collect", "--limit", "10"]),
            ("Team news", ["news_analysis.py", "--collect", "--limit", "5"]),
            ("Prediction engine", ["prediction_engine.py", "--days", "7", "--limit", "5"]),
        ]
        results = []
        total = len(steps)
        for index, (title, command) in enumerate(steps, 1):
            try:
                item = run_step(title, command, progress_callback, index, total)
            except subprocess.TimeoutExpired:
                item = {"title": title, "command": " ".join(command), "returncode": -1,
                        "elapsed": TIMEOUT, "stdout": "", "stderr": "Step timed out."}
            except Exception as exc:
                item = {"title": title, "command": " ".join(command), "returncode": -1,
                        "elapsed": 0, "stdout": "", "stderr": str(exc)}
            results.append(item)
            notify(progress_callback, f"{'✅' if item['returncode']==0 else '❌'} {index}/{total} {title}\n"
                                     f"Completed in {item['elapsed']}s")
            if item["returncode"] != 0:
                break
            if index == 1 and not item["stdout"]:
                break

        finished = datetime.now(LAGOS)
        lines = [
            "⚽ FOOTBALL INTELLIGENCE | ANALYSIS REPORT",
            "=" * 48,
            f"Generated: {finished.strftime('%d %b %Y, %I:%M %p WAT')}",
            f"Duration: {(finished-started).total_seconds()/60:.1f} minutes",
            "Fixture scope: maximum five selected upcoming fixtures",
            "",
            "PIPELINE STATUS",
            "-" * 48,
        ]
        for item in results:
            lines.append(f"{'SUCCESS' if item['returncode']==0 else 'FAILED'} | {item['title']} | {item['elapsed']}s")
        for item in results:
            lines.extend(["", "=" * 48, item["title"].upper(), "=" * 48])
            if item["stdout"]:
                lines.append(item["stdout"][-14000:])
            if item["stderr"]:
                lines.extend(["DIAGNOSTICS:", item["stderr"][-3000:]])
        lines.extend([
            "", "MODEL & DATA DISCLAIMERS",
            "Predictions are estimates, not guarantees or financial advice.",
            "Fewer than five recommendations may be produced if data quality is inadequate.",
            "No bets are placed automatically. This model requires backtesting before performance claims.",
        ])
        report = "\n".join(lines)
        REPORT_FILE.write_text(report, encoding="utf-8")
        notify(progress_callback, "✅ Analysis complete. Preparing your report…")
        return report

if __name__ == "__main__":
    print(run_pipeline())
