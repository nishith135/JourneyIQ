# Success Criteria and Acceptance Standards

**Project:** JourneyIQ (working title)  
**Version:** 0.1 — Initial Draft  
**Status:** Under Review  
**Related Document:** `docs/PRD.md`

---

## 1. Purpose

This document defines measurable conditions for determining whether the Customer Journey Analytics Platform satisfies its functional, analytical, data quality, and demonstration requirements.

Every major feature must have an observable result that can be verified through automated tests, database queries, API responses, or the application interface.

A feature is not considered complete merely because its code exists.

It must produce the expected result under valid conditions and handle relevant invalid, duplicate, delayed, or conflicting inputs correctly.

## 2. Acceptance Standard

Each criterion will have one of three statuses:

- **PASS:** The expected behaviour has been demonstrated and verified.
- **FAIL:** The expected behaviour has not been achieved.
- **NOT TESTED:** There is insufficient evidence to determine whether the requirement is satisfied.

A feature must not be marked as passing without verification.

---

## 3. Dataset Generation and Integrity

### 3.1 Dataset Volume

| ID | Success criterion | Target |
|---|---|---|
| DATA-001 | Generate the full synthetic dataset | At least 100,000 sessions |
| DATA-002 | Generate the full event dataset | At least 2,000,000 events |
| DATA-003 | Support a smaller development dataset | Configurable dataset size |
| DATA-004 | Generate all required domain entities | All mandatory entities populated |
| DATA-005 | Preserve valid relationships | 100% valid foreign-key references for records not explicitly quarantined |
| DATA-006 | Preserve unique primary keys | 100% unique primary keys |
| DATA-007 | Use consistent timestamps | All stored timestamps use UTC |
| DATA-008 | Generate reproducible output | Identical output for identical seed and configuration |
| DATA-009 | Produce a validation report | Every generation run produces a report |
| DATA-010 | Document generation configuration | Seed, scale, and generator version recorded |

### 3.2 Data Integrity

The generated and persisted dataset must satisfy:

1. No duplicate primary keys.
2. No unexplained orphan records.
3. No invalid enumerated values.
4. No negative quantities where quantities must be positive.
5. No invalid transaction totals.
6. No invalid lifecycle transitions.
7. No impossible timestamp relationships unless explicitly represented as an anomaly.
8. No unsupported identity associations.
9. No duplicate analytical conversions.
10. No unexplained inconsistencies between transactional records and their derived events.

Quarantined records must be explicitly identified and included in validation reporting rather than silently discarded.

### 3.3 Reproducibility

**Acceptance test:**

1. Generate a dataset using a fixed seed and configuration.
2. Save the resulting files.
3. Generate the dataset again using the same seed and configuration.
4. Compare record counts and deterministic content checksums.
5. Confirm that corresponding outputs are identical.

**Pass condition:** Both runs produce equivalent deterministic outputs.

---

## 4. Anomaly Generation and Handling

The dataset must contain all four mandatory anomaly categories.

| ID | Anomaly | Required behaviour |
|---|---|---|
| ANOM-001 | Late-arriving events | Preserve event and ingestion timestamps; reconstruct using occurrence time |
| ANOM-002 | Incorrect identity merging | Detect and quarantine conflicting or unauthorized associations |
| ANOM-003 | Duplicate conversion signals | Prevent duplicate analytical conversion counting |
| ANOM-004 | Bot traffic mixed with customer behaviour | Identify and classify bot traffic separately |

### Acceptance tests

**ANOM-001 — Late-arriving events**
- Create events whose ingestion order differs from occurrence order.
- Confirm that the raw records preserve both timestamps.
- Confirm that reconstructed sessions and journeys use event timestamps.
- Confirm that recalculation preserves the original raw events.

**ANOM-002 — Incorrect identity merging**
- Create an unauthorized or conflicting anonymous-to-customer association.
- Confirm that it is not accepted as a valid identity link.
- Confirm that the issue is classified and traceable.
- Confirm that customer-level analytics do not use the invalid association.

