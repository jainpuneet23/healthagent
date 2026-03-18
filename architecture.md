# HealthAgent — Architecture

## System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        iOS Device                               │
│  ┌──────────────────┐      ┌────────────────────────────────┐  │
│  │   Zepp / Amazfit │─────▶│  Apple Health                  │  │
│  └──────────────────┘      └──────────────┬─────────────────┘  │
│                                           │                     │
│                             ┌─────────────▼──────────────────┐ │
│                             │  Health Auto Export (iOS app)  │ │
│                             └─────────────┬──────────────────┘ │
└───────────────────────────────────────────┼─────────────────────┘
                                            │ POST /webhook/{token}
                                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                     Railway (Cloud)                             │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  Process: web  (uvicorn main:app)                        │  │
│  │                                                          │  │
│  │  main.py (FastAPI)                                       │  │
│  │  ├── POST /webhook/{token}  ──► parse_health_auto_export │  │
│  │  │                              ──► upsert HealthMetric  │  │
│  │  ├── GET  /dashboard/{token} ──► Jinja2 render           │  │
│  │  ├── GET  /api/data/{token}  ──► JSON response           │  │
│  │  └── POST /admin/run-analysis ─► health_agent.analyze_user│  │
│  └──────────────────────────────┬───────────────────────────┘  │
│                                 │                               │
│  ┌──────────────────────────────▼───────────────────────────┐  │
│  │  Process: scheduler  (python scheduler.py)               │  │
│  │                                                          │  │
│  │  APScheduler (08:00 IST daily)                           │  │
│  │  └── health_agent.run_all_users()                        │  │
│  │      └── analyze_user(user_id, db) per user              │  │
│  │          ├── fetch yesterday's HealthMetric rows         │  │
│  │          ├── fetch 30-day baseline HealthMetric rows     │  │
│  │          ├── build prompt                                │  │
│  │          ├── call OpenAI gpt-4o-mini                     │  │
│  │          └── upsert AISummary                            │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  SQLite (health.db)                                      │  │
│  │  ├── users                                               │  │
│  │  ├── health_metrics                                      │  │
│  │  └── ai_summaries                                        │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                                            │
                              ┌─────────────▼──────────────┐
                              │  OpenAI API (gpt-4o-mini)  │
                              └────────────────────────────┘
```

---

## Tech Stack

| Library | Version | Role |
|---------|---------|------|
| `fastapi` | 0.111.0 | Web framework, route definitions |
| `uvicorn` | 0.29.0 | ASGI server |
| `sqlalchemy` | ≥2.0.36 | ORM + SQLite connection pool |
| `jinja2` | 3.1.4 | Server-side HTML templating |
| `python-multipart` | 0.0.9 | Form/multipart body parsing |
| `openai` | ≥1.0.0 | gpt-4o-mini API calls (health analysis) |
| `apscheduler` | 3.10.4 | Daily cron scheduler (IST timezone) |
| `anthropic` | ≥0.40.0 | Listed in requirements.txt — **not used** |
| Chart.js | CDN | Client-side chart rendering in dashboard |

---

## Data Flow: Webhook Ingestion

```
Health Auto Export iOS app
    │
    │  POST /webhook/{user_token}
    │  Content-Type: application/json
    │  Body: { "data": { "metrics": [...] } }
    │
    ▼
main.py: receive_health_data()
    │
    ├─ 1. Validate token → get User from DB (404 if not found)
    ├─ 2. Save raw JSON to debug_payload.json (always, for debugging)
    ├─ 3. parse_health_auto_export(payload) → list of records
    │       ├─ Map Apple Health metric names → internal names
    │       ├─ For sleep_analysis: extract totalSleep, core, deep, rem, awake
    │       │   (multiply hours × 60 → minutes for Zepp data)
    │       └─ For all others: aggregate per day (sum or avg)
    └─ 4. Upsert each record:
            DELETE WHERE (user_id, date, metric_type)
            INSERT HealthMetric(user_id, date, metric_type, value, unit)
            db.commit()
    │
    ▼
Response: { "status": "ok", "records_saved": N, "user": name }
```

---

## Data Flow: AI Analysis

```
Trigger: APScheduler at 08:00 IST  OR  POST /admin/run-analysis
    │
    ▼
health_agent.run_all_users()
    │
    └─ For each User in DB:
           analyze_user(user_id, db)
               │
               ├─ 1. Fetch yesterday's HealthMetric rows
               │       If empty → skip with message "no data for {date}"
               │
               ├─ 2. Fetch 30-day baseline rows (excludes yesterday)
               │       Group by metric_type → compute mean per type
               │
               ├─ 3. Build prompt string:
               │       - User's name
               │       - Date analysed
               │       - Baseline averages block (ordered list)
               │       - Yesterday's data block
               │       - Output format instructions
               │
               ├─ 4. Call OpenAI:
               │       model=gpt-4o-mini, max_tokens=600
               │       messages=[{"role": "user", "content": prompt}]
               │
               ├─ 5. Extract score via regex: (\d+)\s*/\s*10
               │
               └─ 6. Upsert AISummary:
                       DELETE WHERE (user_id, date=yesterday)
                       INSERT AISummary(user_id, date, summary, score)
                       db.commit()
