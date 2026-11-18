#!/usr/bin/env python3
"""
Football Prediction Engine
Stage 5

Inputs:
    football.db -> fixtures
    football.db -> match_results
    football.db -> team_news

Model:
    Recency-weighted team attack/defence
    Home/away venue adjustment
    League scoring baseline
    Poisson score distribution
    1X2, Double Chance, Goals, BTTS and correct scores

Important:
    - No betting odds are assumed.
    - No corners predictions without corner data.
    - News is a risk flag, not automatic proof.
    - Insufficient data means NO BET.
    - Maximum five suggested selections.
"""

import argparse
import json
import logging
import math
import re
import sqlite3
import unicodedata

from datetime import datetime, timezone, timedelta
from pathlib import Path


# =========================================================
# CONFIGURATION
# =========================================================

DB_FILE = Path(__file__).resolve().parent / "football.db"

RECENCY_DECAY = 0.90
MAX_HISTORY = 10
MAX_SCORE = 10
MAX_SELECTIONS = 5

MIN_MATCHES = 5
MIN_VENUE_MATCHES = 2

# Probability thresholds are screening rules, not guarantees.
THRESHOLDS = {
    "Home win": 0.64,
    "Draw": 0.32,
    "Away win": 0.64,
    "Home or Draw": 0.79,
    "Away or Draw": 0.79,
    "Home or Away": 0.82,
    "Over 1.5 Goals": 0.78,
    "Under 1.5 Goals": 0.48,
    "Over 2.5 Goals": 0.68,
    "Under 2.5 Goals": 0.68,
    "Over 3.5 Goals": 0.55,
    "Under 3.5 Goals": 0.78,
    "BTTS - Yes": 0.68,
    "BTTS - No": 0.70,
}

# The maximum allowed probability adjustment is deliberately zero.
# News currently changes risk/confidence only, not expected goals.
NEWS_GOAL_ADJUSTMENT = 0.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("prediction_engine")


# =========================================================
# DATABASE
# =========================================================

def connect_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def create_tables():
    with connect_db() as conn:

        conn.execute("""
            CREATE TABLE IF NOT EXISTS prediction_runs (
                prediction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT,
                league TEXT,
                home_team TEXT,
                away_team TEXT,
                kickoff_utc TEXT,
                home_xg REAL,
                away_xg REAL,
                home_win REAL,
                draw_probability REAL,
                away_win REAL,
                btts_yes REAL,
                over_25 REAL,
                confidence REAL,
                data_quality TEXT,
                news_risk TEXT,
                prediction_json TEXT,
                created_at TEXT
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_prediction_event
            ON prediction_runs(event_id)
        """)


def table_exists(conn, table_name):
    row = conn.execute("""
        SELECT name
        FROM sqlite_master
        WHERE type = 'table' AND name = ?
    """, (table_name,)).fetchone()

    return row is not None


# =========================================================
# NORMALISATION
# =========================================================

def normalise_name(value):
    if not value:
        return ""

    value = unicodedata.normalize("NFKD", str(value))

    value = "".join(
        c for c in value
        if not unicodedata.combining(c)
    )

    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)

    return " ".join(value.split())


def parse_date(value):
    if not value:
        return None

    try:
        dt = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc)

    except (ValueError, TypeError):
        return None


def utc_now():
    return datetime.now(timezone.utc).isoformat()


# =========================================================
# LOAD DATABASE DATA
# =========================================================

def load_results():
    with connect_db() as conn:

        if not table_exists(conn, "match_results"):
            raise RuntimeError(
                "match_results table missing. "
                "Run team_analysis.py --collect first."
            )

        rows = conn.execute("""
            SELECT *
            FROM match_results
            WHERE home_goals IS NOT NULL
              AND away_goals IS NOT NULL
              AND kickoff_utc IS NOT NULL
            ORDER BY kickoff_utc DESC
        """).fetchall()

    return [dict(row) for row in rows]


