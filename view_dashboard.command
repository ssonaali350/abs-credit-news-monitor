#!/bin/bash
# Double-click this file in Finder to view the ABS Credit News dashboard.
cd "$(dirname "$0")"
lsof -ti:8000 | xargs kill 2>/dev/null
python3 -m http.server 8000 > /dev/null 2>&1 &
sleep 1
open http://localhost:8000
echo "Dashboard opened in your browser. You can close this window."
sleep 2
