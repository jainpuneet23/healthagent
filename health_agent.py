"""
health_agent.py — Claude-powered daily health analysis.

Called by scheduler.py (daily cron) or via the /admin/run-analysis endpoint.
"""

import os
import json
from datetime import date, timedelta
from statistics import mean

from openai import OpenAI
from sqlalchemy.orm import Session

from models import SessionLocal, User, HealthMetric, AISummary

# Load .env file if present
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")


# ---------------------------------------------------------------------------
# Metric labels for human-readable output
# ---------------------------------------------------------------------------

METRIC_LABELS = {
    "steps":              ("Steps",                "steps"),
    "distance_km":        ("Distance",             "km"),
    "active_calories":    ("Active Calories",      "kcal"),
    "exercise_minutes":   ("Exercise",             "min"),
    "stand_hours":        ("Stand Hours",          "hrs"),
    "resting_hr":         ("Resting Heart Rate",   "bpm"),
    "avg_hr":             ("Avg Heart Rate",       "bpm"),
    "hrv":                ("HRV (SDNN)",           "ms"),
    "sleep_total":        ("Total Sleep",          "min"),
    "sleep_deep":         ("Deep Sleep",           "min"),
    "sleep_rem":          ("REM Sleep",            "min"),
    "sleep_core":         ("Core Sleep",           "min"),
    "sleep_awake":        ("Awake Time",           "min"),
    "spo2":               ("Blood Oxygen (SpO2)",  "%"),
    "respiratory_rate":   ("Respiratory Rate",     "breaths/min"),
    "mindful_minutes":    ("Mindful Minutes",      "min"),
}


def _format_value(metric_type: str, value: float) -> str:
    """Format a metric value nicely for the prompt."""
    if metric_type == "sleep_total":
        hours = int(value // 60)
        mins = int(value % 60)
        return f"{hours}h {mins}m"
    label, unit = METRIC_LABELS.get(metric_type, (metric_type, ""))
    if metric_type in ("steps", "stand_hours", "exercise_minutes"):
        return f"{int(value):,} {unit}"
    return f"{round(value, 1)} {unit}"


def _build_metrics_block(metrics_by_type: dict[str, float]) -> str:
    """Build a formatted string of metrics for the Claude prompt."""
    lines = []
    order = [
        "steps", "distance_km", "active_calories", "exercise_minutes", "stand_hours",
        "resting_hr", "avg_hr", "hrv",
        "sleep_total", "sleep_deep", "sleep_rem", "sleep_core", "sleep_awake",
        "spo2", "respiratory_rate", "mindful_minutes",
    ]
    for key in order:
        if key in metrics_by_type:
            label = METRIC_LABELS.get(key, (key,))[0]
            lines.append(f"  - {label}: {_format_value(key, metrics_by_type[key])}")
    # Any extra metrics not in our ordered list
    for key, val in metrics_by_type.items():
        if key not in order:
            lines.append(f"  - {key}: {val}")
    return "\n".join(lines) if lines else "  (no data available)"


def analyze_user(user_id: int, db: Session) -> str:
    """
    Run the AI health analysis for a single user and save the result to DB.
    Returns a status string.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return "user not found"

    today = date.today()
    yesterday = today - timedelta(days=1)
    thirty_days_ago = today - timedelta(days=30)

    # Fetch yesterday's metrics
    yesterday_rows = db.query(HealthMetric).filter(
        HealthMetric.user_id == user_id,
        HealthMetric.date == yesterday,
    ).all()

    if not yesterday_rows:
        return f"no data for {yesterday}, skipping"

    yesterday_metrics = {r.metric_type: r.value for r in yesterday_rows}

    # Fetch 30-day baseline (excluding yesterday to keep it clean)
    baseline_rows = db.query(HealthMetric).filter(
        HealthMetric.user_id == user_id,
        HealthMetric.date >= thirty_days_ago,
        HealthMetric.date < yesterday,
    ).all()

    # Group baseline by metric type
    baseline_by_type: dict[str, list[float]] = {}
    for r in baseline_rows:
        baseline_by_type.setdefault(r.metric_type, []).append(r.value)

    baseline_avgs = {k: round(mean(v), 1) for k, v in baseline_by_type.items() if v}

    # Build prompt
    prompt = f"""You are a personal health coach analysing the health data for {user.name}.

DATE ANALYSED: {yesterday}

30-DAY PERSONAL BASELINE (averages):
{_build_metrics_block(baseline_avgs)}

YESTERDAY'S DATA:
{_build_metrics_block(yesterday_metrics)}

Please provide a concise daily health summary with exactly these sections:

**HEALTH SCORE: X/10**
(overall score for yesterday based on the data)

**WINS 🏆**
- (up to 3 specific things that were good or above baseline)

**NEEDS ATTENTION ⚠️**
- (up to 3 specific areas that were below baseline or concerning)

**TODAY'S SUGGESTIONS 💡**
- (2–3 actionable, practical suggestions for today based on yesterday's data)

**QUICK INSIGHT**
1–2 sentences of the most important thing {user.name} should know today.

Rules:
- Be warm, personal, and encouraging — not clinical
- Reference actual numbers from the data
- Never make medical diagnoses
- Keep the whole response under 300 words
- If data is missing for a metric, ignore it rather than guessing
"""

    client = OpenAI(api_key=OPENAI_API_KEY)
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        return f"API error: {e}"

    summary_text = response.choices[0].message.content

    # Extract health score from response
    score = None
    for line in summary_text.splitlines():
        if "HEALTH SCORE" in line.upper():
            import re
            match = re.search(r"(\d+)\s*/\s*10", line)
            if match:
                score = int(match.group(1))
                break

    # Delete any existing summary for this user/date and save new one
    db.query(AISummary).filter(
        AISummary.user_id == user_id,
        AISummary.date == yesterday,
    ).delete()

    ai_summary = AISummary(
        user_id=user_id,
        date=yesterday,
        summary=summary_text,
        score=score,
    )
    db.add(ai_summary)
    db.commit()

    return f"analysis saved for {yesterday}, score={score}"


def run_all_users():
    """Analyze all users — called by the daily scheduler."""
    db = SessionLocal()
    try:
        users = db.query(User).all()
        for user in users:
            result = analyze_user(user.id, db)
            print(f"[health_agent] {user.name}: {result}")
    finally:
        db.close()


if __name__ == "__main__":
    run_all_users()
