# Business Rules and Domain Invariants

**Project:** JourneyIQ (working title)  
**Version:** 0.1 — Initial Draft  
**Status:** Under Review  
**Related Documents:** `PRD.md`, `SUCCESS_CRITERIA.md`

---

## 1. Purpose

This document defines the authoritative business rules governing customer identity, event processing, sessionization, customer journey reconstruction, transactions, funnel analytics, experiments, and journey outcomes.

These rules must be enforced consistently across data generation, backend processing, database constraints, and analytical calculations.

No component should independently implement a conflicting interpretation of a business rule.

---

# 2. Event Management Rules

### BR-EVT-001 — Immutable Raw Events
Once an event is ingested, its original event data must not be silently overwritten.

Corrections and derived interpretations must be represented separately.

### BR-EVT-002 — Event and Ingestion Timestamps
Every event must distinguish:
- `event_timestamp`: when the activity actually occurred.
- `ingestion_timestamp`: when the platform received the event.

Both timestamps must be stored in UTC.

### BR-EVT-003 — Event Ordering
Analytical processing must order events by:
1. `event_timestamp`
2. `source_sequence`, when available
3. Stable event identifier as deterministic tie-breaker.

Ingestion order must not be treated as event occurrence order.

### BR-EVT-004 — Duplicate Event Handling
Raw duplicate deliveries may be preserved for traceability.

However, analytical processing must use stable deduplication or idempotency rules to prevent duplicated business outcomes.

### BR-EVT-005 — Event Taxonomy
Event type describes the activity, while channel describes its origin.

Supported channels:
- `WEB`
- `APP`
- `SUPPORT`
- `PURCHASE`
- `SYSTEM`

`SYSTEM` represents internal system-generated activity rather than a customer-facing channel.

### BR-EVT-006 — Authoritative Purchase Events
The purchase system is the authoritative source for completed purchases.

A `PURCHASE_COMPLETED` event must reference the relevant transaction or authoritative business event.

An order being created does not, by itself, establish a completed purchase.

---

# 3. Identity and Consent Rules

### BR-ID-001 — Identity Separation
The system must distinguish three identifiers:

| Identifier | Meaning |
|---|---|
| `client_instance_id` | Browser or application installation |
| `anonymous_id` | Anonymous visitor |
| `customer_id` | Known customer |

These identifiers must not be treated as interchangeable.

### BR-ID-002 — Consent Requirement
Anonymous-to-known identity stitching is permitted only when the relevant analytics consent is `GRANTED`.

`DENIED` and `UNKNOWN` do not permit identity linking.

### BR-ID-003 — Authentication Is Not Consent
Authentication alone does not authorize analytics identity stitching.

### BR-ID-004 — Multiple Devices
Different browser or application instances remain separate client instances.

Customer-level aggregation is permitted only through a valid, consent-authorized identity relationship.

### BR-ID-005 — Identity Conflict
If an anonymous identity is associated with conflicting customer identities, the conflict must be quarantined.

The platform must not silently choose the latest or most convenient mapping.

### BR-ID-006 — Idempotent Identity Linking
Repeating an identical valid identity-link request must not create duplicate active relationships.

### BR-ID-007 — Effective-Dated Identity Relationships
Identity associations must preserve their validity periods.

Historical associations must remain traceable.

### BR-ID-008 — Consent Withdrawal
When relevant consent is withdrawn:
- The applicable identity association must be revoked.
- Affected customer-level analytics must be recalculated.
- Previously linked anonymous activity must no longer contribute to customer-level analytics where the withdrawal requires exclusion.
- Original events and audit history must remain preserved, subject to applicable retention and deletion requirements.

### BR-ID-009 — Consent Subject
Consent records may belong to an anonymous identity or a known customer.

The record must identify the applicable subject unambiguously.

---

# 4. Sessionization Rules

### BR-SES-001 — Session Inactivity Threshold
The session inactivity threshold is 30 minutes.