**ANOM-003 — Duplicate conversion signals**
- Deliver the same purchase signal multiple times.
- Confirm that duplicate deliveries are handled idempotently.
- Confirm that only one valid analytical conversion is counted.

**ANOM-004 — Bot traffic**
- Generate identifiable bot-like activity.
- Confirm that bot traffic is classified.
- Confirm that bot traffic can be excluded from human customer behaviour metrics.
- Preserve the original event records.

**Pass condition:** All four anomaly categories are represented in the dataset and their corresponding handling behaviour is verified.

---

## 5. Sessionization

### 5.1 Inactivity Threshold

The inactivity threshold is 30 minutes.

| ID | Scenario | Expected result |
|---|---|---|
| SES-001 | Events separated by 15 minutes | Same session |
| SES-002 | Events separated by exactly 30 minutes | Same session |
| SES-003 | Events separated by 31 minutes | Different sessions |
| SES-004 | Events separated by multiple hours | Different sessions |
| SES-005 | Same customer using two browser instances | Separate sessions |
| SES-006 | Same customer using web and mobile app | Separate sessions |
| SES-007 | Customer authenticates during a session | Session remains unchanged |
| SES-008 | Events arrive out of ingestion order | Sessionization follows event timestamps |
| SES-009 | Explicit session termination occurs | Session is terminated |
| SES-010 | Activity occurs after explicit termination | A new eligible session begins |
| SES-011 | Event has no reliable client instance | Event is quarantined or marked unresolvable |
| SES-012 | Support event has no session reference | Event remains valid with nullable session association |
| SES-013 | Purchase occurs after session closure | Purchase remains valid; no incorrect session association |
| SES-014 | Events share timestamps | Deterministic tie-breaking is applied |

**Pass condition:** All sessionization tests return the expected results.

### 5.2 Session Integrity

- Every event assigned to a session must satisfy the session's scope rules.
- Web and app sessions must remain separate.
- Session boundaries must be reproducible.
- Raw events must not be modified during recalculation.
- Sessionization version must be recorded.

---

## 6. Identity Resolution and Consent

| ID | Scenario | Expected result |
|---|---|---|
| ID-001 | Anonymous visitor has GRANTED consent | Eligible for authorized identity linking |
| ID-002 | Anonymous visitor has DENIED consent | No identity linking |
| ID-003 | Anonymous visitor has UNKNOWN consent | No identity linking |
| ID-004 | Customer authenticates without analytics consent | Authentication does not automatically authorize linking |
| ID-005 | Same anonymous identity is linked repeatedly to the same customer | Idempotent result |
| ID-006 | Same anonymous identity is linked to conflicting customers | Conflict quarantined |
| ID-007 | Customer uses multiple devices | Separate client instances retained |
| ID-008 | Consent is withdrawn | Relevant identity association is revoked |
| ID-009 | Previously linked activity is recalculated after withdrawal | Excluded from affected customer-level analytics |
| ID-010 | Identity association has an effective date | Historical validity is respected |
| ID-011 | Anonymous visitor has a consent record | Consent can be evaluated without a customer ID |
| ID-012 | Historical identity association is revoked | Original event and audit history remain preserved |

**Pass condition:** No unauthorized identity association contributes to customer-level analytics.

---

## 7. Funnel and Conversion Analytics

### 7.1 Primary Funnel

The five stages are:

1. SESSION_STARTED
2. PRODUCT_VIEWED
3. ADD_TO_CART
4. CHECKOUT_STARTED
5. PURCHASE_COMPLETED

The platform must calculate stage reach and strict sequential progression separately.

### 7.2 Required Calculations

**Stage reach percentage**

\[
\text{Stage Reach} =
\frac{\text{Sessions reaching stage}}{\text{Eligible sessions}}
\times 100
\]

