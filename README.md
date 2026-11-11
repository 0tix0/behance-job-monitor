# Behance Job Monitor

Checks the Behance job list every 15 minutes and sends a Discord message for
each new job that matches your filters.

## Files

| File | What it is |
| --- | --- |
| `monitor.py` | The tool |
| `config.json` | Your settings: which Behance pages to watch and which keywords matter |
| `.github/workflows/monitor.yml` | Tells GitHub to run the tool every 15 minutes |
| `requirements.txt` | Python packages the tool needs |
| `run_local.bat` | Plan B: run from your own Windows PC |
| `seen.json` | Created automatically. The jobs already announced |

## Settings (`config.json`)

- `search_urls`: Behance job list links to watch. Open Behance Jobs in your
  browser, set any filters you like, and paste the address here. Several links
  are allowed.
- `job_types`: `["freelance"]`, `["fulltime"]`, or `["freelance", "fulltime"]`.
- `include_keywords`: a job is announced only if one of these words appears in
  it. Leave as `[]` to be told about every new job.
- `exclude_keywords`: a job containing any of these is never announced.
- `max_alerts_per_run`: upper limit of messages per check.

## Run on your own PC (Plan B)

Use this if GitHub's servers get blocked by Behance.

1. Install Python 3.10+ from python.org (tick "Add Python to PATH") and Google Chrome.
2. Open a terminal in this folder and run `pip install -r requirements.txt`.
3. Edit `run_local.bat` and paste your webhook link, then double-click it.
4. To repeat it every 15 minutes, run this once in a terminal (fix the path):
   `schtasks /Create /SC MINUTE /MO 15 /TN "BehanceMonitor" /TR "C:\path\to\run_local.bat"`

## Commands

- `python monitor.py --test` sends a test message to Discord.
- `python monitor.py --dry-run` shows what would be sent without sending.
- `python monitor.py --headed` shows the browser window while it works.

## Start over

Delete `seen.json`. The next run treats everything as already seen and sends
the "monitor is live" message again.
