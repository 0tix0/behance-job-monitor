#!/usr/bin/env python3
"""
Behance Job Monitor  ->  Discord alerts

Opens the Behance job list in a real browser, finds every job on the page,
remembers the ones it has already seen (seen.json) and sends a Discord message
for each NEW job that matches the filters in config.json.

Jobs are read in three independent ways, so a Behance layout change is unlikely
to break all of them at once:
  1. JSON the page downloads in the background (network capture)
  2. JSON embedded in the page's HTML
  3. Job links visible on the page (DOM)

Usage:
  python monitor.py              normal run
  python monitor.py --test       send a test message to Discord and exit
  python monitor.py --dry-run    print what would be sent; send and save nothing
  python monitor.py --headed     show the browser window (local debugging)
"""

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "seen.json"
DEBUG_DIR = ROOT / "debug"
BASE = "https://www.behance.net"

DEFAULT_CONFIG = {
    "search_urls": ["https://www.behance.net/joblist"],
    "job_types": ["freelance"],
    "include_keywords": [],
    "exclude_keywords": [],
    "max_alerts_per_run": 15,
    "scroll_rounds": 4,
}

MAX_SEEN = 4000          # how many job ids to remember
FAIL_ALERT_AFTER = 3     # consecutive failed runs before warning on Discord


def log(*parts):
    print(*parts, flush=True)


# --------------------------------------------------------------------------
# Config and state
# --------------------------------------------------------------------------

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError as exc:
            log(f"ERROR: config.json is not valid JSON ({exc}).")
            log("Tip: check for a missing comma or quote, then run again.")
            sys.exit(2)
    urls = [u.strip() for u in cfg.get("search_urls", []) if isinstance(u, str) and u.strip()]
    if not urls:
        log("ERROR: config.json needs at least one link in search_urls.")
        sys.exit(2)
    cfg["search_urls"] = urls
    cfg["job_types"] = [str(t).lower().replace("-", "").replace(" ", "") for t in cfg.get("job_types", [])]
    return cfg


def load_state():
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            state.setdefault("seen", {})
            state.setdefault("initialized", False)
            state.setdefault("fail_streak", 0)
            return state
        except json.JSONDecodeError:
            log("WARNING: seen.json was unreadable, starting fresh.")
    return {"initialized": False, "fail_streak": 0, "seen": {}}


