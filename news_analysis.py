
#!/usr/bin/env python3

"""
Football News and Team News Analysis
News intelligence module for the Football Intelligence Bot.

Source:
- Google News public RSS search feed

No API-Football dependency. This module is compatible with the
football-data.org / OpenLigaDB fixture migration.

Stores:
- Article headline
- Publisher
- Publication date
- Article URL
- Team searched
- News category
- Relevance score

This module does not claim that a headline is verified.
It identifies reports for further review.
"""

import argparse
import hashlib
import logging
import re
import sqlite3
import time
import xml.etree.ElementTree as ET

from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus
from urllib.request import Request, urlopen
from email.utils import parsedate_to_datetime



# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------

DB_FILE = Path(__file__).resolve().parent / "football.db"

RSS_BASE = "https://news.google.com/rss/search"

REQUEST_DELAY = 1.5
TIMEOUT = 25
MAX_ARTICLES_PER_TEAM = 15

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("news_analysis")


# ---------------------------------------------------------
# NEWS KEYWORDS
# ---------------------------------------------------------

KEYWORDS = {
    "injury": [
        "injury", "injured", "injuries", "fitness concern",
        "ruled out", "sidelined", "hamstring", "knee injury",
        "ankle injury", "muscle injury", "recovering from injury"
    ],

    "suspension": [
        "suspended", "suspension", "red card ban",
        "yellow card ban", "disciplinary ban"
    ],

    "lineup": [
        "starting lineup", "starting eleven", "lineup",
        "team news", "expected lineup", "starting xi",
        "returns to training", "back in training"
    ],

    "manager_pressure": [
        "manager under pressure", "manager sacked",
        "manager dismissed", "manager future",
        "manager crisis", "manager faces sack",
        "coach sacked", "coach dismissed"
    ],

    "internal_conflict": [
        "dressing room unrest", "dressing room dispute",
        "player-manager row", "players revolt",
        "training ground bust-up", "internal conflict",
        "players unhappy", "reported dressing room tension"
    ],

    "financial_issue": [
        "unpaid wages", "wage delay", "players unpaid",
        "financial crisis", "salary dispute",
        "players threaten strike"
    ],

    "boardroom": [
        "boardroom crisis", "board dispute",
        "club ownership dispute", "boardroom conflict",
        "owner dispute", "club takeover"
    ],

    "general": [
        "football", "match preview", "match report",
        "club news", "transfer news"
    ]
}


CATEGORY_PRIORITY = [
    "injury",
    "suspension",
    "lineup",
    "manager_pressure",
    "internal_conflict",
    "financial_issue",
    "boardroom",
    "general"
]


# ---------------------------------------------------------
# DATABASE
# ---------------------------------------------------------

def connect_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def create_tables():
    with connect_db() as conn:

        conn.execute("""
            CREATE TABLE IF NOT EXISTS team_news (
                article_id TEXT PRIMARY KEY,
                team_name TEXT,
                headline TEXT,
                publisher TEXT,
                article_url TEXT,
                published_at TEXT,
                category TEXT,
                relevance_score INTEGER,
                review_status TEXT,
                search_query TEXT,
                collected_at TEXT
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_news_team
            ON team_news(team_name)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_news_category
            ON team_news(category)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_news_date
            ON team_news(published_at)
        """)


# ---------------------------------------------------------
# TEXT PROCESSING
# ---------------------------------------------------------

def clean_text(value):
    if not value:
        return ""

    value = re.sub(r"<[^>]+>", " ", str(value))
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def normalise(value):
    value = clean_text(value).lower()
    value = re.sub(r"[^a-z0-9\s]", " ", value)

    return " ".join(value.split())


def identify_categories(headline):
    text = normalise(headline)

    found = []

    for category in CATEGORY_PRIORITY:
        for keyword in KEYWORDS[category]:

            normal_keyword = normalise(keyword)

            if normal_keyword and normal_keyword in text:
                found.append(category)
                break

    return found or ["general"]


