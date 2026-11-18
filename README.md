# Football Intelligence Bot — API-Football Edition

## What changed
- API-Football replaces OpenLigaDB as the primary fixture/results provider.
- `fixtures.py` selects a maximum of five upcoming fixtures.
- `team_analysis.py` retrieves recent completed matches only for the teams in that selected batch.
- `news_analysis.py` remains Google News RSS-based and processes the selected fixture teams.
- `prediction_engine.py` retains the existing statistical/Poisson model and is hard-capped at five fixtures.
- `bot.py` adds a private Telegram dashboard with inline buttons.
- All execution remains manual: `/run` or RUN ANALYSIS. No scheduler and no bet placement.

## Install on Kali Linux
```bash
cd /root/football_intelligence_bot
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
```
Fill in:
- `API_FOOTBALL_KEY`
- `BOT_TOKEN`
- `TELEGRAM_ALLOWED_USER_ID`

Keep `.env` private. Never paste API keys or bot tokens into public chats or GitHub.

## Test in this order
```bash
python -m py_compile api_client.py fixtures.py team_analysis.py news_analysis.py prediction_engine.py daily_report.py bot.py
python fixtures.py --days 7 --limit 5
python team_analysis.py --collect --limit 10
python news_analysis.py --collect --limit 5
python prediction_engine.py --days 7 --limit 5
```
If each module succeeds:
```bash
python bot.py
```
Open your Telegram bot and send `/start`, then press RUN ANALYSIS.

## API request budget
The API-Football free plan currently advertises 100 requests/day. This implementation caches API responses in `football.db`:
- upcoming fixture response: 15 minutes
- recent team fixtures: 12 hours
A normal run uses one fixture request plus up to ten team-history requests, excluding cache hits. We deliberately do not request every optional endpoint for every match; that would consume the free quota quickly. API-Football coverage differs by competition and season.

## Notes and limitations
- API-Football team-history calls request the last 10 matches per selected team. The API may return fewer.
- The API-Football `fixtures` endpoint supplies the team IDs needed to retrieve recent team matches.
- Google News RSS headlines are unverified reporting leads, not confirmed injury or dressing-room facts.
- The retained prediction model is a first-pass statistical model. It is not proven profitable; backtesting and calibration are required before treating probabilities as calibrated forecasts.
- Fewer than five recommendations may be returned, including zero, when the data-quality gates reject a match.
- The current Telegram interface is a native Telegram inline-keyboard dashboard. A Telegram Mini App with charts and richer screens can be a later UI enhancement, but this package delivers the interactive Telegram control panel now.

## Database
The project reuses `football.db`. The active `fixtures` table is cleared and repopulated with the latest selected batch on each fixture collection. Historical results and news remain stored.
