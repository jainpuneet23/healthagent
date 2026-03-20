import os
import json
from datetime import date, datetime, timedelta
from typing import Any

# Load .env file if present
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

from fastapi import FastAPI, Depends, HTTPException, Request, Header
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func

from models import create_tables, get_db, User, HealthMetric, AISummary

app = FastAPI(title="HealthAgent")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

ADMIN_SECRET = os.getenv("ADMIN_SECRET", "change-me-in-production")


@app.on_event("startup")
def startup():
    create_tables()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_user_by_token(token: str, db: Session) -> User:
    user = db.query(User).filter(User.token == token).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


def parse_health_auto_export(payload: dict) -> list[dict]:
    """
    Parse the JSON payload sent by the 'Health Auto Export' iOS app.
    The app sends data in Apple Health format. We normalise it into a flat
    list of { date, metric_type, value, unit } dicts.

    Health Auto Export sends a structure like:
    {
      "data": {
        "metrics": [
          {
            "name": "step_count",
            "units": "count",
            "data": [
              { "date": "2024-03-16 00:00:00 +0530", "qty": 8234 },
              ...
            ]
          },
          ...
        ]
      }
    }
    """
    records = []
    metrics_list = payload.get("data", {}).get("metrics", [])

    # Mapping from Health Auto Export metric names to our internal names
    METRIC_MAP = {
        # Activity
        "step_count":                    ("steps",              "count"),
        "walking_running_distance":      ("distance_km",        "km"),
        "active_energy_burned":          ("active_calories",    "kcal"),
        "active_energy":                 ("active_calories",    "kcal"),
        "apple_exercise_time":           ("exercise_minutes",   "minutes"),
        "apple_stand_hour":              ("stand_hours",        "count"),
        # Heart
        "resting_heart_rate":            ("resting_hr",         "bpm"),
        "heart_rate":                    ("avg_hr",             "bpm"),
        "heart_rate_variability_sdnn":   ("hrv",                "ms"),
        # Sleep
        "sleep_analysis":                ("sleep_total",        "minutes"),
        # SpO2
        "oxygen_saturation":             ("spo2",               "%"),
        # Breathing
        "respiratory_rate":              ("respiratory_rate",   "breaths/min"),
        # Mindfulness / Stress (if Zepp writes these)
        "mindful_minutes":               ("mindful_minutes",    "minutes"),
    }

    # Sleep sub-types from Health Auto Export (nested under sleep_analysis)
    SLEEP_STAGES = {
        "HKCategoryValueSleepAnalysisAsleepCore":    "sleep_core",
        "HKCategoryValueSleepAnalysisAsleepDeep":    "sleep_deep",
        "HKCategoryValueSleepAnalysisAsleepREM":     "sleep_rem",
        "HKCategoryValueSleepAnalysisAwake":         "sleep_awake",
    }

    for metric in metrics_list:
        name = metric.get("name", "")
        unit = metric.get("units", "")
        data_points = metric.get("data", [])

        if name == "sleep_analysis":
            # Health Auto Export (Zepp source) uses hours for sleep values
            # Keys: totalSleep, core, deep, rem, awake (all in hours)
            # Fallback: qty (minutes), HKCategoryValue* keys (minutes)
            for point in data_points:
                raw_date = point.get("date", "")
                day = _parse_date(raw_date)
                if not day:
                    continue
                # Total sleep — Zepp sends "totalSleep" in hours; fallback to "qty" in minutes
                total_hrs = point.get("totalSleep")
                qty = point.get("qty")
                if total_hrs is not None:
                    records.append({"date": day, "metric_type": "sleep_total", "value": round(float(total_hrs) * 60, 1), "unit": "minutes"})
                elif qty is not None:
                    records.append({"date": day, "metric_type": "sleep_total", "value": float(qty), "unit": "minutes"})
                # Sleep stages — Zepp sends hours; HKCategoryValue* keys send minutes
                zepp_stages = {"core": "sleep_core", "deep": "sleep_deep", "rem": "sleep_rem", "awake": "sleep_awake"}
                for key, stage_name in zepp_stages.items():
                    val = point.get(key)
                    if val is not None:
                        records.append({"date": day, "metric_type": stage_name, "value": round(float(val) * 60, 1), "unit": "minutes"})
                # Fallback: old HKCategoryValue* keys (already in minutes)
                for stage_key, stage_name in SLEEP_STAGES.items():
                    stage_val = point.get(stage_key)
                    if stage_val is not None:
                        records.append({"date": day, "metric_type": stage_name, "value": float(stage_val), "unit": "minutes"})

        elif name in METRIC_MAP:
            internal_name, internal_unit = METRIC_MAP[name]
            # Aggregate per-day (sum for steps/calories, avg for HR/HRV/SpO2)
            daily: dict[date, list[float]] = {}
            for point in data_points:
                raw_date = point.get("date", "")
                day = _parse_date(raw_date)
                if not day:
                    continue
                qty = point.get("qty") if point.get("qty") is not None else (point.get("Avg") or point.get("avg"))
                if qty is not None:
                    daily.setdefault(day, []).append(float(qty))

            for day, values in daily.items():
                if internal_name in ("steps", "active_calories", "exercise_minutes", "stand_hours", "distance_km"):
                    agg = sum(values)
                else:
                    agg = sum(values) / len(values)
                records.append({"date": day, "metric_type": internal_name, "value": round(agg, 2), "unit": internal_unit})

    return records