def save_state(state):
    seen = state["seen"]
    if len(seen) > MAX_SEEN:
        keep = sorted(seen, key=lambda k: int(k), reverse=True)[:MAX_SEEN]
        seen = {k: seen[k] for k in keep}
    state["seen"] = dict(sorted(seen.items(), key=lambda kv: int(kv[0])))
    STATE_PATH.write_text(json.dumps(state, indent=1) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# Turning raw page data into jobs
# --------------------------------------------------------------------------

JOB_PATH_RE = re.compile(r"/joblist/(?:(freelance|fulltime|project|apply)/)?(\d{3,})(?=[/?#]|$)", re.I)
JOB_PARAM_RE = re.compile(r"[?&](freelanceJobId|jobId)=(\d{3,})", re.I)
MONEY_RE = re.compile(
    r"(?:US|CA|AU|NZ|HK|SG)?[$€£¥₹]\s?\d[\d,.]*[kK]?(?:\s?[-–—]\s?(?:US)?[$€£¥₹]?\s?\d[\d,.]*[kK]?)?"
)

JOBISH_KEYS = {
    "jobtype", "budget", "budgetmin", "budgetmax", "salary", "salarytimeframe",
    "company", "companyname", "allowedapplyplace", "applicationurl", "postedon",
    "posteddate", "creativefields", "isremote", "remote", "joblocation",
    "timeline", "deliverables", "applicantcount", "hiringorganization",
    "employmenttype", "dateposted", "isfreelance", "freelance",
}


def parse_job_url(url):
    """Return (job_id, job_type) found in a Behance job link, or (None, None)."""
    if not isinstance(url, str):
        return None, None
    m = JOB_PATH_RE.search(url)
    if m:
        kind = (m.group(1) or "").lower()
        if kind in ("freelance", "project"):
            return m.group(2), "freelance"
        if kind == "fulltime":
            return m.group(2), "fulltime"
        return m.group(2), ""
    m = JOB_PARAM_RE.search(url)
    if m:
        return m.group(2), "freelance" if m.group(1).lower() == "freelancejobid" else ""
    return None, None


def clean_text(value, limit=None):
    if not isinstance(value, str):
        return ""
    text = re.sub(r"<[^>]+>", " ", value)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def label_of(value):
    """Best human-readable label for a string / dict / list value."""
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, dict):
        for key in ("displayName", "display_name", "name", "title", "label", "text"):
            if isinstance(value.get(key), str) and value[key].strip():
                return clean_text(value[key])
        parts = [value.get(k) for k in ("city", "stateOrProvince", "state", "country")]
        parts = [clean_text(p) for p in parts if isinstance(p, str) and p.strip()]
        if parts:
            return ", ".join(parts)
        if isinstance(value.get("address"), dict):
            addr = value["address"]
            parts = [addr.get(k) for k in ("addressLocality", "addressRegion", "addressCountry")]
            parts = [clean_text(p) for p in parts if isinstance(p, str) and p.strip()]
            return ", ".join(parts)
    if isinstance(value, list):
        labels = [label_of(v) for v in value[:6]]
        return ", ".join(l for l in labels if l)
    return ""


def pick(d, *keys):
    """First non-empty value among keys (case-insensitive)."""
    if not isinstance(d, dict):
        return None
    lowered = {k.lower(): v for k, v in d.items() if isinstance(k, str)}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, "", [], {}):
            return value
    return None


def fmt_number(n):
    if isinstance(n, bool) or n in (None, ""):
        return ""
    try:
        f = float(n)
    except (TypeError, ValueError):
        return ""
    return f"{int(f):,}" if f == int(f) else f"{f:,.2f}"


def budget_of(d):
    raw = pick(d, "budget", "salary", "compensation", "baseSalary", "price")
    if isinstance(raw, str) and raw.strip():
        return clean_text(raw, 60)
    lo = pick(d, "budgetMin", "salaryMin", "minBudget", "minSalary")
    hi = pick(d, "budgetMax", "salaryMax", "maxBudget", "maxSalary")
    currency = pick(d, "currency", "budgetCurrency", "currencyCode", "salaryCurrency")
    if isinstance(raw, dict):
        inner = raw.get("value") if isinstance(raw.get("value"), dict) else raw
        lo = pick(inner, "min", "minimum", "minValue", "low", "from") or lo
        hi = pick(inner, "max", "maximum", "maxValue", "high", "to") or hi
        single = pick(inner, "amount", "value")
        if isinstance(single, (int, float)) and not lo and not hi:
            lo = hi = single
        currency = pick(raw, "currency", "currencyCode") or pick(inner, "currency") or currency
    elif isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw:
        lo = hi = raw
    lo_s, hi_s = fmt_number(lo), fmt_number(hi)
    if not lo_s and not hi_s:
        return ""
    amount = lo_s if lo_s == hi_s or not hi_s else (hi_s if not lo_s else f"{lo_s}–{hi_s}")
    currency = currency if isinstance(currency, str) else ""
    return f"{currency} {amount}".strip()


def haystack_of(node, depth=0, out=None):
    """All readable text inside a job object (used for keyword matching)."""
    if out is None:
        out = []
    if depth > 4 or len(out) > 200:
        return out
    if isinstance(node, str):
        if not node.startswith(("http://", "https://", "data:")) and len(node) < 6000:
            out.append(clean_text(node))
    elif isinstance(node, dict):
        for value in node.values():
            haystack_of(value, depth + 1, out)
    elif isinstance(node, list):
        for value in node[:40]:
            haystack_of(value, depth + 1, out)
    return out


