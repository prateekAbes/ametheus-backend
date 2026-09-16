# AMETHEUS Backend Engine (Render Deployment Guide)

FastAPI + Async SQLAlchemy 2.0 tournament engine for AMETHEUS.

---

## Deploying on Render (Two Independent Methods)

### Method 1: Render Blueprint (1-Click with PostgreSQL)
1. In the [Render Dashboard](https://dashboard.render.com), click **New +** -> **Blueprint**.
2. Select your repository.
3. Render reads `render.yaml` and automatically creates:
   * **`ametheus-backend`** (Python Web Service)
   * **`ametheus-db`** (Managed PostgreSQL instance)
4. Click **Apply**.
5. Once live, test `https://<your-service>.onrender.com/health` (returns `200 OK`).

---

### Method 2: Manual Web Service on Render (No Blueprint)
If creating a standalone Web Service:
1. Click **New +** -> **Web Service**.
2. Settings:
   * **Name:** `ametheus-backend`
   * **Language / Runtime:** `Python 3`
   * **Root Directory:** `backend` (if deploying from full repo) or leave blank if deploying standalone backend repo.
   * **Build Command:** `pip install -r requirements.txt`
   * **Start Command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
   * **Health Check Path:** `/health`
3. Environment Variables:
   * `PYTHON_VERSION`: `3.12.7`
   * `DATABASE_URL`: Your PostgreSQL connection string from Render or external database. (If left unset, defaults to built-in SQLite `sqlite+aiosqlite:///./ametheus.db`).
   * `SECRET_KEY`: Any secure random string.
   * `CORS_ORIGINS`: `http://localhost:3000,https://your-frontend.vercel.app`

---

## Local Development

```bash
cd backend
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8000
```
Interactive Swagger API Documentation: `http://localhost:8000/docs`
