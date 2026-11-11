# JourneyIQ — Customer Journey & Conversion Analytics Platform

JourneyIQ is an end-to-end customer journey reconstruction, multi-channel attribution, funnel analytics, experimentation, and data-quality guardrail platform built with **FastAPI**, **Jinja2 + HTMX**, **PostgreSQL 17**, **SQLAlchemy**, and **Alembic**.

---

## Architecture & Tech Stack

```mermaid
flowchart LR
    Ingest["Synthetic Generator / Event Ingest (generate_data.py)"] -->|"psycopg COPY / SQLAlchemy"| PG["PostgreSQL 17 (journeyiq_sandbox, 22 tables, 80 indexes)"]
    PG -->|"SQL Aggregates & Keyset/Offset Queries"| Service["Analytics & Domain Services (backend/app/services/)"]
    Service --> API["FastAPI REST API (/api/v1/*)"]
    Service --> UI["Server-Rendered Jinja2 + HTMX Views (/dashboard, /funnels, /journeys, /exceptions, /experiments)"]
    PG -->|"Set-Based SQL Validation (validate_data.py)"| Reports["Manifest & Validation Reports"]
```

- **Backend & Web UI:** FastAPI (`backend/app/main.py`), Jinja2 templates (`backend/app/templates/`), HTMX, and Pydantic Settings (`backend/app/core/config.py`).
- **Database & ORM:** PostgreSQL 17 (`127.0.0.1:5432`), SQLAlchemy 2.x (`psycopg` 3 driver), Alembic migrations (`backend/migrations/`).
- **Analytics & Domain Logic:**
  - `backend/app/services/domain_service.py`: Event ingestion, sessionization, consent-gated identity stitching, authoritative order deduplication, experiment bucketing, and versioned 7-day attribution outcomes.
  - `backend/app/services/analytics_service.py`: SQL-backed KPI dashboard, SQL-joined 5-stage Stage Reach vs. Strict Sequential funnel analytics, paginated journey explorer, bounded exception monitoring, and experiment lift summaries.
- **Documentation:**
  - [Product Requirements (PRD)](docs/PRD.md)
  - [Architecture Overview](docs/ARCHITECTURE.md)
  - [Business Rules](docs/BUSINESS_RULES.md)
  - [Data Dictionary](docs/DATA_DICTIONARY.md)
  - [Design Decisions](docs/DECISIONS.md)
  - [Success Criteria](docs/SUCCESS_CRITERIA.md)

---

## Data Model (ER Diagram — 22 Tables)

All `22` SQLAlchemy models in [`backend/app/models/entities.py`](backend/app/models/entities.py) share `UUID` primary keys alongside unique `*_business_id` identifiers, strict `CHECK` constraints, and `80` PostgreSQL indexes:

```mermaid
erDiagram
    customer {
        uuid customer_id PK
        string customer_business_id UK
        string email UK
        string account_status
    }
    customer_journey {
        uuid journey_id PK
        string journey_business_id UK
        uuid customer_id FK
        string anonymous_id
        string status
        int identity_version
        datetime first_touch_at
        datetime window_end_at
    }
    session {
        uuid session_id PK
        string session_business_id UK
        uuid customer_id FK
        uuid journey_id FK
        string client_instance_id
        string channel
        string traffic_classification
        datetime started_at
    }
    journey_event {
        uuid event_id PK
        string event_business_id UK
        uuid session_id FK
        uuid customer_id FK
        uuid product_id FK
        uuid cart_id FK
        uuid order_id FK
        string event_type
        string channel
        datetime event_timestamp
        datetime ingestion_timestamp
    }
    event_quarantine {
        uuid quarantine_id PK
        string quarantine_business_id UK
        uuid event_id FK
        string issue_type
        string resolution_status
        datetime detected_at
    }
    journey_outcome {
        uuid outcome_id PK
        string outcome_business_id UK
        string scope_type
        uuid session_id FK
        uuid journey_id FK
        uuid conversion_order_id FK
        string outcome_status
        string attribution_status
    }
    product {
        uuid product_id PK
        string product_business_id UK
        string product_name
        string category
        decimal current_price
    }
    cart {
        uuid cart_id PK
        string cart_business_id UK
        uuid customer_id FK
        uuid session_id FK
        string status
    }
    cart_item {
        uuid cart_item_id PK
        string cart_item_business_id UK
        uuid cart_id FK
        uuid product_id FK
        int quantity
        decimal unit_price
    }
    orders {
        uuid order_id PK
        string order_business_id UK
        uuid customer_id FK
        uuid cart_id FK
        uuid session_id FK
        string status
        decimal total_amount
        datetime purchase_confirmed_at
    }
    order_item {
        uuid order_item_id PK
        string order_item_business_id UK
        uuid order_id FK
        uuid product_id FK
        int quantity
        decimal line_total
    }
    payment_attempt {
        uuid payment_attempt_id PK
        string payment_attempt_business_id UK
        uuid order_id FK
        int attempt_number
        decimal amount
        string status
    }
    refund {
        uuid refund_id PK
        string refund_business_id UK
        uuid order_id FK
        decimal amount
        string status
    }
    support_ticket {
        uuid ticket_id PK
        string ticket_business_id UK
        uuid customer_id FK
        uuid session_id FK
        uuid order_id FK
        uuid journey_id FK
        string issue_type
        string priority
        string status
    }
    support_ticket_event {
        uuid ticket_event_id PK
        string ticket_event_business_id UK
        uuid ticket_id FK
        string new_status
        datetime event_timestamp
    }
    consent_record {
        uuid consent_record_id PK
        string consent_business_id UK
        uuid customer_id FK
        string anonymous_id
        string consent_purpose
        string consent_status
    }
    identity_link {
        uuid identity_link_id PK
        string identity_link_business_id UK
        uuid customer_id FK
        uuid consent_record_id FK
        string anonymous_id
        string status
    }
    experiment {
        uuid experiment_id PK
        string experiment_business_id UK
        string name
        string status
        int config_version
    }
    experiment_variant {
        uuid variant_id PK
        string variant_business_id UK
        uuid experiment_id FK
        string variant_name
        bool is_control
        decimal allocation_percentage
    }
    experiment_assignment {
        uuid assignment_id PK
        string assignment_business_id UK
        uuid experiment_id FK
        uuid variant_id FK
        uuid identity_link_id FK
        uuid consent_record_id FK
        int assignment_bucket
    }
    experiment_exposure {
        uuid exposure_id PK
        string exposure_business_id UK
        uuid assignment_id FK
        uuid event_id FK
        string channel
        datetime exposed_at
    }
    audit_event {
        uuid audit_event_id PK
        string audit_business_id UK
        string entity_type
        uuid entity_id
        string action
        datetime occurred_at
    }

    customer ||--o{ customer_journey : "owns"
    customer ||--o{ session : "initiates"
    customer ||--o{ journey_event : "acts"
    customer ||--o{ cart : "owns"
    customer ||--o{ orders : "places"
    customer ||--o{ support_ticket : "opens"
    customer ||--o{ consent_record : "grants"
    customer ||--o{ identity_link : "resolves to"

    customer_journey ||--o{ session : "groups"
    customer_journey ||--o{ support_ticket : "references"
    customer_journey ||--o{ journey_outcome : "evaluates"

    session ||--o{ journey_event : "contains"
    session ||--o{ cart : "creates"
    session ||--o{ orders : "converts to"
    session ||--o{ support_ticket : "originates"
    session ||--o{ journey_outcome : "evaluates"

    product ||--o{ cart_item : "added to"
    product ||--o{ order_item : "purchased in"
    product ||--o{ journey_event : "viewed in"

    cart ||--o{ cart_item : "contains"
    cart ||--o{ orders : "checks out as"
    cart ||--o{ journey_event : "referenced by"

    orders ||--o{ order_item : "contains"
    orders ||--o{ payment_attempt : "attempts"
    orders ||--o{ refund : "refunds"
    orders ||--o{ support_ticket : "concerns"
    orders ||--o{ journey_event : "triggers"
    orders ||--o{ journey_outcome : "converted order"

    support_ticket ||--o{ support_ticket_event : "logs"
    journey_event ||--o{ event_quarantine : "quarantines"
    journey_event ||--o{ experiment_exposure : "records"

    consent_record ||--o{ identity_link : "authorizes"
    consent_record ||--o{ experiment_assignment : "governs"
    identity_link ||--o{ experiment_assignment : "links"

    experiment ||--o{ experiment_variant : "defines"
    experiment ||--o{ experiment_assignment : "assigns"
    experiment_variant ||--o{ experiment_assignment : "allocates"
    experiment_assignment ||--o{ experiment_exposure : "exposes"
```

> `audit_event` records immutable state transitions via polymorphic `(entity_type, entity_id)` references across domain entities rather than a single database foreign key.

---

## PostgreSQL & Environment Configuration

Runtime database selection is explicit via `RUNTIME_DB_TARGET` in `.env` (copy from [`.env.example`](.env.example)):

