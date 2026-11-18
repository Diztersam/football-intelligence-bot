from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock
from urllib.parse import parse_qsl
import hashlib
import hmac
import json
import os
import sqlite3
import logging

from fastapi import FastAPI, HTTPException, Query, Header, Request, Depends
from fastapi.middleware.cors import CORSMiddleware

BASE_DIR = Path(__file__).resolve().parents[2]
DATABASE = BASE_DIR / "football.db"
ENV_PATH = BASE_DIR / ".env"
log = logging.getLogger("football_miniapp_api")


def load_env_file():
    if not ENV_PATH.exists():
        return
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
try:
    ALLOWED_TELEGRAM_ID = int(os.getenv("TELEGRAM_ALLOWED_USER_ID", "0"))
except ValueError:
    ALLOWED_TELEGRAM_ID = 0

origins = [
    item.strip()
    for item in os.getenv("MINI_APP_ORIGINS", "").split(",")
    if item.strip()
]
origins.extend(["http://localhost:5173", "http://127.0.0.1:5173"])

# Make project-root modules available to the backend.
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

app = FastAPI(
    title="Football Intelligence API",
    description="Football-only prediction API for the Telegram Mini App",
    version="2.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(dict.fromkeys(origins)),
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Telegram-Init-Data"],
)

EXCLUDED_LEAGUE_PATTERNS = (
    "hockey", "darts", "basketball", "volleyball", "handball",
    "rugby", "cricket", "baseball", "table tennis", "badminton",
    "snooker", "esports", "e-sports", "motorsport", "boxing",
    "mma", "formula 1", "del ", "del2", "esp8266",
    "spiele mit arminia bielefeld beteiligung",
)


def is_football_league(league):
    value = (league or "").strip().lower()
    return bool(value) and not any(term in value for term in EXCLUDED_LEAGUE_PATTERNS)


