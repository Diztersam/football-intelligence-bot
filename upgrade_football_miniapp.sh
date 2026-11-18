#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="${HOME}/football_intelligence_bot"
cd "$PROJECT_DIR" || {
  echo "ERROR: Project directory not found: $PROJECT_DIR"
  exit 1
}

STAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="backups/telegram_miniapp_upgrade_${STAMP}"
mkdir -p "$BACKUP_DIR"

FILES=(
  "bot.py"
  "miniapp/backend/main.py"
  "miniapp/frontend/src/App.jsx"
  "miniapp/frontend/index.html"
)
for file in "${FILES[@]}"; do
  if [[ ! -f "$file" ]]; then
    echo "ERROR: Required project file is missing: $file"
    exit 1
  fi
  mkdir -p "$BACKUP_DIR/$(dirname "$file")"
  cp -a "$file" "$BACKUP_DIR/$file"
done

echo "Backup created: $BACKUP_DIR"

cat > miniapp/backend/main.py <<'PY'
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
PY

cat > miniapp/frontend/src/App.jsx <<'JS'
import { useCallback, useEffect, useState } from 'react'
import {
  Activity, CalendarDays, RefreshCw, ShieldCheck,
  Target, AlertCircle, CheckCircle2, LoaderCircle, Trophy
} from 'lucide-react'