def _parse_date(raw: str) -> date | None:
    """Parse various date string formats from Health Auto Export."""
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:len(fmt) + 5].strip(), fmt).date()
        except ValueError:
            continue
    # Try just the date portion
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post("/webhook")
async def receive_health_data_simple(request: Request, db: Session = Depends(get_db)):
    """Tokenless webhook — writes to the first user in the DB. For single-user personal use."""
    user = db.query(User).first()
    if not user:
        raise HTTPException(status_code=404, detail="No users found. Create one via /admin/create-user")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    debug_path = os.path.join(os.path.dirname(__file__), "debug_payload.json")
    with open(debug_path, "w") as f:
        json.dump(payload, f, indent=2, default=str)

    records = parse_health_auto_export(payload)

    saved = 0
    for rec in records:
        db.query(HealthMetric).filter(
            HealthMetric.user_id == user.id,
            HealthMetric.date == rec["date"],
            HealthMetric.metric_type == rec["metric_type"],
        ).delete()
        metric = HealthMetric(
            user_id=user.id,
            date=rec["date"],
            metric_type=rec["metric_type"],
            value=rec["value"],
            unit=rec["unit"],
        )
        db.add(metric)
        saved += 1

    db.commit()
    return {"status": "ok", "records_saved": saved, "user": user.name}


