#!/bin/bash
# Daily cron entry point. Uses absolute paths since cron runs with a minimal
# environment and an unrelated working directory.
cd "$(dirname "$0")"
/opt/anaconda3/bin/python3 ingest.py --limit 50 >> cron.log 2>&1
/opt/anaconda3/bin/python3 fetch_market_data.py >> cron.log 2>&1
echo "--- run finished $(date) ---" >> cron.log
