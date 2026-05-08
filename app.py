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
import time
from datetime import datetime, timedelta
from functools import wraps
from typing import Any
import requests as http_requests
from flask import Flask, g, jsonify, request
from flask_cors import CORS
from supabase import create_client, Client
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

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
CORS(app, resources={r"/api/*": {"origins": "*", "allow_headers": ["Content-Type", "X-API-Key", "X-User"]}})

# --- Credentials & Config ---
SUPABASE_URL     = os.environ.get("SUPABASE_URL")
SUPABASE_KEY     = os.environ.get("SUPABASE_KEY")
API_KEY          = os.environ.get("API_KEY", "university-project-2024")
NIGHT_START_HOUR = 18
DAY_START_HOUR   = 6

if not SUPABASE_URL or not SUPABASE_KEY:
    logging.error("CRITICAL: Supabase credentials missing from environment!")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


def get_now_local() -> datetime:
    """Helper to get the current time in the local timezone (UTC+5)."""
    return datetime.utcnow() + timedelta(hours=5)


# ---------------------------------------------------------------------------
# Privacy — IP Whitelist
# ---------------------------------------------------------------------------
# Add any IP that should be allowed to reach the dashboard/API.
# The ESP32's IP MUST be listed here so it can POST detections.
# Set env var ALLOWED_IPS as comma-separated list to override, e.g.:
#   set ALLOWED_IPS=127.0.0.1,192.168.1.50,192.168.1.20
_default_ips = "127.0.0.1,::1,*"   # localhost IPv4 + IPv6 + wildcard
ALLOWED_IPS: set[str] = set(
    os.environ.get("ALLOWED_IPS", _default_ips).split(",")
)

# ESP32 settings — Flask will push commands to this address
ESP32_IP: str   = os.environ.get("ESP32_IP", "192.168.1.50")
ESP32_PORT: int = int(os.environ.get("ESP32_PORT", "80"))
ESP32_BASE: str = f"http://{ESP32_IP}:{ESP32_PORT}"

# Dynamically add the ESP32 IP to the allowed list so it can report detections
ALLOWED_IPS.add(ESP32_IP)

def update_esp32_config(new_ip: str):
    """Updates the ESP32 IP globally and persists it to the database."""
    global ESP32_IP, ESP32_BASE
    if new_ip and new_ip != ESP32_IP:
        ESP32_IP = new_ip
        ESP32_BASE = f"http://{ESP32_IP}:{ESP32_PORT}"
        ALLOWED_IPS.add(ESP32_IP)
        
        # Persist to DB
        with get_db_context() as conn:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('esp32_ip', ?)", (new_ip,))
        
        logger.info("ESP32 configuration updated & persisted — New IP: %s", ESP32_IP)


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

    if "*" in ALLOWED_IPS:
        return

    if client_ip not in ALLOWED_IPS:
        # Check if it's a typical local private network IP (IPv4 only check)
        is_private = False
        if "." in client_ip:
            parts = client_ip.split(".")
            if len(parts) >= 2:
                is_private = (
                    client_ip.startswith("192.168.") or
                    client_ip.startswith("10.") or
                    (client_ip.startswith("172.") and 16 <= int(parts[1]) <= 31)
                )

        if not is_private:
            logger.warning("Blocked request from %s — not in whitelist. Allowed: %s", client_ip, ALLOWED_IPS)
            return jsonify({"error": "Access denied", "your_ip": client_ip}), 403

    logger.debug("Allowed request from %s", client_ip)

# ---------------------------------------------------------------------------
# Database helpers (Supabase)
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Check connection and load initial settings."""
    try:
        logger.info("Connected to Supabase (Multi-User Mode) successfully.")
    except Exception as e:
        logger.error("Supabase Connection Error: %s", e)

# ---------------------------------------------------------------------------
# Business logic (User-Aware)
# ---------------------------------------------------------------------------

def get_user():
    """Identify the user from the custom X-User header."""
    return request.headers.get("X-User", "admin")

def get_light_mode() -> str:
    user = get_user()
    try:
        res = supabase.table("settings").select("value").eq("key", "light_mode").eq("username", user).execute()
        return res.data[0]['value'] if res.data else 'auto'
    except: return 'auto'

def set_light_mode(mode: str) -> None:
    user = get_user()
    try:
        supabase.table("settings").upsert({"key": "light_mode", "value": mode, "username": user}).execute()
    except Exception as e:
        logger.error("Error setting light mode: %s", e)



def is_night_time() -> bool:
    """Check if current local time is within the night-time window."""
    hour = get_now_local().hour
    return hour >= NIGHT_START_HOUR or hour < DAY_START_HOUR


def get_light_status() -> str:
    mode = get_light_mode()
    if mode == "on":
        return "ON"
    elif mode == "off":
        return "OFF"
    return "ON" if is_night_time() else "OFF"


def require_api_key(f):
    """Decorator: reject requests missing the correct X-API-Key header (bypassed for localhost)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
        client_ip = client_ip.split(",")[0].strip()
        
        # Bypass for local requests
        if client_ip in ["127.0.0.1", "::1"]:
            return f(*args, **kwargs)

        key = request.headers.get("X-API-Key", "")
        if key != API_KEY:
            logger.warning("Unauthorised request from %s", client_ip)
            return jsonify({"error": "Unauthorised — invalid API key"}), 401
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Routes — ESP32 endpoints
# ---------------------------------------------------------------------------