def job_from_dict(d):
    """If this JSON object looks like a job posting, return a normalised job."""
    url_value = None
    for key in ("url", "jobUrl", "link", "href", "canonicalUrl", "applicationUrl", "applyUrl"):
        candidate = pick(d, key)
        if isinstance(candidate, str) and parse_job_url(candidate)[0]:
            url_value = candidate
            break
    url_id, url_type = parse_job_url(url_value) if url_value else (None, None)

    typename = str(d.get("__typename") or d.get("@type") or "")
    typename_l = typename.lower()
    type_signal = any(word in typename_l for word in ("job", "freelanceproject", "brief")) \
        and not any(word in typename_l for word in ("category", "location", "connection", "filter", "edge"))
    key_hits = len({k.lower() for k in d if isinstance(k, str)} & JOBISH_KEYS)

    title = pick(d, "title", "jobTitle")
    if not isinstance(title, str) and (url_id or type_signal):
        title = pick(d, "name")
    title = clean_text(title, 200) if isinstance(title, str) else ""
    if not title:
        return None
    if not (url_id or type_signal or key_hits >= 2):
        return None

    job_id = url_id
    if not job_id:
        raw_id = pick(d, "id", "jobId")
        if isinstance(raw_id, int) and not isinstance(raw_id, bool) and raw_id >= 100:
            job_id = str(raw_id)
        elif isinstance(raw_id, str) and raw_id.isdigit() and len(raw_id) >= 3:
            job_id = raw_id
    if not job_id:
        return None

    job_type = url_type or ""
    if not job_type:
        blob = " ".join(str(pick(d, k) or "") for k in ("jobType", "employmentType", "type")).lower()
        blob = f"{blob} {typename_l}".replace("-", "").replace("_", "").replace(" ", "")
        if "freelance" in blob or "contract" in blob or "brief" in blob or pick(d, "isFreelance") is True:
            job_type = "freelance"
        elif "fulltime" in blob or "parttime" in blob or "internship" in blob:
            job_type = "fulltime"

    company = label_of(pick(d, "company", "companyName", "hiringOrganization", "organization",
                            "client", "creator", "owner", "poster", "user") or "")
    location = label_of(pick(d, "location", "jobLocation", "locationName") or "")
    if not location and (pick(d, "isRemote", "remote") is True):
        location = "Remote"

    return {
        "id": job_id,
        "title": title,
        "type": job_type,
        "company": company[:120],
        "location": location[:120],
        "budget": budget_of(d),
        "description": clean_text(pick(d, "description", "shortDescription", "summary", "brief") or "", 280),
        "url": url_value or "",
        "haystack": " ".join(haystack_of(d))[:6000],
    }


def walk_json(node, found, depth=0):
    if depth > 40:
        return
    if isinstance(node, dict):
        job = job_from_dict(node)
        if job:
            found.append(job)
        for value in node.values():
            if isinstance(value, (dict, list)):
                walk_json(value, found, depth + 1)
    elif isinstance(node, list):
        for value in node:
            if isinstance(value, (dict, list)):
                walk_json(value, found, depth + 1)


GENERIC_LINK_TEXT = {"apply", "apply now", "save job", "view job", "learn more", "view all jobs", "share"}


def job_from_link(link):
    """Build a job from a link found on the page: {href, title, text}."""
    job_id, job_type = parse_job_url(link.get("href", ""))
    if not job_id:
        return None
    card_text = link.get("text") or ""
    lines = [clean_text(l) for l in card_text.splitlines() if clean_text(l)]
    lines = [l for l in lines if l.lower() not in GENERIC_LINK_TEXT and l.lower() != "pro"]
    raw_title = link.get("title") or ""
    title = clean_text(raw_title, 200)
    if "\n" in raw_title.strip() or not title or len(title) > 160 or title.lower() in GENERIC_LINK_TEXT:
        title = lines[0][:200] if lines else ""
    if not title:
        return None
    money = MONEY_RE.search(card_text)
    return {
        "id": job_id,
        "title": title,
        "type": job_type or "",
        "company": "",
        "location": "",
        "budget": money.group(0).strip() if money else "",
        "description": "",
        "url": link.get("href", ""),
        "haystack": clean_text(card_text)[:3000],
    }


