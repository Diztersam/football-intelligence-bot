#!/usr/bin/env python3
"""
Multi-source historical team analysis for Football Intelligence Bot.

Sources:
  - football-data.org v4 (requires FOOTBALL_DATA_TOKEN in project .env)
  - OpenLigaDB (public API)

Stores completed matches in the existing football.db match_results table.
No API-Football dependency.

Examples:
  python team_analysis.py --collect
  python team_analysis.py --collect --days 365
  python team_analysis.py --team "Arsenal"
  python team_analysis.py --fixtures
  python team_analysis.py --stats
"""

import argparse
import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "football.db"
ENV_PATH = BASE_DIR / ".env"

FOOTBALL_DATA_BASE = "https://api.football-data.org/v4"
OPENLIGADB_BASE = "https://www.openligadb.de/api"
FOOTBALL_DATA_CODES = [
    "BSA", "ELC", "PL", "CL", "EC", "FL1", "BL1",
    "SA", "DED", "PPL", "CLI", "PD", "WC",
]
REQUEST_TIMEOUT = 30
FOOTBALL_DATA_DELAY = 6.5
OPENLIGADB_DELAY = 0.35

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("team_analysis")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def read_env_value(key):
    """Read a simple KEY=value entry without relying on dotenv auto-discovery."""
    if not ENV_PATH.exists():
        return None
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == key:
            value = value.strip().strip('"').strip("'")
            return value or None
    return None


def get_football_data_token():
    try:
        import os
        token = os.environ.get("FOOTBALL_DATA_TOKEN")
    except Exception:
        token = None
    return token or read_env_value("FOOTBALL_DATA_TOKEN")