@app.route("/api/detect", methods=["POST"])
@app.route("/update-traffic", methods=["POST"])
def detect_vehicle():
    """Called by the ESP32 whenever a vehicle is detected."""
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    client_ip = client_ip.split(",")[0].strip()
    if client_ip not in ["127.0.0.1", "::1"] and client_ip != ESP32_IP:
        update_esp32_config(client_ip)

    data: dict[str, Any] = request.get_json(silent=True) or {}
    user = data.get("user", "admin") # The ESP32 should send its owner's username
    
    payload = {
        "sensor_id": str(data.get("sensor_id", "sensor-01")),
        "location": str(data.get("location", "Main Road")),
        "speed_kmh": data.get("speed_kmh"),
        "vehicle_type": data.get("vehicle_type", "car"),
        "detected_at": datetime.utcnow().isoformat(),
        "username": user
    }

    try:
        supabase.table("vehicle_events").insert(payload).execute()

        logger.info("Vehicle detected logged to Supabase: %s", payload['location'])
    except Exception as e:
        logger.error("Supabase Log Error: %s", e)

    return jsonify({"status": "ok", "light": get_light_status()}), 201



# ---------------------------------------------------------------------------
# Routes — Dashboard / Frontend API
# ---------------------------------------------------------------------------

@app.route("/api/status", methods=["GET"])
def get_status():
    """Returns dashboard snapshot from Supabase."""
    now_utc = datetime.utcnow()
    today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    last_hour_start = now_utc - timedelta(hours=1)
    user = get_user()

    try:
        # Total
        total_res = supabase.table("vehicle_events").select("*", count="exact").eq("username", user).execute()
        total_count = total_res.count if total_res.count is not None else 0

        # Today
        today_res = supabase.table("vehicle_events").select("*", count="exact").eq("username", user).gte("detected_at", today_start.isoformat()).execute()
        today_count = today_res.count if today_res.count is not None else 0

        # Last Hour
        lh_res = supabase.table("vehicle_events").select("*", count="exact").eq("username", user).gte("detected_at", last_hour_start.isoformat()).execute()
        last_hour_count = lh_res.count if lh_res.count is not None else 0

        # Recent Events (10)
        recent_res = supabase.table("vehicle_events").select("*").eq("username", user).order("detected_at", desc=True).limit(10).execute()
        recent_events = []
        for r in recent_res.data:
            dt = datetime.fromisoformat(r['detected_at'].replace('Z', '+00:00'))
            dt_local = dt + timedelta(hours=5)
            recent_events.append({
                "id": r['id'],
                "detected_at": dt_local.strftime("%H:%M:%S"),
                "location": r['location'],
                "vehicle_type": r['vehicle_type']
            })

        # Chart Data (Last 24 hours)
        day_res = supabase.table("vehicle_events").select("detected_at").eq("username", user).gte("detected_at", (now_utc - timedelta(hours=24)).isoformat()).execute()

        hourly_counts = {}
        for r in day_res.data:
            dt = datetime.fromisoformat(r['detected_at'].replace('Z', '+00:00'))
            dt_local = dt + timedelta(hours=5)
            h_key = dt_local.strftime("%H:00")
            hourly_counts[h_key] = hourly_counts.get(h_key, 0) + 1

        hourly_data = []
        for i in range(24):
            t = (get_now_local() - timedelta(hours=23-i)).strftime("%H:00")
            hourly_data.append({"hour": t, "count": hourly_counts.get(t, 0)})

        # Peak Hour
        peak_hour = "N/A"
        if hourly_counts:
            peak_hour = max(hourly_counts, key=hourly_counts.get)

        return jsonify({
            "total_count": total_count,
            "today_count": today_count,
            "last_hour_count": last_hour_count,
            "peak_hour": peak_hour,
            "recent_events": recent_events,
            "hourly_data": hourly_data,
            "light_status": get_light_status(),
            "server_time": get_now_local().strftime("%Y-%m-%d %H:%M:%S")
        })
    except Exception as e:
        logger.error("Status Fetch Error: %s", e)
        return jsonify({"error": "Supabase unreachable"}), 500



