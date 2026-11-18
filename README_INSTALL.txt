# Football Intelligence Bot — Five-Fixture Pipeline Update

This update changes the pipeline so one `/run` processes a maximum of five selected fixtures.

## What changed
- `fixtures.py`: adds `--max-fixtures` (default 5), selects the earliest upcoming fixtures within the requested date window, and writes `selected_fixture_batch.json`.
- `team_analysis.py`: reads the exact selected batch; historical collection is limited to the selected competitions and stores completed matches involving the selected ten teams.
- `news_analysis.py`: searches news only for teams in the selected batch (up to ten teams).
- `prediction_engine.py`: predicts only fixtures in the same selected batch.
- `daily_report.py`: passes the same batch file to all downstream modules.
- `bot.py`: unchanged; `/run` remains manual.

## Important behaviour
The OpenLigaDB endpoint returns match lists for a competition/season. To select the best five across the discovered competitions, `fixtures.py` still has to inspect candidate fixtures from those competition responses. However, only the selected maximum five are saved as the current run's batch and passed to the analysis modules.

The database is not wiped. Older fixture and historical-result rows remain available, but the current run is scoped by `selected_fixture_batch.json`.

Five is a maximum, not a promise of five betting selections. The prediction engine may return fewer or no selections if the evidence is insufficient.

## Install on Kali Linux

1. Stop the Telegram bot if it is running.
2. Go to the project directory:
   ```bash
   cd /root/football_intelligence_bot
   ```
3. Make a backup:
   ```bash
   mkdir -p backup_before_five_fixture_update
   cp fixtures.py team_analysis.py news_analysis.py prediction_engine.py daily_report.py bot.py backup_before_five_fixture_update/
   ```
4. Extract this ZIP on your Windows computer, then transfer the six `.py` files to `/root/football_intelligence_bot/`, overwriting the existing files when asked.
   - `bot.py` is included unchanged.
5. Activate your virtual environment:
   ```bash
   source venv/bin/activate
   ```
6. Check the new fixture option:
   ```bash
   python fixtures.py --help
   ```
   You should see `--max-fixtures MAX_FIXTURES`.
7. Run a manual pipeline test from Kali:
   ```bash
   python daily_report.py
   ```
8. If the test report looks correct, start your Telegram bot as you normally do and request `/run`.

The update does not add a scheduler, automatic betting, or another project stage.
