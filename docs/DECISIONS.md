# JourneyIQ — Architecture & Product Decisions

**Document Version:** 0.2 (Revision 3)  
**Status:** Authoritative Decision Register  
**Historical Project Context:** Originated as an 8-hour solo hackathon project targeting a full-stack Customer Journey Analytics Platform (`FastAPI` + `Jinja2` + `HTMX`, `PostgreSQL` + `SQLAlchemy` + `Alembic`, modular monolith, backend-first development).  
**Canonical Domain Reference vs. MVP Implementation Scope:** The 24-entity canonical model (`docs/DATA_DICTIONARY.md`) is the complete domain reference; it does **not** require all 24 entities, their CRUD operations, APIs, or associated workflows to be implemented in the initial MVP. The MVP must implement what is necessary to complete every mandatory problem-statement requirement, implementing only the entities and relationships needed to support those mandatory workflows initially while allowing other canonical entities to remain documented and deferred—provided that deferring an entity does not violate an approved business rule or mandatory problem-statement requirement. Full-scale validation (100,000 sessions / 2,000,000 events) remains a separate objective under `DEC-013`.

This document is the single authoritative register for JourneyIQ's architectural, product, and data-model decisions and their approval statuses. Where other project documents contain outdated or conflicting status labels, those documents must be synchronized to match this approved register.

Decisions must not be silently changed or promoted by an AI agent.

---

## 1. Approved Decisions (DEC-001–013)

### DEC-001: Backend Framework
- **Status:** Approved
- **Decision:** Use FastAPI with Python.
- **Reason:** Python familiarity, API development support, and integration with data-processing libraries.

### DEC-002: Frontend Architecture
- **Status:** Approved
- **Decision:** Use Jinja2 templates with HTMX.
- **Reason:** Avoid unnecessary React and JavaScript complexity while retaining dynamic interactions.

### DEC-003: Database
- **Status:** Approved
- **Decision:** Use PostgreSQL with SQLAlchemy and Alembic.
- **Reason:** Relational integrity, structured data modelling, and database migration support.

### DEC-004: Application Architecture
- **Status:** Approved
- **Decision:** Use a modular monolithic architecture.
- **Reason:** Maintain logical separation without introducing unnecessary distributed-system complexity.

### DEC-005: Development Strategy
- **Status:** Approved
- **Decision:** Follow backend-first development.
- **Reason:** Establish and test stable APIs before connecting the frontend.

### DEC-006: Data Generation
- **Status:** Approved
- **Decision:** Use reproducible synthetic data with a fixed random seed.
- **Reason:** Control data quality, generate mandatory anomalies, and reproduce results.

### DEC-007: Sessionization
- **Status:** Approved
- **Decision:** Use a 30-minute inactivity threshold based on event occurrence timestamps.
- **Reason:** Reconstruct sessions consistently using actual user activity rather than ingestion delays.

### DEC-008: Identity Resolution
- **Status:** Approved
- **Decision:** Anonymous-to-known identity stitching requires granted, applicable consent.
- **Reason:** Prevent unauthorized identity association and preserve privacy.

### DEC-009: Journey Attribution
- **Status:** Approved
- **Decision:** Use a seven-day attribution window from the first eligible touch.
- **Reason:** Establish a bounded and consistent customer journey attribution period.

### DEC-010: Purchase Authority
- **Status:** Approved
- **Decision:** The purchase system is authoritative for completed purchases.
- **Reason:** Prevent duplicate or fabricated conversion counting.

### DEC-011: Event History
- **Status:** Approved
- **Decision:** Preserve raw events and append-only audit history.
- **Reason:** Maintain traceability and support historical investigation.

### DEC-012: Experiment Assignment
- **Status:** Approved
- **Decision:** Use deterministic, consent-aware experiment assignment.
- **Reason:** Keep assignments reproducible and avoid unauthorized identity merging.

### DEC-013: MVP Execution and Validation Strategy
- **Status:** Approved
- **Decision:** Complete every mandatory requirement of the hackathon problem statement through a working, demonstrable end-to-end MVP first—where a smaller deterministic dataset and simplified implementation are acceptable provided they preserve required behaviour and correctness—while retaining the 100,000-session and 2,000,000-event target as a separate full-scale validation objective. This decision does not require every optional feature or all 24 canonical entities to be fully implemented in the MVP.
- **Reason:** Demonstrate complete, verifiable analytical functionality end-to-end before undertaking separate full-scale performance validation.