def calculate_relevance(headline, team_name):
    text = normalise(headline)
    team = normalise(team_name)

    score = 0

    if team and team in text:
        score += 5

    categories = identify_categories(headline)

    important = {
        "injury": 3,
        "suspension": 3,
        "lineup": 2,
        "manager_pressure": 2,
        "internal_conflict": 2,
        "financial_issue": 2,
        "boardroom": 1
    }

    for category in categories:
        score += important.get(category, 0)

    return score, categories


# ---------------------------------------------------------
# DATE PROCESSING
# ---------------------------------------------------------

def parse_published_date(value):
    if not value:
        return None

    try:
        parsed = parsedate_to_datetime(value)

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc).isoformat()

    except (TypeError, ValueError, OverflowError):
        return None


def utc_now():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------
# RSS COLLECTION
# ---------------------------------------------------------

def build_search_url(team_name):
    keywords = (
        "injury OR injured OR suspension OR suspended OR "
        "lineup OR team news OR manager OR dressing room OR "
        "unpaid wages OR boardroom"
    )

    query = f'"{team_name}" ({keywords}) when:14d'

    return (
        f"{RSS_BASE}?q={quote_plus(query)}"
        "&hl=en-NG&gl=NG&ceid=NG:en"
    )


def fetch_rss(url):
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; FootballIntelligenceBot/2.0)",
            "Accept": "application/rss+xml, application/xml, text/xml"
        }
    )

    try:
        with urlopen(request, timeout=TIMEOUT) as response:

            if response.status != 200:
                log.warning("RSS HTTP status: %s", response.status)
                return None

            return ET.fromstring(response.read())

    except HTTPError as exc:
        log.warning("RSS HTTP error: %s", exc.code)

    except URLError as exc:
        log.warning("RSS connection error: %s", exc.reason)

    except ET.ParseError:
        log.warning("RSS returned invalid XML.")

    except TimeoutError:
        log.warning("RSS request timed out.")

    except Exception as exc:
        log.warning("RSS error: %s", exc)

    return None


def extract_articles(root):
    if root is None:
        return []

    articles = []

    for item in root.findall(".//item"):

        title = item.findtext("title", default="")
        link = item.findtext("link", default="")
        pub_date = item.findtext("pubDate", default="")

        source_element = item.find("source")

        publisher = ""

        if source_element is not None:
            publisher = clean_text(source_element.text)

        if not publisher:
            publisher = "Unknown publisher"

        title = clean_text(title)
        link = clean_text(link)

        if not title or not link:
            continue

        articles.append({
            "headline": title,
            "url": link,
            "publisher": publisher,
            "published_at": parse_published_date(pub_date)
        })

    return articles


# ---------------------------------------------------------
# SAVE ARTICLES
# ---------------------------------------------------------

def save_article(team_name, article, search_query):
    headline = article["headline"]

    score, categories = calculate_relevance(
        headline,
        team_name
    )

    category = ",".join(categories)

    # Internal conflict and financial claims need manual review.
    sensitive_categories = {
        "internal_conflict",
        "financial_issue",
        "boardroom"
    }

    if sensitive_categories.intersection(categories):
        review_status = "REVIEW_REQUIRED"
    elif (
        "injury" in categories
        or "suspension" in categories
    ):
        review_status = "CHECK_OFFICIAL_TEAM_NEWS"
    else:
        review_status = "UNVERIFIED_REPORT"

    # Include the team in the key: one article can concern multiple clubs.
    identity = f"{article['url']}|{normalise(team_name)}"
    article_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()

    with connect_db() as conn:
        conn.execute("""
            INSERT INTO team_news (
                article_id,
                team_name,
                headline,
                publisher,
                article_url,
                published_at,
                category,
                relevance_score,
                review_status,
                search_query,
                collected_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

            ON CONFLICT(article_id) DO UPDATE SET
                team_name = excluded.team_name,
                headline = excluded.headline,
                publisher = excluded.publisher,
                published_at = excluded.published_at,
                category = excluded.category,
                relevance_score = excluded.relevance_score,
                review_status = excluded.review_status,
                collected_at = excluded.collected_at
        """, (
            article_id,
            team_name,
            headline,
            article["publisher"],
            article["url"],
            article["published_at"],
            category,
            score,
            review_status,
            search_query,
            utc_now()
        ))

    return True