def is_football_fixture(fixture):
    """Reject known non-football competitions."""
    league = normalise_name(fixture.get("league", ""))

    blocked = (
        "darts", "hockey", "basketball", "volleyball",
        "handball", "rugby", "cricket", "baseball",
        "table tennis", "badminton", "snooker",
        "esports", "e sports", "motorsport", "boxing",
        "mma", "formula 1"
    )

    return bool(league) and not any(
        normalise_name(term) in league for term in blocked
    )


def load_fixtures(days=14, limit=5):
    """Load a maximum of five upcoming football fixtures."""
    hard_limit = max(1, min(int(limit), 5))

    with connect_db() as conn:
        if not table_exists(conn, "fixtures"):
            raise RuntimeError(
                "fixtures table missing. Run fixtures.py first."
            )

        rows = conn.execute("""
            SELECT *
            FROM fixtures
            WHERE kickoff_utc IS NOT NULL
            ORDER BY julianday(kickoff_utc) ASC
        """).fetchall()

    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=max(1, int(days)))
    fixtures = []

    for row in rows:
        fixture = dict(row)
        kickoff = parse_date(fixture.get("kickoff_utc"))

        if kickoff is None:
            continue

        if not (now <= kickoff <= cutoff):
            continue

        if not is_football_fixture(fixture):
            continue

        fixtures.append(fixture)

        if len(fixtures) >= hard_limit:
            break

    return fixtures

def league_baseline(results, league_name):
    target = normalise_name(league_name)

    league_matches = [
        r for r in results
        if normalise_name(r.get("league")) == target
    ]

    # Avoid pretending a tiny competition sample is reliable.
    if len(league_matches) < 10:
        league_matches = results

    if not league_matches:
        return {
            "home_goals": 1.45,
            "away_goals": 1.15,
            "sample": 0
        }

    home_average = sum(
        r["home_goals"] for r in league_matches
    ) / len(league_matches)

    away_average = sum(
        r["away_goals"] for r in league_matches
    ) / len(league_matches)

    return {
        "home_goals": max(0.35, min(home_average, 3.5)),
        "away_goals": max(0.35, min(away_average, 3.5)),
        "sample": len(league_matches)
    }


# =========================================================
# TEAM HISTORY
# =========================================================

def get_team_history(results, team_name, league_name=None):
    target = normalise_name(team_name)

    matches = []

    for match in results:

        home = normalise_name(match.get("home_team"))
        away = normalise_name(match.get("away_team"))

        if home != target and away != target:
            continue

        matches.append(match)

    matches.sort(
        key=lambda r: r.get("kickoff_utc") or "",
        reverse=True
    )

    # Prefer same-league history where sufficient.
    if league_name:
        target_league = normalise_name(league_name)

        league_matches = [
            m for m in matches
            if normalise_name(m.get("league")) == target_league
        ]

        if len(league_matches) >= MIN_MATCHES:
            return league_matches, "same league"

    return matches, "all available competitions"


def weighted_average(values):
    if not values:
        return None

    weights = [
        RECENCY_DECAY ** index
        for index in range(len(values))
    ]

    total_weight = sum(weights)

    return sum(
        value * weight
        for value, weight in zip(values, weights)
    ) / total_weight


def team_metrics(results, team_name, league_name, venue=None):
    """
    venue:
        None  = all recent matches
        home  = matches played at home
        away  = matches played away
    """

    history, source_scope = get_team_history(
        results,
        team_name,
        league_name
    )

    target = normalise_name(team_name)

    if venue:
        venue_history = []

        for match in history:
            is_home = (
                normalise_name(match["home_team"]) == target
            )

            if venue == "home" and is_home:
                venue_history.append(match)

            elif venue == "away" and not is_home:
                venue_history.append(match)

        history_for_metrics = venue_history[:MAX_HISTORY]

    else:
        history_for_metrics = history[:MAX_HISTORY]

    scored = []
    conceded = []
    points = []

    for match in history_for_metrics:

        is_home = (
            normalise_name(match["home_team"]) == target
        )

        if is_home:
            gf = match["home_goals"]
            ga = match["away_goals"]
        else:
            gf = match["away_goals"]
            ga = match["home_goals"]

        scored.append(float(gf))
        conceded.append(float(ga))

        if gf > ga:
            points.append(3)
        elif gf == ga:
            points.append(1)
        else:
            points.append(0)

    return {
        "matches": len(history_for_metrics),
        "all_matches": len(history),
        "goals_for": weighted_average(scored),
        "goals_against": weighted_average(conceded),
        "points_per_game": (
            weighted_average(points)
            if points else None
        ),
        "scope": source_scope
    }