def tidy_url(url):
    """Drop tracking parameters from path-style job links."""
    if JOB_PATH_RE.search(url):
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return url


def build_url(job):
    slug = quote(job["title"], safe="")
    if job["type"] == "fulltime":
        return f"{BASE}/joblist/fulltime/{job['id']}/{slug}"
    if job["type"] == "freelance" or job["budget"]:
        return f"{BASE}/joblist/freelance/{job['id']}/{slug}"
    return f"{BASE}/joblist/{job['id']}/{slug}"


def merge_jobs(layers):
    """layers = [network_jobs, embedded_jobs, dom_jobs] in priority order."""
    merged = {}
    for layer in layers:
        for job in layer:
            current = merged.get(job["id"])
            if current is None:
                merged[job["id"]] = dict(job)
                continue
            for field in ("title", "type", "company", "location", "budget", "description"):
                if not current[field] and job[field]:
                    current[field] = job[field]
            if job["url"] and "/joblist/apply/" not in job["url"] and (
                    not current["url"] or "/joblist/apply/" in current["url"]
                    or (not JOB_PATH_RE.search(current["url"]) and JOB_PATH_RE.search(job["url"]))):
                current["url"] = job["url"]
            current["haystack"] = (current["haystack"] + " " + job["haystack"])[:9000]
    for job in merged.values():
        if job["url"].startswith("/"):
            job["url"] = BASE + job["url"]
        if not job["url"] or "/joblist/apply/" in job["url"]:
            job["url"] = build_url(job)
        else:
            job["url"] = tidy_url(job["url"])
    return merged


# --------------------------------------------------------------------------
# Browser
# --------------------------------------------------------------------------

BLOCK_MARKERS = ("access denied", "captcha", "verify you are human", "unusual traffic",
                 "request blocked", "are you a robot", "pardon our interruption")

LINKS_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  const nodes = document.querySelectorAll('a[href*="joblist"], a[href*="obId="]');
  for (const a of nodes) {
    const href = a.href || '';
    if (!/\d{3,}/.test(href) || seen.has(href)) continue;
    seen.add(href);
    const card = a.closest('[class*="ard"], li, article') || a;
    const cardText = (card.innerText || '').trim();
    out.push({
      href,
      title: (a.getAttribute('aria-label') || a.getAttribute('title') || a.innerText || '').trim(),
      text: (cardText.length < 1500 ? cardText : (a.innerText || '')).slice(0, 1500),
    });
  }
  return out;
}
"""

EMBEDDED_JS = r"""
() => Array.from(document.querySelectorAll(
        'script[type="application/json"], script[type="application/ld+json"]'))
      .map(s => s.textContent || '').filter(t => t.length > 20 && t.length < 8000000)
"""


def launch_browser(playwright, headed):
    """Prefer the real Chrome/Edge installed on the machine, then Playwright's own."""
    errors = []
    for options in ({"channel": "chrome"}, {"channel": "msedge"}, {"channel": "chromium"}, {}):
        try:
            browser = playwright.chromium.launch(headless=not headed, **options)
            log(f"Browser: {options.get('channel', 'playwright default')} {browser.version}")
            return browser
        except Exception as exc:  # noqa: BLE001 - try the next browser
            errors.append(f"{options.get('channel', 'default')}: {str(exc).splitlines()[0]}")
    log("ERROR: no browser could be started.")
    for line in errors:
        log("  -", line)
    log("Fix: install Google Chrome, or run:  python -m playwright install chromium")
    sys.exit(2)


