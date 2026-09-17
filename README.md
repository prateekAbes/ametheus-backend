# AMETHEUS Backend Engine (Render Deployment Guide)

FastAPI + Asynchronous MongoDB (`motor`) tournament engine for AMETHEUS.

---

## Deploying on Render (Two Independent Methods)

### Method 1: Render Blueprint (1-Click with MongoDB Atlas)
1. In the [Render Dashboard](https://dashboard.render.com), click **New +** -> **Blueprint**.
2. Select your repository.
3. Render reads `render.yaml` and configures `ametheus-backend`.
4. Supply your `MONGODB_URI` (from MongoDB Atlas) when prompted or in Environment settings.
5. Click **Apply**.
6. Once live, test `https://<your-service>.onrender.com/health` (returns `200 OK`) and `https://<your-service>.onrender.com/health/db` (returns MongoDB ping and collection counts).

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
   * `MONGODB_URI`: Your MongoDB Atlas connection string (e.g. `mongodb+srv://<user>:<password>@cluster.mongodb.net/?retryWrites=true&w=majority`).
   * `MONGODB_DB_NAME`: `ametheus`
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
