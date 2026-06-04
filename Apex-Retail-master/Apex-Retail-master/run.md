# Project Execution Steps

### 1. Navigate to Project Root
```powershell
cd d:\Kirat_WebDev\purplle_round_2\Apex-Retail-master\Apex-Retail-master
```
*What it does:* Changes directory to the source code folder containing configuration files and scripts.

---

### 2. Activate Python Virtual Environment
```powershell
..\.venv\Scripts\Activate.ps1
```
*What it does:* Activates the isolated Python environment containing all necessary packages.

---

### 3. Install Required Dependencies
```powershell
pip install -r requirements-pipeline.txt
```
*What it does:* Installs YOLO, OpenCV, PyTorch, and other processing dependencies.

---

### 4. Initialize Database Tables
```powershell
alembic upgrade head
```
*What it does:* Runs migrations to generate the database schema and tables inside `./data/apex.db`.

---

### 5. Start API Server (Run in Terminal 1)
```powershell
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```
*What it does:* Boots the FastAPI web server to receive events and host the dashboard.

---

### 6. Load POS Sales Data (Run in Terminal 2)
```powershell
python -m scripts.load_pos --path "data/POS - sample transactionsb1e826f (1).csv"
```
*What it does:* Parses and loads the POS transaction export into the SQLite database.

---

### 7. Process CCTV Clips and Ingest Events (Run in Terminal 2)

#### Camera 1 (Zone video)
```powershell
python -m pipeline.run --detector yolo --video "data/clips/CAM 1 - zone.mp4" --camera CAM_FLOOR_01 --store ST1008 --start-time 2026-04-10T06:45:00Z --api-url http://127.0.0.1:8000
```
*What it does:* Runs YOLO object tracking on Camera 1 to generate and ingest floor events.

#### Camera 2 (Zone video)
```powershell
python -m pipeline.run --detector yolo --video "data/clips/CAM 2 - zone.mp4" --camera CAM_FLOOR_02 --store ST1008 --start-time 2026-04-10T06:45:00Z --api-url http://127.0.0.1:8000
```
*What it does:* Runs YOLO object tracking on Camera 2 to generate and ingest floor events.

#### Camera 3 (Entry video)
```powershell
python -m pipeline.run --detector yolo --video "data/clips/CAM 3 - entry.mp4" --camera CAM_FLOOR_03 --store ST1008 --start-time 2026-04-10T06:45:00Z --api-url http://127.0.0.1:8000
```
*What it does:* Runs YOLO object tracking on Camera 3 to generate and ingest entry events.

#### Camera 5 (Billing video)
```powershell
python -m pipeline.run --detector yolo --video "data/clips/CAM 5 - billing.mp4" --camera CAM_FLOOR_05 --store ST1008 --start-time 2026-04-10T06:45:00Z --api-url http://127.0.0.1:8000
```
*What it does:* Runs YOLO object tracking on Camera 5 to generate and ingest checkout events.

---

### 8. Run Live Replay (Optional Alternative to Video Processing)
```powershell
python -m scripts.replay_events --speed 3 --api-url http://127.0.0.1:8000
```
*What it does:* Bypasses CPU video analysis and plays a pre-programmed synthetic flow at 3x speed.

---

### 9. Launch the Dashboard
```powershell
start "http://127.0.0.1:8000/dashboard?store=ST1008"
```
*What it does:* Opens the Live Store Intelligence web page in your default browser.

---

### 10. Execute Test Suite
```powershell
pytest -v
```
*What it does:* Runs all unit, integration, and migration test assertions.