def scrape_url(context, url, scroll_rounds, index):
    """Return (jobs_by_id, problem). problem is '' when the page loaded fine."""
    page = context.new_page()
    json_responses = []

    def on_response(response):
        try:
            if "json" in (response.headers.get("content-type") or "").lower():
                json_responses.append(response)
        except Exception:  # noqa: BLE001
            pass

    page.on("response", on_response)
    status = 0
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
        status = response.status if response else 0
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:  # noqa: BLE001 - busy pages never go fully idle
            pass
        for selector in ("#onetrust-accept-btn-handler", "#onetrust-reject-all-handler"):
            try:
                button = page.locator(selector)
                if button.count() and button.first.is_visible():
                    button.first.click(timeout=3000)
                    break
            except Exception:  # noqa: BLE001
                pass
        for _ in range(max(0, int(scroll_rounds))):
            page.mouse.wheel(0, 3500)
            page.wait_for_timeout(1300)
        page.wait_for_timeout(800)

        network_jobs, embedded_jobs, dom_jobs = [], [], []
        for resp in list(json_responses):
            try:
                walk_json(resp.json(), network_jobs)
            except Exception:  # noqa: BLE001 - body gone or not JSON
                pass
        for blob in page.evaluate(EMBEDDED_JS):
            try:
                walk_json(json.loads(blob), embedded_jobs)
            except Exception:  # noqa: BLE001
                pass
        for link in page.evaluate(LINKS_JS):
            job = job_from_link(link)
            if job:
                dom_jobs.append(job)

        jobs = merge_jobs([network_jobs, embedded_jobs, dom_jobs])
        log(f"  [{index}] HTTP {status} | network {len({j['id'] for j in network_jobs})}"
            f" | embedded {len({j['id'] for j in embedded_jobs})}"
            f" | links {len({j['id'] for j in dom_jobs})} | total {len(jobs)}")

        problem = ""
        if not jobs:
            title = (page.title() or "").lower()
            body = ""
            try:
                body = (page.inner_text("body", timeout=3000) or "")[:3000].lower()
            except Exception:  # noqa: BLE001
                pass
            if status in (401, 403, 429, 503) or any(m in title or m in body for m in BLOCK_MARKERS):
                problem = f"Behance blocked the request (HTTP {status})"
            elif status >= 400:
                problem = f"Behance returned HTTP {status}"
            else:
                problem = "the page loaded but no jobs were found (layout may have changed)"
            save_debug(page, json_responses, index)
        return jobs, problem
    except Exception as exc:  # noqa: BLE001
        save_debug(page, json_responses, index)
        return {}, f"could not load the page ({str(exc).splitlines()[0][:140]})"
    finally:
        page.close()


def save_debug(page, json_responses, index):
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        page.screenshot(path=str(DEBUG_DIR / f"page{index}.png"), full_page=False)
        (DEBUG_DIR / f"page{index}.html").write_text(page.content(), encoding="utf-8")
        urls = [f"{r.status} {r.url}" for r in json_responses]
        (DEBUG_DIR / f"page{index}_json_responses.txt").write_text("\n".join(urls), encoding="utf-8")
        log(f"  Debug files saved in {DEBUG_DIR.name}/")
    except Exception as exc:  # noqa: BLE001
        log(f"  (could not save debug files: {exc})")


def scrape_all(cfg, headed):
    all_jobs, problems = {}, []
    with sync_playwright() as playwright:
        browser = launch_browser(playwright, headed)
        context = browser.new_context(locale="en-US", viewport={"width": 1366, "height": 900})
        try:
            for index, url in enumerate(cfg["search_urls"], start=1):
                log(f"Checking {url}")
                jobs, problem = scrape_url(context, url, cfg["scroll_rounds"], index)
                if problem:
                    problems.append(problem)
                    log(f"  PROBLEM: {problem}")
                for job_id, job in jobs.items():
                    all_jobs.setdefault(job_id, job)
                time.sleep(2)
        finally:
            context.close()
            browser.close()
    return all_jobs, problems


# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------

def keyword_hit(keywords, text):
    for keyword in keywords:
        keyword = str(keyword).strip().lower()
        if keyword and re.search(r"(?<![a-z0-9])" + re.escape(keyword), text):
            return keyword
    return None


def matches(job, cfg):
    if cfg["job_types"] and job["type"] and job["type"] not in cfg["job_types"]:
        return False
    text = f"{job['title']} {job['description']} {job['haystack']}".lower()
    if keyword_hit(cfg.get("exclude_keywords", []), text):
        return False
    include = [k for k in cfg.get("include_keywords", []) if str(k).strip()]
    return True if not include else bool(keyword_hit(include, text))


# --------------------------------------------------------------------------
# Discord
# --------------------------------------------------------------------------

def webhook_url():
    url = (os.environ.get("DISCORD_WEBHOOK_URL") or "").strip().strip('"').strip("'")
    if not url:
        return ""
    if "/api/webhooks/" not in url or not url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
        log("ERROR: DISCORD_WEBHOOK_URL does not look like a Discord webhook link.")
        log("It should start with https://discord.com/api/webhooks/ ...")
        sys.exit(2)
    return url


def discord_send(url, payload):
    payload = dict(payload, username="Behance Jobs", allowed_mentions={"parse": []})
    for attempt in range(5):
        try:
            response = requests.post(url, params={"wait": "true"}, json=payload, timeout=25)
        except requests.RequestException as exc:
            log(f"  Discord network error ({type(exc).__name__}); retrying")
            time.sleep(3 + attempt * 2)
            continue
        if response.status_code == 429:
            try:
                wait = float(response.json().get("retry_after", 2))
            except (ValueError, AttributeError):
                wait = 2.0
            time.sleep(min(wait, 30) + 0.5)
            continue
        if response.status_code >= 500:
            time.sleep(3 + attempt * 2)
            continue
        if response.status_code in (401, 403, 404):
            log(f"  Discord rejected the webhook (HTTP {response.status_code}). "
                "The webhook link is wrong or was deleted - create a new one and update the secret.")
            return False
        if response.ok:
            return True
        log(f"  Discord error HTTP {response.status_code}: {response.text[:200]}")
        return False
    return False