const API_BASE = (import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000').replace(/\/$/, '')
const telegram = window.Telegram?.WebApp
telegram?.ready?.()
telegram?.expand?.()

function formatKickoff(fixture) {
  const value = fixture.kickoff_lagos || fixture.kickoff_utc
  if (!value) return 'Kickoff time unavailable'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('en-NG', {
    timeZone: 'Africa/Lagos',
    weekday: 'short',
    day: 'numeric',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
    hour12: true
  }).format(date)
}

function percent(value) {
  const number = Number(value)
  return Number.isFinite(number) ? `${(number * 100).toFixed(1)}%` : '—'
}

function authHeaders() {
  const initData = telegram?.initData || ''
  return {
    'Content-Type': 'application/json',
    ...(initData ? { 'X-Telegram-Init-Data': initData } : {})
  }
}

function App() {
  const [fixtures, setFixtures] = useState([])
  const [predictions, setPredictions] = useState([])
  const [loading, setLoading] = useState(true)
  const [predicting, setPredicting] = useState(false)
  const [jobStatus, setJobStatus] = useState('idle')
  const [jobMessage, setJobMessage] = useState('Press Predict Matches to analyse up to five upcoming football fixtures.')
  const [error, setError] = useState('')

  const request = useCallback(async (path, options = {}) => {
    const response = await fetch(`${API_BASE}${path}`, {
      ...options,
      headers: { ...authHeaders(), ...(options.headers || {}) }
    })
    const data = await response.json().catch(() => ({}))
    if (!response.ok) {
      throw new Error(data.detail || `API request failed (${response.status})`)
    }
    return data
  }, [])

  const loadFixtures = useCallback(async () => {
    const data = await request('/api/fixtures?limit=50')
    setFixtures(Array.isArray(data.fixtures) ? data.fixtures : [])
  }, [request])

  const loadPredictions = useCallback(async () => {
    const data = await request('/api/predictions?limit=5')
    setPredictions(Array.isArray(data.predictions) ? data.predictions : [])
  }, [request])

  const refreshAll = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      await Promise.all([loadFixtures(), loadPredictions()])
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [loadFixtures, loadPredictions])

  useEffect(() => {
    refreshAll()
  }, [refreshAll])

  const pollJob = useCallback(async () => {
    try {
      const data = await request('/api/predict/status')
      setJobStatus(data.status || 'idle')
      setJobMessage(data.message || 'Checking prediction status…')
      if (data.status === 'completed') {
        setPredictions(Array.isArray(data.predictions) ? data.predictions : [])
        await loadFixtures()
        setPredicting(false)
      } else if (data.status === 'failed') {
        setPredicting(false)
        if (data.error) setError(data.error)
      }
    } catch (err) {
      setPredicting(false)
      setError(err.message)
    }
  }, [request, loadFixtures])

  useEffect(() => {
    if (!['queued', 'running'].includes(jobStatus)) return undefined
    const timer = window.setTimeout(pollJob, 2500)
    return () => window.clearTimeout(timer)
  }, [jobStatus, pollJob])

  async function predictMatches() {
    if (predicting) return
    setPredicting(true)
    setError('')
    setJobStatus('queued')
    setJobMessage('Sending your request to the football analysis engine…')
    try {
      const data = await request('/api/predict', { method: 'POST' })
      setJobStatus(data.status || 'queued')
      setJobMessage(data.message || 'Prediction job queued.')
    } catch (err) {
      setPredicting(false)
      setJobStatus('failed')
      setJobMessage('Could not start the prediction run.')
      setError(err.message)
    }
  }

  const isBusy = predicting || ['queued', 'running'].includes(jobStatus)

  return (
    <div className="min-h-screen bg-[#0b1512] text-[#e5eee9]">
      <header className="sticky top-0 z-20 flex h-16 items-center justify-between border-b border-white/10 bg-[#0b1512]/95 px-5 backdrop-blur md:px-8">
        <div className="flex items-center gap-3">
          <div className="grid h-9 w-9 place-items-center rounded-xl bg-emerald-500/15 text-emerald-400">
            <Activity size={21} />
          </div>
          <div>
            <h1 className="text-sm font-bold tracking-wide">
              FOOTBALL <span className="text-emerald-400">INTELLIGENCE</span>
            </h1>
            <p className="text-[9px] tracking-[2px] text-gray-500">MATCH PREDICTION APP</p>
          </div>
        </div>
        <span className="flex items-center gap-2 text-[10px] text-emerald-400">
          <span className="h-2 w-2 animate-pulse rounded-full bg-emerald-400" />
          FOOTBALL ONLY
        </span>
      </header>

      <main className="mx-auto max-w-5xl p-4 md:p-8">
        <section className="mb-7 rounded-2xl border border-emerald-500/15 bg-gradient-to-br from-[#14271e] to-[#101d17] p-5 md:p-7">
          <p className="mb-2 text-[10px] tracking-[2px] text-emerald-400">YOUR MATCH INTELLIGENCE</p>
          <h2 className="text-2xl font-bold md:text-3xl">Football predictions</h2>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-gray-400">
            Analyse upcoming football fixtures using your existing data and prediction engine.
            Analysis starts only when you press the button.
          </p>
          <button
            onClick={predictMatches}
            disabled={isBusy}
            className="mt-6 flex w-full items-center justify-center gap-3 rounded-xl bg-emerald-500 px-5 py-4 text-sm font-extrabold tracking-wide text-[#07110d] transition hover:bg-emerald-400 disabled:cursor-not-allowed disabled:opacity-60 sm:w-auto sm:min-w-64"
          >
            {isBusy ? <LoaderCircle size={19} className="animate-spin" /> : <Target size={19} />}
            {isBusy ? 'ANALYSIS RUNNING…' : 'PREDICT MATCHES'}
          </button>

          <div className="mt-4 flex items-start gap-2 text-xs text-gray-400">
            {jobStatus === 'completed'
              ? <CheckCircle2 size={15} className="mt-0.5 shrink-0 text-emerald-400" />
              : isBusy
              ? <LoaderCircle size={15} className="mt-0.5 shrink-0 animate-spin text-amber-300" />
              : <ShieldCheck size={15} className="mt-0.5 shrink-0 text-emerald-400" />}
            <span>{jobMessage}</span>
          </div>
        </section>

        {error && (
          <div className="mb-6 flex items-start gap-3 rounded-xl border border-red-500/20 bg-red-500/10 p-4 text-sm text-red-300">
            <AlertCircle size={18} className="mt-0.5 shrink-0" />
            <div className="min-w-0">
              <p className="font-semibold">Connection or analysis issue</p>
              <p className="mt-1 break-words text-xs">{error}</p>
              <button onClick={refreshAll} className="mt-3 text-xs underline">Retry connection</button>
            </div>
          </div>
        )}

        <section className="mb-7">
          <div className="mb-4 flex items-center justify-between gap-3">
            <div>
              <p className="text-[10px] tracking-[2px] text-emerald-400">FIXTURE LIST</p>
              <h3 className="mt-1 text-lg font-bold">Upcoming matches</h3>
            </div>
            <button
              onClick={refreshAll}
              disabled={loading}
              aria-label="Refresh fixtures"
              className="rounded-lg border border-white/10 p-2 text-gray-400 hover:border-emerald-500/40 hover:text-emerald-400 disabled:opacity-50"
            >
              <RefreshCw size={16} className={loading ? 'animate-spin' : ''} />
            </button>
          </div>

          {loading ? (
            <div className="rounded-xl border border-white/5 bg-[#111f19] py-10 text-center text-sm text-gray-500">
              <LoaderCircle size={22} className="mx-auto mb-3 animate-spin text-emerald-400" />
              Loading upcoming fixtures…
            </div>
          ) : fixtures.length === 0 ? (
            <div className="rounded-xl border border-white/5 bg-[#111f19] py-10 text-center text-sm text-gray-500">
              <Trophy size={24} className="mx-auto mb-3 opacity-50" />
              No upcoming football fixtures were found in the database.
            </div>
          ) : (
            <div className="space-y-3">
              {fixtures.map(fixture => (
                <article key={fixture.event_id} className="rounded-xl border border-white/5 bg-[#111f19] p-4">
                  <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                    <span className="text-[10px] tracking-wide text-emerald-400">{fixture.league || 'Football'}</span>
                    <span className="flex items-center gap-1 text-[10px] text-gray-500">
                      <CalendarDays size={12} /> {formatKickoff(fixture)}
                    </span>
                  </div>
                  <div className="flex items-center justify-between gap-3">
                    <span className="flex-1 text-sm font-semibold">{fixture.home_team}</span>
                    <span className="text-[10px] font-bold text-gray-600">VS</span>
                    <span className="flex-1 text-right text-sm font-semibold">{fixture.away_team}</span>
                  </div>
                </article>
              ))}
            </div>
          )}
        </section>

        <section>
          <div className="mb-4">
            <p className="text-[10px] tracking-[2px] text-emerald-400">ENGINE OUTPUT</p>
            <h3 className="mt-1 text-lg font-bold">Latest predictions</h3>
          </div>

          {predictions.length === 0 ? (
            <div className="rounded-xl border border-white/5 bg-[#111f19] p-5 text-sm text-gray-500">
              No saved predictions to display yet. Press <span className="font-semibold text-emerald-400">Predict Matches</span> to start a new run.
            </div>
          ) : (
            <div className="space-y-3">
              {predictions.map(item => (
                <article key={item.prediction_id} className="rounded-xl border border-emerald-500/10 bg-[#111f19] p-4 md:p-5">
                  <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                    <span className="text-[10px] tracking-wide text-emerald-400">{item.league}</span>
                    <span className="text-[10px] text-gray-500">{formatKickoff(item)}</span>
                  </div>
                  <h4 className="text-sm font-bold md:text-base">{item.home_team} <span className="mx-1 text-gray-600">vs</span> {item.away_team}</h4>
                  <div className="mt-4 grid grid-cols-3 gap-2">
                    <div className="rounded-lg bg-[#0b1512] p-3 text-center">
                      <p className="text-[9px] text-gray-500">HOME</p>
                      <p className="mt-1 text-sm font-bold">{percent(item.home_win)}</p>
                    </div>
                    <div className="rounded-lg bg-[#0b1512] p-3 text-center">
                      <p className="text-[9px] text-gray-500">DRAW</p>
                      <p className="mt-1 text-sm font-bold">{percent(item.draw_probability)}</p>
                    </div>
                    <div className="rounded-lg bg-[#0b1512] p-3 text-center">
                      <p className="text-[9px] text-gray-500">AWAY</p>
                      <p className="mt-1 text-sm font-bold">{percent(item.away_win)}</p>
                    </div>
                  </div>
                  <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs">
                    <span className="text-gray-400">Confidence: <strong className="text-emerald-300">{percent(item.confidence)}</strong></span>
                    <span className="rounded-md bg-emerald-400/10 px-2 py-1 text-emerald-300">
                      {item.best_candidate?.market || 'No qualifying selection'}
                    </span>
                  </div>
                  <p className="mt-2 text-[10px] text-gray-600">
                    Data quality: {item.data_quality || 'Not specified'} · Estimates, not guarantees.
                  </p>
                </article>
              ))}
            </div>
          )}
        </section>

        <p className="mt-8 text-center text-[10px] text-gray-600">
          FOOTBALL INTELLIGENCE · MAXIMUM FIVE MATCHES PER RUN · NO AUTOMATIC BETTING
        </p>
      </main>
    </div>
  )
}

