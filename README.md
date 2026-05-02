# 🌃 Smart Street Light IoT System

> Real-time vehicle detection & automated street light management  
> **Stack:** ESP32 · Flask · SQLite · Tailwind CSS · Chart.js

---

## 📁 Folder Structure

```
Street light project webapp/
├── app.py                    # Flask backend (REST API + SQLite)
├── requirements.txt          # Python dependencies
├── street_light.db           # SQLite database (auto-created)
├── static/
│   └── index.html            # Dashboard UI (Tailwind + Chart.js)
└── esp32_code/
    └── smart_street_light.ino  # ESP32 Arduino firmware
```

---

## 🚀 Quick Start

### 1. Install Python dependencies
```bash
pip install -r requirements.txt
```

### 2. Run the Flask server
```bash
python app.py
```
Server starts at → **http://localhost:5000**

### 3. Open the Dashboard
Navigate to **http://localhost:5000** in your browser.

---

## 🔌 API Endpoints

| Method | Endpoint              | Description                        |
|--------|-----------------------|------------------------------------|
| GET    | `/api/status`         | Dashboard snapshot (polled 3 s)    |
| POST   | `/api/detect`         | ESP32 vehicle detection event      |
| GET    | `/api/stats/weekly`   | 7-day daily totals                 |
| POST   | `/api/simulate`       | Simulate a detection (dev only)    |
| GET    | `/api/health`         | Server health check                |
| DELETE | `/api/clear`          | Erase all records (requires key)   |

### ESP32 POST `/api/detect`
```json
{
  "sensor_id":    "sensor-01",
  "location":     "Main Road",
  "vehicle_type": "car",
  "speed_kmh":    45.2
}
```
Header: `X-API-Key: esp32-secret-key-2024`

---

## 💡 Street Light Logic

| Time Window     | Light Status |
|-----------------|-------------|
| 06:00 → 17:59   | **OFF** (Daytime)  |
| 18:00 → 05:59   | **ON**  (Night)    |

---

## 🛠 ESP32 Wiring

| ESP32 Pin | Component        |
|-----------|------------------|
| GPIO14    | IR Sensor OUT    |
| GPIO2     | Built-in LED     |
| 3.3V      | IR Sensor VCC    |
| GND       | IR Sensor GND    |
