# HealthAgent — Developer Quick Reference

## What it does
A FastAPI server that receives health data via webhook, stores it in SQLite, runs AI analysis via OpenAI (gpt-4o-mini), and serves a personal health dashboard.

## Key Files

| File | Purpose |
|------|---------|
| `main.py` | FastAPI app: routes, webhook parser, metric upsert |
| `models.py` | SQLAlchemy models: `User`, `HealthMetric`, `AISummary` |
| `health_agent.py` | AI analysis: fetches metrics, calls OpenAI, saves `AISummary` |
| `scheduler.py` | APScheduler cron job — runs `health_agent.run_all_users()` at 08:00 IST |
| `setup.py` | One-time CLI: create users, print webhook/dashboard URLs |
| `templates/base.html` | Base Jinja2 layout (header, Chart.js CDN) |
| `templates/dashboard.html` | Dashboard: score badge, AI summary, 30-day charts |
| `static/style.css` | Dark-theme CSS |
| `Procfile` | Two Railway processes: `web` and `scheduler` |
| `railway.toml` | Railway deployment config |
| `debug_payload.json` | Last raw webhook payload saved to disk (for debugging) |

## API Routes

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/webhook/{user_token}` | token in URL | Receive health data, save to DB |
| POST | `/debug/webhook/{user_token}` | token in URL | Parse only — saves raw JSON, does NOT write to DB |
| GET | `/dashboard/{user_token}` | token in URL | HTML dashboard (last 30 days) |
| GET | `/api/data/{user_token}` | token in URL | Raw JSON metrics (`?days=N`, default 30) |
| POST | `/admin/run-analysis` | `{ "secret": ADMIN_SECRET }` in body | Trigger AI analysis for all users |
| GET | `/` | none | Health check |

## Data Models

```
User:          id, name, token (UUID, unique), created_at
HealthMetric:  id, user_id, date, metric_type (str), value (float), unit (str)
AISummary:     id, user_id, date, summary (text), score (int 1–10), created_at
```

Upsert pattern (both models): `DELETE WHERE (user_id, date, metric_type) → INSERT`

## Supported Metrics (16 types)

| Internal name | Apple Health source | Aggregation | Unit |
|---------------|---------------------|-------------|------|
| `steps` | `step_count` | sum | count |
| `distance_km` | `walking_running_distance` | sum | km |
| `active_calories` | `active_energy_burned` / `active_energy` | sum | kcal |
| `exercise_minutes` | `apple_exercise_time` | sum | minutes |
| `stand_hours` | `apple_stand_hour` | sum | count |
| `resting_hr` | `resting_heart_rate` | avg | bpm |
| `avg_hr` | `heart_rate` | avg | bpm |
| `hrv` | `heart_rate_variability_sdnn` | avg | ms |
| `sleep_total` | `sleep_analysis` (totalSleep field) | — | minutes |
| `sleep_core` | `sleep_analysis` (core field) | — | minutes |
| `sleep_deep` | `sleep_analysis` (deep field) | — | minutes |
| `sleep_rem` | `sleep_analysis` (rem field) | — | minutes |
| `sleep_awake` | `sleep_analysis` (awake field) | — | minutes |
| `spo2` | `oxygen_saturation` | avg | % |
| `respiratory_rate` | `respiratory_rate` | avg | breaths/min |
| `mindful_minutes` | `mindful_minutes` | sum | minutes |

Sleep values from Zepp (via Health Auto Export) arrive in **hours** and are converted to minutes (`× 60`) during parsing.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `OPENAI_API_KEY` | Yes | Key for OpenAI gpt-4o-mini (used in `health_agent.py`) |
| `ADMIN_SECRET` | Yes | Password for `/admin/run-analysis` |
| `DATABASE_URL` | No | Defaults to `sqlite:///health.db` |
| `ANTHROPIC_API_KEY` | No | Listed in `.env.example` but **not used** by any code |

## AI Analysis (health_agent.py)

- Uses **OpenAI `gpt-4o-mini`** — NOTE: `anthropic` is in `requirements.txt` but is **not imported or used**
- Analyzes yesterday's metrics against a 30-day baseline (baseline excludes yesterday)
- Output sections: `HEALTH SCORE X/10`, `WINS`, `NEEDS ATTENTION`, `TODAY'S SUGGESTIONS`, `QUICK INSIGHT`
- Score extracted via regex and stored as integer in `AISummary.score`

## Deployment (Railway)

```
web:       uvicorn main:app --host 0.0.0.0 --port $PORT
scheduler: python scheduler.py
```

Two separate Railway processes share the same database. Scheduler runs once on startup, then daily at 08:00 IST.

## Data Source

Designed for **Apple Health via "Health Auto Export" iOS app**.
- Zepp/Amazfit on iOS: Zepp → Apple Health sync → Health Auto Export → webhook (no code changes needed)
- Android Amazfit: requires separate Zepp API script or CSV import

## Common Operations

```bash
# Create a new user
python setup.py

# Run analysis manually (local)
python health_agent.py

# Start the server locally
uvicorn main:app --reload

# Trigger analysis via API
curl -X POST http://localhost:8000/admin/run-analysis \
  -H "Content-Type: application/json" \
  -d '{"secret": "your-admin-secret"}'
```
