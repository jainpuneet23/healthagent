# HealthAgent — Project Specification

## Product Overview

HealthAgent is a personal health tracking and analysis server. It ingests health metrics from Apple Health (via the "Health Auto Export" iOS app), stores them in a SQLite database, runs daily AI analysis against a personal baseline, and presents the results on a dark-themed web dashboard.

**Target user:** An individual who wants a private, self-hosted health coach that summarises their daily metrics and highlights trends against their own baseline — not against generic population norms.

---

## Features

### 1. Webhook Ingestion
- Accepts POST payloads from the "Health Auto Export" iOS app in Apple Health JSON format
- Parses and normalises 16 metric types into a flat `(date, metric_type, value, unit)` structure
- Upserts data: existing records for the same `(user, date, metric_type)` are replaced on re-send
- Each user gets a unique token URL for their webhook; no shared endpoints

### 2. Debug Webhook
- `POST /debug/webhook/{token}` parses the payload and returns parsed records **without writing to DB**
- Raw payload always saved to `debug_payload.json` on disk for inspection
- Useful when configuring a new data source or testing payload format

### 3. AI Daily Analysis
- Runs every day at 08:00 AM IST via APScheduler
- Can also be triggered manually via `POST /admin/run-analysis`
- Compares yesterday's metrics against the user's own 30-day rolling baseline
- Generates a structured summary using OpenAI gpt-4o-mini (see AI Summary Format below)
- Saves result to `AISummary` table (upsert per user/date)

### 4. Dashboard
- Server-side rendered HTML (Jinja2 + Chart.js)
- Shows: health score badge, latest AI summary, yesterday's key metrics, 6 interactive 30-day charts
- Charts: Steps, Sleep, Resting HR, HRV, SpO2, Active Calories
- URL: `/dashboard/{user_token}` — shareable, token-protected
- Copy-to-clipboard button for dashboard URL

### 5. Raw Data API
- `GET /api/data/{user_token}?days=N` returns JSON array of all metrics for the last N days (default 30)

### 6. User Management
- Users created via `setup.py` CLI (not via API)
- Each user gets a UUID token used for all URLs
- No passwords or login UI

---

## API Reference

### `POST /webhook/{user_token}`
Receive health data from Health Auto Export iOS app.

**Request body:** Health Auto Export JSON (see Webhook Payload Format below)

**Response:**
```json
{ "status": "ok", "records_saved": 42, "user": "Puneet" }
```

**Errors:** `404` if token not found, `400` if invalid JSON

---

### `POST /debug/webhook/{user_token}`
Parse payload and return normalised records without saving to DB.

**Response:**
```json
{
  "raw_metric_names": ["step_count", "heart_rate", ...],
  "parsed_records_count": 38,
  "parsed_records": [...],
  "debug_payload_saved_to": "/path/to/debug_payload.json"
}
```

---

### `GET /dashboard/{user_token}`
Returns the HTML dashboard page.

---

### `GET /api/data/{user_token}`
Returns raw metrics as JSON.

**Query params:** `days` (integer, default 30)

**Response:**
```json
{
  "user": "Puneet",
  "metrics": [
    { "date": "2026-03-17", "type": "steps", "value": 8234.0, "unit": "count" }
  ]
}
```

---

### `POST /admin/run-analysis`
Manually trigger AI analysis for all users.

**Request body:**
```json
{ "secret": "your-admin-secret" }
```

**Response:**
```json
{
  "status": "ok",
  "analyses": [
    { "user": "Puneet", "result": "analysis saved for 2026-03-17, score=7" }
  ]
}
```

**Errors:** `403` if secret is wrong

---

### `GET /`
Health check. Returns `{"message": "HealthAgent API is running..."}`.

---

## Webhook Payload Format (Health Auto Export)

