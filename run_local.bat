@echo off
rem Plan B: run the monitor from your own Windows PC instead of GitHub.
rem 1) Paste your Discord webhook link after the = sign below (no quotes).
rem 2) Double-click this file to run one check.
cd /d "%~dp0"
set DISCORD_WEBHOOK_URL=PASTE_YOUR_WEBHOOK_LINK_HERE
python monitor.py
