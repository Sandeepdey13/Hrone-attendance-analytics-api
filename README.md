# Employee Attendance & Analytics API

FastAPI and MongoDB implementation of the HROne attendance contract. All CRUD, correction, aggregation, and explain routes are implemented in `app/main.py`.

Requires Python 3.11+ and MongoDB 6.0+. Install dependencies with `python -m pip install -r requirements.txt`.

Set `MONGO_URI` and `MONGO_DB` in the environment, then start from the repository root with `uvicorn app.main:app --port 8000`.

Indexes are created idempotently on startup. No `.env`, secrets, or Dockerfile are included.