export default App
JS

cat > miniapp/frontend/index.html <<'HTML'
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <meta name="theme-color" content="#0b1512" />
    <title>Football Intelligence</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.jsx"></script>
  </body>
</html>
HTML

cat > bot.py <<'PY'
#!/usr/bin/env python3
"""Telegram launcher for the Football Intelligence Mini App."""
import os
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"


def load_env():
    if not ENV_PATH.exists():
        return
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()
TOKEN = os.getenv("BOT_TOKEN", "").strip()
MINI_APP_URL = os.getenv("MINI_APP_URL", "").strip()
try:
    ALLOWED_ID = int(os.getenv("TELEGRAM_ALLOWED_USER_ID", "0"))
except ValueError:
    ALLOWED_ID = 0

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is missing from .env")
if not ALLOWED_ID:
    raise RuntimeError("TELEGRAM_ALLOWED_USER_ID is missing from .env")


def allowed(update: Update):
    return bool(update.effective_user and update.effective_user.id == ALLOWED_ID)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.effective_message.reply_text("Access denied. This is a private app.")
        return

    if not MINI_APP_URL.startswith("https://"):
        await update.effective_message.reply_text(
            "Your Mini App launcher is ready, but MINI_APP_URL is not configured.\n\n"
            "Add your public HTTPS Mini App URL to .env:\n"
            "MINI_APP_URL=https://your-domain.example\n\n"
            "Then restart this bot."
        )
        return

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "⚽ OPEN FOOTBALL PREDICTIONS",
            web_app=WebAppInfo(url=MINI_APP_URL),
        )
    ]])
    await update.effective_message.reply_text(
        "⚽ FOOTBALL INTELLIGENCE\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Your football prediction Mini App is ready.\n\n"
        "Open the app below and press Predict Matches when you want a new analysis.",
        reply_markup=keyboard,
    )


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        f"Your Telegram ID: {update.effective_user.id}"
    )