**Drop-off percentage between consecutive stages**

\[
\text{Drop-off} =
\frac{\text{Previous stage count} - \text{Next stage count}}
{\text{Previous stage count}}
\times 100
\]

**Session conversion rate**

\[
\text{Session Conversion Rate} =
\frac{\text{Converted eligible sessions}}
{\text{Eligible sessions}}
\times 100
\]

The denominator and eligibility rules must be documented consistently for every metric.

### 7.3 Acceptance Tests

| ID | Scenario | Expected result |
|---|---|---|
| FN-001 | Session reaches all five stages | Every stage is counted once |
| FN-002 | Session skips ADD_TO_CART but purchases | Purchase is counted; missing cart stage remains missing |
| FN-003 | Session reaches a stage multiple times | Stage reach counted once |
| FN-004 | Duplicate purchase events arrive | One conversion counted |
| FN-005 | Events arrive in ingestion-time order that differs from occurrence order | Funnel follows occurrence order |
| FN-006 | Purchase has no reliable source session | Purchase is UNATTRIBUTED |
| FN-007 | Customer journey includes multiple sessions | Journey-level metrics aggregate eligible sessions |
| FN-008 | Support occurs before purchase and is linked to journey | Support-assisted classification is eligible |
| FN-009 | Support occurs after purchase | Not classified as purchase assistance |
| FN-010 | Identity association is not consent-authorized | Customer-level aggregation excludes the association |

**Pass condition:** Funnel metrics match independently calculated expected values on controlled test datasets.

---

## 8. Cart and Order Lifecycle

### 8.1 Cart

| ID | Scenario | Expected result |
|---|---|---|
| CART-001 | Empty cart enters checkout | Rejected |
| CART-002 | Quantity is zero or negative | Rejected |
| CART-003 | Cart has 24 hours of qualifying inactivity without purchase | Marked ABANDONED |
| CART-004 | Payment fails | Cart is not immediately abandoned |
| CART-005 | Cart has a completed purchase | Marked CONVERTED |
| CART-006 | Converted cart is converted again | Rejected or treated idempotently |
| CART-007 | Product price changes after cart item creation | Historical price snapshot preserved |
| CART-008 | Cart item quantity changes | Change is recorded |
| CART-009 | Cart lifecycle transition occurs | Lifecycle history is preserved |
| CART-010 | Customer starts a new shopping journey after terminal cart state | New cart can be created |

### 8.2 Order and Payment

| ID | Scenario | Expected result |
|---|---|---|
| ORD-001 | Order has no valid items | Rejected |
| ORD-002 | Order has multiple payment attempts | All attempts reference the same order |
| ORD-003 | Payment attempt fails | Attempt remains in history |
| ORD-004 | Later payment attempt succeeds | Order can become CONFIRMED after authoritative confirmation |
| ORD-005 | Duplicate purchase confirmation arrives | No duplicate conversion |
| ORD-006 | Order total is inconsistent with its items and adjustments | Validation fails |
| ORD-007 | Product price changes after purchase | Order-item price snapshot remains unchanged |
| ORD-008 | Order has no reliable session association | Order remains valid; attribution is UNATTRIBUTED |
| ORD-009 | Payment attempt has an invalid amount | Rejected |
| ORD-010 | Confirmed order receives duplicate confirmation | No duplicate state transition |
| ORD-011 | Multiple attempts occur | Attempt numbering remains unique within the order |
| ORD-012 | Order is refunded | Refund state and history are preserved |

**Pass condition:** Cart, order and payment state transitions are valid, traceable, and consistent with the authoritative purchase records.

---

## 9. Support Ticket Lifecycle

| ID | Scenario | Expected result |
|---|---|---|
| SUP-001 | Valid ticket status transition occurs | Accepted and recorded |
| SUP-002 | Invalid status transition occurs | Rejected |
| SUP-003 | Ticket is reopened | Previous resolution history remains |
| SUP-004 | Ticket has no session association | Ticket remains valid |
| SUP-005 | Support occurs after purchase | Not classified as purchase assistance |