| Variable | Purpose | Configured Sandbox / Test Separation |
| --- | --- | --- |
| `RUNTIME_DB_TARGET` | Selects backend runtime target (`sandbox` or `primary`, default `primary`) | Set to `sandbox` in `.env` so Uvicorn connects explicitly to `SANDBOX_DATABASE_URL` |
| `SANDBOX_DATABASE_URL` | Populated full-scale PostgreSQL 17 sandbox | `postgresql+psycopg://postgres:<PW>@127.0.0.1:5432/journeyiq_sandbox` |
| `DATABASE_URL` | Separate primary database URL (`journeyiq`) | Never silently redirected; isolated CLI/tests refuse to touch it |
| `TEST_DATABASE_URL` | Dedicated destructive pytest database (`*test*`) | Enforced isolated from both primary (`journeyiq`) and `journeyiq_sandbox` |
| `JOURNEYIQ_READER_PASSWORD` | Password for read-only verification role (`journeyiq_reader`) | `SELECT`-only access across all 22 tables |

Wait-free credential safety: `backend/app/db/session.py` (`get_runtime_database_info`) and FastAPI startup logs always redact passwords.

---

## Setup & Execution Instructions (Fresh Clone)

> [!NOTE]
> `.env` and local PostgreSQL databases/CSVs (`.pgdata/`, `data-generation/sandbox-output/`) are intentionally excluded from Git. After cloning this repository, follow the steps below to configure `.env` and populate PostgreSQL before launching the UI.

### 1. Install Dependencies & Create `.env`
Create a Python virtual environment, install dependencies from [`backend/requirements.txt`](backend/requirements.txt), and copy [`.env.example`](.env.example) to `.env`:
```powershell
python -m venv .venv
.\.venv\Scripts\pip.exe install -r backend\requirements.txt
Copy-Item .env.example .env
```
Create an empty PostgreSQL 17 database (for example `journeyiq_sandbox`), then update `.env` with your local credentials:
- Set `SANDBOX_DATABASE_URL` to your `journeyiq_sandbox` connection string
- Set `RUNTIME_DB_TARGET=sandbox`
- Set separate `DATABASE_URL` and `TEST_DATABASE_URL` values if you plan to run tests

### 2. Populate & Validate Dataset (`seed=42`)
Both [`generate_data.py`](data-generation/generator/generate_data.py) and [`validate_data.py`](data-generation/validation/validate_data.py) require an explicit isolated target (`--db-env-var` or `--database-url`) and explicit `--output-dir`. Running `--confirm-reset` automatically creates all `22` tables and `80` indexes via SQLAlchemy `Base.metadata.create_all`:

```powershell
# Option A — Quick MVP dataset (149 sessions, 510 events)
.\.venv\Scripts\python.exe data-generation\generator\generate_data.py `
  --db-env-var SANDBOX_DATABASE_URL --seed 42 --scale mvp --confirm-reset `
  --output-dir data-generation\sandbox-output\seed-42-mvp

# Option B — Full-scale dataset (107,801 sessions, 2,053,601 events + optional CSV export)
.\.venv\Scripts\python.exe data-generation\generator\generate_data.py `
  --db-env-var SANDBOX_DATABASE_URL --seed 42 --scale full --confirm-reset --export-csv `
  --output-dir data-generation\sandbox-output\seed-42-full

# Non-destructive SQL aggregate & anti-join validator (7 checks)
.\.venv\Scripts\python.exe data-generation\validation\validate_data.py `
  --db-env-var SANDBOX_DATABASE_URL `
  --output-dir data-generation\sandbox-output\seed-42-full
```

### 3. Start the FastAPI + Jinja2/HTMX Server
```powershell
cd backend
..\.venv\Scripts\uvicorn.exe app.main:app --host 127.0.0.1 --port 8000
```
Then open:
- Dashboard: `http://127.0.0.1:8000/dashboard`
- Funnels: `http://127.0.0.1:8000/funnels`
- Journeys Explorer: `http://127.0.0.1:8000/journeys` (`3,921` pages at `25` per page at full scale; sample hero journey: `/journeys/JRN-HERO-001`)
- Exceptions & Quarantine: `http://127.0.0.1:8000/exceptions`
- Experiments & Lift: `http://127.0.0.1:8000/experiments`
- Health Check JSON: `http://127.0.0.1:8000/api/v1/health`

### 4. Run Automated Safety & Workflow Tests
Pass an isolated `TEST_DATABASE_URL` (separate from both `journeyiq` and `journeyiq_sandbox`) when running [`test_mandatory_workflows.py`](backend/tests/test_mandatory_workflows.py):
```powershell
.\.venv\Scripts\pytest.exe backend\tests\test_mandatory_workflows.py -v
```

---

## Verified Full-Scale Sandbox Dataset (`seed=42`, `scale=full`)

