#!/usr/bin/env python3
"""Shared API-Football client with SQLite response caching and quota awareness."""
import json
import os
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "football.db"
BASE_URL = "https://v3.football.api-sports.io"

def load_env():
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

load_env()
API_KEY = os.getenv("API_FOOTBALL_KEY", "").strip() or os.getenv("APISPORTS_KEY", "").strip()

class APIFootballError(RuntimeError):
    pass

def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_cache (
            cache_key TEXT PRIMARY KEY,
            endpoint TEXT NOT NULL,
            params_json TEXT NOT NULL,
            response_json TEXT NOT NULL,
            fetched_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_usage (
            id INTEGER PRIMARY KEY CHECK (id=1),
            remaining INTEGER,
            daily_limit INTEGER,
            minute_remaining INTEGER,
            checked_at REAL
        )
    """)
    conn.commit()
    return conn

def get(endpoint, params=None, ttl=3600, force=False):
    if not API_KEY:
        raise APIFootballError(
            "API_FOOTBALL_KEY is missing. Add your API-Football key to the .env file."
        )
    params = params or {}
    clean_params = {k: v for k, v in params.items() if v is not None and v != ""}
    params_json = json.dumps(clean_params, sort_keys=True, separators=(",", ":"))
    cache_key = endpoint.strip("/") + "?" + params_json
    now = time.time()

    with _db() as conn:
        cached = conn.execute(
            "SELECT response_json, fetched_at FROM api_cache WHERE cache_key=?",
            (cache_key,)
        ).fetchone()
        if cached and not force and now - cached[1] < ttl:
            return json.loads(cached[0])

    url = f"{BASE_URL}/{endpoint.strip('/')}"
    try:
        response = requests.get(
            url,
            headers={"x-apisports-key": API_KEY, "Accept": "application/json"},
            params=clean_params,
            timeout=30
        )
    except requests.RequestException as exc:
        raise APIFootballError(f"API connection failed: {exc}") from exc

    if response.status_code == 429:
        raise APIFootballError("API rate limit reached. Wait for the quota to reset.")
    if response.status_code in (401, 403):
        raise APIFootballError(
            f"API authentication/permission error ({response.status_code}). Check the API key and plan."
        )
    if response.status_code >= 400:
        raise APIFootballError(f"API returned HTTP {response.status_code}: {response.text[:300]}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise APIFootballError("API returned invalid JSON.") from exc

    errors = payload.get("errors")
    if errors:
        raise APIFootballError(f"API reported an error: {errors}")

    headers = response.headers
    remaining = headers.get("x-ratelimit-requests-remaining")
    daily_limit = headers.get("x-ratelimit-requests-limit")
    minute_remaining = headers.get("X-RateLimit-Remaining")
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO api_cache(cache_key,endpoint,params_json,response_json,fetched_at) VALUES(?,?,?,?,?)",
            (cache_key, endpoint, params_json, json.dumps(payload), now)
        )
        conn.execute(
            "INSERT OR REPLACE INTO api_usage(id,remaining,daily_limit,minute_remaining,checked_at) VALUES(1,?,?,?,?)",
            (
                int(remaining) if remaining and remaining.isdigit() else None,
                int(daily_limit) if daily_limit and daily_limit.isdigit() else None,
                int(minute_remaining) if minute_remaining and minute_remaining.isdigit() else None,
                now
            )
        )
    return payload

def response_items(payload):
    return payload.get("response", []) if isinstance(payload, dict) else []

def usage_status():
    with _db() as conn:
        row = conn.execute(
            "SELECT remaining,daily_limit,minute_remaining,checked_at FROM api_usage WHERE id=1"
        ).fetchone()
    if not row:
        return "Quota status not yet retrieved from API."
    remaining, daily_limit, minute_remaining, checked = row
    age = int(time.time() - checked) if checked else None
    return (
        f"Daily requests remaining: {remaining if remaining is not None else 'unknown'}"
        f"/{daily_limit if daily_limit is not None else 'unknown'}\n"
        f"Minute requests remaining: {minute_remaining if minute_remaining is not None else 'unknown'}\n"
        f"Quota last updated: {age}s ago" if age is not None else "Quota status unavailable."
    )
