# Employee Attendance & Analytics API

FastAPI + PyMongo service implementing the HROne attendance contract. All application code lives in `app/main.py`.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI='mongodb://localhost:27017'
export MONGO_DB='attendance_db'
uvicorn app.main:app --port 8000
```

MongoDB 6.0+ is required. `MONGO_URI` and `MONGO_DB` are read from the environment; a local `.env` can also be used. Indexes are created idempotently when the service starts and are retried when `/health` is called after MongoDB becomes available. The hiring evaluator seeds its own dataset. For local testing, the `sample_seed.py` and `sample_data/` files from the original candidate kit can be used privately; do not commit data dumps.

## Main areas

- Employee creation and paginated employee listing
- Race-safe punch-in / punch-out and audited regularization
- MongoDB aggregation pipelines for monthly, department, leaderboard, and trend analytics
- `GET /admin/explain/{endpoint}` for execution-statistics explain output

Read `REVIEW.md` for starter defects and `DECISIONS.md` for the main design choices. Do not commit `.env`, credentials, or local virtual environments.