```json
{
  "data": {
    "metrics": [
      {
        "name": "step_count",
        "units": "count",
        "data": [
          { "date": "2024-03-16 00:00:00 +0530", "qty": 8234 }
        ]
      },
      {
        "name": "sleep_analysis",
        "units": "hr",
        "data": [
          {
            "date": "2024-03-16 00:00:00 +0530",
            "totalSleep": 7.5,
            "core": 4.2,
            "deep": 1.1,
            "rem": 2.2,
            "awake": 0.3
          }
        ]
      }
    ]
  }
}
```

**Date formats accepted:** `"2024-03-16 00:00:00 +0530"`, `"2024-03-16 00:00:00"`, `"2024-03-16"`

**Value fields:** `qty` (primary), `Avg` / `avg` (fallback for heart rate averages)

**Sleep note:** Zepp/Health Auto Export sends sleep stage durations in **hours**; the parser multiplies by 60 to store as minutes. Legacy Apple Health format uses `HKCategoryValue*` keys with values already in minutes.

---

## Supported Metrics

| Internal name | Apple Health name | Aggregation | Stored unit | Dashboard label |
|---------------|-------------------|-------------|-------------|-----------------|
| `steps` | `step_count` | sum | count | Steps |
| `distance_km` | `walking_running_distance` | sum | km | Distance |
| `active_calories` | `active_energy_burned`, `active_energy` | sum | kcal | Active Calories |
| `exercise_minutes` | `apple_exercise_time` | sum | minutes | Exercise |
| `stand_hours` | `apple_stand_hour` | sum | count | Stand Hours |
| `resting_hr` | `resting_heart_rate` | avg | bpm | Resting Heart Rate |
| `avg_hr` | `heart_rate` | avg | bpm | Avg Heart Rate |
| `hrv` | `heart_rate_variability_sdnn` | avg | ms | HRV (SDNN) |
| `sleep_total` | `sleep_analysis` → `totalSleep` / `qty` | — | minutes | Total Sleep |
| `sleep_core` | `sleep_analysis` → `core` | — | minutes | Core Sleep |
| `sleep_deep` | `sleep_analysis` → `deep` | — | minutes | Deep Sleep |
| `sleep_rem` | `sleep_analysis` → `rem` | — | minutes | REM Sleep |
| `sleep_awake` | `sleep_analysis` → `awake` | — | minutes | Awake Time |
| `spo2` | `oxygen_saturation` | avg | % | Blood Oxygen (SpO2) |
| `respiratory_rate` | `respiratory_rate` | avg | breaths/min | Respiratory Rate |
| `mindful_minutes` | `mindful_minutes` | sum | minutes | Mindful Minutes |

---

## AI Summary Format

The AI analysis prompt instructs gpt-4o-mini to return exactly these sections:

```
**HEALTH SCORE: X/10**

**WINS 🏆**
- up to 3 positive observations vs baseline

**NEEDS ATTENTION ⚠️**
- up to 3 areas below baseline or concerning

**TODAY'S SUGGESTIONS 💡**
- 2–3 actionable suggestions based on yesterday's data

**QUICK INSIGHT**
1–2 sentences of the most important takeaway
```

Rules baked into the prompt:
- Warm, personal tone — not clinical
- References actual numbers from the data
- No medical diagnoses
- Under 300 words
- Ignores missing metrics rather than guessing

The score (integer 1–10) is extracted via regex `(\d+)\s*/\s*10` and saved separately in `AISummary.score` for badge display.

---

## Data Sources

| Source | Platform | Setup required |
|--------|----------|----------------|
| Apple Health (Health Auto Export) | iOS | Install app, point webhook URL to `/webhook/{token}` |
| Zepp/Amazfit | iOS | Enable Zepp → Apple Health sync, then use Health Auto Export |
| Zepp/Amazfit | Android | Needs custom Zepp API script or manual CSV import (not implemented) |

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `OPENAI_API_KEY` | Yes | — | OpenAI API key for gpt-4o-mini |
| `ADMIN_SECRET` | Yes | `change-me-in-production` | Password for `/admin/run-analysis` |
| `DATABASE_URL` | No | `sqlite:///health.db` | SQLAlchemy database URL |
| `ANTHROPIC_API_KEY` | No | — | Present in `.env.example` but not used by any code |