### BR-SES-002 — Session Continuation
An inactivity gap of 30 minutes or less belongs to the same session, provided no explicit session termination has occurred.

### BR-SES-003 — New Session
An inactivity gap greater than 30 minutes starts a new session.

### BR-SES-004 — Session Scope
A session is scoped by:
- Channel.
- `client_instance_id`.

Web and application activity must not be merged into one session merely because the same customer is involved.

### BR-SES-005 — Authentication During a Session
Customer authentication does not automatically create a new session.

### BR-SES-006 — Explicit Session Termination
An explicit `SESSION_ENDED` event terminates the session.

Subsequent eligible activity must start a new session.

### BR-SES-007 — Missing Client Instance
An event without a reliable client instance identifier must not be assigned to a guessed session.

It must be quarantined or marked unresolvable according to its source and event type.

### BR-SES-008 — Late-Arriving Events
Sessionization uses event occurrence timestamps.

Late-arriving events may change derived session boundaries.

The raw event records remain immutable.

### BR-SES-009 — Recalculation
Session assignments must be reproducible from the same raw event history and sessionization rule version.

The calculation version must be recorded.

### BR-SES-010 — Logical Session End
When a session ends because of inactivity, its logical end time is:

`last_event_at + 30 minutes`

When an explicit termination event exists, the termination event timestamp determines the end time.

### BR-SES-011 — Unassociated Support and Purchase Events
Support and purchase events may exist without session associations.

A session association must be established through a reliable explicit reference or documented derivation rule.

The platform must never guess the most recent session.

### BR-SES-012 — Deterministic Event Ties
Events sharing timestamps must be ordered using source sequence where available and a stable event identifier otherwise.

---

# 5. Customer Journey Rules

### BR-JRN-001 — Journey Scope
A customer journey may contain multiple sessions.

A journey may also exist for an anonymous visitor without a known customer identity.

### BR-JRN-002 — Consent-Aware Aggregation
Customer-level journeys must respect approved identity associations and consent restrictions.

### BR-JRN-003 — Journey Attribution Window
The journey attribution window is seven days from the first eligible journey touch.

### BR-JRN-004 — Attribution Window
The seven-day window is a maximum duration measured from the journey's first eligible touch, not an indefinitely sliding window.

### BR-JRN-005 — Purchases Outside the Window
A purchase after the attribution window remains a valid purchase.

It is not automatically attributed to the earlier journey.

### BR-JRN-006 — Reliable Association
Purchases and support interactions must be associated with a journey only through reliable references or documented identity relationships.

No speculative association is permitted.

### BR-JRN-007 — Anonymous Journeys
Anonymous journeys must remain analytically distinct unless valid consent-authorized identity resolution permits association.

---

# 6. Funnel and Conversion Rules

### BR-FUN-001 — Primary Funnel Stages

The primary funnel consists of:

1. `SESSION_STARTED`
2. `PRODUCT_VIEWED`
3. `ADD_TO_CART`
4. `CHECKOUT_STARTED`
5. `PURCHASE_COMPLETED`

### BR-FUN-002 — Stage Reach
A session counts at most once per funnel stage.

Repeated events at the same stage do not increase the number of sessions reaching that stage.

### BR-FUN-003 — Strict Sequential Progression
Stage reach and strict sequential progression must be calculated separately.

Stage reach measures whether a stage was reached.

Strict sequential progression measures whether the required preceding stages were completed in order.

### BR-FUN-004 — Skipped Stages
A purchase without a preceding cart event is still a valid purchase.

The system must not manufacture missing stages.

### BR-FUN-005 — Conversion Authority
Only an authoritative completed purchase qualifies as a purchase conversion.

### BR-FUN-006 — Duplicate Conversion Prevention
Duplicate deliveries or signals for the same completed purchase must not produce duplicate analytical conversions.

### BR-FUN-007 — Session Conversion
Session conversion must be calculated from eligible session activity and reliable purchase associations.

