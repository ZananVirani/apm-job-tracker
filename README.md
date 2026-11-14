# APM Notifier

APM Notifier watches company career sites for **Summer 2027 product-management internships and adjacent roles**, classifies every candidate posting against strict eligibility rules, and sends a Telegram/ntfy digest of new **ACCEPT** and **REVIEW** postings.

## What it looks for

- **Core titles (high priority):** Product Manager / Product Management / PM / APM / AI / Technical Product Manager intern or co-op.
- **Adjacent titles (accepted):** Product Analyst, Owner, Coordinator, Associate, Operations, Strategy, Marketing, Specialist, Growth; Program / Technical Program Manager; Digital Operations / Product / Strategy / Transformation; Strategy & Operations; Innovation / Venture; Technology or AI Strategy. Product Engineer, Product Builder, Forward-Deployed/Solutions, Project Manager, Business Analyst and Customer Insights titles are accepted only when the description is product-oriented.
- **Any other intern title** is judged on its description (roadmaps, PRDs/specs, backlog prioritization, discovery, engineering + design partnership, product metrics). Pure software, data science/ML, UX design, sales, accounting/audit, HR, legal and admin roles are rejected.

## Eligibility rules (applied in order; the first REJECT wins)

1. **Internship, co-op or student programme.** Full-time, new-grad, contract and part-time-during-school roles are rejected. Terms must start May–Aug 2027 (4-month summers and 8/12/16-month terms starting then); Fall 2027+, Winter 2027 and 2026 starts are rejected, and "Spring 2027" or April starts go to REVIEW.
2. **Education:** must accept undergraduates. MBA/Master's/PhD-only postings are rejected; "Bachelor's or Master's" is accepted.
3. **Graduation year (April 2028):** accepted for 2028-or-later, returning-to-school, penultimate-year or rising-junior/senior language, or no stated year; rejected for 2026/2027-only, final-year or recent-graduate requirements; ambiguous wording goes to REVIEW.
4. **Location:** any Canadian location (GTA flagged as priority; Waterloo Region noted); US roles only if remote or sponsorship/CPT/OPT is supported. US roles (and roles with no stated location) that deny sponsorship or require citizenship/clearance are rejected; US onsite roles silent on sponsorship go to REVIEW. Everywhere else is rejected. Canadian work-authorization requirements are recorded.
5. **Open postings only:** closed postings and past application deadlines are rejected.

Every decision stores a one-line reason and the exact quoted text that triggered it. Rejected postings stay in SQLite so false negatives can be audited.

## Coverage

`config/sources.json` holds 25 must-include companies (scanned every run) and about 110 additional companies (scanned at most hourly). Structured adapters read official public job-board APIs — Greenhouse, Lever, Ashby, SmartRecruiters, Workday CxS (with automatic Canada/US and intern facet discovery), SAP SuccessFactors RSS, Microsoft and Eightfold — and fetch the full description for each candidate. A few sites still use the legacy HTML/JSON extractors. The Simplify Summer 2027 list and a community tracker act as secondary discovery sources; employers found only there are added to a company registry.

Disabled entries document companies whose careers sites could not be read politely (no public board, login or bot walls, robots.txt). `python3 -m apm_notifier report` shows which companies have zero matching postings versus failing adapters.

## How it behaves

- Must-include companies are checked on every run (every five minutes in the cloud); additional companies at most every `ADDITIONAL_SCAN_MINUTES`.
- Requests identify themselves with a descriptive User-Agent, are spaced at least `HOST_INTERVAL_SECONDS` apart per host (3s for LinkedIn and Microsoft), retry with exponential backoff, and check robots.txt before fetching any HTML job page. Logins, CAPTCHAs and paywalls are never bypassed.
- Descriptions are cached in SQLite and refreshed every `DESCRIPTION_REFRESH_HOURS`; at most `MAX_DESCRIPTION_FETCHES` are fetched per run and the rest are picked up next run.
- Postings are deduplicated by company + requisition id, then canonical URL, then company + title + location, so reposts and aggregator copies do not re-alert.
- Alerts are sent for new ACCEPT/REVIEW postings and for status changes (REVIEW → ACCEPT, closed, reopened). The digest is sorted by must-include company, core title, GTA, then newest.
- Each run writes `job_matches.csv` and `job_matches.json` (all open ACCEPT/REVIEW postings with the full schema), logs a per-run summary, and warns when an adapter returns zero postings for `ZERO_RESULT_WARN_RUNS` runs in a row.
- TikTok's campus feed still runs first and its digest is sent as soon as it finishes.

## Quick start with Docker

Docker is the easiest way to leave the service running on a Mac, home server, or small cloud host.

1. Create your local settings file:

   ```bash
   cp .env.example .env
   ```

2. Configure either Telegram or ntfy in `.env` using one of the sections below.

3. Verify the phone notification:

   ```bash
   docker compose run --rm notifier test-notification
   ```

4. Preview currently matching jobs without notifying or changing state:

   ```bash
   docker compose run --rm notifier check
   ```

5. Start the monitor:

   ```bash
   docker compose up --build -d
   docker compose logs -f notifier
   ```

