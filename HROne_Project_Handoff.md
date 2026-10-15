# HROne Employee Attendance & Analytics API — Project Handoff

Last updated: 15 October 2026

## Repository
- GitHub: https://github.com/Sandeepdey13/Hrone-attendance-analytics-api
- Visibility: Public
- Branch: `main`
- Latest confirmed commit: `63bd109` — `Add API validation tests`
- The latest screenshot showed the `tests/` folder and the latest commit on GitHub.
- Latest local Git status was clean and up to date with `origin/main`.

## Stack and important files
- Python, FastAPI, PyMongo, MongoDB
- `app/main.py` — API implementation
- `openapi.yaml` — OpenAPI contract
- `requirements.txt` — dependencies
- `README.md` — setup and overview
- `DATA_MODEL.md`, `DECISIONS.md`, `REVIEW.md` — documentation
- `.env.example` — placeholder configuration
- `.gitignore` — excludes local environments and `.env` files
- `DEPLOY_MAC.md`, `render.yaml` — deployment files
- `tests/test_api.py` — 10 automated tests, pushed in commit `63bd109`

## API endpoints identified
1. `GET /health`
2. `POST /employees`
3. `GET /employees`
4. `POST /attendance/punch-in`
5. `GET /attendance`
6. `POST /attendance/punch-out`
7. `PATCH /attendance/{emp_code}/{date}`
8. `GET /analytics/employees/{emp_code}/monthly`
9. `GET /analytics/departments/summary`
10. `GET /analytics/leaderboard/late`
11. `GET /analytics/departments/{department}/trend`
12. `GET /admin/explain/{endpoint}`

## Local environment
Project directory: `~/Downloads/candidate_kit_submission`

Activate the virtual environment:
```bash
cd ~/Downloads/candidate_kit_submission
source .venv/bin/activate
```

Local MongoDB settings:
- URI: `mongodb://127.0.0.1:27017`
- Database: `attendance_db`
- Do not commit `.env` or real credentials. `.env.example` should contain placeholders only.

MongoDB installed on the Mac reports version `8.3.3`. `brew trust mongodb/brew` succeeded, but `brew services start mongodb-community` reported that the formula has no service definition. MongoDB was successfully started manually with:

```bash
mkdir -p ~/data/db
mongod --dbpath ~/data/db --bind_ip 127.0.0.1 --port 27017
```

Keep that terminal running while using the local database. Verify the connection from a second terminal:
```bash
python -c "from pymongo import MongoClient; c=MongoClient('mongodb://127.0.0.1:27017', serverSelectionTimeoutMS=3000); print(c.admin.command('ping'))"
```
Expected output: `{'ok': 1.0}`

## Running the API
With `.venv` active and MongoDB running:
```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```
If port `8000` is already in use, another server may already be running. Check it before starting another copy.

Useful local URLs:
- Swagger docs: http://127.0.0.1:8000/docs
- OpenAPI JSON: http://127.0.0.1:8000/openapi.json
- Health: http://127.0.0.1:8000/health

The health endpoint returns `503 {"detail":"MongoDB unavailable"}` when MongoDB is stopped or unreachable.

## Automated tests
`tests/test_api.py` contains 10 tests covering:
- Health endpoint
- Valid/invalid date parsing
- Valid/invalid month parsing
- Work-hours calculation
- Employee code validation
- Employee email validation
- Attendance status validation
- Invalid attendance date range

The suite initially reported 9 passed and 1 failed when MongoDB was stopped. After MongoDB was started manually and its ping succeeded, the test run completed with:
```text
10 passed, 2 warnings
```

Run tests from the project root:
```bash
python -m pytest -v
```
Do not run the test file directly by its absolute path; that produced `ModuleNotFoundError: No module named 'app'`. Use `python -m pytest -v` from the project root instead.

The two warnings concern FastAPI's deprecated `@app.on_event("startup")` mechanism; they did not fail the tests.

## Git synchronization
The push succeeded:
```text
[main 63bd109] Add API validation tests
6c0a2a4..63bd109  main -> main
```
A subsequent check showed branch `main`, clean working tree, and branch up to date with `origin/main`. No further commit is needed unless files change.

## Remaining checks
- Click the `tests/` folder on GitHub and verify `test_api.py` is inside.
- Verify `.env` and real secrets are not tracked or visible on GitHub. `.env.example` is expected.
- The 10 tests cover selected behaviors; they do not prove every endpoint or all assignment requirements are fully tested.
- `PROBLEM_STATEMENT.docx`, `sample_data/`, and `sample_seed.py` were not present in the cleaned GitHub listing. The README says the evaluator seeds its own data and advises not committing data dumps. Add omitted assignment files only if the official instructions explicitly require them.
- Submission link: https://github.com/Sandeepdey13/Hrone-attendance-analytics-api
- Submit the repository URL using the hiring/application form and email instructions, using the same email address as the application if required.

## Useful final checks
```bash
git branch --show-current
git status --short
git log -1 --oneline
python -m pytest -v
```
Before running tests, ensure MongoDB is running. An empty `git status --short` means there are no uncommitted changes.
