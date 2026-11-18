#!/usr/bin/env python3
"""
Football Intelligence Bot - Multi-source fixture collector

Sources:
  1. football-data.org API v4 (requires FOOTBALL_DATA_TOKEN in .env)
  2. OpenLigaDB public API (no API key)

Writes the existing fixtures schema in football.db so the current
team_analysis.py, news_analysis.py and prediction_engine.py can continue
reading fixtures without schema changes.

Coverage is limited to competitions supported by each provider/account.
"""

import argparse
import logging
import os
import re
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


# ---------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DATABASE = BASE_DIR / "football.db"
ENV_FILE = BASE_DIR / ".env"

OPENLIGA_URL = "https://api.openligadb.de"
FOOTBALL_DATA_URL = "https://api.football-data.org/v4"

LAGOS = ZoneInfo("Africa/Lagos")
REQUEST_TIMEOUT = 30
OPENLIGA_DELAY = 1.15

# Keep requests below the football-data.org free-tier rate ceiling.
# A delay is applied between every football-data.org request.
FOOTBALL_DATA_DELAY = 6.5

USER_AGENT = "FootballIntelligenceBot/3.0 (personal football research project)"

# These are the 13 competition codes returned successfully by this user's
# authenticated /competitions test. A competition can still return no
# matches for a particular date window or account permission.
FOOTBALL_DATA_COMPETITIONS = [
    ("BSA", "Campeonato Brasileiro Série A"),
    ("ELC", "Championship"),
    ("PL", "Premier League"),
    ("CL", "UEFA Champions League"),
    ("EC", "European Championship"),
    ("FL1", "Ligue 1"),
    ("BL1", "Bundesliga"),
    ("SA", "Serie A"),
    ("DED", "Eredivisie"),
    ("PPL", "Primeira Liga"),
    ("CLI", "Copa Libertadores"),
    ("PD", "Primera Division"),
    ("WC", "FIFA World Cup"),
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("football_fixtures")


# ---------------------------------------------------------------------
# ENVIRONMENT AND HTTP
# ---------------------------------------------------------------------

def load_environment():
    """Load .env explicitly; avoids dotenv auto-discovery edge cases."""
    if load_dotenv is not None:
        load_dotenv(dotenv_path=ENV_FILE, override=False)
        return

    # Small fallback parser so the collector can still read .env if
    # python-dotenv is not installed.
    if not ENV_FILE.exists():
        return

    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def create_session():
    session = requests.Session()
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        backoff_factor=1.5,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET"],
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    return session


SESSION = create_session()


def utc_now():
    return datetime.now(timezone.utc)


def parse_utc(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def request_json(url, headers=None, params=None):
    try:
        response = SESSION.get(
            url,
            headers=headers or {},
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return response.json(), response.status_code
    except requests.HTTPError as exc:
        response = exc.response
        status = response.status_code if response is not None else "HTTP"
        body = response.text[:300] if response is not None else str(exc)
        logger.warning("Request failed (%s): %s | %s", status, url, body)
    except requests.RequestException as exc:
        logger.warning("Network request failed: %s | %s", url, exc)
    except ValueError:
        logger.warning("Invalid JSON returned by %s", url)
    return None, None


# ---------------------------------------------------------------------
# DATABASE - SAME SCHEMA USED BY THE EXISTING PROJECT
# ---------------------------------------------------------------------

def create_database():
    with sqlite3.connect(DATABASE) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS fixtures (
                event_id TEXT PRIMARY KEY,
                league TEXT NOT NULL,
                home_team TEXT NOT NULL,
                away_team TEXT NOT NULL,
                kickoff_utc TEXT NOT NULL,
                kickoff_lagos TEXT NOT NULL,
                match_status TEXT,
                source_url TEXT NOT NULL,
                collected_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_fixtures_kickoff
            ON fixtures(kickoff_utc)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS collection_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                competition TEXT,
                season TEXT,
                source TEXT,
                status TEXT,
                fixtures_found INTEGER,
                collected_at TEXT
            )
        """)


def log_collection(competition, season, source, status, count=0):
    with sqlite3.connect(DATABASE) as conn:
        conn.execute("""
            INSERT INTO collection_log
            (competition, season, source, status, fixtures_found, collected_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            competition,
            str(season or ""),
            source,
            status,
            int(count),
            utc_now().isoformat(),
        ))


# ---------------------------------------------------------------------
# NORMALIZATION AND DEDUPLICATION
# ---------------------------------------------------------------------

def normalize_team(name):
    value = unicodedata.normalize("NFKD", str(name or ""))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower()
    value = re.sub(r"\b(fc|cf|afc|sc|club|deportivo)\b", " ", value)
    return re.sub(r"[^a-z0-9]+", "", value)


def fixture_identity(fixture):
    kickoff = parse_utc(fixture.get("kickoff_utc"))
    if kickoff is None:
        return None
    # Exact UTC minute plus normalized home/away names.
    minute = kickoff.strftime("%Y%m%d%H%M")
    return (
        minute,
        normalize_team(fixture.get("home_team")),
        normalize_team(fixture.get("away_team")),
    )


def deduplicate_fixtures(fixtures):
    """Keep one copy when both providers report the same matchup."""
    chosen = {}
    for fixture in fixtures:
        identity = fixture_identity(fixture)
        if identity is None:
            continue
        current = chosen.get(identity)
        # Prefer football-data.org when duplicate records match.
        if current is None or str(fixture.get("event_id", "")).startswith("football-data:"):
            chosen[identity] = fixture
    return list(chosen.values())


# ---------------------------------------------------------------------
# FOOTBALL-DATA.ORG
# ---------------------------------------------------------------------

def fetch_football_data_competition(code, name, start_date, end_date, token):
    endpoint = f"{FOOTBALL_DATA_URL}/competitions/{code}/matches"
    headers = {"X-Auth-Token": token}
    params = {
        "dateFrom": start_date.isoformat(),
        "dateTo": end_date.isoformat(),
    }

    data, status = request_json(endpoint, headers=headers, params=params)
    if not isinstance(data, dict):
        status_text = f"HTTP_{status}" if status else "FAILED"
        log_collection(name, code, "football-data.org", status_text, 0)
        return None

    matches = data.get("matches", [])
    if not isinstance(matches, list):
        log_collection(name, code, "football-data.org", "INVALID_RESPONSE", 0)
        return None

    now = utc_now()
    fixtures = []

    for match in matches:
        if not isinstance(match, dict):
            continue

        kickoff = parse_utc(match.get("utcDate"))
        home = (match.get("homeTeam") or {}).get("name")
        away = (match.get("awayTeam") or {}).get("name")
        match_id = match.get("id")

        if kickoff is None or kickoff <= now or not home or not away or match_id is None:
            continue

        kickoff_lagos = kickoff.astimezone(LAGOS)
        competition_name = (
            (match.get("competition") or {}).get("name")
            or name
        )
        season = (match.get("season") or {}).get("startDate", "")
        status_value = match.get("status") or "SCHEDULED"

        fixtures.append({
            "event_id": f"football-data:{match_id}",
            "league": f"{competition_name} ({season[:4]})" if season else competition_name,
            "home_team": home,
            "away_team": away,
            "kickoff_utc": kickoff.isoformat(),
            "kickoff_lagos": kickoff_lagos.strftime("%Y-%m-%d %H:%M"),
            "match_status": status_value,
            "source_url": f"{FOOTBALL_DATA_URL}/matches/{match_id}",
        })

    log_collection(name, code, "football-data.org", "SUCCESS", len(fixtures))
    return fixtures


def collect_football_data(days, limit=0):
    token = os.getenv("FOOTBALL_DATA_TOKEN", "").strip()
    if not token:
        logger.error("FOOTBALL_DATA_TOKEN is missing from %s", ENV_FILE)
        return [], 1, 0

    start_date = utc_now().date()
    end_date = start_date + timedelta(days=days)
    competitions = FOOTBALL_DATA_COMPETITIONS[:limit] if limit else FOOTBALL_DATA_COMPETITIONS

    print(f"\nFOOTBALL-DATA.ORG: {len(competitions)} competitions")
    print(f"Date window: {start_date} to {end_date}")

    all_fixtures = []
    successful = 0
    failed = 0

    for index, (code, name) in enumerate(competitions, start=1):
        print(f"[football-data.org {index}/{len(competitions)}] {name} ({code})")
        result = fetch_football_data_competition(
            code, name, start_date, end_date, token
        )
        if result is None:
            failed += 1
        else:
            successful += 1
            all_fixtures.extend(result)

        if index < len(competitions):
            time.sleep(FOOTBALL_DATA_DELAY)

    return all_fixtures, successful, failed


# ---------------------------------------------------------------------
# OPENLIGADB
# ---------------------------------------------------------------------

def openligadb_get(endpoint):
    url = f"{OPENLIGA_URL}/{endpoint.lstrip('/')}"
    data, _ = request_json(url)
    return data


def discover_openligadb_competitions():
    current_year = utc_now().year
    discovered = {}

    for year in (current_year, current_year - 1):
        print(f"Discovering OpenLigaDB competitions for {year}...")
        leagues = openligadb_get(f"getavailableleagues/{year}")

        if isinstance(leagues, list):
            for league in leagues:
                if not isinstance(league, dict):
                    continue
                shortcut = league.get("leagueShortcut")
                season = league.get("leagueSeason")
                if not shortcut or season is None:
                    continue
                key = (str(shortcut), str(season))
                discovered[key] = {
                    "shortcut": str(shortcut),
                    "season": str(season),
                    "name": league.get("leagueName") or str(shortcut),
                }
        else:
            logger.warning("Could not retrieve OpenLigaDB competitions for %s", year)

        time.sleep(OPENLIGA_DELAY)

    latest = {}
    for competition in discovered.values():
        shortcut = competition["shortcut"]
        try:
            season_number = int(competition["season"])
        except ValueError:
            continue
        if shortcut not in latest or season_number > int(latest[shortcut]["season"]):
            latest[shortcut] = competition

    return sorted(latest.values(), key=lambda item: item["name"].lower())


def is_football_competition(name):
    """Reject known non-football OpenLigaDB competitions."""
    value = str(name or "").lower()

    blocked = (
        "darts", "hockey", "basketball", "volleyball",
        "handball", "rugby", "cricket", "baseball",
        "table tennis", "badminton", "snooker",
        "esports", "e-sports", "motorsport"
    )

    return bool(value.strip()) and not any(
        term in value for term in blocked
    )


def fetch_openligadb_competition(competition, days):
    if not is_football_competition(competition.get("name")):
        print(f"SKIPPED non-football competition: {competition.get('name')}")
        return []

    shortcut = competition["shortcut"]
    season = competition["season"]
    endpoint = f"getmatchdata/{shortcut}/{season}"
    matches = openligadb_get(endpoint)

    if not isinstance(matches, list):
        log_collection(
            competition["name"], season, "OpenLigaDB",
            "FAILED" if matches is None else "INVALID_RESPONSE", 0
        )
        return None

    now = utc_now()
    cutoff = now + timedelta(days=days)
    fixtures = []

    for match in matches:
        if not isinstance(match, dict) or match.get("matchID") is None:
            continue

        kickoff = parse_utc(match.get("matchDateTimeUTC"))
        if kickoff is None or not (now < kickoff <= cutoff):
            continue

        team1 = match.get("team1") or {}
        team2 = match.get("team2") or {}
        home = team1.get("teamName")
        away = team2.get("teamName")
        if not home or not away:
            continue

        kickoff_lagos = kickoff.astimezone(LAGOS)
        league_name = match.get("leagueName") or competition["name"]
        season_name = match.get("leagueSeason") or season

        fixtures.append({
            "event_id": f"openligadb:{match['matchID']}",
            "league": f"{league_name} ({season_name})",
            "home_team": home,
            "away_team": away,
            "kickoff_utc": kickoff.isoformat(),
            "kickoff_lagos": kickoff_lagos.strftime("%Y-%m-%d %H:%M"),
            "match_status": "SCHEDULED",
            "source_url": f"{OPENLIGA_URL}/{endpoint}",
        })

    log_collection(competition["name"], season, "OpenLigaDB", "SUCCESS", len(fixtures))
    return fixtures


def collect_openligadb(days, limit=0):
    competitions = discover_openligadb_competitions()
    if limit:
        competitions = competitions[:limit]

    print(f"\nOPENLIGADB: {len(competitions)} discovered competitions")
    all_fixtures = []
    successful = 0
    failed = 0

    for index, competition in enumerate(competitions, start=1):
        print(
            f"[OpenLigaDB {index}/{len(competitions)}] "
            f"{competition['name']} ({competition['season']})"
        )
        result = fetch_openligadb_competition(competition, days)
        if result is None:
            failed += 1
        else:
            successful += 1
            all_fixtures.extend(result)
        time.sleep(OPENLIGA_DELAY)

    return all_fixtures, successful, failed


# ---------------------------------------------------------------------
# SAVE, READ AND REPORT
# ---------------------------------------------------------------------

def save_fixtures(fixtures):
    if not fixtures:
        return 0

    collected_at = utc_now().isoformat()
    saved = 0

    with sqlite3.connect(DATABASE) as conn:
        for fixture in fixtures:
            conn.execute("""
                INSERT INTO fixtures (
                    event_id, league, home_team, away_team,
                    kickoff_utc, kickoff_lagos, match_status,
                    source_url, collected_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    league = excluded.league,
                    home_team = excluded.home_team,
                    away_team = excluded.away_team,
                    kickoff_utc = excluded.kickoff_utc,
                    kickoff_lagos = excluded.kickoff_lagos,
                    match_status = excluded.match_status,
                    source_url = excluded.source_url,
                    collected_at = excluded.collected_at
            """, (
                fixture["event_id"],
                fixture["league"],
                fixture["home_team"],
                fixture["away_team"],
                fixture["kickoff_utc"],
                fixture["kickoff_lagos"],
                fixture["match_status"],
                fixture["source_url"],
                collected_at,
            ))
            saved += 1

    return saved


def get_upcoming_fixtures(days=14):
    start = utc_now()
    end = start + timedelta(days=days)
    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT *
            FROM fixtures
            WHERE kickoff_utc > ? AND kickoff_utc <= ?
            ORDER BY kickoff_utc ASC
        """, (start.isoformat(), end.isoformat())).fetchall()
    return [dict(row) for row in rows]


def display_report(days, raw_count, deduped_count, saved_count,
                   fd_success, fd_failed, ol_success, ol_failed):
    upcoming = get_upcoming_fixtures(days)

    print("\n" + "=" * 65)
    print("              FOOTBALL INTELLIGENCE")
    print("              FIXTURE COLLECTION REPORT")
    print("=" * 65)
    print(f"Football-data.org competitions: {fd_success} successful, {fd_failed} failed")
    print(f"OpenLigaDB competitions:        {ol_success} successful, {ol_failed} failed")
    print(f"Raw fixtures received:          {raw_count}")
    print(f"After cross-source deduplication: {deduped_count}")
    print(f"Rows inserted/updated:          {saved_count}")
    print(f"Upcoming fixtures in {days} days: {len(upcoming)}")

    print("\nUPCOMING MATCHES")
    print("-" * 65)
    if not upcoming:
        print("No upcoming fixtures found in the selected date window.")

    for index, match in enumerate(upcoming, start=1):
        print(f"\n{index}. {match['home_team']} vs {match['away_team']}")
        print(f"   Competition: {match['league']}")
        print(f"   Kick-off:   {match['kickoff_lagos']} WAT")
        print(f"   Status:     {match.get('match_status') or 'Unknown'}")
        source = "football-data.org" if str(match["event_id"]).startswith("football-data:") else "OpenLigaDB"
        print(f"   Source:     {source}")

    print("\nNOTES")
    print("- Fixture coverage is limited to the leagues exposed by these providers.")
    print("- A successful API connection does not guarantee every competition has fixtures in this date window.")
    print("- Historical team statistics are still collected by the existing OpenLigaDB team_analysis.py module.")
    print("- No fixtures or predictions are guaranteed to be complete.")
    print(f"\nDatabase: {DATABASE}")
    print("=" * 65)


# ---------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Collect upcoming football fixtures from football-data.org and OpenLigaDB"
    )
    parser.add_argument("--days", type=int, default=14,
                        help="Number of days ahead to collect/display (default: 14)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Limit competitions per provider for testing; 0 means all")
    parser.add_argument("--source", choices=("all", "football-data", "openligadb"),
                        default="all", help="Choose provider(s), default: all")
    args = parser.parse_args()

    if args.days < 1 or args.days > 365:
        parser.error("--days must be between 1 and 365")
    if args.limit < 0:
        parser.error("--limit cannot be negative")

    load_environment()
    create_database()

    print("\nStarting Multi-source Football Fixture Collector")
    print("Timezone: Africa/Lagos")
    print(f"Database: {DATABASE}")
    print(f"Collection window: next {args.days} days")

    all_fixtures = []
    fd_success = fd_failed = 0
    ol_success = ol_failed = 0

    if args.source in ("all", "football-data"):
        fd_fixtures, fd_success, fd_failed = collect_football_data(args.days, args.limit)
        all_fixtures.extend(fd_fixtures)

    if args.source in ("all", "openligadb"):
        ol_fixtures, ol_success, ol_failed = collect_openligadb(args.days, args.limit)
        all_fixtures.extend(ol_fixtures)

    deduped = deduplicate_fixtures(all_fixtures)
    saved = save_fixtures(deduped)

    display_report(
        args.days,
        len(all_fixtures),
        len(deduped),
        saved,
        fd_success,
        fd_failed,
        ol_success,
        ol_failed,
    )

    # Exit non-zero only when all requested providers failed completely.
    requested_failed = (
        (args.source in ("all", "football-data") and fd_success == 0)
        and
        (args.source in ("all", "openligadb") and ol_success == 0)
    ) if args.source == "all" else (
        fd_success == 0 if args.source == "football-data" else ol_success == 0
    )

    if requested_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
