import sqlite3
from datetime import datetime, timedelta

DATABASE = "street_light.db"

def fix_timestamps():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    print("Checking for future timestamps (local vs UTC mismatch)...")
    now_utc = datetime.utcnow()
    
    # Find records where detected_at is in the future (likely stored as local time)
    rows = cursor.execute("SELECT id, detected_at FROM vehicle_events").fetchall()
    
    fixed_count = 0
    for row in rows:
        dt_str = row['detected_at']
        try:
            # Handle formats with or without microseconds
            if '.' in dt_str:
                dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S.%f")
            else:
                dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                
            # If the timestamp is more than 1 minute in the future compared to UTC, 
            # it was likely stored as Local Time (UTC+5).
            if dt > now_utc + timedelta(minutes=1):
                new_dt = dt - timedelta(hours=5)
                cursor.execute("UPDATE vehicle_events SET detected_at = ? WHERE id = ?", (new_dt.strftime("%Y-%m-%d %H:%M:%S.%f"), row['id']))
                fixed_count += 1
        except Exception as e:
            print(f"Error processing row {row['id']}: {e}")
            
    conn.commit()
    conn.close()
    print(f"Done. Fixed {fixed_count} timestamps.")

if __name__ == "__main__":
    fix_timestamps()