---

## 2. Approved Data Model Decisions (DD-001–008)

All eight data-model decisions from `docs/DATA_DICTIONARY.md` Section 8 are approved for the 24-entity canonical domain reference model. Approval of a canonical data-model decision does not mandate implementing every canonical entity in the initial MVP (provided mandatory problem-statement requirements and approved business rules are preserved), nor does it automatically resolve downstream algorithmic or implementation details noted below.

### DD-001: Primary Keys and Business Identifiers
- **Status:** Approved
- **Decision:** Every core entity uses an internal UUID primary key for all relational foreign keys and a unique, immutable, human-readable business identifier (`<entity>_business_id`) for display, logs, exports, and demonstrations.
- **Unresolved Implementation Detail:** Exact business-ID formatting patterns across implemented entities (`docs/DATA_DICTIONARY.md` Section 9.1).

### DD-002: Cart Item History
- **Status:** Approved
- **Decision:** Represent cart-item additions, quantity changes, and removals via an append-only `CartItemEvent` entity alongside current-state `CartItem` in the canonical domain model.
- **Unresolved Implementation Detail:** Single convention for `REMOVE` events (`new_quantity = 0` vs. `NULL`, `docs/DATA_DICTIONARY.md` Section 9.3) when `CartItemEvent` is implemented.

### DD-003: Journey Membership
- **Status:** Approved
- **Decision:** Associate sessions to `CustomerJourney` via a direct nullable `journey_id` UUID foreign key on `Session` rather than a bridge table.
- **Unresolved Implementation Detail:** `DD-003` defines the current-state foreign-key relationship on `Session`; it does not by itself specify how historical journey membership and reassignment traceability are preserved when late-arriving events or consent withdrawals change a session's journey association.

### DD-004: Refund History
- **Status:** Approved
- **Decision:** Represent refund requests and outcomes via a dedicated `Refund` entity linked to `Order` in the canonical domain model.

### DD-005: Bot Classification Storage
- **Status:** Approved
- **Decision:** Store bot classification as derived metadata on `Session` (`traffic_classification`, `classification_reason`, `classification_version`, `classified_at`), keeping raw `JourneyEvent` records immutable.
- **Unresolved Implementation Detail:** `DD-005` approves the storage location for derived session-level bot metadata; it does not define the bot detection algorithm, classification rules/thresholds (`docs/SUCCESS_CRITERIA.md` Section 18.4), or whether event-level bot labels will ever be required (`docs/DATA_DICTIONARY.md` Section 9.6).

### DD-006: Quarantine and Generation Metadata
- **Status:** Approved
- **Decision:** Persist quarantined events in PostgreSQL via `EventQuarantine`; store generation-run metadata and anomaly ground-truth labels in generator output files initially rather than relational database tables.

### DD-007: Experiment Identity Evidence
- **Status:** Approved
- **Decision:** Preserve `assignment_identity_type`, `assignment_identity_id`, optional `identity_link_id`, optional `consent_record_id`, and `identity_version` on `ExperimentAssignment` (consolidating earlier `assignment_unit_*` and `assignment_identity_*` field proposals).
- **Unresolved Implementation Detail:** Exact representation of customer identity in `assignment_identity_id` relative to `DD-001` internal UUID conventions.

### DD-008: Purchase Idempotency
- **Status:** Approved
- **Decision:** Preserve duplicate raw deliveries in `JourneyEvent` for auditability while enforcing purchase idempotency on authoritative `Order` records via `(purchase_source, source_purchase_reference)`.
- **Unresolved Implementation Detail:** Database/service enforcement when source purchase references are missing or when multiple orders reference the same cart (`BR-ORD-011`).

---

## 3. Proposed Decisions

The following are proposed technical and architectural choices gathered from `docs/DECISIONS.md` and `docs/ARCHITECTURE.md`. They are **not approved** and require explicit approval before being treated as binding.

### DEC-P01: Synchronous SQLAlchemy Database Access
- **Status:** Proposed
- **Description:** Use synchronous SQLAlchemy initially for database session management, queries, and template rendering to reduce implementation complexity compared to asynchronous ORM management.

