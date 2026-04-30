#!/usr/bin/env python3
"""
sessions_archive.py — Full per-session archive from Intervals.icu.

Designed to run alongside the existing sync.py pipeline. Outputs
`sessions_history.json`, a flat list of every activity in your
Intervals.icu account with all the per-session fields a coach LLM
needs to compare current vs. past sessions of any type.

This script is ADDITIVE. It does not modify or depend on sync.py.
If it fails, the rest of the pipeline is unaffected.

Environment variables (same as sync.py):
  INTERVALS_ATHLETE_ID  — your Intervals.icu athlete ID
  INTERVALS_API_KEY     — your Intervals.icu API key

Output file: sessions_history.json (written to current working directory)
"""

import os
import sys
import json
import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ---- Configuration ----
INTERVALS_BASE_URL = "https://intervals.icu/api/v1"
HISTORY_YEARS = 5  # how far back to fetch (cap at 5y; adjust if you have more)
OUTPUT_FILE = "sessions_history.json"
SCHEMA_VERSION = 1


def extract_fields(act: dict) -> dict:
    """Map a raw Intervals.icu activity to the archive schema.

    Missing fields become null. Older activities will have many nulls;
    that's fine — the archive degrades gracefully.
    """
    moving_time = act.get("moving_time") or 0
    joules = act.get("icu_joules")

    return {
        # Identity
        "id": act.get("id"),
        "date": act.get("start_date_local"),
        "type": act.get("type"),
        "name": act.get("name"),

        # Duration & distance
        "duration_s": moving_time,
        "duration_min": round(moving_time / 60, 1) if moving_time else None,
        "elapsed_time_s": act.get("elapsed_time"),
        "distance_km": round((act.get("distance") or 0) / 1000, 2),
        "elevation_gain_m": act.get("total_elevation_gain"),
        "elevation_loss_m": act.get("total_elevation_loss"),

        # Load
        "tss": act.get("icu_training_load"),
        "intensity_factor": act.get("icu_intensity"),
        "load_basis": act.get("icu_intensity_basis"),
        "is_hard_session": act.get("icu_hard_session"),

        # Power
        "avg_power": act.get("icu_average_watts"),
        "normalized_power": act.get("icu_weighted_avg_watts"),
        "max_power": act.get("max_watts"),
        "variability_index": act.get("icu_variability_index"),
        "work_kj": round(joules / 1000, 1) if joules else None,
        "ftp_at_time": act.get("icu_ftp"),

        # HR
        "avg_hr": act.get("average_heartrate"),
        "max_hr": act.get("max_heartrate"),
        "lthr_at_time": act.get("icu_lthr") or act.get("threshold_heartrate"),

        # Pace (run/swim)
        "avg_pace_s_per_km": act.get("pace"),
        "threshold_pace_at_time": act.get("threshold_pace"),

        # Cadence
        "avg_cadence": act.get("average_cadence"),
        "max_cadence": act.get("max_cadence"),

        # Quality signals
        "efficiency_factor": act.get("icu_efficiency_factor"),
        "decoupling_pct": act.get("icu_power_hr_decoupling") or act.get("decoupling"),
        "pw_hr_pct": act.get("icu_pw_hr"),

        # Subjective
        "feel": act.get("feel"),
        "rpe": act.get("icu_rpe"),

        # Environment
        "avg_temp": act.get("average_temp"),
        "min_temp": act.get("min_temp"),
        "max_temp": act.get("max_temp"),
        "humidity": act.get("average_humidity"),
        "wind_speed": act.get("average_wind_speed"),

        # Fueling & calories
        "calories": act.get("calories"),
        "carbs_ingested_g": act.get("icu_carbs_ingested"),
        "carbs_used_g": act.get("icu_carbs_used"),

        # Swim-specific
        "pool_length_m": act.get("pool_length"),
        "total_strokes": act.get("total_strokes"),
        "avg_swolf": act.get("icu_swolf"),

        # Sub-type flags
        "trainer": act.get("trainer"),
        "commute": act.get("commute"),
        "race": act.get("race"),

        # Notes (truncated)
        "notes_summary": (act.get("description") or "")[:200] or None,
    }


def fetch_activities(athlete_id: str, api_key: str, oldest: str, newest: str) -> list:
    """Fetch all activities in a date range from Intervals.icu."""
    auth = base64.b64encode(f"API_KEY:{api_key}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth}",
        "Accept": "application/json",
    }
    url = f"{INTERVALS_BASE_URL}/athlete/{athlete_id}/activities"
    params = {"oldest": oldest, "newest": newest}

    resp = requests.get(url, headers=headers, params=params, timeout=60)
    resp.raise_for_status()
    return resp.json()


def main() -> int:
    athlete_id = os.environ.get("INTERVALS_ATHLETE_ID")
    api_key = os.environ.get("INTERVALS_API_KEY")

    if not athlete_id or not api_key:
        print("❌ Missing INTERVALS_ATHLETE_ID or INTERVALS_API_KEY env vars",
              file=sys.stderr)
        return 1

    now = datetime.now()
    oldest = (now - timedelta(days=365 * HISTORY_YEARS)).strftime("%Y-%m-%d")
    newest = now.strftime("%Y-%m-%d")

    print(f"📥 Fetching activities {oldest} → {newest} ...")
    try:
        activities = fetch_activities(athlete_id, api_key, oldest, newest)
    except requests.HTTPError as e:
        print(f"❌ Intervals.icu API error: {e}", file=sys.stderr)
        return 2
    except requests.RequestException as e:
        print(f"❌ Network error: {e}", file=sys.stderr)
        return 3

    print(f"   Got {len(activities)} activities")

    sessions = [extract_fields(a) for a in activities]
    # Sort newest first (matches recent_activities convention in latest.json)
    sessions.sort(key=lambda s: s.get("date") or "", reverse=True)

    # Type breakdown for quick sanity check
    type_counts = {}
    for s in sessions:
        t = s.get("type") or "Unknown"
        type_counts[t] = type_counts.get(t, 0) + 1

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "schema_version": SCHEMA_VERSION,
        "source": "Intervals.icu API (sessions_archive.py)",
        "athlete_id": athlete_id,
        "fetch_window": {
            "oldest_requested": oldest,
            "newest_requested": newest,
            "history_years": HISTORY_YEARS,
        },
        "summary": {
            "total_sessions": len(sessions),
            "earliest_session": sessions[-1]["date"][:10] if sessions else None,
            "latest_session": sessions[0]["date"][:10] if sessions else None,
            "type_breakdown": type_counts,
        },
        "sessions": sessions,
    }

    Path(OUTPUT_FILE).write_text(json.dumps(output, indent=2, default=str))
    print(f"✅ Wrote {len(sessions)} sessions to {OUTPUT_FILE}")
    print(f"   Type breakdown: {type_counts}")
    print(f"   Latest session: {output['summary']['latest_session']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
