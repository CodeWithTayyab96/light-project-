import sqlite3, sys, os

db_path = os.path.join(os.path.dirname(__file__), '..', 'street_light.db')
try:
    conn = sqlite3.connect(db_path)
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    print("Tables:", tables)
    settings = conn.execute("SELECT * FROM settings").fetchall()
    print("Settings:", settings)
    count = conn.execute("SELECT COUNT(*) FROM vehicle_events").fetchone()
    print("Event count:", count[0])
    recent = conn.execute("SELECT id, detected_at FROM vehicle_events ORDER BY id DESC LIMIT 5").fetchall()
    print("Recent events:", recent)
    conn.close()
    print("\nDB OK")
except Exception as e:
    print("DB ERROR:", e)

# Check imports
try:
    import flask, flask_cors, requests
    print("Flask:", flask.__version__)
    print("flask-cors OK")
    print("requests OK")
except ImportError as e:
    print("IMPORT ERROR:", e)