# =========================================================
# SHRINKAGE / EXPECTED GOALS
# =========================================================

def shrink(value, baseline, sample, minimum=5):
    """
    Reduce extreme estimates from small samples.

    More matches -> more weight on observed team data.
    Fewer matches -> more weight on league baseline.
    """

    if value is None:
        return baseline

    reliability = min(1.0, sample / 10.0)

    if sample < minimum:
        reliability *= 0.65

    return (
        reliability * value
        + (1.0 - reliability) * baseline
    )


def calculate_expected_goals(
    results,
    home_team,
    away_team,
    league_name
):
    baseline = league_baseline(results, league_name)

    home_all = team_metrics(
        results, home_team, league_name
    )

    away_all = team_metrics(
        results, away_team, league_name
    )

    home_venue = team_metrics(
        results, home_team, league_name, venue="home"
    )

    away_venue = team_metrics(
        results, away_team, league_name, venue="away"
    )

    # Blend overall and venue-specific form.
    home_attack = (
        0.45 * (home_all["goals_for"] or baseline["home_goals"])
        + 0.55 * (
            home_venue["goals_for"]
            or home_all["goals_for"]
            or baseline["home_goals"]
        )
    )

    home_defence = (
        0.45 * (
            home_all["goals_against"]
            or baseline["away_goals"]
        )
        + 0.55 * (
            home_venue["goals_against"]
            or home_all["goals_against"]
            or baseline["away_goals"]
        )
    )

    away_attack = (
        0.45 * (away_all["goals_for"] or baseline["away_goals"])
        + 0.55 * (
            away_venue["goals_for"]
            or away_all["goals_for"]
            or baseline["away_goals"]
        )
    )

    away_defence = (
        0.45 * (
            away_all["goals_against"]
            or baseline["home_goals"]
        )
        + 0.55 * (
            away_venue["goals_against"]
            or away_all["goals_against"]
            or baseline["home_goals"]
        )
    )

    # Attack vs opponent defensive weakness.
    home_raw = (
        home_attack + away_defence
    ) / 2.0

    away_raw = (
        away_attack + home_defence
    ) / 2.0

    home_sample = min(
        home_all["matches"],
        max(home_venue["matches"], MIN_VENUE_MATCHES)
    )

    away_sample = min(
        away_all["matches"],
        max(away_venue["matches"], MIN_VENUE_MATCHES)
    )

    home_xg = shrink(
        home_raw,
        baseline["home_goals"],
        home_sample
    )

    away_xg = shrink(
        away_raw,
        baseline["away_goals"],
        away_sample
    )

    # Small generic home advantage, capped to avoid extremes.
    home_xg *= 1.06

    home_xg = max(0.15, min(home_xg, 4.0))
    away_xg = max(0.15, min(away_xg, 4.0))

    return {
        "home_xg": round(home_xg, 3),
        "away_xg": round(away_xg, 3),
        "home_all": home_all,
        "away_all": away_all,
        "home_venue": home_venue,
        "away_venue": away_venue,
        "baseline": baseline
    }


# =========================================================
# POISSON MODEL
# =========================================================

def poisson_probability(goals, expected_goals):
    return (
        math.exp(-expected_goals)
        * expected_goals ** goals
        / math.factorial(goals)
    )