The SQLite history lives in the persistent `notifier-state` Docker volume. Restarting or rebuilding the container does not cause duplicate alerts.

## Phone alerts

### Option A: Telegram

Telegram is private and gives each alert a tappable application link.

The easiest and safest setup is the interactive command below. It hides the token while you type, discovers your chat ID, stores the credentials in a user-readable-only `.env` file, and sends a test alert:

```bash
python3 -m apm_notifier setup-telegram
```

1. In Telegram, message `@BotFather`, run `/newbot`, and copy the bot token into `TELEGRAM_BOT_TOKEN` in `.env`.
2. Open the new bot and send it `/start`.
3. In a browser, open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` and copy the numeric `message.chat.id` into `TELEGRAM_CHAT_ID`.
4. Run the test-notification command above.

### Option B: ntfy

ntfy requires no bot account. Topics on the public ntfy server are reachable by anyone who knows their name, so use a long random topic.

1. Generate a topic name, for example with `openssl rand -hex 20`, and put it in `NTFY_TOPIC` in `.env`.
2. Install the ntfy app on your phone.
3. Subscribe in the app to `https://ntfy.sh/<YOUR_RANDOM_TOPIC>`.
4. Run the test-notification command above.

You can configure both; the service will send through both channels and considers an alert delivered if at least one succeeds.

## Run without Docker

The monitor uses only the Python standard library and needs Python 3.11 or newer.

```bash
cp .env.example .env
python3 -m apm_notifier test-notification
python3 -m apm_notifier check
python3 -m apm_notifier run
```

Your computer must remain awake and connected. For genuine 24/7 coverage, run the same Docker container on an always-on server or cloud container service with persistent storage.

## Free cloud monitoring with GitHub Actions

A free Cloudflare Worker ([`dispatcher/`](dispatcher/)) dispatches the included `.github/workflows/monitor.yml` every five minutes, while GitHub's own schedule provides an hourly backup. Deploy it with `npx wrangler deploy` from `dispatcher/` after setting `GITHUB_OWNER`/`GITHUB_REPO` in `wrangler.toml` and storing a fine-grained token (this repository only, **Actions: Read and write**) with `npx wrangler secret put GITHUB_TOKEN`. The repository may be public, but `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` must be stored as encrypted repository secrets under **Settings → Secrets and variables → Actions**. Never commit `.env`.

The five-minute dispatch cadence is a target, not an instant-alert guarantee: runner queues, previous scans, provider rate limits, and the time before a job appears in TikTok's feed can delay detection. TikTok notifications no longer wait for the full multi-company scan once the job starts.

The workflow restores and saves the SQLite notification history through the Actions cache. Existing notification history is restored before every check. If history is missing, current matches are sent so a lost cache cannot silently suppress opportunities. Each run uploads `job_matches.csv` and `job_matches.json` as the **job-matches** workflow artifact (kept for three days). Public-repository scheduled workflows may be disabled after 60 days without repository activity and should be checked periodically.

## Useful commands

```bash
# One real check: persists results and sends any pending digest
python3 -m apm_notifier run --once

# Read-only live dry run with decisions, reasons and quotes (add --all for REJECTs)
python3 -m apm_notifier check
python3 -m apm_notifier check --all --source td --source bree

# Watchlist health: zero matching postings vs adapter failures, plus discovered companies
python3 -m apm_notifier report

# Rewrite job_matches.csv / job_matches.json from stored state
python3 -m apm_notifier export

# Source health and decision counts
python3 -m apm_notifier status

# Stored postings with discovery and notification timestamps
python3 -m apm_notifier history --company TikTok

# Run automated tests
python3 -m unittest discover -s tests -v
```

## Customize the targets

Edit [`config/sources.json`](config/sources.json). Structured sources need only one line of adapter options:

```json
{"id": "bree", "name": "Bree", "tier": "must", "ats": "ashby", "board": "bree"}
{"id": "td", "name": "TD Bank Group", "ats": "workday", "host": "td.wd3.myworkdayjobs.com", "tenant": "td", "site": "TD_Bank_Careers"}
{"id": "telus", "name": "TELUS", "ats": "successfactors", "host": "careers.telus.com"}
```

Supported `ats` values: `greenhouse`, `lever`, `ashby`, `smartrecruiters` (`board`), `workday` (`host`, `tenant`, `site`), `successfactors` (`host`), `microsoft`, `eightfold` (`host`, `domain`), `atlassian`, `ibm` and `simplify` (`listings_url`). Optional `queries` override the default intern keywords. Sources without `ats` use a list of `urls` and the generic extractor. `tier` is `must` or `additional`; set `enabled: false` with a `note` to document a company that cannot be scanned.

## First-run behavior

`ALERT_ON_FIRST_RUN=true` sends any matching role already live when the monitor first starts. Set it to `false` if you want the first successful check to establish a silent baseline and only alert for later additions.

Deleting the SQLite state intentionally resets deduplication and may alert for every currently matching job again.

## Location rules

Canada is always eligible. US postings are eligible when remote or when the posting supports visa sponsorship, CPT or OPT; onsite US postings that say nothing about sponsorship are sent as REVIEW. Every other country is rejected. Location evidence comes from the listing, the job board's country codes, and the description.
