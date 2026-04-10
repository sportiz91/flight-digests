# Flight Digests

Daily flight price monitor: BCN, MAD and LIS → EZE (Buenos Aires), delivered to a Telegram bot via GitHub Actions.

## What it does

Twice a day (06:13 and 18:13 UTC) a GitHub Action:

1. Scrapes Google Flights for ~18 round-trip combinations (3 origins × 6 date pairs in the +60..+180 day window)
2. Compares today's cheapest options vs the previous run (price history committed back to the repo)
3. Asks Groq (Llama 3.3 70B, free) for a rioplatense analysis: best deal, buy-now-or-wait, patterns
4. Sends an HTML digest to a private Telegram bot

Cero servidor. Cero costo (GitHub Actions free + Groq free + Google Flights gratis).

## Architecture

```
┌──────────────────────┐    artifact    ┌──────────────────────┐
│  Job 1: scrape       │ ─────────────▶ │  Job 2: notify       │
│  · fast-flights      │   results.json │  · Groq analysis     │
│  · NO secrets ❌     │                │  · Telegram send     │
│  · Google Flights    │                │  · Commit history    │
└──────────────────────┘                └──────────────────────┘
                                                  │
                                                  ▼
                                          Your Telegram bot
```

## Setup

1. Fork this repo (or clone and push to your own)
2. Add secrets in **Settings → Secrets and variables → Actions**:
   - `FLIGHT_TELEGRAM_BOT_TOKEN` — from @BotFather
   - `FLIGHT_TELEGRAM_CHAT_ID` — your Telegram user ID
   - `GROQ_API_KEY` — free at console.groq.com
3. Enable Actions in the repo
4. Trigger the first run manually from the **Actions** tab → *Daily Flight Digest* → *Run workflow*

## Local development

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Just the scrape (no Telegram, prints JSON to stdout or --out file)
.venv/bin/python scripts/search_flights.py --out /tmp/results.json

# Full pipeline (needs the env vars set)
export FLIGHT_TELEGRAM_BOT_TOKEN=...
export FLIGHT_TELEGRAM_CHAT_ID=...
export GROQ_API_KEY=...
.venv/bin/python scripts/daily_flights.py --results /tmp/results.json
```

The `price_history.json` file is created on first run and tracks the cheapest price per origin per day so the next run can show drops/raises.

## Configuration

Edit `scripts/search_flights.py`:

| Constant | Default | What it controls |
|---|---|---|
| `ORIGINS` | `["BCN", "MAD", "LIS"]` | Departure airports |
| `DESTINATION` | `"EZE"` | Arrival airport |
| `DAYS_OUT_MIN/MAX` | `60..180` | Outbound date window |
| `TRIP_LEN_MIN/MAX` | `14..30` | Trip duration in days |
| `COMBOS_PER_ORIGIN` | `6` | Date pairs sampled per origin per run |
| `MAX_STOPS` | `2` | Max connections accepted |
| `SEAT` | `"economy"` | Cabin class |

## Security notes

This project uses [`fast-flights`](https://github.com/AWeirdDev/flights), a solo-maintained PyPI package that scrapes Google Flights. It was security-audited before adoption (no malicious code, signed via Sigstore Trusted Publishing, dependencies all sane). The audit found one yellow flag: it's a one-person hobby project, so future supply-chain risk exists.

**Mitigation: split-job architecture.** The `scrape` job runs `fast-flights` in a process that has **no secrets** in its environment — neither the Telegram bot token nor the Groq API key. Even if a future malicious release of `fast-flights` (or its dependencies `primp`, `protobuf`, `selectolax`, `rjsonc`) tries to harvest credentials, there is nothing to steal in that job. The `notify` job has the secrets but only runs trusted code (`urllib` stdlib calls to api.telegram.org and api.groq.com) — it never imports `fast-flights`.

**Pinned versions** in `requirements.txt`. Dependabot is recommended for CVE alerts.

> **Note on `fast-flights==3.0rc0`:** the stable v2.2 was the audited version, but Google Flights changed its HTML structure in early 2026 and v2.2's CSS-selector parser is no longer functional. v3.0rc0 parses Google's embedded JS data blob instead, which is structurally more robust. Same author, same Sigstore signing, same risk profile. The split-jobs architecture makes the version choice moot from a security standpoint.

## Why fast-flights and not an official API?

| Option | Verdict |
|---|---|
| **Amadeus Self-Service** | Decommissioned 2026-07-17 ❌ |
| **Skyscanner Partners API** | Application-only, partners only ❌ |
| **Kiwi Tequila** | Free dev tier exists but signup pushes you into commercial track ⚠️ |
| **Duffel** | Excellent API but charges per search beyond a search-to-book ratio (we never book) ❌ |
| **Apify Google Flights actor** | Works, ~$0.30/month, no dependency in our env ✅ (fallback) |
| **fast-flights + Google Flights** | $0, MIT, audited, robust JS parser, our pick ✅ |