Additional requirements:

- Every status transition must produce a lifecycle history record.
- Support attribution requires a reliable journey association.
- Support must occur before the relevant purchase.
- Support association must not be presented as proof of causality.

---

## 10. Experiment Analytics

| ID | Scenario | Expected result |
|---|---|---|
| EXP-001 | Experiment is created | Starts in DRAFT |
| EXP-002 | Experiment has invalid allocation percentages | Rejected |
| EXP-003 | Experiment has no control variant | Rejected |
| EXP-004 | Assignment is repeated for the same unit | Same variant returned |
| EXP-005 | Variant is assigned but not served | No exposure recorded |
| EXP-006 | Variant is actually served | Exposure recorded |
| EXP-007 | Experiment is PAUSED | New assignments and exposures stopped |
| EXP-008 | Experiment configuration changes | Historical assignments retain original version |
| EXP-009 | Duplicate exposure event arrives | Exposure is not counted twice |
| EXP-010 | Identity resolution is not consent-authorized | No unauthorized customer-level assignment |

**Pass condition:** Assignment and exposure counts are correct, deterministic, and independently verifiable.

Experiment reporting must distinguish observed conversion differences from proven causal effects.

---

## 11. JourneyOutcome

| ID | Scenario | Expected result |
|---|---|---|
| OUT-001 | Session outcome is calculated | Session scope is used |
| OUT-002 | Customer journey outcome is calculated | Customer journey scope is used |
| OUT-003 | Valid completed purchase occurs within the window | CONVERTED |
| OUT-004 | Duplicate purchase delivery occurs | One conversion |
| OUT-005 | Purchase occurs within seven days of first eligible touch | Eligible for journey attribution |
| OUT-006 | Purchase occurs after the seven-day window | Not attributed to the earlier journey |
| OUT-007 | Journey window remains open | IN_PROGRESS |
| OUT-008 | Window ends without valid purchase | NON_CONVERTED |
| OUT-009 | Cart is abandoned | Cart abandonment remains distinct from journey non-conversion |
| OUT-010 | Journey converts after payment friction | CONVERTED with friction flags |
| OUT-011 | Consent is withdrawn | Customer-level outcome recalculated according to consent rules |
| OUT-012 | Outcome is recalculated | Previous outcome version preserved |
| OUT-013 | Same inputs and calculation version are used | Identical outcome produced |
| OUT-014 | Journey is excluded | Explicit exclusion reason recorded |

**Pass condition:** Every outcome is reproducible, traceable, and consistent with the documented business rules.

---

## 12. Backend and API Acceptance

The backend must expose the functionality required by the frontend and analytical workflow.

| ID | Success criterion |
|---|---|
| API-001 | All required endpoints respond using documented contracts |
| API-002 | Invalid requests return appropriate HTTP error responses |
| API-003 | Request validation prevents invalid domain state |
| API-004 | Database constraints enforce critical integrity rules |
| API-005 | Duplicate-sensitive operations are idempotent |
| API-006 | Business logic is not dependent on frontend validation |
| API-007 | Errors are understandable and do not expose secrets |
| API-008 | API responses use consistent structures |
| API-009 | Analytics endpoints return independently verifiable metrics |
| API-010 | Critical backend functionality has automated tests |

---

## 13. Frontend Acceptance

| ID | Success criterion |
|---|---|
| UI-001 | Overview dashboard displays key business metrics |
| UI-002 | Funnel dashboard displays stage reach and drop-off |
| UI-003 | Journey investigation displays a traceable event timeline |
| UI-004 | Exception view displays relevant anomalies |
| UI-005 | Experiment dashboard distinguishes assignments and exposures |
| UI-006 | Loading, empty, and error states are handled |
| UI-007 | Displayed metrics agree with backend responses |
| UI-008 | Navigation between core analytical views works |
| UI-009 | Dashboard remains usable with the full generated dataset |