@app.post("/debug/webhook/{user_token}")
async def debug_webhook(user_token: str, request: Request, db: Session = Depends(get_db)):
    """Debug endpoint: saves raw payload to debug_payload.json and returns parsed records (does NOT save to DB)."""
    get_user_by_token(user_token, db)  # validate token
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
    # Save raw payload to disk for inspection
    debug_path = os.path.join(os.path.dirname(__file__), "debug_payload.json")
    with open(debug_path, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    records = parse_health_auto_export(payload)
    return {
        "raw_metric_names": [m.get("name") for m in payload.get("data", {}).get("metrics", [])],
        "parsed_records_count": len(records),
        "parsed_records": records[:50],  # first 50
        "debug_payload_saved_to": debug_path,
    }


@app.post("/webhook/{user_token}")
async def receive_health_data(user_token: str, request: Request, db: Session = Depends(get_db)):
    """Endpoint called by the 'Health Auto Export' iOS app."""
    user = get_user_by_token(user_token, db)

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # Save raw payload for debugging
    debug_path = os.path.join(os.path.dirname(__file__), "debug_payload.json")
    with open(debug_path, "w") as f:
        json.dump(payload, f, indent=2, default=str)

    records = parse_health_auto_export(payload)

    saved = 0
    for rec in records:
        # Upsert: delete existing record for same user/date/metric then insert
        db.query(HealthMetric).filter(
            HealthMetric.user_id == user.id,
            HealthMetric.date == rec["date"],
            HealthMetric.metric_type == rec["metric_type"],
        ).delete()
        metric = HealthMetric(
            user_id=user.id,
            date=rec["date"],
            metric_type=rec["metric_type"],
            value=rec["value"],
            unit=rec["unit"],
        )
        db.add(metric)
        saved += 1

    db.commit()
    return {"status": "ok", "records_saved": saved, "user": user.name}


@app.get("/dashboard/{user_token}", response_class=HTMLResponse)
def dashboard(user_token: str, request: Request, db: Session = Depends(get_db)):
    user = get_user_by_token(user_token, db)

    today = date.today()
    thirty_days_ago = today - timedelta(days=30)

    # Fetch last 30 days of metrics
    metrics = db.query(HealthMetric).filter(
        HealthMetric.user_id == user.id,
        HealthMetric.date >= thirty_days_ago,
    ).order_by(HealthMetric.date).all()

    # Organise into { metric_type: { date_str: value } }
    metric_data: dict[str, dict[str, float]] = {}
    for m in metrics:
        metric_data.setdefault(m.metric_type, {})[str(m.date)] = m.value

    # Latest AI summary
    summary = db.query(AISummary).filter(
        AISummary.user_id == user.id,
    ).order_by(AISummary.date.desc()).first()

    # Yesterday's key metrics (for the "at a glance" cards)
    yesterday = today - timedelta(days=1)
    yesterday_metrics = db.query(HealthMetric).filter(
        HealthMetric.user_id == user.id,
        HealthMetric.date == yesterday,
    ).all()
    yesterday_dict = {m.metric_type: m.value for m in yesterday_metrics}

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": user,
        "metric_data_json": json.dumps(metric_data),
        "summary": summary,
        "yesterday": yesterday_dict,
        "yesterday_date": str(yesterday),
        "user_token": user_token,
    })


@app.get("/api/data/{user_token}")
def get_raw_data(user_token: str, days: int = 30, db: Session = Depends(get_db)):
    user = get_user_by_token(user_token, db)
    since = date.today() - timedelta(days=days)
    metrics = db.query(HealthMetric).filter(
        HealthMetric.user_id == user.id,
        HealthMetric.date >= since,
    ).order_by(HealthMetric.date).all()
    return {
        "user": user.name,
        "metrics": [
            {"date": str(m.date), "type": m.metric_type, "value": m.value, "unit": m.unit}
            for m in metrics
        ]
    }


@app.post("/admin/create-user")
async def create_user(request: Request, db: Session = Depends(get_db)):
    """Create a new user and return their webhook/dashboard URLs. Protected by ADMIN_SECRET."""
    body = await request.json()
    if body.get("secret") != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    import uuid
    token = str(uuid.uuid4())
    user = User(name=name, token=token)
    db.add(user)
    db.commit()
    db.refresh(user)
    return {
        "name": user.name,
        "token": user.token,
        "webhook_url": f"/webhook/{token}",
        "dashboard_url": f"/dashboard/{token}",
        "note": "Prepend your base URL to webhook_url and dashboard_url",
    }


@app.get("/admin/users")
def list_users(secret: str, db: Session = Depends(get_db)):
    """List all users and their tokens. Protected by ADMIN_SECRET."""
    if secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    users = db.query(User).all()
    return [
        {
            "name": u.name,
            "token": u.token,
            "webhook_url": f"/webhook/{u.token}",
            "dashboard_url": f"/dashboard/{u.token}",
        }
        for u in users
    ]


@app.post("/admin/run-analysis")
async def run_analysis(request: Request, db: Session = Depends(get_db)):
    """Manually trigger the AI health agent for all users. Protected by ADMIN_SECRET."""
    body = await request.json()
    if body.get("secret") != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    from health_agent import analyze_user
    users = db.query(User).all()
    results = []
    for user in users:
        result = analyze_user(user.id, db)
        results.append({"user": user.name, "result": result})
    return {"status": "ok", "analyses": results}


@app.get("/")
def root():
    return {"message": "HealthAgent API is running. Visit /dashboard/{your_token} to see your data."}