# ---------------------------------------------------------
# GET TEAMS FROM UPCOMING FIXTURES
# ---------------------------------------------------------

def get_fixture_teams(limit=5):
    with connect_db() as conn:

        try:
            rows = conn.execute("""
                SELECT home_team, away_team
                FROM fixtures
                WHERE kickoff_utc IS NOT NULL
                ORDER BY kickoff_utc ASC
                LIMIT ?
            """, (limit,)).fetchall()

        except sqlite3.OperationalError:
            log.error(
                "Fixtures table not found. Run fixtures.py first."
            )
            return []

    teams = []

    for row in rows:
        for team in (row["home_team"], row["away_team"]):

            if team and team not in teams:
                teams.append(team)

    return teams


# ---------------------------------------------------------
# COLLECT TEAM NEWS
# ---------------------------------------------------------

def collect_team_news(team_name):
    url = build_search_url(team_name)

    log.info("Searching news for %s", team_name)

    root = fetch_rss(url)

    if root is None:
        return 0

    articles = extract_articles(root)

    saved = 0

    search_query = f"{team_name} football team news"

    for article in articles[:MAX_ARTICLES_PER_TEAM]:

        if save_article(team_name, article, search_query):
            saved += 1

    log.info(
        "%s: processed %s articles",
        team_name,
        saved
    )

    return saved




def collect_all_fixture_news(fixture_limit=5):
    """
    Collect Google News RSS reports for teams in the next fixture batch.

    API-Football is deliberately not used after the provider migration.
    RSS headlines are reports, not verified team announcements.
    """
    create_tables()

    # Keep each manual collection run focused on no more than five fixtures.
    fixture_limit = max(1, min(int(fixture_limit), 5))
    teams = get_fixture_teams(fixture_limit)

    if not teams:
        print("No teams found in the fixtures database.")
        print("Run fixtures.py first and confirm fixtures were saved.")
        return

    total = 0
    failed = 0

    print("\\nTEAMS TO SEARCH")
    print("-" * 45)
    for team in teams:
        print("-", team)
    print("-" * 45)
    print(f"Fixture limit: {fixture_limit}")
    print(f"Team searches: {len(teams)}")
    print("Source: Google News RSS")
    print("API-Football: not used")

    for index, team in enumerate(teams, start=1):
        log.info("Team %s of %s: %s", index, len(teams), team)
        try:
            saved = collect_team_news(team)
            total += saved
        except Exception as exc:
            failed += 1
            log.exception("News collection failed for %s: %s", team, exc)

        if index < len(teams):
            time.sleep(REQUEST_DELAY)

    with connect_db() as conn:
        stored_total = conn.execute(
            "SELECT COUNT(*) FROM team_news"
        ).fetchone()[0]

    print("\\nNEWS COLLECTION SUMMARY")
    print("-" * 45)
    print("Fixtures processed:", fixture_limit)
    print("Teams searched:", len(teams))
    print("Articles processed:", total)
    print("Team searches with errors:", failed)
    print("Total news records in database:", stored_total)
    print("\\nDATA QUALITY NOTICE")
    print("Google News headlines are reports, not verified team announcements.")
    print("Check injury and suspension claims against official club/league sources.")


# ---------------------------------------------------------
# NEWS REPORT
# ---------------------------------------------------------

def get_team_news(team_name, limit=20):
    with connect_db() as conn:

        rows = conn.execute("""
            SELECT *
            FROM team_news
            WHERE lower(team_name) = lower(?)
            ORDER BY
                CASE WHEN published_at IS NULL THEN 1 ELSE 0 END,
                published_at DESC,
                relevance_score DESC
            LIMIT ?
        """, (team_name, limit)).fetchall()

    return [dict(row) for row in rows]