def request_json(url, headers=None, timeout=REQUEST_TIMEOUT):
    req_headers = {"User-Agent": "FootballIntelligenceBot/2.0", "Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    request = Request(url, headers=req_headers)
    with urlopen(request, timeout=timeout) as response:
        payload = response.read()
        if not payload:
            return {}
        return json.loads(payload.decode("utf-8"))


def connect_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema():
    with connect_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS match_results (
                event_id TEXT PRIMARY KEY,
                league TEXT,
                league_shortcut TEXT,
                season INTEGER,
                home_team TEXT,
                away_team TEXT,
                home_goals INTEGER,
                away_goals INTEGER,
                kickoff_utc TEXT,
                source TEXT,
                source_url TEXT,
                collected_at TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_results_home ON match_results(home_team, kickoff_utc)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_results_away ON match_results(away_team, kickoff_utc)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_results_date ON match_results(kickoff_utc)")
        conn.commit()


def selected_fixtures(limit=5):
    with connect_db() as conn:
        try:
            rows = conn.execute("""
                SELECT * FROM fixtures
                WHERE kickoff_utc IS NOT NULL
                ORDER BY kickoff_utc ASC
                LIMIT ?
            """, (limit,)).fetchall()
        except sqlite3.OperationalError as exc:
            raise RuntimeError("Fixtures table is missing. Run fixtures.py first.") from exc
    return [dict(row) for row in rows]


def normalise_name(value):
    value = (value or "").casefold()
    value = re.sub(r"\b(fc|cf|afc|sc|ac|club)\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def same_team(a, b):
    na, nb = normalise_name(a), normalise_name(b)
    return bool(na and nb and (na == nb or na in nb or nb in na))


def parse_iso(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return str(value)


def save_result(event_id, league, shortcut, season, home, away, hg, ag,
                kickoff, source, source_url):
    if not all((event_id, home, away)) or hg is None or ag is None:
        return False
    try:
        hg, ag = int(hg), int(ag)
    except (TypeError, ValueError):
        return False
    if hg < 0 or ag < 0:
        return False
    with connect_db() as conn:
        conn.execute("""
            INSERT INTO match_results (
                event_id, league, league_shortcut, season, home_team, away_team,
                home_goals, away_goals, kickoff_utc, source, source_url, collected_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                league=excluded.league,
                league_shortcut=excluded.league_shortcut,
                season=excluded.season,
                home_team=excluded.home_team,
                away_team=excluded.away_team,
                home_goals=excluded.home_goals,
                away_goals=excluded.away_goals,
                kickoff_utc=excluded.kickoff_utc,
                source=excluded.source,
                source_url=excluded.source_url,
                collected_at=excluded.collected_at
        """, (
            str(event_id), league, str(shortcut or ""), season, home, away,
            hg, ag, parse_iso(kickoff), source, source_url, utc_now()
        ))
    return True


def collect_football_data(days=365, selected_teams=None, max_competitions=None):
    token = get_football_data_token()
    if not token:
        log.warning("FOOTBALL_DATA_TOKEN not found in %s; skipping football-data.org.", ENV_PATH)
        return 0, 0
    date_to = datetime.now(timezone.utc).date()
    date_from = date_to - timedelta(days=days)
    codes = FOOTBALL_DATA_CODES[:max_competitions] if max_competitions else FOOTBALL_DATA_CODES
    total, successful = 0, 0

    for index, code in enumerate(codes):
        url = (
            f"{FOOTBALL_DATA_BASE}/competitions/{quote(code)}/matches"
            f"?dateFrom={date_from.isoformat()}&dateTo={date_to.isoformat()}"
        )
        try:
            payload = request_json(url, {"X-Auth-Token": token})
            successful += 1
            competition = payload.get("competition") or {}
            season_info = payload.get("filters") or {}
            matches = payload.get("matches") or []
            saved_here = 0
            for match in matches:
                if match.get("status") not in ("FINISHED", "AWARDED"):
                    continue
                home = (match.get("homeTeam") or {}).get("name")
                away = (match.get("awayTeam") or {}).get("name")
                score = match.get("score") or {}
                full_time = score.get("fullTime") or {}
                if full_time.get("home") is None or full_time.get("away") is None:
                    continue
                if selected_teams and not any(
                    same_team(candidate, home) or same_team(candidate, away)
                    for candidate in selected_teams
                ):
                    continue
                comp = competition.get("name") or code
                season = (match.get("season") or {}).get("startDate")
                season_year = int(season[:4]) if season and season[:4].isdigit() else None
                match_id = match.get("id")
                if save_result(
                    f"fd:{match_id}", comp, code, season_year, home, away,
                    full_time["home"], full_time["away"], match.get("utcDate"),
                    "football-data.org",
                    f"https://www.football-data.org/matches/{match_id}"
                ):
                    saved_here += 1
            total += saved_here
            log.info("%s: %d completed matches saved", code, saved_here)
        except HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:250]
            except Exception:
                pass
            log.warning("%s request failed (HTTP %s): %s", code, exc.code, body)
        except (URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            log.warning("%s request failed: %s", code, exc)
        if index < len(codes) - 1:
            time.sleep(FOOTBALL_DATA_DELAY)
    return total, successful


def collect_openligadb(days=365, selected_teams=None):
    """Discover current/previous league shortcuts and collect finished results."""
    date_from = datetime.now(timezone.utc).date() - timedelta(days=days)
    date_to = datetime.now(timezone.utc).date()
    try:
        leagues = request_json(f"{OPENLIGADB_BASE}/getavailableleagues")
    except Exception as exc:
        log.warning("OpenLigaDB league discovery failed: %s", exc)
        return 0, 0

    candidates = []
    current_year = datetime.now(timezone.utc).year
    for league in leagues if isinstance(leagues, list) else []:
        shortcut = league.get("leagueShortcut")
        season = league.get("leagueSeason")
        if not shortcut or not season:
            continue
        candidates.append((str(shortcut), int(season), league.get("leagueName") or str(shortcut)))

    # Keep one most recent season for each shortcut.
    latest = {}
    for shortcut, season, name in candidates:
        if shortcut not in latest or season > latest[shortcut][0]:
            latest[shortcut] = (season, name)

    total, successful = 0, 0
    for index, (shortcut, (season, league_name)) in enumerate(sorted(latest.items())):
        try:
            matches = request_json(
                f"{OPENLIGADB_BASE}/getmatchdata/{quote(shortcut)}/{season}"
            )
            successful += 1
            saved_here = 0
            for match in matches if isinstance(matches, list) else []:
                kickoff = parse_iso(match.get(" matchDateTimeUTC") or match.get("matchDateTimeUTC"))
                if not kickoff:
                    continue
                try:
                    match_date = datetime.fromisoformat(kickoff.replace("Z", "+00:00")).date()
                except ValueError:
                    continue
                if match_date < date_from or match_date > date_to:
                    continue
                if match.get("matchIsFinished") is not True:
                    continue
                team1 = match.get("team1") or {}
                team2 = match.get("team2") or {}
                home, away = team1.get("teamName"), team2.get("teamName")
                if selected_teams and not any(
                    same_team(candidate, home) or same_team(candidate, away)
                    for candidate in selected_teams
                ):
                    continue
                results = match.get("matchResults") or []
                final = next(
                    (r for r in reversed(results)
                     if (r.get("resultTypeID") == 2 or str(r.get("resultName", "")).lower() == "end result")),
                    results[-1] if results else None
                )
                if not final:
                    continue
                match_id = match.get("matchID")
                if save_result(
                    f"oldb:{match_id}", league_name, shortcut, season, home, away,
                    final.get("pointsTeam1"), final.get("pointsTeam2"), kickoff,
                    "OpenLigaDB", f"https://www.openligadb.de/api/getmatchdata/{shortcut}/{season}"
                ):
                    saved_here += 1
            total += saved_here
            log.info("OpenLigaDB %s %s: %d completed matches saved", shortcut, season, saved_here)
        except HTTPError as exc:
            log.warning("OpenLigaDB %s failed (HTTP %s)", shortcut, exc.code)
        except Exception as exc:
            log.warning("OpenLigaDB %s failed: %s", shortcut, exc)
        if index < len(latest) - 1:
            time.sleep(OPENLIGADB_DELAY)
    return total, successful


def get_team_matches(team_name, limit=10):
    with connect_db() as conn:
        rows = conn.execute("""
            SELECT * FROM match_results
            WHERE lower(home_team)=lower(?) OR lower(away_team)=lower(?)
            ORDER BY kickoff_utc DESC
            LIMIT ?
        """, (team_name, team_name, limit)).fetchall()
    # If exact name matching finds nothing, use conservative normalized matching.
    if not rows:
        with connect_db() as conn:
            all_rows = conn.execute("""
                SELECT * FROM match_results
                ORDER BY kickoff_utc DESC
            """).fetchall()
        rows = [
            row for row in all_rows
            if same_team(team_name, row["home_team"]) or same_team(team_name, row["away_team"])
        ][:limit]
    return [dict(row) for row in rows]


def analyse_matches(team_name, matches):
    played = wins = draws = losses = goals_for = goals_against = 0
    home_played = home_wins = away_played = away_wins = clean_sheets = failed_to_score = 0
    form = []
    for match in matches:
        is_home = same_team(team_name, match["home_team"])
        scored = match["home_goals"] if is_home else match["away_goals"]
        conceded = match["away_goals"] if is_home else match["home_goals"]
        if scored is None or conceded is None:
            continue
        played += 1
        goals_for += scored
        goals_against += conceded
        if is_home:
            home_played += 1
        else:
            away_played += 1
        if scored > conceded:
            wins += 1
            form.append("W")
            if is_home:
                home_wins += 1
            else:
                away_wins += 1
        elif scored == conceded:
            draws += 1
            form.append("D")
        else:
            losses += 1
            form.append("L")
        if conceded == 0:
            clean_sheets += 1
        if scored == 0:
            failed_to_score += 1
    if not played:
        return None
    return {
        "played": played, "wins": wins, "draws": draws, "losses": losses,
        "goals_for": goals_for, "goals_against": goals_against,
        "gf_avg": goals_for / played, "ga_avg": goals_against / played,
        "win_pct": wins / played * 100, "clean_sheets": clean_sheets,
        "failed_to_score": failed_to_score, "form": "".join(form),
        "home_played": home_played, "home_wins": home_wins,
        "away_played": away_played, "away_wins": away_wins,
    }


def head_to_head(team_name, opponent, limit=10):
    with connect_db() as conn:
        rows = conn.execute("""
            SELECT * FROM match_results
            WHERE (lower(home_team)=lower(?) AND lower(away_team)=lower(?))
               OR (lower(home_team)=lower(?) AND lower(away_team)=lower(?))
            ORDER BY kickoff_utc DESC LIMIT ?
        """, (team_name, opponent, opponent, team_name, limit)).fetchall()
    return [dict(row) for row in rows]


def print_team_report(team_name, limit=10):
    matches = get_team_matches(team_name, limit)
    stats = analyse_matches(team_name, matches)
    print("\n" + "=" * 64)
    print(f"TEAM PERFORMANCE: {team_name}")
    print("=" * 64)
    if not stats:
        print("No completed historical matches found for this team.")
        print("Collect history first with: python team_analysis.py --collect")
        return
    print(f"Sample: last {stats['played']} completed matches")
    print(f"Form (newest first): {stats['form']}")
    print(f"Record: {stats['wins']}W - {stats['draws']}D - {stats['losses']}L")
    print(f"Win rate: {stats['win_pct']:.1f}%")
    print(f"Goals: {stats['goals_for']} scored ({stats['gf_avg']:.2f}/match), "
          f"{stats['goals_against']} conceded ({stats['ga_avg']:.2f}/match)")
    print(f"Clean sheets: {stats['clean_sheets']} | Failed to score: {stats['failed_to_score']}")
    print(f"Home: {stats['home_wins']} wins from {stats['home_played']} matches")
    print(f"Away: {stats['away_wins']} wins from {stats['away_played']} matches")
    if stats["played"] < min(5, limit):
        print("DATA WARNING: Small sample; treat these statistics as low confidence.")
    print("\nRECENT RESULTS")
    for match in matches[:min(10, limit)]:
        print(f"  {str(match.get('kickoff_utc') or '')[:10]} | "
              f"{match['home_team']} {match['home_goals']}-{match['away_goals']} {match['away_team']} "
              f"| {match.get('league') or 'Unknown league'} [{match.get('source') or 'Unknown source'}]")


def print_fixture_analysis(limit=5):
    fixtures = selected_fixtures(limit)
    if not fixtures:
        print("No upcoming fixtures found.")
        return
    for fixture in fixtures:
        home, away = fixture["home_team"], fixture["away_team"]
        print("\n" + "#" * 64)
        print(f"{home} vs {away}")
        print(f"League: {fixture.get('league') or 'Unknown'}")
        print(f"Kickoff: {fixture.get('kickoff_lagos') or fixture.get('kickoff_utc') or 'Unknown'}")
        print_team_report(home, 10)
        print_team_report(away, 10)
        h2h = head_to_head(home, away, 10)
        print(f"\nHEAD-TO-HEAD: {len(h2h)} stored meetings")
        for match in h2h[:5]:
            print(f"  {str(match.get('kickoff_utc') or '')[:10]} | "
                  f"{match['home_team']} {match['home_goals']}-{match['away_goals']} {match['away_team']}")


def print_database_stats():
    with connect_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM match_results").fetchone()[0]
        sources = conn.execute("""
            SELECT COALESCE(source, 'Unknown') AS source, COUNT(*) AS n
            FROM match_results GROUP BY source ORDER BY n DESC
        """).fetchall()
        teams = conn.execute("""
            SELECT COUNT(DISTINCT team_name) FROM (
                SELECT home_team AS team_name FROM match_results
                UNION SELECT away_team AS team_name FROM match_results
            )
        """).fetchone()[0]
    print(f"Historical matches stored: {count}")
    print(f"Distinct teams represented: {teams}")
    print("By source:")
    for row in sources:
        print(f"  {row['source']}: {row['n']}")


def collect_historical_results(days=365, fixture_limit=5, max_competitions=None):
    ensure_schema()
    fixtures = selected_fixtures(fixture_limit)
    if not fixtures:
        raise RuntimeError("No upcoming fixtures found. Run fixtures.py first.")
    teams = sorted({
        name for fixture in fixtures
        for name in (fixture.get("home_team"), fixture.get("away_team"))
        if name
    })
    print("=" * 64)
    print("MULTI-SOURCE HISTORICAL RESULTS COLLECTION")
    print(f"Target fixtures: {len(fixtures)} | Target teams: {len(teams)}")
    print(f"Historical window: {days} days")
    print("Collecting competition history, then matching it to target teams.")
    fd_saved, fd_ok = collect_football_data(days, teams, max_competitions)
    oldb_saved, oldb_ok = collect_openligadb(days, teams)
    print("\nCOLLECTION SUMMARY")
    print(f"football-data.org: {fd_saved} rows saved/updated; {fd_ok} competition requests succeeded")
    print(f"OpenLigaDB: {oldb_saved} rows saved/updated; {oldb_ok} league requests succeeded")
    print(f"Total rows processed: {fd_saved + oldb_saved}")
    print("\nTEAM REPORTS")
    for team in teams:
        print_team_report(team, 10)
    print_database_stats()


def main():
    parser = argparse.ArgumentParser(description="Multi-source historical football team analysis")
    parser.add_argument("--collect", action="store_true", help="Collect historical results for teams in the next fixtures")
    parser.add_argument("--team", help="Print stored analysis for a team")
    parser.add_argument("--fixtures", action="store_true", help="Analyse teams in the next five fixtures")
    parser.add_argument("--stats", action="store_true", help="Show historical database counts")
    parser.add_argument("--days", type=int, default=365, help="Historical window in days (default: 365)")
    parser.add_argument("--limit", type=int, default=5, help="Maximum upcoming fixtures (default: 5)")
    parser.add_argument("--competitions", type=int, help="Limit football-data.org competition requests for a quick test")
    args = parser.parse_args()

    ensure_schema()
    try:
        if args.collect:
            collect_historical_results(
                days=max(30, min(args.days, 730)),
                fixture_limit=max(1, min(args.limit, 5)),
                max_competitions=max(1, args.competitions) if args.competitions else None,
            )
        elif args.team:
            print_team_report(args.team, 10)
        elif args.fixtures:
            print_fixture_analysis(max(1, min(args.limit, 5)))
        elif args.stats:
            print_database_stats()
        else:
            parser.print_help()
    except (RuntimeError, sqlite3.Error) as exc:
        raise SystemExit(f"TEAM ANALYSIS FAILED: {exc}")


if __name__ == "__main__":
    main()
