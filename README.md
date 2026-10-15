# Employee Attendance & Analytics API

A FastAPI service for employee records, daily attendance, audited corrections, and MongoDB aggregation-based analytics. The API paths and response shapes follow the supplied OpenAPI contract.

## Requirements

- Python 3.11 or newer
- MongoDB 6.0 or newer

## Local setup

Start MongoDB and keep it running. For local MongoDB, the included environment example uses `127.0.0.1:27017`.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open the API documentation at `http://127.0.0.1:8000/docs`. The health endpoint checks MongoDB connectivity and returns `503` if the database is unavailable.

To use MongoDB Atlas instead, set `MONGO_URI` to your own connection string in the local `.env` file. Never commit `.env` or real credentials.

## Tests

With MongoDB running, install the development dependencies and run the tests from the project root:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -v
```

The health test requires a reachable MongoDB instance. The tests cover selected validation, date helpers, work-hour calculation, and health behavior; they are not a substitute for testing every API operation against the evaluation dataset.

## Main functionality

- Employee creation and paginated listing
- Punch-in and punch-out with attendance rules
- Manual regularization with a recorded history
- Monthly employee, department summary, late leaderboard, and department trend analytics using MongoDB aggregation pipelines
- Explain output for supported query paths

See `REVIEW.md` for issues found in the starter and `DECISIONS.md` for implementation choices. `DATA_MODEL.md` and `openapi.yaml` describe the model and API contract.