def job_message(job):
    type_label = {"freelance": "Freelance", "fulltime": "Full-time"}.get(job["type"], "")
    headline = f"🆕 **{job['title'][:180]}**"
    extras = [x for x in (job["budget"], type_label) if x]
    if extras:
        headline += "  ·  " + "  ·  ".join(extras)
    fields = []
    for name, value in (("Budget", job["budget"]), ("Type", type_label),
                        ("Client", job["company"]), ("Location", job["location"])):
        if value:
            fields.append({"name": name, "value": value[:200], "inline": True})
    embed = {
        "title": job["title"][:250],
        "url": job["url"],
        "color": 0x0057FF,
        "fields": fields,
        "footer": {"text": f"Behance job #{job['id']}"},
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    if job["description"]:
        embed["description"] = job["description"]
    return {"content": headline, "embeds": [embed]}


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    for stream in (sys.stdout, sys.stderr):      # Windows consoles + emoji
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="Behance job monitor")
    parser.add_argument("--test", action="store_true", help="send a Discord test message and exit")
    parser.add_argument("--dry-run", action="store_true", help="print only; send and save nothing")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    args = parser.parse_args()

    cfg = load_config()
    hook = webhook_url()

    if args.test:
        if not hook:
            log("ERROR: DISCORD_WEBHOOK_URL is not set.")
            return 2
        ok = discord_send(hook, {"content": "✅ Test message from your Behance job monitor. Discord is connected."})
        log("Test message sent." if ok else "Test message FAILED.")
        return 0 if ok else 3

    if not hook and not args.dry_run:
        log("ERROR: DISCORD_WEBHOOK_URL is not set. Add it as a secret (GitHub) "
            "or environment variable (PC), or use --dry-run.")
        return 2

    state = load_state()
    today = dt.date.today().isoformat()
    jobs, problems = scrape_all(cfg, args.headed)
    log(f"Jobs found on Behance right now: {len(jobs)}")

    # ---- nothing readable: treat as a failed run -------------------------
    if not jobs:
        state["fail_streak"] = state.get("fail_streak", 0) + 1
        reason = problems[0] if problems else "no jobs were found"
        log(f"FAILED run #{state['fail_streak']}: {reason}")
        if not args.dry_run:
            if state["fail_streak"] == FAIL_ALERT_AFTER:
                discord_send(hook, {"content": (
                    f"⚠️ **Behance monitor can't read the job list** ({FAIL_ALERT_AFTER} runs in a row).\n"
                    f"Reason: {reason}\nNo job alerts will arrive until this is fixed.")})
            save_state(state)
        return 1

    was_failing = state.get("fail_streak", 0) >= FAIL_ALERT_AFTER
    state["fail_streak"] = 0
    by_newest = sorted(jobs.values(), key=lambda j: int(j["id"]), reverse=True)
    matching_now = [j for j in by_newest if matches(j, cfg)]

    # ---- first run: remember everything, send a confirmation --------------
    if not state.get("initialized"):
        log(f"First run. {len(matching_now)} of {len(jobs)} current jobs match your filters.")
        samples = matching_now[:3]
        if args.dry_run:
            for job in matching_now:
                log("  match:", job["title"], "|", job["budget"], "|", job["url"])
            return 0
        ok = discord_send(hook, {"content": (
            "✅ **Behance job monitor is live.**\n"
            f"Jobs on the page right now: **{len(jobs)}** · matching your filters: **{len(matching_now)}**\n"
            "From now on you get a message here for every new matching job."
            + ("\nLatest matching jobs as a sample:" if samples else ""))})
        if not ok:
            return 3
        for job in reversed(samples):
            discord_send(hook, job_message(job))
            time.sleep(1)
        for job_id in jobs:
            state["seen"][job_id] = today
        state["initialized"] = True
        save_state(state)
        return 0

    # ---- normal run -------------------------------------------------------
    new_jobs = [j for j in by_newest if j["id"] not in state["seen"]]
    to_alert = [j for j in new_jobs if matches(j, cfg)]
    alert_ids = {j["id"] for j in to_alert}
    log(f"New jobs: {len(new_jobs)} | matching your filters: {len(to_alert)}")

    if args.dry_run:
        for job in to_alert:
            log("  would alert:", job["title"], "|", job["budget"], "|", job["url"])
        return 0

    if was_failing:
        discord_send(hook, {"content": "✅ Behance monitor is reading the job list again."})

    for job in new_jobs:
        if job["id"] not in alert_ids:
            state["seen"][job["id"]] = today          # filtered out: never alert

    limit = max(1, int(cfg.get("max_alerts_per_run", 15)))
    send_now, overflow = to_alert[:limit], to_alert[limit:]
    failed = 0
    for job in reversed(send_now):                    # oldest first, newest last
        if discord_send(hook, job_message(job)):
            state["seen"][job["id"]] = today
            log("  alerted:", job["title"])
        else:
            failed += 1                               # stays unseen -> retried next run
        time.sleep(1)
    if overflow and not failed:
        if discord_send(hook, {"content": f"…and **{len(overflow)}** more new matching jobs: "
                                          f"{cfg['search_urls'][0]}"}):
            for job in overflow:
                state["seen"][job["id"]] = today

    save_state(state)
    if failed:
        log(f"{failed} alert(s) could not be delivered; they will be retried next run.")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