### DEC-P02: Local PostgreSQL Development Runtime
- **Status:** Proposed
- **Description:** Run PostgreSQL locally during development rather than introducing containerized database infrastructure, unless a containerized setup is explicitly selected.

### DEC-P03: Chart.js Visualization Library
- **Status:** Proposed
- **Description:** Use Chart.js in server-rendered templates to visualize backend-computed funnel, trend, channel, and experiment metrics.

### DEC-P04: Tailwind CSS Styling
- **Status:** Proposed
- **Description:** Use Tailwind CSS to style Jinja2 dashboard templates and reusable UI components with a consistent layout and information hierarchy.

### DEC-P05: Simple Local Authentication (If Required)
- **Status:** Proposed
- **Description:** Use a lightweight local authentication mechanism if user authentication is required by the final scope resolution of `OPEN-001`.

### DEC-P06: Independent Synthetic Data Generator
- **Status:** Proposed *(from `docs/ARCHITECTURE.md` `ADR-010`)*
- **Description:** Keep the Python synthetic data generator logically separate from the FastAPI runtime so dataset generation and validation remain decoupled from application request serving.

### DEC-P07: Deterministic, Version-Aware Analytical Processing Pipeline
- **Status:** Proposed *(from `docs/ARCHITECTURE.md` `ADR-011`)*
- **Description:** Execute analytical stages (validation, quarantine, deduplication, sessionization, identity resolution, journey reconstruction, funnels, outcomes) deterministically while recording rule/calculation versions. Specific execution mechanisms (such as bulk `COPY` loading, set-based SQL queries, or pre-aggregated summary tables) remain proposed and subject to `OPEN-002` and `OPEN-003`.

### DEC-P08: Optional Python Data-Analysis Libraries (Pandas / NumPy)
- **Status:** Proposed *(from `docs/ARCHITECTURE.md` Section 2)*
- **Description:** Permit optional use of Pandas and NumPy for synthetic data generation, validation, or analytical computations where helpful, without treating them as mandatory dependencies where standard Python or SQL suffices.

### DEC-P09: Pytest Testing Framework
- **Status:** Proposed *(from `docs/ARCHITECTURE.md` Section 2)*
- **Description:** Use Pytest for unit, integration, data-validation, and API test suites.

---

## 4. Open Decisions

The following unresolved decisions require explicit resolution before making the specific implementation choices they govern. Duplicate open items that restate `DEC-P01`–`DEC-P04` have been omitted.

- **OPEN-001: Authentication and authorization.** Define whether analyst login/authentication or role-based access control is required for the MVP (and whether `DEC-P05` is needed), while ensuring consent and validation rules are enforced server-side.
- **OPEN-002: Persisted vs. on-demand analytical results.** Decide which derived analytical outputs and dashboard aggregates are persisted (e.g., in derived/summary tables or views) versus computed on demand.
- **OPEN-003: Two-million-event processing strategy.** Define the ingestion, indexing, bulk-loading, and query/processing strategy needed for separate full-scale validation of 100,000 sessions and 2,000,000 events (`DEC-013`).
- **OPEN-004: Performance thresholds.** Confirm the final acceptance targets for dataset generation, full-scale processing, dashboard API response latency, and memory usage (`docs/SUCCESS_CRITERIA.md` `PERF-001`–`PERF-005`).
- **OPEN-005: Anomaly sample sizes.** Specify the target counts or proportions for the four mandatory anomaly categories (`ANOM-001`–`ANOM-004`) in both the smaller deterministic MVP dataset and the full-scale validation dataset (`DEC-013`).
- **OPEN-006: Exact MVP feature/workflow deferrals.** Define which non-essential canonical entities, CRUD operations, APIs, and associated workflows from the 24-entity canonical domain reference model remain documented and deferred in the initial MVP—ensuring that every mandatory problem-statement requirement is completed, only the entities and relationships needed to support those mandatory workflows are required initially, no approved business rule or mandatory requirement is violated by a deferral, and separate full-scale validation remains preserved under `DEC-013`.

---

## 5. Legacy Traceability

This table maps legacy identifiers from `docs/ARCHITECTURE.md` (Sections 15 and 16) and `docs/DATA_DICTIONARY.md` (Section 8) to their canonical identifiers in this register.