def get_connection():
    if not DATABASE.exists():
        raise FileNotFoundError(f"Database not found: {DATABASE}")
    connection = sqlite3.connect(DATABASE, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def table_exists(connection, table):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def validate_telegram_init_data(init_data):
    if not BOT_TOKEN or not ALLOWED_TELEGRAM_ID or not init_data:
        return False
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = pairs.pop("hash", "")
        if not received_hash:
            return False

        data_check_string = "\n".join(
            f"{key}={value}" for key, value in sorted(pairs.items())
        )
        secret_key = hmac.new(
            key=b"WebAppData",
            msg=BOT_TOKEN.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        calculated_hash = hmac.new(
            key=secret_key,
            msg=data_check_string.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(calculated_hash, received_hash):
            return False

        auth_date = int(pairs.get("auth_date", "0"))
        now = int(datetime.now(timezone.utc).timestamp())
        if auth_date <= 0 or now - auth_date > 86400 or auth_date > now + 60:
            return False

        user = json.loads(pairs.get("user", "{}"))
        return int(user.get("id", 0)) == ALLOWED_TELEGRAM_ID
    except (ValueError, TypeError, json.JSONDecodeError):
        return False


def require_telegram_user(
    request: Request,
    telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    # Localhost-only development access. This does not permit remote clients.
    host = request.client.host if request.client else ""
    origin = request.headers.get("origin", "")
    local_request = host in ("127.0.0.1", "::1", "testclient") and (
        not origin or origin.startswith("http://localhost") or origin.startswith("http://127.0.0.1")
    )
    if local_request:
        return True
    if not validate_telegram_init_data(telegram_init_data or ""):
        raise HTTPException(status_code=401, detail="Open this app from your authorised Telegram account.")
    return True


job_lock = Lock()
executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="football-prediction")
job = {
    "status": "idle",
    "message": "Ready. Press Predict Matches to analyse up to five upcoming football fixtures.",
    "started_at": None,
    "finished_at": None,
    "error": None,
    "predictions": [],
}


def read_predictions(after_id=0, limit=5):
    if not DATABASE.exists():
        return []
    with get_connection() as connection:
        if not table_exists(connection, "prediction_runs"):
            return []
        rows = connection.execute(
            """
            SELECT prediction_id, event_id, league, home_team, away_team,
                   kickoff_utc, home_xg, away_xg, home_win,
                   draw_probability, away_win, btts_yes, over_25,
                   confidence, data_quality, news_risk, prediction_json, created_at
            FROM prediction_runs
            WHERE prediction_id > ?
            ORDER BY prediction_id DESC
            LIMIT ?
            """,
            (int(after_id), int(limit)),
        ).fetchall()

    output = []
    for row in rows:
        item = dict(row)
        if not is_football_league(item.get("league")):
            continue
        raw = item.pop("prediction_json", None)
        try:
            details = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            details = {}
        item["best_candidate"] = details.get("best_candidate")
        item["top_scores"] = details.get("top_scores", [])
        item["probabilities"] = details.get("probabilities", {})
        output.append(item)
    return output


def latest_prediction_id():
    with get_connection() as connection:
        if not table_exists(connection, "prediction_runs"):
            return 0
        row = connection.execute(
            "SELECT COALESCE(MAX(prediction_id), 0) FROM prediction_runs"
        ).fetchone()
        return int(row[0] or 0)


def run_prediction_job(baseline_id):
    global job
    with job_lock:
        job.update(
            status="running",
            message="The analysis engine is collecting fixtures, team data and news, then generating predictions.",
        )
    try:
        from daily_report import run_pipeline

        report = run_pipeline()
        if "An analysis is already running" in report:
            with job_lock:
                job.update(
                    status="failed",
                    message="Another analysis is already running. Wait for it to finish, then try again.",
                    error="Pipeline lock is already held.",
                    finished_at=datetime.now(timezone.utc).isoformat(),
                    predictions=[],
                )
            return

        predictions = read_predictions(after_id=baseline_id, limit=5)
        with job_lock:
            job.update(
                status="completed",
                message=(
                    f"Analysis finished. {len(predictions)} new football prediction(s) saved."
                    if predictions else
                    "Analysis finished, but the engine saved no new football predictions. Check the report and data quality."
                ),
                finished_at=datetime.now(timezone.utc).isoformat(),
                error=None,
                predictions=predictions,
            )
    except Exception as exc:
        log.exception("Prediction job failed")
        with job_lock:
            job.update(
                status="failed",
                message="The prediction run failed. Check the backend terminal logs.",
                error=str(exc)[:1000],
                finished_at=datetime.now(timezone.utc).isoformat(),
                predictions=[],
            )


@app.get("/")
def home():
    return {
        "name": "Football Intelligence API",
        "status": "online",
        "version": "2.0.0",
        "database_exists": DATABASE.exists(),
    }


@app.get("/api/health")
def health():
    return {"status": "online", "database_exists": DATABASE.exists()}


@app.get("/api/fixtures")
def get_fixtures(
    limit: int = Query(default=50, ge=1, le=100),
    _: bool = Depends(require_telegram_user),
):
    if not DATABASE.exists():
        raise HTTPException(status_code=503, detail="Football database is not available.")
    with get_connection() as connection:
        if not table_exists(connection, "fixtures"):
            raise HTTPException(status_code=503, detail="Fixtures table has not been created yet.")
        total = connection.execute(
            "SELECT COUNT(*) FROM fixtures WHERE julianday(kickoff_utc) >= julianday('now')"
        ).fetchone()[0]
        rows = connection.execute(
            """
            SELECT event_id, league, home_team, away_team, kickoff_utc,
                   kickoff_lagos, match_status,
                   CASE
                     WHEN event_id LIKE 'football-data:%' THEN 'football-data'
                     WHEN event_id LIKE 'openligadb:%' THEN 'openligadb'
                     ELSE 'other'
                   END AS source
            FROM fixtures
            WHERE julianday(kickoff_utc) >= julianday('now')
            ORDER BY julianday(kickoff_utc) ASC
            """
        ).fetchall()

    football_rows = [dict(row) for row in rows if is_football_league(row["league"])]
    return {
        "count": min(len(football_rows), limit),
        "available_after_filter": len(football_rows),
        "before_filter": total,
        "excluded": max(0, total - len(football_rows)),
        "fixtures": football_rows[:limit],
    }


@app.post("/api/predict")
def start_prediction(_: bool = Depends(require_telegram_user)):
    global job
    with job_lock:
        if job["status"] in ("queued", "running"):
            return dict(job)
        try:
            baseline_id = latest_prediction_id()
        except (sqlite3.Error, FileNotFoundError):
            baseline_id = 0
        job = {
            "status": "queued",
            "message": "Prediction request accepted. Preparing the analysis pipeline…",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "error": None,
            "predictions": [],
        }
        executor.submit(run_prediction_job, baseline_id)
        return dict(job)


@app.get("/api/predict/status")
def prediction_status(_: bool = Depends(require_telegram_user)):
    with job_lock:
        return dict(job)


@app.get("/api/predictions")
def get_predictions(
    limit: int = Query(default=5, ge=1, le=20),
    _: bool = Depends(require_telegram_user),
):
    try:
        return {"count": len(read_predictions(limit=limit)), "predictions": read_predictions(limit=limit)}
    except (sqlite3.Error, FileNotFoundError) as exc:
        raise HTTPException(status_code=503, detail=str(exc))
