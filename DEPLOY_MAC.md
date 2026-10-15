# Mac setup and deployment

## 1. Unzip and open the project

Extract the ZIP, open Terminal, and change into the folder containing `app/` and `requirements.txt`:

```bash
cd ~/Downloads/SDT_candidate_solution
ls
```

If you extracted it somewhere else, use that folder's path instead. The `ls` output should include `app`, `requirements.txt`, `openapi.yaml`, and `README.md`.

## 2. Check Python and Git

```bash
python3 --version
git --version
```

Python 3.11 or later is recommended. If either command is missing and you use Homebrew:

```bash
brew install python git
```

## 3. Create a virtual environment and install packages

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## 4. Create a MongoDB Atlas database

1. Sign in or create an account at https://www.mongodb.com/atlas.
2. Create a cluster and a database user with read/write access to the `attendance_db` database.
3. In Network Access, add your current IP address for local testing.
4. In the cluster's Connect option, choose Drivers and copy the Python/MongoDB connection string.
5. Replace the username/password placeholders. Use a URL-encoded password if it contains reserved URL characters.
6. Do not share this URI or commit it to GitHub.

For Render, Atlas must accept connections from the hosted service. If the chosen hosting plan does not provide fixed outbound IPs, an Atlas access-list entry of `0.0.0.0/0` may be needed for a simple demo. That permits connection attempts from any IP, so use a strong database password and a database user limited to the required database. Prefer fixed outbound IPs or private networking where available, and remove broad access if no longer needed.

## 5. Configure local environment variables

Create a local `.env` file in the project root. You can start from the example:

```bash
cp .env.example .env
```

Edit `.env` and replace `MONGO_URI` with your real Atlas connection string. Keep `MONGO_DB=attendance_db`. `.env` is excluded by `.gitignore` and must never be committed.

## 6. Run and test locally

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Open these URLs in your browser:

- API documentation: http://127.0.0.1:8000/docs
- Health check: http://127.0.0.1:8000/health

The health check should return `{"status":"ok"}`. If it returns a database error, verify the Atlas URI, database credentials, and network access list. Stop the local server with `Control+C`.

## 7. Push to a public GitHub repository

Create an empty **public** GitHub repository named `hrone-attendance-analytics-api`. Do not initialize it with a README, licence, or `.gitignore`, because those files already exist locally.

From the project root in Terminal:

```bash
git init
git add .
git status
git commit -m "Build attendance analytics API"
git branch -M main
git remote add origin https://github.com/YOUR_GITHUB_USERNAME/hrone-attendance-analytics-api.git
git push -u origin main
```

Replace `YOUR_GITHUB_USERNAME` with your username. Before committing, check `git status` and confirm `.env` is not listed. If you already initialized Git, do not run `git init` again; add the remote if one is not set.

## 8. Deploy on Render

1. Sign in at https://render.com and connect your GitHub account.
2. Choose **New + → Blueprint** and select `hrone-attendance-analytics-api`, or choose **New + → Web Service** and select the same repository.
3. If using the Blueprint, Render reads `render.yaml`. If creating a Web Service manually, set:
   - Runtime: Python
   - Build command: `pip install -r requirements.txt`
   - Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
   - Health check path: `/health`
4. Add environment variables:
   - `MONGO_URI`: your complete Atlas connection string
   - `MONGO_DB`: `attendance_db`
   - `LOG_LEVEL`: `INFO`
5. Create the service and wait for the deploy to finish. Check the deploy logs if it fails.

Never paste the MongoDB connection string into a source file, commit it to GitHub, or include it in screenshots.

## 9. Verify the public deployment

Render gives the service a URL similar to `https://your-service-name.onrender.com`. Open:

- `https://your-service-name.onrender.com/health`
- `https://your-service-name.onrender.com/docs`
- `https://your-service-name.onrender.com/openapi.json`

The first should report `{"status":"ok"}` once MongoDB is reachable. In `/docs`, you can send test requests to the endpoints.

For a basic employee-creation test, use a unique employee code each time:

```bash
curl -X POST 'https://YOUR-SERVICE.onrender.com/employees' \
  -H 'Content-Type: application/json' \
  -d '{"emp_code":"EMP1001","name":"Test Employee","email":"test.employee@example.com","department":"Engineering","shift_start":"09:30","shift_end":"18:30","joined_on":"2026-01-15"}'
```

Replace `YOUR-SERVICE` with the assigned Render hostname. A successful create request should return HTTP 201. Running it again with the same `emp_code` should return HTTP 409 because employee codes are unique.

## 10. Share the links

Share both of these with the recruiter:

- GitHub repository: `https://github.com/YOUR_GITHUB_USERNAME/hrone-attendance-analytics-api`
- Live API docs: `https://YOUR-SERVICE.onrender.com/docs`

Also include the base API URL if requested: `https://YOUR-SERVICE.onrender.com`.

Keep the repository public as requested by the assignment, but never publish `.env`, database passwords, private data, or credentials. Re-check the repository's file list after pushing.
