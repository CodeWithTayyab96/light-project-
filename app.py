"""
Smart Street Light IoT System — Flask Backend
=============================================
Author  : Smart IoT Systems
Version : 2.0.0
License : MIT

Handles:
  - ESP32 vehicle-detection events via REST API
  - SQLite persistence with schema migrations
  - Automatic street-light status based on time-of-day
  - Real-time dashboard data aggregation
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from functools import wraps
from typing import Any, Generator

import requests as http_requests
from flask import Flask, g, jsonify, request
from flask_cors import CORS

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("SmartStreetLight")

# ---------------------------------------------------------------------------
# App & Config
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app, resources={r"/api/*": {"origins": "*"}})

DATABASE = os.path.join(os.path.dirname(__file__), "street_light.db")

# Night-time window (24 h clock).  Lights are ON when hour ∈ [NIGHT_START, 24) ∪ [0, DAY_START)
NIGHT_START_HOUR: int = 18   # 6 PM
DAY_START_HOUR: int = 6      # 6 AM

# API key for minimal ESP32 auth (set env var API_KEY to override)
API_KEY: str = os.environ.get("API_KEY", "esp32-secret-key-2024")

# ---------------------------------------------------------------------------
# Privacy — IP Whitelist
# ---------------------------------------------------------------------------
# Add any IP that should be allowed to reach the dashboard/API.
# The ESP32's IP MUST be listed here so it can POST detections.
# Set env var ALLOWED_IPS as comma-separated list to override, e.g.:
#   set ALLOWED_IPS=127.0.0.1,192.168.1.50,192.168.1.20
_default_ips = "127.0.0.1,::1"   # localhost IPv4 + IPv6
ALLOWED_IPS: set[str] = set(
    os.environ.get("ALLOWED_IPS", _default_ips).split(",")
)

# ESP32 settings — Flask will push commands to this address
ESP32_IP: str   = os.environ.get("ESP32_IP", "192.168.1.50")
ESP32_PORT: int = int(os.environ.get("ESP32_PORT", "80"))
ESP32_BASE: str = f"http://{ESP32_IP}:{ESP32_PORT}"


@app.before_request
def enforce_ip_whitelist() -> None:
    """
    Block every request whose source IP is not in ALLOWED_IPS.
    Checks both REMOTE_ADDR and X-Forwarded-For (proxy-aware).
    """
    # Allow OPTIONS (CORS pre-flight) through
    if request.method == "OPTIONS":
        return

    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    # X-Forwarded-For can be a comma-list; take the first (real client)
    client_ip = client_ip.split(",")[0].strip()

    if client_ip not in ALLOWED_IPS:
        logger.warning("Blocked request from %s — not in whitelist", client_ip)
        return jsonify({"error": "Access denied"}), 403

    logger.debug("Allowed request from %s", client_ip)

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    """Return the per-request SQLite connection stored in Flask's `g`."""
    if "db" not in g:
        conn = sqlite3.connect(DATABASE, detect_types=sqlite3.PARSE_DECLTYPES)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(exc: Exception | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


@contextmanager
def get_db_context() -> Generator[sqlite3.Connection, None, None]:
    """Context-manager for use outside of a request (e.g. init_db)."""
    conn = sqlite3.connect(DATABASE, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create tables if they do not already exist."""
    schema = """
    CREATE TABLE IF NOT EXISTS vehicle_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        detected_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        sensor_id   TEXT      NOT NULL DEFAULT 'sensor-01',
        location    TEXT      NOT NULL DEFAULT 'Main Road',
        speed_kmh   REAL,
        vehicle_type TEXT
    );

    CREATE TABLE IF NOT EXISTS light_status_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        changed_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        status      TEXT      NOT NULL CHECK(status IN ('ON','OFF')),
        reason      TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_vehicle_events_detected_at
        ON vehicle_events(detected_at);

    CREATE INDEX IF NOT EXISTS idx_light_status_changed_at
        ON light_status_log(changed_at);
    """
    with get_db_context() as conn:
        conn.executescript(schema)
    logger.info("Database initialised at %s", DATABASE)


# ---------------------------------------------------------------------------
# Business logic
# ---------------------------------------------------------------------------

def is_night_time() -> bool:
    hour = datetime.now().hour
    return hour >= NIGHT_START_HOUR or hour < DAY_START_HOUR


def get_light_status() -> str:
    return "ON" if is_night_time() else "OFF"


def require_api_key(f):
    """Decorator: reject requests missing the correct X-API-Key header."""
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("X-API-Key", "")
        if key != API_KEY:
            logger.warning("Unauthorised request from %s", request.remote_addr)
            return jsonify({"error": "Unauthorised — invalid API key"}), 401
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Routes — ESP32 endpoints
# ---------------------------------------------------------------------------

@app.route("/api/detect", methods=["POST"])
@require_api_key
def detect_vehicle():
    """
    Called by the ESP32 whenever the IR sensor detects a vehicle.

    Expected JSON body (all fields optional):
    {
        "sensor_id":    "sensor-01",
        "location":     "Main Road",
        "speed_kmh":    45.2,
        "vehicle_type": "car"
    }
    """
    data: dict[str, Any] = request.get_json(silent=True) or {}

    sensor_id    = str(data.get("sensor_id",    "sensor-01"))[:64]
    location     = str(data.get("location",     "Main Road"))[:128]
    speed_kmh    = data.get("speed_kmh")
    vehicle_type = data.get("vehicle_type")

    db = get_db()
    db.execute(
        """INSERT INTO vehicle_events (sensor_id, location, speed_kmh, vehicle_type)
           VALUES (?, ?, ?, ?)""",
        (sensor_id, location, speed_kmh, vehicle_type),
    )
    db.commit()

    logger.info("Vehicle detected — sensor=%s  location=%s", sensor_id, location)
    return jsonify({"status": "ok", "light": get_light_status()}), 201


# ---------------------------------------------------------------------------
# Routes — Dashboard / Frontend API
# ---------------------------------------------------------------------------

@app.route("/api/status", methods=["GET"])
def get_status():
    """
    Returns a comprehensive snapshot for the dashboard.
    Polled every 3 s by the frontend.
    """
    db   = get_db()
    now  = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Total all-time count
    total_count = db.execute(
        "SELECT COUNT(*) FROM vehicle_events"
    ).fetchone()[0]

    # Today's count
    today_count = db.execute(
        "SELECT COUNT(*) FROM vehicle_events WHERE detected_at >= ?",
        (today_start,),
    ).fetchone()[0]

    # Last hour count
    last_hour = db.execute(
        "SELECT COUNT(*) FROM vehicle_events WHERE detected_at >= ?",
        (now - timedelta(hours=1),),
    ).fetchone()[0]

    # Last 24 h — hourly breakdown for chart
    hourly_data: list[dict] = []
    for h in range(24):
        hour_start = (now - timedelta(hours=23 - h)).replace(
            minute=0, second=0, microsecond=0
        )
        hour_end = hour_start + timedelta(hours=1)
        count = db.execute(
            "SELECT COUNT(*) FROM vehicle_events WHERE detected_at >= ? AND detected_at < ?",
            (hour_start, hour_end),
        ).fetchone()[0]
        hourly_data.append({"hour": hour_start.strftime("%H:%M"), "count": count})

    # Last 10 detection events
    recent_rows = db.execute(
        """SELECT id, detected_at, sensor_id, location, speed_kmh, vehicle_type
           FROM vehicle_events
           ORDER BY detected_at DESC
           LIMIT 10"""
    ).fetchall()
    recent_events = [
        {
            "id":           r["id"],
            "detected_at":  r["detected_at"],
            "sensor_id":    r["sensor_id"],
            "location":     r["location"],
            "speed_kmh":    r["speed_kmh"],
            "vehicle_type": r["vehicle_type"],
        }
        for r in recent_rows
    ]

    # Peak hour today
    peak_row = db.execute(
        """SELECT strftime('%H', detected_at) AS hr, COUNT(*) AS cnt
           FROM vehicle_events
           WHERE detected_at >= ?
           GROUP BY hr
           ORDER BY cnt DESC
           LIMIT 1""",
        (today_start,),
    ).fetchone()
    peak_hour = f"{peak_row['hr']}:00" if peak_row else "N/A"

    light_status = get_light_status()

    return jsonify(
        {
            "timestamp":      now.isoformat(),
            "light_status":   light_status,
            "is_night":       is_night_time(),
            "total_count":    total_count,
            "today_count":    today_count,
            "last_hour_count": last_hour,
            "peak_hour":      peak_hour,
            "hourly_data":    hourly_data,
            "recent_events":  recent_events,
            "server_time":    now.strftime("%Y-%m-%d %H:%M:%S"),
        }
    )


@app.route("/api/stats/weekly", methods=["GET"])
def get_weekly_stats():
    """Daily vehicle counts for the last 7 days."""
    db  = get_db()
    now = datetime.now()

    weekly: list[dict] = []
    for d in range(6, -1, -1):
        day = (now - timedelta(days=d)).date()
        count = db.execute(
            "SELECT COUNT(*) FROM vehicle_events WHERE DATE(detected_at) = ?",
            (day.isoformat(),),
        ).fetchone()[0]
        weekly.append({"day": day.strftime("%a"), "date": day.isoformat(), "count": count})

    return jsonify({"weekly": weekly})


@app.route("/api/simulate", methods=["POST"])
def simulate_detection():
    """
    Development-only endpoint — inserts a fake detection event.
    Remove or protect this in production.
    """
    db = get_db()
    db.execute(
        """INSERT INTO vehicle_events (sensor_id, location, vehicle_type)
           VALUES ('sensor-01', 'Main Road - Sim', 'car')"""
    )
    db.commit()
    return jsonify({"status": "simulated", "light": get_light_status()}), 201


@app.route("/api/clear", methods=["DELETE"])
@require_api_key
def clear_data():
    """Erase all event records (admin use only)."""
    db = get_db()
    db.execute("DELETE FROM vehicle_events")
    db.commit()
    logger.warning("All vehicle events cleared by %s", request.remote_addr)
    return jsonify({"status": "cleared"}), 200


@app.route("/", methods=["GET"])
def index():
    return app.send_static_file("index.html")


@app.route("/<path:path>", methods=["GET"])
def static_files(path):
    return app.send_static_file(path)


# ---------------------------------------------------------------------------
# ESP32 Command Push  (Flask → ESP32)
# ---------------------------------------------------------------------------

@app.route("/api/command", methods=["POST"])
@require_api_key
def send_command_to_esp32():
    """
    Push a command from the Flask server to the ESP32's built-in HTTP server.

    Body JSON:
    {
        "command": "light_on" | "light_off" | "light_auto"
    }

    The ESP32 must have a route /cmd that accepts:
        GET /cmd?action=light_on
    """
    data    = request.get_json(silent=True) or {}
    command = data.get("command", "light_auto")

    VALID_COMMANDS = {"light_on", "light_off", "light_auto"}
    if command not in VALID_COMMANDS:
        return jsonify({"error": f"Unknown command '{command}'"}), 400

    esp32_url = f"{ESP32_BASE}/cmd"
    try:
        resp = http_requests.get(
            esp32_url,
            params={"action": command},
            timeout=3,
        )
        logger.info("Command '%s' sent to ESP32 — HTTP %s", command, resp.status_code)
        return jsonify({
            "status":      "sent",
            "command":     command,
            "esp32_reply": resp.text[:200],
        }), 200
    except http_requests.exceptions.ConnectionError:
        logger.error("ESP32 unreachable at %s", esp32_url)
        return jsonify({"error": "ESP32 not reachable", "url": esp32_url}), 503
    except http_requests.exceptions.Timeout:
        logger.error("ESP32 command timed out")
        return jsonify({"error": "ESP32 timed out"}), 504


# ---------------------------------------------------------------------------
# Health-check
# ---------------------------------------------------------------------------

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify(
        {
            "status":   "healthy",
            "service":  "Smart Street Light IoT",
            "version":  "2.0.0",
            "uptime_s": round(time.time() - START_TIME, 1),
        }
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

START_TIME = time.time()

if __name__ == "__main__":
    init_db()
    logger.info("Starting Smart Street Light IoT Server …")
    # host='127.0.0.1' → localhost only; no other device can connect
    app.run(host="127.0.0.1", port=5000, debug=True)