---

## 14. Security and Privacy Acceptance

| ID | Success criterion |
|---|---|
| SEC-001 | No credentials or secrets are committed to Git |
| SEC-002 | Environment configuration is externalized |
| SEC-003 | Backend validates all relevant inputs |
| SEC-004 | Database access follows least privilege |
| SEC-005 | Consent rules are enforced by backend logic |
| SEC-006 | Unauthorized identity links cannot enter customer-level analytics |
| SEC-007 | Error responses do not expose sensitive implementation details |
| SEC-008 | Audit records preserve important state changes |

---

## 15. Performance and Execution Targets

The platform must run on the hackathon development environment and process the full synthetic dataset without exhausting available memory or corrupting data.

**Proposed targets for review:**

| ID | Measurement | Proposed target |
|---|---|---|
| PERF-001 | Full dataset generation | Complete within 10 minutes |
| PERF-002 | Full dataset processing | Complete within 10 minutes |
| PERF-003 | Standard analytical API response | Under 2 seconds for representative dashboard queries |
| PERF-004 | Memory usage | No out-of-memory failure during full-scale generation and processing |
| PERF-005 | Frontend loading | Core dashboard usable without waiting for all raw events to load into the browser |

These are provisional engineering targets, not yet approved guarantees. Measurements must be performed on the actual development machine and recorded.

---

## 16. Testing and Verification Requirements

The project must include:

### Unit Tests
- Sessionization rules.
- Identity resolution.
- Consent enforcement.
- Funnel calculations.
- Conversion deduplication.
- Cart and order transitions.
- Payment retry behaviour.
- Support attribution.
- Experiment assignment.
- Journey outcome calculation.

### Integration Tests
- Database relationships.
- API and database interaction.
- Event ingestion and persistence.
- End-to-end analytical calculations.
- Transactional consistency.

### Data Validation Tests
- Primary key uniqueness.
- Foreign key integrity.
- Lifecycle consistency.
- Timestamp ordering.
- Reproducibility.
- Anomaly classification.

### End-to-End Tests
Demonstrate at least one complete customer journey:

1. Customer discovers a product.
2. Customer views the product.
3. Customer adds the product to a cart.
4. Customer starts checkout.
5. First payment attempt fails.
6. Customer contacts linked support.
7. Customer retries payment.
8. Purchase is successfully completed.
9. Journey is classified as CONVERTED.
10. Payment and support friction are visible in the analytical interface.

---

## 17. Final Hackathon Demonstration Criteria

The final demonstration must establish that the platform can:

1. Generate the required synthetic dataset.
2. Validate data integrity.
3. Identify and classify the four mandatory anomaly categories.
4. Reconstruct sessions and customer journeys.
5. Enforce consent-aware identity resolution.
6. Calculate funnel and conversion metrics.
7. Identify cart abandonment and payment friction.
8. Display linked support interactions.
9. Track experiment assignment and exposure.
10. Produce explainable, versioned journey outcomes.
11. Trace important analytical results back to source records.

### Final Completion Rule

A core requirement is complete only when:

- Its expected behaviour is documented.
- Its implementation exists.
- Its relevant tests pass.
- Its output can be independently verified.
- Any known limitations are recorded.

A successful visual demonstration alone is not sufficient evidence of analytical correctness.

---

## 18. Open Decisions

The following targets and definitions require final review:

1. Whether the proposed 10-minute generation and processing targets are achievable on the development machine.
2. Whether the proposed two-second dashboard API target is appropriate for the local demonstration.
3. Minimum number of injected records for each anomaly category.
4. Exact bot classification rules and acceptable false-positive behaviour.
5. Final API and frontend acceptance scope within the eight-hour time budget.

**End of Success Criteria v0.1**