# Haunter

Haunter is an autonomous CI failure diagnosis & fix agent.

## Project Structure

```
haunter/
├── frontend/   # Next.js web interface (App Router, TypeScript, Tailwind CSS)
└── backend/    # FastAPI Python service
```

### `/frontend`
Next.js client built with TypeScript, Tailwind CSS, and the App Router.

**Local Setup & Run:**
```bash
cd frontend
npm install
npm run dev
```
The frontend application will be available at [http://localhost:3000](http://localhost:3000).

### `/backend`
FastAPI backend API service.

**Local Setup & Run:**
```bash
cd backend
# Windows:
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload

# macOS/Linux:
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```
The backend service will be available at [http://localhost:8000](http://localhost:8000), with the health check at [http://localhost:8000/health](http://localhost:8000/health).