### BR-FUN-008 — Customer Journey Conversion
Customer journey conversion must respect identity consent and the seven-day attribution window.

### BR-FUN-009 — Unattributed Purchases
A valid purchase without a reliable journey or session association remains `UNATTRIBUTED`.

### BR-FUN-010 — Attribution Dimensions
Where evidence is available, preserve:
- Acquisition channel.
- Conversion channel.
- Assisted channel.
- Friction channel.
- Attribution status.
- Source session.
- Customer journey.

These dimensions must not overwrite one another.

---

# 7. Cart Rules

### BR-CART-001 — Cart Ownership
A cart belongs to a known customer or a documented anonymous identity.

### BR-CART-002 — Cart Items
A cart contains one or more CartItem records.

Each CartItem references a valid Product.

### BR-CART-003 — Positive Quantity
Cart item quantities must be positive integers.

### BR-CART-004 — Empty Cart
An empty cart cannot enter checkout.

### BR-CART-005 — Price Snapshot
Historical item prices must be preserved.

Changes to a Product's current price must not silently alter historical cart or order records.

### BR-CART-006 — Cart Abandonment
A cart becomes abandoned after 24 hours of qualifying inactivity without a completed purchase converting that cart.

### BR-CART-007 — Payment Failure
A payment failure does not immediately abandon a cart.

### BR-CART-008 — Cart Conversion
A cart becomes converted only after a valid completed purchase associated with it is confirmed.

### BR-CART-009 — Duplicate Conversion
A converted cart cannot produce another analytical conversion.

### BR-CART-010 — Cart History
Cart lifecycle and item changes must be traceable.

---

# 8. Order and Payment Rules

### BR-ORD-001 — Order Validity
An order must contain at least one valid order item.

### BR-ORD-002 — Order Items
Each OrderItem references a valid Product and Order.

Quantities must be positive.

### BR-ORD-003 — Order Price Snapshot
Order-item prices must preserve the applicable checkout-time price.

### BR-ORD-004 — Order Total
The order total must reconcile with its item values and applicable tax, shipping, discount, and adjustment records.

### BR-ORD-005 — Payment Attempt Separation
Payment failures belong to PaymentAttempt records, not terminal order states.

### BR-ORD-006 — Multiple Payment Attempts
Multiple payment attempts are permitted against the same order.

### BR-ORD-007 — Payment Attempt History
Each attempt must retain its own identifier, attempt number, amount, status, timestamps, and available failure details.

Failed attempts must never be overwritten by successful attempts.

### BR-ORD-008 — Successful Payment
A successful payment attempt does not independently establish a completed purchase unless the authoritative purchase system confirms it.

### BR-ORD-009 — Order Confirmation
An order becomes confirmed only after authoritative purchase confirmation.

### BR-ORD-010 — Idempotent Confirmation
Repeated confirmation events must not create duplicate order transitions or conversions.

### BR-ORD-011 — Cart and Order Relationship
A cart may be associated with multiple orders over its lifecycle, but only one valid successful conversion may be counted for that cart.

### BR-ORD-012 — Unattributed Orders
A valid order may exist without a reliable session association.

Such a purchase remains valid but must not receive a guessed attribution.

### BR-ORD-013 — Order Lifecycle
Supported order states:

- `PENDING`
- `CONFIRMED`
- `CANCELLED`
- `REFUNDED`
- `PARTIALLY_REFUNDED`

Valid transitions must be enforced and recorded.

---

# 9. Support Ticket Rules

### BR-SUP-001 — Ticket Statuses
Supported statuses:

- `OPEN`
- `IN_PROGRESS`
- `RESOLVED`
- `CLOSED`
- `CANCELLED`

### BR-SUP-002 — Valid Transitions
Allowed transitions:

- `OPEN → IN_PROGRESS`
- `OPEN → CANCELLED`
- `IN_PROGRESS → RESOLVED`
- `IN_PROGRESS → OPEN`
- `RESOLVED → CLOSED`
- `RESOLVED → IN_PROGRESS`

`CLOSED` and `CANCELLED` are terminal states in the initial implementation.

### BR-SUP-003 — Lifecycle History
Every status transition must create a SupportTicketEvent record.

The previous status, new status, timestamp, and actor/source must be preserved.

### BR-SUP-004 — Optional Associations
A support ticket may reference a customer, session, or order when the relationship is reliable.

A ticket without a session association remains valid.

### BR-SUP-005 — Channel Separation
`origin_channel` describes where the issue originated.

`support_channel` describes how support was provided.

### BR-SUP-006 — Support-Assisted Attribution
Support is considered assisted only when:
- The interaction is reliably linked to the relevant customer journey.
- The interaction occurred before the completed purchase.
- The interaction falls within the applicable journey attribution window.

### BR-SUP-007 — No Causal Assumption
A linked support interaction is evidence of an observed interaction, not proof that support caused the purchase.

### BR-SUP-008 — Post-Purchase Support
Support occurring after purchase cannot be attributed as assistance for that completed purchase.

---

# 10. Experiment Rules

### BR-EXP-001 — Experiment Lifecycle
Supported statuses:

- `DRAFT`
- `SCHEDULED`
- `RUNNING`
- `PAUSED`
- `COMPLETED`
- `ARCHIVED`

### BR-EXP-002 — Variant Requirements
An experiment must contain at least two variants, including one control variant.

### BR-EXP-003 — Allocation
Variant allocation percentages must total exactly 100%.

### BR-EXP-004 — Deterministic Assignment
Variant assignment must be deterministic using:

`hash(experiment_id + assignment_unit_id + experiment_salt)`

The implementation must use a stable, documented hash and bucketing algorithm.

### BR-EXP-005 — Assignment Consistency
An assignment unit must receive the same variant throughout the experiment, unless an explicitly versioned experiment policy permits otherwise.

### BR-EXP-006 — Consent-Aware Assignment
Assignment policy:

1. Use `customer_id` when consent-authorized identity resolution is available.
2. Otherwise use a permitted anonymous identity.
3. Never merge identities without valid consent.
4. Keep identities separate when their relationship cannot be reliably established.

### BR-EXP-007 — Assignment and Exposure Separation
Assignment means a variant has been allocated.

Exposure means the variant was actually served to the subject.

Assignment alone does not establish exposure.

### BR-EXP-008 — Exposure Evidence
An exposure must be recorded only after the application confirms that the assigned variant was actually served.

### BR-EXP-009 — Experiment Window
Assignments and exposures must respect the experiment's configured active window.

### BR-EXP-010 — Pause Behaviour
A paused experiment must stop new assignments and exposures.

Historical records remain preserved.

### BR-EXP-011 — Idempotency
Duplicate assignment and exposure deliveries must not inflate counts.

### BR-EXP-012 — Versioning
Historical assignments must retain the experiment configuration and variant version used at assignment time.

### BR-EXP-013 — Analytical Interpretation
Assignment-based and exposure-based results must be reported separately.

Observed conversion differences must not automatically be described as causal effects.

---

# 11. Journey Outcome Rules

### BR-OUT-001 — Outcome Scope
Journey outcomes support:

- `SESSION`
- `CUSTOMER_JOURNEY`

Exactly one corresponding scope reference must be populated.

### BR-OUT-002 — Outcome Statuses
Supported statuses:

- `IN_PROGRESS`
- `CONVERTED`
- `NON_CONVERTED`
- `EXCLUDED`

### BR-OUT-003 — Conversion
A valid authoritative purchase within the applicable measurement window produces a converted outcome.

### BR-OUT-004 — Open Journey
A journey whose measurement window has not ended and has no completed purchase remains `IN_PROGRESS`.