| Legacy Identifier | Source Document | Legacy Status Label | Canonical ID | Reconciled Status |
| :--- | :--- | :--- | :--- | :--- |
| `ADR-001` | `docs/ARCHITECTURE.md` Sec. 15 | `Proposed` | `DEC-004` | `Approved` |
| `ADR-002` | `docs/ARCHITECTURE.md` Sec. 15 | `Intended` | `DEC-001` | `Approved` |
| `ADR-003` | `docs/ARCHITECTURE.md` Sec. 15 | `Confirmed` | `DEC-002` | `Approved` |
| `ADR-004` | `docs/ARCHITECTURE.md` Sec. 15 | `Proposed` | `DEC-002` | `Approved` |
| `ADR-005` | `docs/ARCHITECTURE.md` Sec. 15 | `Proposed` | `DEC-P04` | `Proposed` |
| `ADR-006` | `docs/ARCHITECTURE.md` Sec. 15 | `Proposed` | `DEC-P03` | `Proposed` |
| `ADR-007` | `docs/ARCHITECTURE.md` Sec. 15 | `Intended` | `DEC-003` | `Approved` |
| `ADR-008` | `docs/ARCHITECTURE.md` Sec. 15 | `Intended` | `DEC-003` | `Approved` |
| `ADR-009` | `docs/ARCHITECTURE.md` Sec. 15 | `Intended` | `DEC-003` | `Approved` |
| `ADR-010` | `docs/ARCHITECTURE.md` Sec. 15 | `Intended` | `DEC-P06` | `Proposed` |
| `ADR-011` | `docs/ARCHITECTURE.md` Sec. 15 | `Intended` | `DEC-P07` | `Proposed` |
| `ADR-012` | `docs/ARCHITECTURE.md` Sec. 15 | `Intended` | `DEC-005` | `Approved` |
| `ADR-013` | `docs/ARCHITECTURE.md` Sec. 15 | `Confirmed` | `DD-001` | `Approved` |
| `ADR-014` | `docs/ARCHITECTURE.md` Sec. 15 | `Confirmed` | `DEC-011` | `Approved` |
| `OPEN-001` (HTMX) | `docs/ARCHITECTURE.md` Sec. 16 | `Open` | `DEC-002` | Superseded (`Approved` by `DEC-002`) |
| `OPEN-002` (Tailwind) | `docs/ARCHITECTURE.md` Sec. 16 | `Open` | `DEC-P04` | Consolidated into `DEC-P04` (`Proposed`) |
| `OPEN-003` (Chart.js) | `docs/ARCHITECTURE.md` Sec. 16 | `Open` | `DEC-P03` | Consolidated into `DEC-P03` (`Proposed`) |
| `OPEN-004` (Sync/Async ORM) | `docs/ARCHITECTURE.md` Sec. 16 | `Open` | `DEC-P01` | Consolidated into `DEC-P01` (`Proposed`) |
| `OPEN-005` (Local/Docker DB) | `docs/ARCHITECTURE.md` Sec. 16 | `Open` | `DEC-P02` | Consolidated into `DEC-P02` (`Proposed`) |
| `OPEN-006` (Persist vs. On-Demand) | `docs/ARCHITECTURE.md` Sec. 16 | `Open` | `OPEN-002` | `Open` |
| `OPEN-007` (2M Event Strategy) | `docs/ARCHITECTURE.md` Sec. 16 | `Open` | `OPEN-003` | `Open` |
| `OPEN-008` (Auth Scope) | `docs/ARCHITECTURE.md` Sec. 16 | `Open` | `OPEN-001` | `Open` |
| `OPEN-009` (Perf Thresholds) | `docs/ARCHITECTURE.md` Sec. 16 | `Open` | `OPEN-004` | `Open` |
| `OPEN-010` (Anomaly Volumes) | `docs/ARCHITECTURE.md` Sec. 16 | `Open` | `OPEN-005` | `Open` |
| `DD-001`–`DD-008` | `docs/DATA_DICTIONARY.md` Sec. 8 | `Approved` | `DD-001`–`DD-008` | `Approved` |

---

## 6. Decision Management Rules

