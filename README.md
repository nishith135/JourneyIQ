# JourneyIQ — Customer Journey & Conversion Analytics Platform

JourneyIQ is an end-to-end customer journey reconstruction, multi-channel attribution, funnel analytics, experimentation, and data-quality guardrail platform built with **FastAPI**, **Jinja2 + HTMX**, **PostgreSQL 17**, **SQLAlchemy 2.1**, and **Alembic**.

---

## Quick Start Commands

### 1. Start PostgreSQL 17 (Port 5439)
```powershell
postgres -D "c:\CandidateWorkspace\journeyIQ\.pgdata" -p 5439 -h 127.0.0.1
```

### 2. Apply Database Schema Migrations
```powershell
cd c:\CandidateWorkspace\journeyIQ\backend
c:\CandidateWorkspace\journeyIQ\.venv\Scripts\alembic.exe upgrade head
```

### 3. Generate Deterministic Synthetic Dataset & Run Data Validator
```powershell
cd c:\CandidateWorkspace\journeyIQ
c:\CandidateWorkspace\journeyIQ\.venv\Scripts\python.exe data-generation\generator\generate_data.py --seed 42 --scale mvp
c:\CandidateWorkspace\journeyIQ\.venv\Scripts\python.exe data-generation\validation\validate_data.py
```

### 4. Start the FastAPI + Jinja2/HTMX Server
```powershell
cd c:\CandidateWorkspace\journeyIQ\backend
c:\CandidateWorkspace\journeyIQ\.venv\Scripts\uvicorn.exe app.main:app --host 127.0.0.1 --port 8000
```
Open **`http://127.0.0.1:8000/dashboard`** in your browser.

### 5. Run the Automated Pytest Verification Suite
```powershell
cd c:\CandidateWorkspace\journeyIQ
c:\CandidateWorkspace\journeyIQ\.venv\Scripts\pytest.exe backend\tests\test_mandatory_workflows.py -v
```