### BR-OUT-005 — Non-Conversion
A valid journey whose measurement window ends without a completed purchase becomes `NON_CONVERTED`.

### BR-OUT-006 — Exclusion
Invalid or analytically disallowed journeys must be marked `EXCLUDED` with an explicit reason.

### BR-OUT-007 — Cart Abandonment Separation
Cart abandonment is distinct from session or customer journey non-conversion.

### BR-OUT-008 — Friction Indicators
A converted journey may still contain friction indicators.

Examples include:
- Payment failures.
- Checkout retries.
- Cart abandonment.
- Support interactions.
- Identity resolution issues.
- Delayed events.
- Duplicate conversion signals.

### BR-OUT-009 — Attribution Classification
Attribution must distinguish:
- `ATTRIBUTED`
- `ASSISTED`
- `UNATTRIBUTED`
- `OUT_OF_WINDOW`
- `EXCLUDED`

### BR-OUT-010 — Outcome Versioning
Every calculation must retain its calculation version and timestamp.

### BR-OUT-011 — Recalculation
Recalculation must create a traceable new outcome version rather than silently overwriting the previous result.

### BR-OUT-012 — Determinism
Identical source data and calculation rule versions must produce identical outcomes.

### BR-OUT-013 — Source Traceability
Outcomes must be traceable to their supporting events and transactional records.

---

# 12. Data Quality and Audit Rules

### BR-DQ-001 — Primary Key Integrity
All primary keys must be unique and immutable.

### BR-DQ-002 — Referential Integrity
Every non-null foreign key must reference a valid parent record, except explicitly quarantined records.

### BR-DQ-003 — Invalid Records
Invalid or conflicting records must be classified and traceable rather than silently discarded.

### BR-DQ-004 — Ground Truth Separation
Synthetic anomaly ground-truth labels must be stored separately from analytical input features.

### BR-DQ-005 — Bot Classification
Bot traffic must be identifiable and separately reportable.

Bot classification must not require deleting the underlying events.

### BR-DQ-006 — Audit History
Important business state changes must generate auditable records.

### BR-DQ-007 — Reproducible Processing
Derived records must retain the processing or calculation version used to produce them.

### BR-DQ-008 — Validation
Every full dataset generation or processing run must produce a validation summary.

---

# 13. Metric Definitions

The following formulas provide the baseline definitions for the initial analytical implementation.

### Funnel Stage Reach

`Stage Reach = Sessions reaching stage / Eligible sessions × 100`

### Stage Drop-off

`Drop-off = (Previous stage count - Next stage count) / Previous stage count × 100`

### Session Conversion Rate

`Session Conversion Rate = Converted eligible sessions / Eligible sessions × 100`

### Cart Abandonment Rate

`Cart Abandonment Rate = Abandoned eligible carts / Eligible carts × 100`

The exact eligible-cart denominator and measurement window must be documented in the analytics specification.

### Payment Failure Rate

`Payment Failure Rate = Failed payment attempts / Total payment attempts × 100`

Payment attempts, not orders, are the denominator.

### Support-Assisted Conversion Rate

`Support-Assisted Conversion Rate = Valid converted journeys with eligible linked pre-purchase support / Eligible converted journeys × 100`

The denominator must be explicitly identified in the dashboard.

All percentages must handle zero denominators safely and return an appropriate null or not-applicable result rather than dividing by zero.

---

# 14. Rule Governance

1. These rules are the shared business-logic reference for the project.
2. Changes must be documented and reviewed.
3. Changes affecting historical calculations require a calculation or processing version change.
4. The backend is responsible for enforcing critical business rules.
5. The data generator must produce records consistent with these rules, except where labelled anomalies are intentionally injected.
6. Database constraints should enforce rules that can be represented reliably at the persistence layer.
7. Analytical code must not silently override transactional source-of-truth records.
8. Any rule that remains unresolved must be explicitly marked as proposed or open rather than treated as approved.

**End of Business Rules v0.1**