1. `docs/DECISIONS.md` serves as the single authoritative register for decision approval status across the project.
2. Only explicitly `Approved` decisions (`DEC-001`–`DEC-013`, `DD-001`–`DD-008`) may be treated as binding.
3. `Proposed` decisions (`DEC-P01`–`DEC-P09`) must not be implemented as approved requirements without explicit user approval.
4. An `Open` decision (`OPEN-001`–`OPEN-006`) must be resolved before making the specific implementation choice it governs. However:
   - Unrelated implementation must not be blocked by an open decision.
   - Decisions affecting deferred or optional features may remain open.
   - An agent must explicitly identify when a decision is being deferred and why.
5. Conflicting status labels in other documents do not reopen approved decisions in this register; those documents must be synchronized to match `docs/DECISIONS.md`.
6. Changing an approved decision requires explicit user approval.
7. When a decision is added, promoted, or modified, update this document first and synchronize affected specifications.
8. Every significant implementation decision must reference its canonical `DEC-*`, `DD-*`, `DEC-P*`, or `OPEN-*` identifier.
9. AI agents must distinguish documented facts, approved decisions, unresolved implementation details, and their own recommendations.

---

## 7. Documents Requiring Synchronization

During subsequent documentation reconciliation phases, the following sections must be updated to align with this register:

1. **`docs/ARCHITECTURE.md`:**
   - **Section 2 (Technology Stack table, lines 48–63):** Align status labels (`Intended`, `Confirmed`, `Proposed`) with `DEC-001`–`DEC-006`, `DEC-P03`, `DEC-P04`, `DEC-P08`, and `DEC-P09`.
   - **Section 4.2, Section 5.1, & Section 7.2:** Clarify that the 24-entity canonical model and proposed route/module inventory are the complete domain reference, while initial MVP implementation covers only the entities, relationships, and endpoints required to satisfy mandatory problem-statement workflows (`DEC-013`, `OPEN-006`).
   - **Section 15 (Architectural Decisions, lines 970–990):** Replace outdated `Proposed` / `Intended` / `Confirmed` statuses on `ADR-001`–`ADR-014` with references to `DEC-001`–`DEC-005`, `DEC-011`, `DD-001`, and `DEC-P03`–`DEC-P07` as mapped in Section 5 above.
   - **Section 16 (Open Architectural Decisions, lines 994–1011):** Remove colliding `OPEN-001`–`OPEN-010` identifiers (`ARCH OPEN-001` is resolved by `DEC-002`; `ARCH OPEN-002`–`005` map to `DEC-P01`–`DEC-P04`; `ARCH OPEN-006`–`010` map to `OPEN-001`–`OPEN-005`).
2. **`docs/DATA_DICTIONARY.md`:**
   - **Section 2 (Core Entity Inventory) & Section 8 (`DD-001`–`DD-008`, lines 811–829):** Cross-reference `docs/DECISIONS.md` Section 2 (`DD-001`–`DD-008`), note the distinction between the 24-entity canonical reference model and initial MVP implementation scope (`DEC-013`, `OPEN-006`), and track unresolved implementation details (such as historical journey membership tracking under `DD-003`, bot detection rules under `DD-005`, `ExperimentVariant` version uniqueness, and `Order` purchase idempotency constraints under `DD-008`).
3. **`docs/PRD.md`:**
   - **Section 6 (Core Data Entities, lines 362–389):** Align the entity inventory with the 24-entity canonical domain reference (`CartItemEvent`, `Refund`, `EventQuarantine` per `DD-002`, `DD-004`, `DD-006`) while noting MVP implementation scoping under `DEC-013` and `OPEN-006`.
   - **Section 7.1 (Synthetic Dataset, lines 394–410) & Section 13 (Document Status, line 623):** Reference `DEC-013` (working end-to-end MVP with smaller deterministic dataset + separate full-scale validation) and `docs/DECISIONS.md`.
4. **`docs/SUCCESS_CRITERIA.md`:**
   - **Section 3.1 (Dataset Volume, lines 34–48), Section 15 (Performance Targets, lines 390–405), & Section 18 (Open Decisions, lines 485–494):** Align MVP vs. separate full-scale dataset validation with `DEC-013` and cross-reference `OPEN-004`, `OPEN-005`, and `OPEN-006`.
5. **`docs/BUSINESS_RULES.md`:**
   - **Section 14 (Rule Governance, line 630):** Reference `docs/DECISIONS.md` for tracking proposed and open decisions.