def score_matrix(home_xg, away_xg):
    matrix = []

    for home_goals in range(MAX_SCORE + 1):
        home_p = poisson_probability(
            home_goals,
            home_xg
        )

        for away_goals in range(MAX_SCORE + 1):
            away_p = poisson_probability(
                away_goals,
                away_xg
            )

            matrix.append({
                "home_goals": home_goals,
                "away_goals": away_goals,
                "probability": home_p * away_p
            })

    # Normalise truncated matrix.
    total = sum(x["probability"] for x in matrix)

    if total <= 0:
        raise ValueError("Invalid Poisson score matrix.")

    for item in matrix:
        item["probability"] /= total

    return matrix


def calculate_probabilities(matrix):
    home_win = 0.0
    draw = 0.0
    away_win = 0.0

    btts_yes = 0.0

    over_15 = 0.0
    over_25 = 0.0
    over_35 = 0.0

    for item in matrix:

        h = item["home_goals"]
        a = item["away_goals"]
        p = item["probability"]

        if h > a:
            home_win += p
        elif h == a:
            draw += p
        else:
            away_win += p

        if h > 0 and a > 0:
            btts_yes += p

        if h + a >= 2:
            over_15 += p

        if h + a >= 3:
            over_25 += p

        if h + a >= 4:
            over_35 += p

    return {
        "home_win": home_win,
        "draw": draw,
        "away_win": away_win,
        "home_or_draw": home_win + draw,
        "away_or_draw": away_win + draw,
        "home_or_away": home_win + away_win,
        "btts_yes": btts_yes,
        "btts_no": 1.0 - btts_yes,
        "over_15": over_15,
        "under_15": 1.0 - over_15,
        "over_25": over_25,
        "under_25": 1.0 - over_25,
        "over_35": over_35,
        "under_35": 1.0 - over_35
    }


# =========================================================
# NEWS RISK ASSESSMENT
# =========================================================

def get_team_news(team_name, days=10):
    with connect_db() as conn:

        if not table_exists(conn, "team_news"):
            return []

        rows = conn.execute("""
            SELECT *
            FROM team_news
            WHERE lower(team_name) = lower(?)
            ORDER BY published_at DESC
        """, (team_name,)).fetchall()

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    fresh = []

    for row in rows:
        item = dict(row)
        published = parse_date(item.get("published_at"))

        if published and published >= cutoff:
            fresh.append(item)

    return fresh


def assess_news_risk(team_name):
    articles = get_team_news(team_name)

    categories = set()
    review_required = 0
    official_check = 0

    for article in articles:

        found = set(
            str(article.get("category") or "").split(",")
        )

        categories.update(found)

        status = article.get("review_status", "")

        if status == "REVIEW_REQUIRED":
            review_required += 1

        if status == "CHECK_OFFICIAL_TEAM_NEWS":
            official_check += 1

    serious_categories = {
        "injury",
        "suspension",
        "manager_pressure",
        "internal_conflict",
        "financial_issue",
        "boardroom"
    }

    serious_found = categories.intersection(
        serious_categories
    )

    risk_points = 0

    if "injury" in categories:
        risk_points += 2

    if "suspension" in categories:
        risk_points += 2

    if "manager_pressure" in categories:
        risk_points += 1

    if "internal_conflict" in categories:
        risk_points += 2

    if "financial_issue" in categories:
        risk_points += 2

    if "boardroom" in categories:
        risk_points += 1

    if risk_points >= 4:
        risk_level = "HIGH - VERIFY REPORTS"
    elif risk_points >= 2:
        risk_level = "MODERATE - CHECK TEAM NEWS"
    elif risk_points > 0:
        risk_level = "LOW - MONITOR"
    else:
        risk_level = "NO RECENT FLAG FOUND"

    return {
        "articles": len(articles),
        "categories": sorted(serious_found),
        "review_required": review_required,
        "official_check": official_check,
        "risk_points": risk_points,
        "risk_level": risk_level,
        "warning": (
            "News headlines are unverified until checked "
            "against original reporting or official sources."
            if articles else
            "No recent indexed news found; this does not mean "
            "the team has no injuries or suspensions."
        )
    }


# =========================================================
# DATA QUALITY
# =========================================================

