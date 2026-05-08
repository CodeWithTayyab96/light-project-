import sqlite3
import os

DATABASE = "street_light.db"
conn = sqlite3.connect(DATABASE)
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT * FROM vehicle_events ORDER BY id DESC LIMIT 5").fetchall()
for row in rows:
    print(dict(row))
conn.close()