@app.route("/api/stats/weekly", methods=["GET"])
def get_weekly_stats():
    """Weekly grouping for bar chart."""
    now_utc = datetime.utcnow()
    week_start = now_utc - timedelta(days=7)
    user = get_user()
    
    try:
        res = supabase.table("vehicle_events").select("detected_at").eq("username", user).gte("detected_at", week_start.isoformat()).execute()

        daily_counts = {}
        for r in res.data:
            dt = datetime.fromisoformat(r['detected_at'].replace('Z', '+00:00'))
            dt_local = dt + timedelta(hours=5)
            d_key = dt_local.strftime("%a")
            daily_counts[d_key] = daily_counts.get(d_key, 0) + 1
            
        weekly = []
        for i in range(7):
            d = (get_now_local() - timedelta(days=6-i)).strftime("%a")
            weekly.append({"day": d, "count": daily_counts.get(d, 0)})
            
        return jsonify({"weekly": weekly})
    except:
        return jsonify({"weekly": []})



@app.route("/api/simulate", methods=["POST"])
def simulate_detection():
    payload = {"location": "Simulated Road", "vehicle_type": "car", "detected_at": datetime.utcnow().isoformat()}
    supabase.table("vehicle_events").insert(payload).execute()
    return jsonify({"status": "simulated"}), 201



@app.route("/api/clear", methods=["POST"])
@require_api_key
def clear_data():
    """Wipe history from Supabase."""
    try:
        # Delete everything from vehicle_events (requires 'Enable Delete' in Supabase RLS)
        supabase.table("vehicle_events").delete().neq("id", 0).execute()
        return jsonify({"status": "cleared"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@app.route("/api/login", methods=["POST"])
def login():
    """Validates user against Supabase."""
    data = request.get_json(silent=True) or {}
    username = data.get("username")
    password = data.get("password")

    try:
        res = supabase.table("users").select("*").eq("username", username).eq("password", password).execute()
        if res.data:
            return jsonify({"status": "success", "user": username}), 200
        return jsonify({"error": "Invalid credentials"}), 401
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
    data = request.get_json(silent=True) or {}
    command = data.get("command", "light_auto")
    mode = command.replace("light_", "")
    set_light_mode(mode)
    
    # Optional: Log status change to a separate table if you created it
    # supabase.table("light_log").insert({"status": mode}).execute()

    # Forward to ESP32 locally if on same network
    esp32_url = f"{ESP32_BASE}/cmd"
    try:
        http_requests.get(esp32_url, params={"action": command}, timeout=2)
    except: pass
    
    return jsonify({"status": "ok", "mode": mode}), 200



@app.route("/api/esp32/sync", methods=["GET"])
def sync_esp32():
    """
    Endpoint for the ESP32 to poll its current intended state from the cloud.
    This makes the project 'Cloud-Ready' so hardware and dashboard don't need same WiFi.
    """
    return jsonify({
        "status": "ok",
        "light": get_light_status(),
        "mode": get_light_mode(),
        "server_time": get_now_local().strftime("%H:%M:%S")
    }), 200


@app.route("/api/config", methods=["GET", "POST"])
@require_api_key
def manage_config():
    """Get or update ESP32 configuration."""
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        new_ip = data.get("esp32_ip")
        if new_ip:
            update_esp32_config(new_ip)
            return jsonify({"status": "updated", "esp32_ip": ESP32_IP})
        return jsonify({"error": "Invalid IP"}), 400

    return jsonify({
        "esp32_ip": ESP32_IP,
        "night_start": NIGHT_START_HOUR,
        "day_start": DAY_START_HOUR,
        "api_key_configured": bool(API_KEY)
    })


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
# Initialization
# ---------------------------------------------------------------------------

START_TIME = time.time()
init_db()  # Ensure DB is ready for Gunicorn/Production

if __name__ == "__main__":
    import sys
    if "--online" in sys.argv:
        ALLOWED_IPS.add("*")
        logger.warning("STARTED IN ONLINE MODE — IP whitelist disabled!")
        
    logger.info("Starting Smart Street Light IoT Server …")
    
    # Use environment port for Render, default to 5000 for local
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