def print_team_news(team_name, limit=20):
    articles = get_team_news(team_name, limit)

    print("\n" + "=" * 70)
    print("TEAM NEWS REPORT:", team_name)
    print("=" * 70)

    if not articles:
        print("No stored news articles found for this team.")
        return

    for index, article in enumerate(articles, start=1):

        print(f"\n{index}. {article['headline']}")
        print("Publisher:", article["publisher"])
        print("Published:", article["published_at"] or "Unknown date")
        print("Category:", article["category"])
        print("Review:", article["review_status"])
        print("Relevance score:", article["relevance_score"])
        print("Source:", article["article_url"])

    print("\nIMPORTANT:")
    print("Headlines are not proof of team news.")
    print("Check the original article and official club updates.")
    print("Do not treat unverified rumours as confirmed facts.")
    print("REVIEW_REQUIRED means manual verification is especially important.")


# ---------------------------------------------------------
# UPCOMING FIXTURE NEWS REPORT
# ---------------------------------------------------------

def print_fixture_news(limit=5):
    fixtures = []

    with connect_db() as conn:

        try:
            fixtures = conn.execute("""
                SELECT home_team, away_team, league,
                       kickoff_lagos, kickoff_utc
                FROM fixtures
                WHERE kickoff_utc IS NOT NULL
                ORDER BY kickoff_utc ASC
                LIMIT ?
            """, (limit,)).fetchall()

        except sqlite3.OperationalError:
            print("Fixtures table not found. Run fixtures.py first.")
            return

    if not fixtures:
        print("No upcoming fixtures found.")
        return

    for fixture in fixtures:

        home = fixture["home_team"]
        away = fixture["away_team"]

        print("\n" + "=" * 70)
        print(f"{home} vs {away}")
        print("League:", fixture["league"])
        print(
            "Kickoff:",
            fixture["kickoff_lagos"] or fixture["kickoff_utc"]
        )

        for team in (home, away):

            articles = get_team_news(team, 5)

            print(f"\nNEWS FOR {team.upper()}")

            if not articles:
                print("No stored reports found.")
                continue

            for article in articles:

                print("-", article["headline"])
                print("  Category:", article["category"])
                print("  Review:", article["review_status"])
                print("  Publisher:", article["publisher"])
                print("  Published:", article["published_at"])
                print("  Link:", article["article_url"])


# ---------------------------------------------------------
# COMMAND LINE
# ---------------------------------------------------------

def print_news_stats():
    create_tables()
    with connect_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM team_news").fetchone()[0]
        teams = conn.execute(
            "SELECT COUNT(DISTINCT team_name) FROM team_news"
        ).fetchone()[0]
        categories = conn.execute("""
            SELECT category, COUNT(*) AS total
            FROM team_news
            GROUP BY category
            ORDER BY total DESC
        """).fetchall()
        reviews = conn.execute("""
            SELECT review_status, COUNT(*) AS total
            FROM team_news
            GROUP BY review_status
            ORDER BY total DESC
        """).fetchall()

    print("\\nNEWS DATABASE OVERVIEW")
    print("-" * 45)
    print("Total records:", total)
    print("Teams represented:", teams)
    print("\\nReview statuses:")
    for row in reviews:
        print(f"- {row['review_status']}: {row['total']}")
    print("\\nCategories:")
    for row in categories:
        print(f"- {row['category']}: {row['total']}")


def main():
    parser = argparse.ArgumentParser(
        description="Football team news collector and analyser"
    )

    parser.add_argument(
        "--collect",
        action="store_true",
        help="Collect news for teams in upcoming fixtures"
    )

    parser.add_argument(
        "--team",
        type=str,
        help="Display stored news for one team"
    )

    parser.add_argument(
        "--fixtures",
        action="store_true",
        help="Display news for upcoming fixtures"
    )

    parser.add_argument(
        "--stats",
        action="store_true",
        help="Show stored news database statistics"
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Maximum upcoming fixtures to process (hard cap: 5)"
    )

    args = parser.parse_args()

    create_tables()

    if args.collect:
        if args.limit < 1:
            parser.error("--limit must be at least 1")
        collect_all_fixture_news(args.limit)

    if args.team:
        print_team_news(args.team)

    if args.fixtures:
        print_fixture_news(min(args.limit, 5))

    if args.stats:
        print_news_stats()

    if not args.collect and not args.team and not args.fixtures and not args.stats:
        parser.print_help()


if __name__ == "__main__":
    main()
