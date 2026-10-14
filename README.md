# Employee Attendance & Analytics API

Python 3.11+, FastAPI, and MongoDB 6.0+. All application code is in `app/main.py`.
The HTTP contract is in `openapi.yaml`; stored data is described in `DATA_MODEL.md`.

## Run on Windows PowerShell

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:MONGO_URI = 'mongodb://localhost:27017'
$env:MONGO_DB = 'attendance_db'
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

Use your Atlas URI in place of the local URI when connecting to Atlas. Alternatively,
create a local `.env` with `MONGO_URI` and `MONGO_DB`; existing environment variables
take priority. Keep credentials and `.env` out of Git.

On Linux/macOS, activate the virtual environment with `source .venv/bin/activate`,
install `requirements.txt`, set those environment variables, and run
`uvicorn app.main:app --port 8000`.

Open `http://localhost:8000/docs` for the API, or `http://localhost:8000/health` for
database readiness. All twelve contract operations are implemented. Indexes are
created automatically at startup; index creation failures stop startup.

Optional sample setup: `python sample_seed.py` replaces every document in the two
configured collections with six employees and nine attendance records. Use it only
for a database whose attendance data you intend to replace.

## Verify

```powershell
.venv\Scripts\python.exe tests\check_api.py
.venv\Scripts\python.exe tests\check_api.py --scale
```

Checks use a uniquely named temporary database on the configured MongoDB server,
exercise HTTP validation, writes, concurrency, analytics and explain plans, then
remove that temporary database. The configured attendance database is only read.
The optional scale check adds 100,000 records to the temporary database. MongoDB
credentials must allow creating and removing temporary databases.

Submission files include source, requirements, README, REVIEW and DECISIONS.
Do not submit credentials, virtual environments or generated data dumps.