```

---

## Data Flow: Dashboard Render

```
Browser: GET /dashboard/{user_token}
    │
    ▼
main.py: dashboard()
    │
    ├─ 1. Validate token → get User
    ├─ 2. Query HealthMetric: last 30 days, ordered by date
    │       → metric_data: { metric_type: { "YYYY-MM-DD": value } }
    ├─ 3. Query AISummary: latest by date desc → summary object
    ├─ 4. Query HealthMetric: yesterday only → yesterday_dict
    └─ 5. Render templates/dashboard.html with context:
               user, metric_data_json, summary,
               yesterday (dict), yesterday_date, user_token
    │
    ▼
templates/dashboard.html (Jinja2)
    ├─ Extends base.html (Chart.js CDN)
    ├─ Renders score badge, AI summary (collapsible)
    ├─ Renders "yesterday at a glance" metric cards
    └─ Renders 6 Chart.js charts from metric_data_json
           (all data pre-rendered; no frontend API calls)
```

---

## Database Schema

### `users`
| Column | Type | Constraints |
|--------|------|-------------|
| `id` | INTEGER | PK, autoincrement |
| `name` | VARCHAR | NOT NULL |
| `token` | VARCHAR | NOT NULL, UNIQUE, indexed |
| `created_at` | DATETIME | default: utcnow |

### `health_metrics`
| Column | Type | Constraints |
|--------|------|-------------|
| `id` | INTEGER | PK, autoincrement |
| `user_id` | INTEGER | NOT NULL, indexed |
| `date` | DATE | NOT NULL, indexed |
| `metric_type` | VARCHAR | NOT NULL |
| `value` | FLOAT | NOT NULL |
| `unit` | VARCHAR | NOT NULL |

Unique constraint enforced at application level: upsert via `DELETE + INSERT` on `(user_id, date, metric_type)`.

### `ai_summaries`
| Column | Type | Constraints |
|--------|------|-------------|
| `id` | INTEGER | PK, autoincrement |
| `user_id` | INTEGER | NOT NULL, indexed |
| `date` | DATE | NOT NULL, indexed |
| `summary` | TEXT | NOT NULL |
| `score` | INTEGER | nullable (1–10) |
| `created_at` | DATETIME | default: utcnow |

Upsert enforced at application level: `DELETE + INSERT` on `(user_id, date)`.

---

## Metric Processing Rules

### Aggregation
- **Sum:** `steps`, `active_calories`, `exercise_minutes`, `stand_hours`, `distance_km`
- **Average:** `resting_hr`, `avg_hr`, `hrv`, `spo2`, `respiratory_rate`
- **Sleep:** parsed from `sleep_analysis` metric — each stage is a separate field, not aggregated

### Sleep Unit Conversion
- Zepp/Health Auto Export sends sleep durations in **hours** (`totalSleep`, `core`, `deep`, `rem`, `awake`)
- Parser converts: `minutes = hours × 60` (rounded to 1 decimal)
- Legacy Apple Health format uses `HKCategoryValueSleepAnalysis*` keys already in minutes

### Date Parsing
`_parse_date()` tries these formats in order:
1. `"%Y-%m-%d %H:%M:%S %z"` (with timezone)
2. `"%Y-%m-%d %H:%M:%S"` (without timezone)
3. `"%Y-%m-%d"` (date only)
4. `date.fromisoformat(raw[:10])` (fallback)

---

## Authentication Model

| Resource | Mechanism |
|----------|-----------|
| User webhook/dashboard/API | UUID token in URL path — knowledge-based, no session |
| Admin analysis trigger | `{ "secret": ADMIN_SECRET }` in POST body |
| No auth | `GET /` health check |

There is no login UI, password system, or session management. Tokens are created once by `setup.py` and do not expire.

---

## Deployment (Railway)

### Two-process setup (Procfile)
```
web:       uvicorn main:app --host 0.0.0.0 --port $PORT
scheduler: python scheduler.py
```

Both processes share the same SQLite file (`health.db`). SQLAlchemy is configured with `check_same_thread=False` to support this.

### railway.toml
```toml
[build]
builder = "NIXPACKS"

[deploy]
startCommand = "uvicorn main:app --host 0.0.0.0 --port $PORT"
healthcheckPath = "/"
healthcheckTimeout = 30
restartPolicyType = "ON_FAILURE"
```

### Scheduler behaviour
- `scheduler.py` calls `run_all_users()` **once immediately on startup** (so you get output right away)
- Then schedules it to run again at 08:00 IST every day using `BlockingScheduler` with `Asia/Kolkata` timezone

---

## Known Issues / Anomalies

| Issue | Detail |
|-------|--------|
| `anthropic` in requirements.txt but unused | The package is listed but never imported. All AI calls use the `openai` library targeting gpt-4o-mini. Can be safely removed from requirements.txt. |
| SQLite write contention | Both `web` and `scheduler` processes write to the same SQLite file. This works at low concurrency but could cause locking errors under heavy load. PostgreSQL would be more appropriate for production. |
| No FK constraints | `health_metrics.user_id` and `ai_summaries.user_id` are not declared as foreign keys in the ORM — referential integrity is managed at the application level only. |
| Tokens never expire | UUID tokens are permanent. There is no token rotation or revocation mechanism. |