def assess_data_quality(model):
    home_n = model["home_all"]["matches"]
    away_n = model["away_all"]["matches"]

    home_venue_n = model["home_venue"]["matches"]
    away_venue_n = model["away_venue"]["matches"]

    reasons = []

    if home_n < MIN_MATCHES:
        reasons.append("Home team has fewer than 5 results")

    if away_n < MIN_MATCHES:
        reasons.append("Away team has fewer than 5 results")

    if home_venue_n < MIN_VENUE_MATCHES:
        reasons.append("Insufficient home-only sample")

    if away_venue_n < MIN_VENUE_MATCHES:
        reasons.append("Insufficient away-only sample")

    if reasons:
        return {
            "status": "INSUFFICIENT",
            "reasons": reasons,
            "quality_score": 0.0
        }

    sample_quality = min(
        1.0,
        home_n / 10.0,
        away_n / 10.0
    )

    venue_quality = min(
        1.0,
        home_venue_n / 5.0,
        away_venue_n / 5.0
    )

    score = (
        0.70 * sample_quality
        + 0.30 * venue_quality
    )

    return {
        "status": (
            "GOOD" if score >= 0.75 else "LIMITED"
        ),
        "reasons": [],
        "quality_score": score
    }


# =========================================================
# MARKET GENERATION
# =========================================================

def market_candidates(probabilities):
    candidates = []

    market_map = {
        "Home win": probabilities["home_win"],
        "Draw": probabilities["draw"],
        "Away win": probabilities["away_win"],
        "Home or Draw": probabilities["home_or_draw"],
        "Away or Draw": probabilities["away_or_draw"],
        "Home or Away": probabilities["home_or_away"],
        "Over 1.5 Goals": probabilities["over_15"],
        "Under 1.5 Goals": probabilities["under_15"],
        "Over 2.5 Goals": probabilities["over_25"],
        "Under 2.5 Goals": probabilities["under_25"],
        "Over 3.5 Goals": probabilities["over_35"],
        "Under 3.5 Goals": probabilities["under_35"],
        "BTTS - Yes": probabilities["btts_yes"],
        "BTTS - No": probabilities["btts_no"],
    }

    for market, probability in market_map.items():

        threshold = THRESHOLDS[market]

        if probability >= threshold:
            candidates.append({
                "market": market,
                "probability": probability,
                "threshold": threshold
            })

    candidates.sort(
        key=lambda x: x["probability"],
        reverse=True
    )

    return candidates


# =========================================================
# SINGLE MATCH PREDICTION
# =========================================================

def predict_fixture(fixture, results):
    home = fixture.get("home_team")
    away = fixture.get("away_team")
    league = fixture.get("league") or "Unknown league"

    if not home or not away:
        return None

    model = calculate_expected_goals(
        results,
        home,
        away,
        league
    )

    quality = assess_data_quality(model)

    matrix = score_matrix(
        model["home_xg"],
        model["away_xg"]
    )

    probabilities = calculate_probabilities(matrix)

    home_news = assess_news_risk(home)
    away_news = assess_news_risk(away)

    combined_news_points = (
        home_news["risk_points"]
        + away_news["risk_points"]
    )

    # News does not change the score probabilities.
    # It reduces confidence in the reliability of the report.
    news_penalty = min(
        0.25,
        combined_news_points * 0.035
    )

    confidence = max(
        0.0,
        quality["quality_score"] - news_penalty
    )

    top_scores = sorted(
        matrix,
        key=lambda x: x["probability"],
        reverse=True
    )[:5]

    candidates = market_candidates(probabilities)

    # No selections when the statistical sample is inadequate.
    if quality["status"] == "INSUFFICIENT":
        candidates = []

    # News uncertainty can prevent a high-risk match being selected.
    if combined_news_points >= 5:
        candidates = []

    # Require minimum data quality for recommendations.
    if confidence < 0.60:
        candidates = []

    # One candidate per fixture; do not stack correlated markets.
    best_candidate = candidates[0] if candidates else None

    if best_candidate:
        best_candidate = dict(best_candidate)
        best_candidate["confidence"] = confidence
        best_candidate["selection_score"] = (
            best_candidate["probability"] * confidence
        )

    result = {
        "event_id": fixture.get("event_id"),
        "league": league,
        "home_team": home,
        "away_team": away,
        "kickoff_utc": fixture.get("kickoff_utc"),
        "kickoff_lagos": fixture.get("kickoff_lagos"),
        "home_xg": model["home_xg"],
        "away_xg": model["away_xg"],
        "probabilities": probabilities,
        "quality": quality,
        "confidence": confidence,
        "home_news": home_news,
        "away_news": away_news,
        "top_scores": top_scores,
        "best_candidate": best_candidate,
        "all_candidates": candidates,
        "home_sample": model["home_all"]["matches"],
        "away_sample": model["away_all"]["matches"],
        "home_venue_sample": model["home_venue"]["matches"],
        "away_venue_sample": model["away_venue"]["matches"],
    }

    save_prediction(result)

    return result


