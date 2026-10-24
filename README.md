# APM Notifier

APM Notifier watches official company career searches and sends a one-time phone alert when it finds a new:

- Associate, Rotational, Apprentice, Junior, Entry-Level, or Early-Career Product Manager / Product Owner / Product Builder roles
- Bachelor-eligible Graduate or New Grad Product Manager, Product Strategy, Product Operations, Product Solutions, and Product Marketing roles
- Non-senior Product Management specialist, analyst, and coordinator roles across monitored companies
- Product Manager / Product Management internship or co-op
- Technical or Growth Product Manager internship
- Non-senior Growth Product Manager roles at Anthropic and OpenAI
- Product Marketing or Product Marketing Manager internship
- Summer marketing internships and junior marketing associate/analyst/specialist/coordinator roles; graduate campaign, advertising, growth, and account roles
  at the requested companies

The configuration monitors 53 career feeds and job-network targets, including Revolut, Figma, Questrade, Google, Meta, Amazon, Microsoft, Apple, TikTok, Autodesk Canada-wide, Anthropic, Coinbase, DoorDash, Duolingo, Electronic Arts, Instacart, LinkedIn, Lyft, Reddit, Robinhood, Shopify, Snap, Spotify, Square/Block, Visa, Zynga, NVIDIA, Mastercard, Wealthsimple, OpenAI, Lime, Stripe, Uber, Airbnb, Adobe, and several others. Wellfound is present but disabled because its job pages require an interactive session. A separate [new-grad community tracker](https://github.com/SimplifyJobs/New-Grad-Positions), with live employer-link verification, and the US-wide [Summer 2027 community tracker](https://github.com/sndsh404/summer-2027-internships) are monitored as independent back-checks, so matching roles from companies outside that fixed list can also be detected. The original target list was seeded from the [Extern Summer 2027 PM guide](https://www.extern.com/post/product-management-internships-summer-2027-guide).

## What it does

- Checks the configured career searches every five minutes by default.
- Scans every result page for configured Workday, Dayforce, LinkedIn, and Disney feeds; resolves anonymous Dayforce search sessions.
- Scans TikTok's official public API across the entire campus feed plus an unrestricted graduate search, with all result pages and no upstream city or job-function exclusions. Local role, year, country, and education filters then select relevant openings.
- Starts TikTok first and sends its new matches as soon as its scan finishes, without waiting for slower company feeds. Qualifications come directly from the API to avoid extra page fetches.
- Uses employer-published LinkedIn feeds for Revolut, Uber, and HubSpot where direct feeds are blocked or retired.
- Keeps distinct postings with the same title separate and reads all provided locations before filtering.
- Accepts titles mentioning 2027 or no year and rejects explicitly older-year titles.
- Rejects non-summer internships, while allowing full-time graduate cohorts that start in fall, winter, or spring.
- Only alerts for roles with a recognizable US, Canadian, or UK location. Ambiguous `Remote` or missing locations are excluded.
- Checks official qualifications for graduate matches from configured sources and excludes roles that explicitly require a master's, MBA, or PhD while retaining bachelor's-eligible alternatives.
- Treats non-senior Growth Product Manager titles at Anthropic and OpenAI as eligible even when they are not labeled as internships or graduate roles; preferred experience language does not suppress them.
- Stores a fingerprint for every match in SQLite, so a role alerts only once.
- Retries temporary HTTP failures and checks sources concurrently.
- Uses headless Chromium only for the few JavaScript-only searches; official JSON/HTML stays on the faster path.
- Rejects empty JavaScript career shells instead of counting them as healthy job feeds.
- Warns after TikTok cannot be fully checked for 15 minutes; other source warnings wait six hours. Repeated outage warnings remain limited to weekly.
- Keeps undelivered roles pending if your phone notification provider is temporarily down.

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

Cloudflare dispatches the included `.github/workflows/monitor.yml` every five minutes, while GitHub's own schedule provides an hourly backup. The repository may be public, but `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` must be stored as encrypted repository secrets under **Settings → Secrets and variables → Actions**. Never commit `.env`.

The five-minute dispatch cadence is a target, not an instant-alert guarantee: runner queues, previous scans, provider rate limits, and the time before a job appears in TikTok's feed can delay detection. TikTok notifications no longer wait for the full multi-company scan once the job starts.

The workflow restores and saves the SQLite notification history through the Actions cache. Existing notification history is restored before every check. If history is missing, current matches are sent so a lost cache cannot silently suppress opportunities. Public-repository scheduled workflows may be disabled after 60 days without repository activity and should be checked periodically.

## Useful commands

```bash
# One real check: persists results and sends any pending alerts
python3 -m apm_notifier run --once

# Continuous check every two minutes
python3 -m apm_notifier run --interval 120

# Read-only live preview
python3 -m apm_notifier check

# Source health and notification backlog
python3 -m apm_notifier status

# Read stored discovery and notification timestamps without modifying history
python3 -m apm_notifier history --company TikTok

# Show all configured companies
python3 -m apm_notifier sources

# Run automated tests
python3 -m unittest discover -s tests -v
```

The cloud workflow's optional **show_tiktok_history** input runs a read-only audit independently of the full monitoring scan. It prints restored TikTok history, skips notification delivery, and preserves the cloud cache. `notified_at` is the stored notification timestamp; older silent baselines can also populate it, so corroborate a disputed historical delivery with the corresponding run's `Alert delivered` log. LinkedIn alert dates do not establish when an employer first published a job.

## Customize the targets

Edit [`config/sources.json`](config/sources.json). Each source needs a stable `id`, display `name`, official `career_url`, and one or more searchable `urls`.

```json
{
  "id": "example",
  "name": "Example Company",
  "career_url": "https://example.com/careers",
  "urls": [
    "https://example.com/careers?query=product%20manager%20intern",
    "https://example.com/careers?query=product%20marketing%20intern"
  ]
}
```

The extractor reads normal job links, JSON job APIs, JSON-LD `JobPosting` data, and embedded application state used by many JavaScript career sites. Career providers change their markup occasionally; the persisted `status` report and throttled phone health warning make those changes visible.

## First-run behavior

`ALERT_ON_FIRST_RUN=true` sends any matching role already live when the monitor first starts. Set it to `false` if you want the first successful check to establish a silent baseline and only alert for later additions.

Deleting the SQLite state intentionally resets deduplication and may alert for every currently matching job again.

## Location filtering

`ALLOWED_COUNTRIES=US,CA,UK` is the default. A posting must identify at least one allowed country, constituent region, province/state, or recognizable city. Multi-location roles qualify when at least one listed location is allowed. Generic `Remote`, worldwide, and missing-location postings are rejected because their eligibility cannot be confirmed.