def main():
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("myid", myid))
    print("Football Intelligence Telegram Mini App launcher is running.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
PY

echo
echo "Checking Python syntax..."
python -m py_compile bot.py daily_report.py miniapp/backend/main.py prediction_engine.py

echo
if [[ -d miniapp/frontend/node_modules ]]; then
  echo "Building React Mini App..."
  (cd miniapp/frontend && npm run build)
else
  echo "NOTE: frontend/node_modules is missing; run npm install in miniapp/frontend, then npm run build."
fi

echo
echo "UPGRADE FILES INSTALLED."
echo "Backup: $BACKUP_DIR"
echo
echo "NEXT CONFIGURATION:"
echo "1) Add your public HTTPS frontend URL to .env as MINI_APP_URL=https://YOUR-FRONTEND-DOMAIN"
echo "2) Set VITE_API_BASE_URL=https://YOUR-BACKEND-DOMAIN in miniapp/frontend/.env.production"
echo "3) Set MINI_APP_ORIGINS=https://YOUR-FRONTEND-DOMAIN in .env"
echo "4) Restart backend and Telegram bot. Use one Uvicorn worker for this in-process job queue."
echo
echo "Local backend:"
echo "  source venv/bin/activate"
echo "  uvicorn main:app --app-dir miniapp/backend --host 0.0.0.0 --port 8000"
echo "Local frontend:"
echo "  cd miniapp/frontend && npm run dev"
echo "Telegram bot:"
echo "  cd ~/football_intelligence_bot && source venv/bin/activate && python bot.py"