# =========================================================
# SAVE PREDICTION
# =========================================================

def save_prediction(result):
    if not result:
        return

    probs = result["probabilities"]

    with connect_db() as conn:
        conn.execute("""
            INSERT INTO prediction_runs (
                event_id,
                league,
                home_team,
                away_team,
                kickoff_utc,
                home_xg,
                away_xg,
                home_win,
                draw_probability,
                away_win,
                btts_yes,
                over_25,
                confidence,
                data_quality,
                news_risk,
                prediction_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            result["event_id"],
            result["league"],
            result["home_team"],
            result["away_team"],
            result["kickoff_utc"],
            result["home_xg"],
            result["away_xg"],
            probs["home_win"],
            probs["draw"],
            probs["away_win"],
            probs["btts_yes"],
            probs["over_25"],
            result["confidence"],
            result["quality"]["status"],
            (
                result["home_news"]["risk_level"]
                + " / "
                + result["away_news"]["risk_level"]
            ),
            json.dumps(result),
            utc_now()
        ))


# =========================================================
# DISPLAY
# =========================================================

def pct(value):
    return f"{value * 100:.1f}%"


def print_fixture_report(result):
    if not result:
        return

    p = result["probabilities"]

    print("\n" + "=" * 76)
    print(f"{result['league']}")
    print(
        f"{result['home_team']} vs {result['away_team']}"
    )
    print(
        "Kickoff:",
        result["kickoff_lagos"] or result["kickoff_utc"]
    )
    print("=" * 76)

    print("\nEXPECTED GOALS")
    print(
        f"{result['home_team']}: {result['home_xg']}"
    )
    print(
        f"{result['away_team']}: {result['away_xg']}"
    )

    print("\n1X2 PROBABILITIES")
    print("Home win :", pct(p["home_win"]))
    print("Draw     :", pct(p["draw"]))
    print("Away win :", pct(p["away_win"]))

    print("\nDOUBLE CHANCE")
    print("Home or Draw :", pct(p["home_or_draw"]))
    print("Away or Draw :", pct(p["away_or_draw"]))
    print("Home or Away :", pct(p["home_or_away"]))

    print("\nGOALS")
    print("Over 1.5  :", pct(p["over_15"]))
    print("Under 1.5 :", pct(p["under_15"]))
    print("Over 2.5  :", pct(p["over_25"]))
    print("Under 2.5 :", pct(p["under_25"]))
    print("Over 3.5  :", pct(p["over_35"]))
    print("Under 3.5 :", pct(p["under_35"]))

    print("\nBTTS")
    print("Yes:", pct(p["btts_yes"]))
    print("No :", pct(p["btts_no"]))

    print("\nTOP CORRECT SCORES")

    for item in result["top_scores"]:
        print(
            f"{item['home_goals']}-{item['away_goals']}: "
            f"{pct(item['probability'])}"
        )

    print("\nDATA QUALITY")
    print("Status:", result["quality"]["status"])
    print("Confidence score:", pct(result["confidence"]))
    print(
        "Samples:",
        f"Home {result['home_sample']},",
        f"Away {result['away_sample']}"
    )
    print(
        "Venue samples:",
        f"Home {result['home_venue_sample']},",
        f"Away {result['away_venue_sample']}"
    )

    for reason in result["quality"]["reasons"]:
        print("Warning:", reason)

    print("\nNEWS RISK")
    print(
        result["home_team"] + ":",
        result["home_news"]["risk_level"]
    )
    print(
        result["away_team"] + ":",
        result["away_news"]["risk_level"]
    )

    print("\nRECOMMENDATION")

    if result["best_candidate"]:
        pick = result["best_candidate"]

        print("Candidate:", pick["market"])
        print("Model probability:", pct(pick["probability"]))
        print("Screening threshold:", pct(pick["threshold"]))
        print("Selection score:", pct(pick["selection_score"]))

    else:
        print("NO BET - insufficient evidence or no market passed.")

    print("\nCORNERS: NOT AVAILABLE - no reliable corner dataset.")
    print("Odds/value: NOT CALCULATED - bookmaker odds not supplied.")
    print("Probabilities are model estimates, not guarantees.")


# =========================================================
# DAILY SHORTLIST
# =========================================================

def build_shortlist(results):
    candidates = []

    for fixture in results:
        pick = fixture.get("best_candidate")

        if not pick:
            continue

        candidates.append({
            "fixture": fixture,
            "pick": pick
        })

    candidates.sort(
        key=lambda x: x["pick"]["selection_score"],
        reverse=True
    )

    # Maximum five; one pick per match.
    return candidates[:MAX_SELECTIONS]


def print_shortlist(shortlist):
    print("\n")
    print("#" * 76)
    print("FOOTBALL INTELLIGENCE - TOP SELECTION CANDIDATES")
    print("#" * 76)

    if not shortlist:
        print("NO QUALIFIED SELECTIONS TODAY.")
        print("The engine will not force five picks.")
        return

    for index, item in enumerate(shortlist, start=1):
        fixture = item["fixture"]
        pick = item["pick"]

        print(f"\n{index}. {fixture['home_team']} vs {fixture['away_team']}")
        print("League:", fixture["league"])
        print("Market:", pick["market"])
        print("Model probability:", pct(pick["probability"]))
        print("Confidence:", pct(pick["confidence"]))
        print("Selection score:", pct(pick["selection_score"]))
        print("Data quality:", fixture["quality"]["status"])

    print("\nMaximum five selections.")
    print("These are candidates, not guaranteed outcomes.")


# =========================================================
# MAIN
# =========================================================

def main():
    parser = argparse.ArgumentParser(
        description="Evidence-based football prediction engine"
    )

    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Upcoming fixture window"
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Maximum fixtures to analyse"
    )

    args = parser.parse_args()

    create_tables()

    print("\nLoading historical results...")
    results = load_results()

    print("Historical results loaded:", len(results))

    if len(results) < 20:
        print(
            "WARNING: Very small historical database. "
            "Predictions may be unavailable."
        )

    fixtures = load_fixtures(
        days=args.days,
        limit=args.limit
    )

    print("Upcoming fixtures found:", len(fixtures))

    if not fixtures:
        print("No upcoming fixtures in the selected date range.")
        return

    predictions = []

    for index, fixture in enumerate(fixtures, start=1):
        print(
            f"\nAnalysing {index}/{len(fixtures)}: "
            f"{fixture.get('home_team')} vs "
            f"{fixture.get('away_team')}"
        )

        try:
            result = predict_fixture(fixture, results)

            if result:
                predictions.append(result)
                print_fixture_report(result)

        except Exception as exc:
            log.exception(
                "Could not analyse fixture: %s",
                exc
            )

    shortlist = build_shortlist(predictions)
    print_shortlist(shortlist)

    print("\nPrediction engine completed.")
    print("Predictions saved in:", DB_FILE)


if __name__ == "__main__":
    main()