Populated in PostgreSQL 17 (`127.0.0.1:5432`, `journeyiq_sandbox`, `22` tables, `80` indexes, `1,323.62 MB`):
- **Dataset Checksum (`dataset_sha256`):** `477968e262182568387fe6a3c11240867d89e18ef763bc95a343dfeb5d56ab5c`
- **Journeys:** `98,001` (`93,101` eligible non-bot journeys; `30,381` converted, `12,180` in progress, `50,540` non-converted, `4,900` excluded)
- **Sessions:** `107,801` (`102,901` human: `68,602` Web / `34,299` App; `4,900` bot excluded)
- **Events:** `2,053,601`
- **Confirmed Orders & Revenue:** `35,281` confirmed orders, `$11,472,288.24` total revenue (`32.63%` 7-day journey conversion rate, `34.29%` session conversion rate)

### Four Mandatory Anomaly Scenarios (`ANOM-001`–`ANOM-004`)
| Code | Scenario | Verified Full-Scale Count |
| --- | --- | ---: |
| `ANOM-001` | Late-arriving events reconstructed accurately by `event_timestamp` | `2,940` |
| `ANOM-002` | Conflicting identity links & consent-blocked identity merges quarantined | `1,960` conflicted links (`3,920` identity quarantines) |
| `ANOM-003` | Duplicate purchase conversion signals deduplicated against `orders` | `2,940` |
| `ANOM-004` | Bot traffic sessions classified (`BOT`) and excluded from human KPIs | `4,900` |
| `SES-006` | Events missing `client_instance_id` quarantined (included in `4,410` total quarantined) | `490` |

### Data Integrity Validation (`7/7 PASSED` in `2.37s`)
- `DATA-001`: Primary keys and `2,053,601` event business IDs unique (`PASSED`)
- `DATA-002`: Foreign key integrity — `0` orphan sessions or events (`PASSED`)
- `DATA-003`: Session timestamps satisfy `started_at <= last_event_at <= ended_at` (`PASSED`)
- `DATA-004`: Exact decimal order line-item arithmetic across `35,281` orders (`PASSED`)
- `DATA-005`: Every `CONFIRMED` order has a corresponding `SUCCEEDED` `PaymentAttempt` (`PASSED`)
- `DATA-006`: Experiment configuration invariants across `2` experiments (`PASSED`)
- `ANOM-001-004`: All 4 anomaly classes and `4,410` quarantined records verified (`PASSED`)

---

## Known Limitations

- **UI Page-Switching Latency at Full Scale (`2,053,601` events / `107,801` sessions):** Because JourneyIQ serves uncached queries synchronously per request, switching between analytics tabs re-executes heavy queries on every page load:
  - `/funnels` (~`20.56s` for `ALL` channels; ~`13.60s` `WEB`, ~`7.57s` `APP`) and `/dashboard` (~`11.60s`): `get_funnel_metrics` joins `journey_event` to `session` and streams every funnel-stage event tuple (`SESSION_STARTED`, `PRODUCT_VIEWED`, `ADD_TO_CART`, `CHECKOUT_STARTED`, `PURCHASE_COMPLETED`) across `102,901` human sessions into Python memory (`session_stage_times`) to enforce non-decreasing per-session stage timestamps (`Sequential Progression`). `/funnels` also invokes `get_dashboard_metrics` (`~1.73s`) on the same request.
  - `/experiments` (~`10.28s`): `get_experiments_summary` loads `93,101` `ExperimentAssignment`, `75,461` `ExperimentExposure`, `35,281` confirmed `Order`, and `107,801` `SessionRecord` ORM objects into Python memory per request instead of aggregating inside PostgreSQL.
  - `/journeys` (~`1.61s`–`2.00s` per page): `_query_journeys_page` computes `ROW_NUMBER() OVER (PARTITION BY journey_id ...)` over `205,802` `journey_outcome` rows and runs a full `COUNT(*)` subquery on every page transition, plus `CASE` hero sorting.
  - `/journeys/{id}` (~`0.12s`) and `/exceptions` (~`0.61s`) remain sub-second because they use indexed point lookups or SQL counts with `LIMIT 50`.
- The legacy primary PostgreSQL instance on port `5439` (`.pgdata`) is intentionally separate; runtime defaults in `.env` select `SANDBOX_DATABASE_URL` (`127.0.0.1:5432/journeyiq_sandbox`) via `RUNTIME_DB_TARGET=sandbox`.
- `/exceptions` displays summary totals from the entire database while capping individual queue tables (quarantined items, late events, duplicate signals, bot sessions) to the top `50` records per section to keep HTML payloads fast and compact.
- Large CSV exports (`840.98 MB` across `21` files in `data-generation/sandbox-output/seed-42-full/csv/`) are gitignored to prevent bloating repository history.
