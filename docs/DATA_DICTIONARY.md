JourneyIQ — Data Dictionary and Domain Data Model
Version: 0.2
Status: Reconciled draft — ready for final review
Project: JourneyIQ Customer Journey Analytics Platform
Database: PostgreSQL
Related documents: PRD.md, SUCCESS_CRITERIA.md, BUSINESS_RULES.md, ARCHITECTURE.md
1. Purpose
This document defines JourneyIQ's logical relational data model. It is the reference for the ERD, PostgreSQL schema, SQLAlchemy models, migrations, and API contracts.

It documents:
- The 24 core entities and their responsibilities.
- Attributes, PostgreSQL data types, nullability, and defaults.
- Internal primary keys and human-readable business identifiers.
- Foreign keys, uniqueness, checks, and relationships.
- Lifecycle values and historical-data requirements.
- Important query indexes.
The model preserves raw event history and important state transitions while keeping derived analytical classifications and outcomes traceable.
1.1 Conventions
Type	Usage
UUID	Internal primary and foreign keys
VARCHAR(n)	Bounded text
TEXT	Unbounded descriptive text
INTEGER, BIGINT, SMALLINT	Integer values and counters
NUMERIC(12,2)	Monetary values
NUMERIC(5,2)	Percentages
BOOLEAN	Boolean values
TIMESTAMPTZ	Timezone-aware timestamps
DATE	Calendar dates
JSONB	Structured metadata/configuration


All timestamps are stored as timezone-aware UTC values. Monetary values must use an explicit ISO 4217 currency code.
1.2 Identifier strategy — approved DD-001
Every core entity has:
1. An internal UUID primary key, used by all relational foreign keys.
2. A unique, immutable, human-readable business identifier, used for display, logs, exports, and demonstrations.
Example:
Field	Example
customer_id (internal PK)	550e8400-e29b-41d4-a716-446655440000
customer_business_id	CUS-000001


The same pattern applies to all entities. A field named <entity>_id is the internal UUID unless explicitly identified as an external/source identifier. Business identifiers use <entity>_business_id.

Business identifiers are not foreign-key targets.
1.3 General conventions
- Table and column names use snake_case; table names are singular.
- Every entity has created_at where creation time is meaningful.
- Mutable master/operational records generally have updated_at.
- Append-only history records are not updated to rewrite history.
- Status and type values are documented in this file.
- Foreign keys use UUID and have explicitly chosen deletion behaviour. Historical records must not be silently cascaded away.
- NOT NULL and UNIQUE are database constraints, not merely application checks.
2. Core Entity Inventory
The reconciled model contains 24 core entities.
#	Entity	Table	Responsibility
1	Customer	customer	Known customer profile
2	Session	session	Browser/app interaction session
3	JourneyEvent	journey_event	Immutable observed event
4	Page	page	Page metadata
5	Product	product	Product catalogue
6	Cart	cart	Shopping cart
7	CartItem	cart_item	Current cart line item
8	CartItemEvent	cart_item_event	Append-only cart-item history
9	Order	orders	Commercial order and authoritative purchase reference
10	OrderItem	order_item	Purchased order line
11	PaymentAttempt	payment_attempt	Individual payment attempt
12	Refund	refund	Refund request/result history
13	SupportTicket	support_ticket	Support case
14	SupportTicketEvent	support_ticket_event	Append-only ticket lifecycle history
15	Experiment	experiment	Experiment configuration and lifecycle
16	ExperimentVariant	experiment_variant	Control/treatment variant
17	ExperimentAssignment	experiment_assignment	Deterministic assignment evidence
18	ExperimentExposure	experiment_exposure	Actual variant exposure
19	CustomerJourney	customer_journey	Seven-day analytical journey
20	JourneyOutcome	journey_outcome	Versioned analytical outcome
21	IdentityLink	identity_link	Consent-authorized identity association
22	ConsentRecord	consent_record	Effective-dated consent history
23	AuditEvent	audit_event	Append-only audit trail
24	EventQuarantine	event_quarantine	Persisted event quality exception


Generation-run metadata and anomaly ground-truth labels are files in the data-generation output, not additional relational entities in the initial implementation.
3. Entity Definitions
3.1 Customer
Table: customer
Purpose: Stores known customer profile information.
Field	Type	Constraints	Description
customer_id	UUID	PK	Internal identifier
customer_business_id	VARCHAR(32)	UNIQUE, NOT NULL	Human-readable ID, e.g. CUS-000001
email	VARCHAR(255)	NULL, UNIQUE where present	Customer email; not an identity-resolution key
first_name	VARCHAR(100)	NULL	First name
last_name	VARCHAR(100)	NULL	Last name
account_status	VARCHAR(20)	NOT NULL	Account state
created_at	TIMESTAMPTZ	NOT NULL	Creation time
updated_at	TIMESTAMPTZ	NOT NULL	Last update


Statuses: ACTIVE, INACTIVE, SUSPENDED.

Relationships: Customer 1:N Session, Cart, Order, SupportTicket, CustomerJourney, ConsentRecord, IdentityLink.

Email must never be used as the key for identity stitching. Use customer_id and approved identity-link evidence.
3.2 Session
Table: session
Purpose: Represents an interaction session scoped to a browser or app installation and channel.
Field	Type	Constraints	Description
session_id	UUID	PK	Internal identifier
session_business_id	VARCHAR(40)	UNIQUE, NOT NULL	Human-readable session ID
customer_id	UUID	FK → customer, NULL	Known customer, if reliably resolved
anonymous_id	VARCHAR(64)	NULL	Anonymous visitor identifier
client_instance_id	VARCHAR(64)	NOT NULL	Browser/app installation identifier
channel	VARCHAR(20)	NOT NULL	WEB or APP
journey_id	UUID	FK → customer_journey, NULL	Current derived journey membership
started_at	TIMESTAMPTZ	NOT NULL	First event occurrence time
last_event_at	TIMESTAMPTZ	NOT NULL	Latest event occurrence time
ended_at	TIMESTAMPTZ	NULL	Logical or explicit end time
status	VARCHAR(20)	NOT NULL	Session lifecycle
sessionization_version	VARCHAR(30)	NOT NULL	Applied rule version
traffic_classification	VARCHAR(20)	NOT NULL, DEFAULT UNKNOWN	Derived traffic class
classification_reason	TEXT	NULL	Explanation for classification
classification_version	VARCHAR(30)	NULL	Classifier/rule version
classified_at	TIMESTAMPTZ	NULL	Classification time
created_at	TIMESTAMPTZ	NOT NULL	Persistence time
updated_at	TIMESTAMPTZ	NOT NULL	Last derived-record update


Channels: WEB, APP.
Statuses: ACTIVE, ENDED, FINALIZED.
Traffic classifications: HUMAN, BOT, UNKNOWN.

Relationships: Customer 1:N Session (optional customer on session); CustomerJourney 1:N Session (optional journey on session); Session 1:N JourneyEvent, Cart, SupportTicket.

Rules:
- Sessionization uses event_timestamp, not ingestion time.
- A gap of 30 minutes or less remains in the same session; a gap greater than 30 minutes starts a new session.
- WEB and APP sessions are separate.
- Authentication alone does not split a session.
- journey_id is nullable and directly references CustomerJourney; no bridge table is required.
- Support and purchase events are associated with a session only through a reliable explicit reference.
- Raw events remain immutable when sessions are recomputed.
- Late-event recomputation must be deterministic and version-aware.
- Bot classification is derived session metadata; it must not modify raw JourneyEvent records.
3.3 JourneyEvent
Table: journey_event
Purpose: Stores immutable records of observed customer, operational, or system interactions.
Field	Type	Constraints	Description
event_id	UUID	PK	Internal identifier
event_business_id	VARCHAR(80)	UNIQUE, NOT NULL	Human-readable event ID
session_id	UUID	FK → session, NULL	Associated session
customer_id	UUID	FK → customer, NULL	Known customer where permitted
anonymous_id	VARCHAR(64)	NULL	Anonymous identity
client_instance_id	VARCHAR(64)	NULL	Originating browser/app instance
page_id	UUID	FK → page, NULL	Associated page
product_id	UUID	FK → product, NULL	Associated product
cart_id	UUID	FK → cart, NULL	Associated cart
order_id	UUID	FK → orders, NULL	Associated order
event_type	VARCHAR(50)	NOT NULL	Event taxonomy value
channel	VARCHAR(20)	NOT NULL	Event origin
event_timestamp	TIMESTAMPTZ	NOT NULL	Actual occurrence time
ingestion_timestamp	TIMESTAMPTZ	NOT NULL	Platform ingestion time
source_sequence	BIGINT	NULL	Source-provided ordering value
source_event_id	VARCHAR(128)	NULL	Source system event identifier
idempotency_key	VARCHAR(160)	NULL	Delivery/processing reference
properties	JSONB	NOT NULL, DEFAULT {}	Event-specific attributes
schema_version	VARCHAR(30)	NOT NULL	Event schema version
created_at	TIMESTAMPTZ	NOT NULL	Persistence time


Channels: WEB, APP, SUPPORT, PURCHASE, SYSTEM.

Initial event taxonomy:

SESSION_STARTED, SESSION_ENDED, PAGE_VIEWED, SEARCH_PERFORMED, PRODUCT_VIEWED, PRODUCT_CLICKED, ADD_TO_CART, REMOVE_FROM_CART, CHECKOUT_STARTED, PAYMENT_FAILED, PURCHASE_COMPLETED, SUPPORT_CONTACTED, SUPPORT_RESOLVED, IDENTITY_LINKED, CONSENT_UPDATED, EXPERIMENT_ASSIGNED.

Relationships: Session, Customer, Page, Product, Cart, and Order each have 1:N JourneyEvents; the corresponding event-side FK is nullable.

Rules:
- Raw event content is immutable.
- Preserve both occurrence and ingestion timestamps.
- Duplicate deliveries may remain in raw history; analytical processing must not double count them.
- Purchase completion is authoritative only when supported by the purchase system and the Order's unique source purchase reference.
- Customer identity fields must not bypass consent rules.
- Event-specific required properties are validated by event schema/version.
- A duplicate-delivery idempotency key must not be made globally unique on raw events if doing so would discard duplicate deliveries that must remain auditable. Enforce idempotency in processing and authoritative purchase records.
3.4 Page
Table: page
Purpose: Stores web/app page metadata.
Field	Type	Constraints	Description
page_id	UUID	PK	Internal identifier
page_business_id	VARCHAR(32)	UNIQUE, NOT NULL	Human-readable ID
page_name	VARCHAR(150)	NOT NULL	Display name
page_path	VARCHAR(500)	NOT NULL	Route/path
page_category	VARCHAR(80)	NULL	Page classification
platform	VARCHAR(20)	NOT NULL	WEB or APP
is_active	BOOLEAN	NOT NULL, DEFAULT TRUE	Availability
created_at	TIMESTAMPTZ	NOT NULL	Creation time


Relationship: Page 1:N JourneyEvent.
3.5 Product
Table: product
Purpose: Stores product catalogue information.
Field	Type	Constraints	Description
product_id	UUID	PK	Internal identifier
product_business_id	VARCHAR(32)	UNIQUE, NOT NULL	Human-readable ID
product_name	VARCHAR(200)	NOT NULL	Product name
category	VARCHAR(100)	NOT NULL	Product category
description	TEXT	NULL	Description
current_price	NUMERIC(12,2)	NOT NULL, CHECK >= 0	Current price
currency	VARCHAR(3)	NOT NULL	ISO 4217 currency
is_active	BOOLEAN	NOT NULL, DEFAULT TRUE	Availability
created_at	TIMESTAMPTZ	NOT NULL	Creation time
updated_at	TIMESTAMPTZ	NOT NULL	Last update


Relationships: Product 1:N JourneyEvent, CartItem, OrderItem.
3.6 Cart
Table: cart
Purpose: Represents a customer's or anonymous visitor's shopping cart.
Field	Type	Constraints	Description
cart_id	UUID	PK	Internal identifier
cart_business_id	VARCHAR(32)	UNIQUE, NOT NULL	Human-readable ID
customer_id	UUID	FK → customer, NULL	Known owner
anonymous_id	VARCHAR(64)	NULL	Anonymous owner
session_id	UUID	FK → session, NULL	Originating session
status	VARCHAR(20)	NOT NULL	Cart lifecycle
currency	VARCHAR(3)	NOT NULL	Cart currency
created_at	TIMESTAMPTZ	NOT NULL	Creation time
updated_at	TIMESTAMPTZ	NOT NULL	Last qualifying activity
abandoned_at	TIMESTAMPTZ	NULL	Abandonment time
converted_at	TIMESTAMPTZ	NULL	Successful conversion time


Statuses: ACTIVE, CHECKOUT, ABANDONED, CONVERTED.

Relationships: Customer 1:N Cart (optional customer); Session 1:N Cart (optional session); Cart 1:N CartItem and Order.

Rules:
- Cart must have a documented owner: customer or anonymous identity.
- Cart abandonment is 24 hours of qualifying inactivity without a completed purchase converting the cart.
- A converted cart cannot produce another counted conversion.
- Cart status and timestamps must be consistent.
3.7 CartItem
Table: cart_item
Purpose: Represents a product and its current quantity within a cart.
Field	Type	Constraints	Description
cart_item_id	UUID	PK	Internal identifier
cart_item_business_id	VARCHAR(40)	UNIQUE, NOT NULL	Human-readable ID
cart_id	UUID	FK → cart, NOT NULL	Parent cart
product_id	UUID	FK → product, NOT NULL	Product
quantity	INTEGER	NOT NULL, CHECK > 0	Current quantity
unit_price	NUMERIC(12,2)	NOT NULL, CHECK >= 0	Price snapshot
added_at	TIMESTAMPTZ	NOT NULL	Initial addition time
updated_at	TIMESTAMPTZ	NOT NULL	Last current-state update


Relationships: Cart 1:N CartItem; Product 1:N CartItem; CartItem 1:N CartItemEvent.

Rules: Quantity must be positive. Unit price must be non-negative. Each quantity or membership change must produce a CartItemEvent. Current-state updates must not erase history.
3.8 CartItemEvent
Table: cart_item_event
Purpose: Append-only history of cart-item additions, quantity changes, and removals. Added under approved DD-002.
Field	Type	Constraints	Description
cart_item_event_id	UUID	PK	Internal identifier
event_business_id	VARCHAR(48)	UNIQUE, NOT NULL	Human-readable ID
cart_item_id	UUID	FK → cart_item, NOT NULL	Related cart item
action	VARCHAR(20)	NOT NULL	ADD, QUANTITY_CHANGED, REMOVE
previous_quantity	INTEGER	NULL	Quantity before action
new_quantity	INTEGER	NULL	Quantity after action
unit_price_snapshot	NUMERIC(12,2)	NOT NULL, CHECK >= 0	Price at action time
occurred_at	TIMESTAMPTZ	NOT NULL	Action occurrence time
recorded_at	TIMESTAMPTZ	NOT NULL	Persistence time
source_event_id	UUID	FK → journey_event, NULL	Supporting raw event


Relationship: CartItem 1:N CartItemEvent; JourneyEvent 1:N CartItemEvent (optional source event).

Rules: Records are append-only. ADD has no previous quantity and a positive new quantity; QUANTITY_CHANGED has valid old/new quantities that differ; REMOVE records the prior quantity and a zero/NULL new quantity according to the implementation's single documented convention. Price snapshots remain unchanged.
3.9 Order
Table: orders
Purpose: Represents a commercial transaction and the authoritative purchase record.
Field	Type	Constraints	Description
order_id	UUID	PK	Internal identifier
order_business_id	VARCHAR(32)	UNIQUE, NOT NULL	Human-readable ID
customer_id	UUID	FK → customer, NULL	Customer, if known
cart_id	UUID	FK → cart, NULL	Associated cart
session_id	UUID	FK → session, NULL	Reliable source session
source_purchase_reference	VARCHAR(128)	NULL	Authoritative purchase ID
purchase_source	VARCHAR(50)	NULL	Authoritative source system
currency	VARCHAR(3)	NOT NULL	Transaction currency
subtotal	NUMERIC(12,2)	NOT NULL, CHECK >= 0	Item subtotal
discount_amount	NUMERIC(12,2)	NOT NULL, CHECK >= 0	Discount total
tax_amount	NUMERIC(12,2)	NOT NULL, CHECK >= 0	Tax
shipping_amount	NUMERIC(12,2)	NOT NULL, CHECK >= 0	Shipping
total_amount	NUMERIC(12,2)	NOT NULL, CHECK >= 0	Final order amount
status	VARCHAR(25)	NOT NULL	Order lifecycle
created_at	TIMESTAMPTZ	NOT NULL	Order creation time
purchase_confirmed_at	TIMESTAMPTZ	NULL	Authoritative purchase time
cancelled_at	TIMESTAMPTZ	NULL	Cancellation time


Statuses: PENDING, CONFIRMED, CANCELLED, REFUNDED, PARTIALLY_REFUNDED.

Relationships: Customer 1:N Order (optional customer); Cart 1:N Order (optional cart); Session 1:N Order (optional source session); Order 1:N OrderItem, PaymentAttempt, Refund, SupportTicket, JourneyOutcome.

Rules:
- An order must contain at least one valid item before confirmation.
- Totals must reconcile with items and applicable adjustments.
- Purchase confirmation must originate from the authoritative purchase system.
- A purchase is identified by (purchase_source, source_purchase_reference).
- Enforce a unique constraint on (purchase_source, source_purchase_reference) when both are present; implement as a partial unique index for non-NULL references.
- Duplicate deliveries remain traceable in JourneyEvent but cannot confirm or count the same authoritative purchase twice.
- Refund and cancellation history must remain traceable.
- PAYMENT_FAILED is a PaymentAttempt status/event, not an Order terminal status.
3.10 OrderItem
Table: order_item
Purpose: Represents a product purchased within an order.
Field	Type	Constraints	Description
order_item_id	UUID	PK	Internal identifier
order_item_business_id	VARCHAR(40)	UNIQUE, NOT NULL	Human-readable ID
order_id	UUID	FK → orders, NOT NULL	Parent order
product_id	UUID	FK → product, NOT NULL	Purchased product
quantity	INTEGER	NOT NULL, CHECK > 0	Quantity
unit_price	NUMERIC(12,2)	NOT NULL, CHECK >= 0	Checkout-time price
line_total	NUMERIC(12,2)	NOT NULL, CHECK >= 0	Quantity × unit price


Relationships: Order 1:N OrderItem; Product 1:N OrderItem.

Rule: line_total must reconcile with quantity and unit price, subject to the documented rounding convention.
3.11 PaymentAttempt
Table: payment_attempt
Purpose: Preserves every payment attempt associated with an order.
Field	Type	Constraints	Description
payment_attempt_id	UUID	PK	Internal identifier
payment_attempt_business_id	VARCHAR(40)	UNIQUE, NOT NULL	Human-readable ID
order_id	UUID	FK → orders, NOT NULL	Parent order
attempt_number	INTEGER	NOT NULL, CHECK > 0	Attempt sequence
amount	NUMERIC(12,2)	NOT NULL, CHECK > 0	Attempted amount
currency	VARCHAR(3)	NOT NULL	Currency
status	VARCHAR(20)	NOT NULL	Attempt result
failure_reason	VARCHAR(255)	NULL	Failure explanation
provider_reference	VARCHAR(128)	NULL	External payment reference
attempted_at	TIMESTAMPTZ	NOT NULL	Attempt time
completed_at	TIMESTAMPTZ	NULL	Completion time


Statuses: PENDING, SUCCEEDED, FAILED, CANCELLED.

Relationship: Order 1:N PaymentAttempt.

Constraints: Unique (order_id, attempt_number). Multiple attempts are allowed for one order. Failed attempts remain in history.
3.12 Refund
Table: refund
Purpose: Records refund requests and their outcomes. Added under approved DD-004.
Field	Type	Constraints	Description
refund_id	UUID	PK	Internal identifier
refund_business_id	VARCHAR(40)	UNIQUE, NOT NULL	Human-readable ID
order_id	UUID	FK → orders, NOT NULL	Related order
amount	NUMERIC(12,2)	NOT NULL, CHECK > 0	Refund amount
currency	VARCHAR(3)	NOT NULL	Refund currency
status	VARCHAR(20)	NOT NULL	Refund lifecycle
reason	VARCHAR(255)	NULL	Refund reason
provider_reference	VARCHAR(128)	NULL	External provider reference
requested_at	TIMESTAMPTZ	NOT NULL	Request time
completed_at	TIMESTAMPTZ	NULL	Completion time


Statuses: PENDING, SUCCEEDED, FAILED, CANCELLED.

Relationship: Order 1:N Refund.

Rules: An order may have multiple refund records. Completed refund totals must not exceed the refundable order balance. Full and partial refund status is derived from successful refund records. Refund history is preserved.
3.13 SupportTicket
Table: support_ticket
Purpose: Represents a customer support case.
Field	Type	Constraints	Description
ticket_id	UUID	PK	Internal identifier
ticket_business_id	VARCHAR(32)	UNIQUE, NOT NULL	Human-readable ID
customer_id	UUID	FK → customer, NULL	Associated customer
session_id	UUID	FK → session, NULL	Explicitly associated session
order_id	UUID	FK → orders, NULL	Related order
journey_id	UUID	FK → customer_journey, NULL	Reliable journey association
issue_type	VARCHAR(30)	NOT NULL	Issue classification
priority	VARCHAR(15)	NOT NULL	Ticket priority
status	VARCHAR(20)	NOT NULL	Ticket lifecycle
origin_channel	VARCHAR(20)	NOT NULL	Channel where issue originated
support_channel	VARCHAR(20)	NOT NULL	Channel used for support
created_at	TIMESTAMPTZ	NOT NULL	Creation time
resolved_at	TIMESTAMPTZ	NULL	Resolution time
closed_at	TIMESTAMPTZ	NULL	Closure time


Issue types: PAYMENT, CHECKOUT, DELIVERY, PRODUCT, OTHER.
Priorities: LOW, MEDIUM, HIGH, CRITICAL.
Statuses: OPEN, IN_PROGRESS, RESOLVED, CLOSED, CANCELLED.

Relationships: Customer 1:N SupportTicket (optional customer); Session 1:N SupportTicket (optional session); Order 1:N SupportTicket (optional order); CustomerJourney 1:N SupportTicket (optional journey); SupportTicket 1:N SupportTicketEvent.

Support-assisted attribution requires a valid journey association and a support interaction before purchase within the seven-day window. It is not proof of causation.
3.14 SupportTicketEvent
Table: support_ticket_event
Purpose: Append-only lifecycle history for support tickets.
Field	Type	Constraints	Description
ticket_event_id	UUID	PK	Internal identifier
ticket_event_business_id	VARCHAR(40)	UNIQUE, NOT NULL	Human-readable ID
ticket_id	UUID	FK → support_ticket, NOT NULL	Parent ticket
previous_status	VARCHAR(20)	NULL	Previous state
new_status	VARCHAR(20)	NOT NULL	New state
actor_type	VARCHAR(20)	NOT NULL	Actor category
actor_reference	VARCHAR(64)	NULL	Actor identifier
event_timestamp	TIMESTAMPTZ	NOT NULL	Transition time
notes	TEXT	NULL	Context


Relationship: SupportTicket 1:N SupportTicketEvent.

Valid transitions:
- OPEN → IN_PROGRESS or CANCELLED
- IN_PROGRESS → RESOLVED or OPEN
- RESOLVED → CLOSED or IN_PROGRESS
- CLOSED and CANCELLED are terminal.
History is append-only.
3.15 Experiment
Table: experiment
Purpose: Defines a controlled experiment and its lifecycle.
Field	Type	Constraints	Description
experiment_id	UUID	PK	Internal identifier
experiment_business_id	VARCHAR(32)	UNIQUE, NOT NULL	Human-readable ID
name	VARCHAR(150)	NOT NULL	Experiment name
description	TEXT	NULL	Hypothesis/purpose
status	VARCHAR(20)	NOT NULL	Lifecycle
primary_metric	VARCHAR(100)	NOT NULL	Primary success metric
eligibility_rules	JSONB	NOT NULL	Eligibility configuration
allocation_config	JSONB	NOT NULL	Allocation configuration
experiment_salt	VARCHAR(128)	NOT NULL	Deterministic assignment salt
config_version	INTEGER	NOT NULL, CHECK > 0	Configuration version
start_at	TIMESTAMPTZ	NULL	Scheduled/start time
end_at	TIMESTAMPTZ	NULL	End time
created_at	TIMESTAMPTZ	NOT NULL	Creation time
updated_at	TIMESTAMPTZ	NOT NULL	Last update


Statuses: DRAFT, SCHEDULED, RUNNING, PAUSED, COMPLETED, ARCHIVED.

Relationships: Experiment 1:N ExperimentVariant and ExperimentAssignment.

Every experiment must have at least two variants including a control; allocation totals 100%. Configuration changes are versioned. Pausing stops new assignments/exposures but does not erase history.
3.16 ExperimentVariant
Table: experiment_variant
Purpose: Defines a control or treatment variant.
Field	Type	Constraints	Description
variant_id	UUID	PK	Internal identifier
variant_business_id	VARCHAR(40)	UNIQUE, NOT NULL	Human-readable ID
experiment_id	UUID	FK → experiment, NOT NULL	Parent experiment
variant_name	VARCHAR(100)	NOT NULL	Variant name
is_control	BOOLEAN	NOT NULL	Control indicator
allocation_percentage	NUMERIC(5,2)	NOT NULL, CHECK 0–100	Allocation
configuration	JSONB	NOT NULL	Variant configuration
config_version	INTEGER	NOT NULL, CHECK > 0	Variant version
created_at	TIMESTAMPTZ	NOT NULL	Creation time


Relationships: Experiment 1:N ExperimentVariant; ExperimentVariant 1:N ExperimentAssignment.

Constraints: Unique (experiment_id, variant_name) and (experiment_id, config_version, variant_name). Each experiment/version has one control and allocations totalling 100%; cross-row totals require service-level validation or a deferred validation mechanism.
3.17 ExperimentAssignment
Table: experiment_assignment
Purpose: Records the variant assigned to a consent-eligible assignment identity, preserving the exact identity and consent evidence. This consolidates the earlier assignment_unit_* and assignment_identity_* proposals into one field pair.
Field	Type	Constraints	Description
assignment_id	UUID	PK	Internal identifier
assignment_business_id	VARCHAR(40)	UNIQUE, NOT NULL	Human-readable ID
experiment_id	UUID	FK → experiment, NOT NULL	Parent experiment
variant_id	UUID	FK → experiment_variant, NOT NULL	Assigned variant
assignment_identity_type	VARCHAR(20)	NOT NULL	CUSTOMER or ANONYMOUS
assignment_identity_id	VARCHAR(64)	NOT NULL	Customer business ID or anonymous ID, as defined by type
identity_link_id	UUID	FK → identity_link, NULL	Link evidence used, if applicable
consent_record_id	UUID	FK → consent_record, NULL	Consent evidence used, if applicable
identity_version	INTEGER	NOT NULL, CHECK > 0	Identity-resolution version
assignment_bucket	INTEGER	NOT NULL	Deterministic bucket
config_version	INTEGER	NOT NULL, CHECK > 0	Experiment configuration version
assigned_at	TIMESTAMPTZ	NOT NULL	Assignment time


Relationships: Experiment 1:N ExperimentAssignment; ExperimentVariant 1:N ExperimentAssignment; IdentityLink 1:N ExperimentAssignment (optional); ConsentRecord 1:N ExperimentAssignment (optional); ExperimentAssignment 1:N ExperimentExposure.

Constraints and rules:
- Unique (experiment_id, assignment_identity_type, assignment_identity_id, config_version) for the chosen assignment policy.
- Assignment is deterministic from experiment ID, assignment identity, and salt.
- Use customer identity only when consent-authorized identity resolution is available; otherwise use the permitted anonymous identity.
- Do not merge identities without consent.
- Preserve the identity basis, consent evidence, and version used at assignment time.
- Later consent withdrawal does not rewrite historical evidence; reporting must still respect applicable privacy restrictions.
- Assignment is not exposure.
3.18 ExperimentExposure
Table: experiment_exposure
Purpose: Records actual delivery of an experiment variant.
Field	Type	Constraints	Description
exposure_id	UUID	PK	Internal identifier
exposure_business_id	VARCHAR(40)	UNIQUE, NOT NULL	Human-readable ID
assignment_id	UUID	FK → experiment_assignment, NOT NULL	Related assignment
event_id	UUID	FK → journey_event, NULL	Supporting event
channel	VARCHAR(20)	NOT NULL	Exposure channel
exposed_at	TIMESTAMPTZ	NOT NULL	Exposure time
exposure_context	JSONB	NOT NULL, DEFAULT {}	Additional context


Relationships: ExperimentAssignment 1:N ExperimentExposure; JourneyEvent 1:N ExperimentExposure (optional event).

Exposure requires a valid assignment and actual variant delivery. Duplicate exposure deliveries must not inflate exposure counts; use a stable source exposure reference/idempotency constraint if supplied by the source.
3.19 CustomerJourney
Table: customer_journey
Purpose: Groups related sessions and interactions into a seven-day analytical journey.
Field	Type	Constraints	Description
journey_id	UUID	PK	Internal identifier
journey_business_id	VARCHAR(40)	UNIQUE, NOT NULL	Human-readable ID
customer_id	UUID	FK → customer, NULL	Resolved customer when permitted
anonymous_id	VARCHAR(64)	NULL	Anonymous journey identity
first_touch_at	TIMESTAMPTZ	NOT NULL	First eligible touch
last_touch_at	TIMESTAMPTZ	NOT NULL	Latest eligible activity
window_end_at	TIMESTAMPTZ	NOT NULL	Seven-day window end
status	VARCHAR(20)	NOT NULL	Journey processing state
identity_version	INTEGER	NOT NULL, CHECK > 0	Identity resolution version
created_at	TIMESTAMPTZ	NOT NULL	Creation time


Statuses: OPEN, CLOSED, FINALIZED.

Relationships: Customer 1:N CustomerJourney (optional customer); CustomerJourney 1:N Session, SupportTicket, JourneyOutcome.

Rules:
- window_end_at is exactly seven days after first_touch_at, using the documented UTC convention.
- Exactly one of customer or anonymous journey identity must be populated.
- Journey membership must respect consent.
- Session membership uses the nullable session.journey_id FK.
- Reassignment due to late events or identity changes must be versioned and traceable.
3.20 JourneyOutcome
Table: journey_outcome
Purpose: Stores versioned analytical outcomes for sessions and customer journeys.
Field	Type	Constraints	Description
outcome_id	UUID	PK	Internal identifier
outcome_business_id	VARCHAR(48)	UNIQUE, NOT NULL	Human-readable ID
scope_type	VARCHAR(20)	NOT NULL	SESSION or CUSTOMER_JOURNEY
session_id	UUID	FK → session, NULL	Session scope reference
journey_id	UUID	FK → customer_journey, NULL	Journey scope reference
outcome_status	VARCHAR(20)	NOT NULL	Analytical result
outcome_reason	VARCHAR(255)	NULL	Explanation/exclusion reason
conversion_order_id	UUID	FK → orders, NULL	Supporting converted order
first_touch_at	TIMESTAMPTZ	NOT NULL	Measurement start
window_end_at	TIMESTAMPTZ	NOT NULL	Measurement end
converted_at	TIMESTAMPTZ	NULL	Purchase time
attribution_status	VARCHAR(20)	NOT NULL	Attribution classification
friction_flags	JSONB	NOT NULL, DEFAULT []	Derived friction indicators
calculation_version	VARCHAR(30)	NOT NULL	Calculation rules version
calculated_at	TIMESTAMPTZ	NOT NULL	Calculation time


Statuses: IN_PROGRESS, CONVERTED, NON_CONVERTED, EXCLUDED.
Attribution: ATTRIBUTED, ASSISTED, UNATTRIBUTED, OUT_OF_WINDOW, EXCLUDED.

Relationships: Session 1:N JourneyOutcome (for recalculation history); CustomerJourney 1:N JourneyOutcome; Order 1:N JourneyOutcome (optional conversion order).

Constraints:
- Exactly one of session_id and journey_id is populated.
- scope_type must agree with the populated scope FK.
- A CONVERTED outcome requires a valid authoritative purchase.
- Previous calculations remain preserved; a recalculation inserts a new versioned record rather than overwriting the prior result.
- Open window without purchase is IN_PROGRESS; expired window without purchase is NON_CONVERTED.
- Cart abandonment is distinct from journey non-conversion.
- Friction may coexist with conversion.
3.21 IdentityLink
Table: identity_link
Purpose: Records consent-authorized associations between anonymous and known customer identities.
Field	Type	Constraints	Description
identity_link_id	UUID	PK	Internal identifier
identity_link_business_id	VARCHAR(40)	UNIQUE, NOT NULL	Human-readable ID
anonymous_id	VARCHAR(64)	NOT NULL	Anonymous identity
customer_id	UUID	FK → customer, NOT NULL	Associated customer
client_instance_id	VARCHAR(64)	NULL	Relevant client instance
link_method	VARCHAR(30)	NOT NULL	Linking method
consent_record_id	UUID	FK → consent_record, NOT NULL	Consent evidence authorizing link
effective_from	TIMESTAMPTZ	NOT NULL	Start of validity
effective_to	TIMESTAMPTZ	NULL	End of validity
status	VARCHAR(20)	NOT NULL	Link lifecycle
created_at	TIMESTAMPTZ	NOT NULL	Creation time


Statuses: ACTIVE, REVOKED, CONFLICTED.

Relationships: Customer 1:N IdentityLink; ConsentRecord 1:N IdentityLink; IdentityLink may be referenced by ExperimentAssignment.

Rules:
- Only effective GRANTED consent for the applicable analytics purpose permits an active link.
- Conflicting mappings are quarantined rather than resolved by choosing the latest mapping.
- Effective periods must be valid.
- Repeated identical link requests are idempotent.
- Consent withdrawal revokes the applicable association and requires affected customer-level analytics to be excluded as specified by the business rules.
- Historical link evidence is retained.
3.22 ConsentRecord
Table: consent_record
Purpose: Preserves effective-dated consent decisions for known or anonymous subjects.
Field	Type	Constraints	Description
consent_record_id	UUID	PK	Internal identifier
consent_business_id	VARCHAR(40)	UNIQUE, NOT NULL	Human-readable ID
customer_id	UUID	FK → customer, NULL	Known subject
anonymous_id	VARCHAR(64)	NULL	Anonymous subject
consent_purpose	VARCHAR(50)	NOT NULL	Explicit purpose
consent_status	VARCHAR(20)	NOT NULL	Consent decision
effective_from	TIMESTAMPTZ	NOT NULL	Effective start
effective_to	TIMESTAMPTZ	NULL	Effective end
recorded_at	TIMESTAMPTZ	NOT NULL	Recording time
source	VARCHAR(50)	NOT NULL	Consent source


Statuses: GRANTED, DENIED, UNKNOWN, WITHDRAWN.

Relationships: Customer 1:N ConsentRecord (optional customer); ConsentRecord 1:N IdentityLink and ExperimentAssignment.

Constraint: Exactly one of customer_id and anonymous_id is populated. Consent purpose is explicit. Consent history and effective periods are preserved.
3.23 AuditEvent
Table: audit_event
Purpose: Provides an append-only audit trail for important business and processing actions.
Field	Type	Constraints	Description
audit_event_id	UUID	PK	Internal identifier
audit_business_id	VARCHAR(48)	UNIQUE, NOT NULL	Human-readable ID
entity_type	VARCHAR(50)	NOT NULL	Affected entity type
entity_id	UUID	NOT NULL	Internal UUID of affected entity
action	VARCHAR(50)	NOT NULL	Action performed
actor_type	VARCHAR(30)	NOT NULL	User, system, or process
actor_reference	VARCHAR(64)	NULL	Actor identifier
previous_state	JSONB	NULL	Previous state snapshot
new_state	JSONB	NULL	New state snapshot
occurred_at	TIMESTAMPTZ	NOT NULL	Action occurrence time
recorded_at	TIMESTAMPTZ	NOT NULL	Audit recording time
correlation_id	VARCHAR(80)	NULL	Related operation
metadata	JSONB	NOT NULL, DEFAULT {}	Additional context


Audit records are append-only. Important lifecycle changes must create audit records. entity_id is a polymorphic reference and therefore cannot have a conventional FK to every possible entity; validate it through the audit-writing service and retain entity_type.
3.24 EventQuarantine
Table: event_quarantine
Purpose: Persists events that cannot safely enter normal analytical processing. Added under approved DD-006.
Field	Type	Constraints	Description
quarantine_id	UUID	PK	Internal identifier
quarantine_business_id	VARCHAR(48)	UNIQUE, NOT NULL	Human-readable ID
event_id	UUID	FK → journey_event, NOT NULL	Original event
issue_type	VARCHAR(40)	NOT NULL	Problem category
reason	TEXT	NOT NULL	Explanation
detected_at	TIMESTAMPTZ	NOT NULL	Detection time
resolution_status	VARCHAR(20)	NOT NULL	Resolution state
resolution_notes	TEXT	NULL	Resolution explanation
resolved_at	TIMESTAMPTZ	NULL	Resolution time


Statuses: OPEN, RESOLVED, DISMISSED.

Relationships: JourneyEvent 1:N EventQuarantine.

Rules:
- Quarantined events remain traceable to original immutable events.
- Quarantine never silently deletes or modifies raw events.
- An event may have multiple quarantine records for distinct issues.
- Resolution is recorded.
- Quarantine records are persisted in PostgreSQL.
4. Relationship and Cardinality Summary
All FK columns below are UUIDs.
Parent	Child	Cardinality	Child FK optional?
Customer	Session	1:N	Yes
Customer	Cart	1:N	Yes
Customer	Order	1:N	Yes
Customer	SupportTicket	1:N	Yes
Customer	CustomerJourney	1:N	Yes
Customer	ConsentRecord	1:N	Yes, when anonymous
Customer	IdentityLink	1:N	No
Session	JourneyEvent	1:N	Yes
Session	Cart	1:N	Yes
Session	Order	1:N	Yes
Session	SupportTicket	1:N	Yes
CustomerJourney	Session	1:N	Yes
CustomerJourney	SupportTicket	1:N	Yes
Page	JourneyEvent	1:N	Yes
Product	JourneyEvent	1:N	Yes
Product	CartItem	1:N	No
Product	OrderItem	1:N	No
Cart	CartItem	1:N	No
Cart	Order	1:N	Yes
CartItem	CartItemEvent	1:N	No
JourneyEvent	CartItemEvent	1:N	Yes
JourneyEvent	EventQuarantine	1:N	No
Order	OrderItem	1:N	No
Order	PaymentAttempt	1:N	No
Order	Refund	1:N	No
Order	SupportTicket	1:N	Yes
SupportTicket	SupportTicketEvent	1:N	No
Experiment	ExperimentVariant	1:N	No
Experiment	ExperimentAssignment	1:N	No
ExperimentVariant	ExperimentAssignment	1:N	No
ExperimentAssignment	ExperimentExposure	1:N	No
JourneyEvent	ExperimentExposure	1:N	Yes
IdentityLink	ExperimentAssignment	1:N	Yes
ConsentRecord	IdentityLink	1:N	No
ConsentRecord	ExperimentAssignment	1:N	Yes
Session	JourneyOutcome	1:N	Yes
CustomerJourney	JourneyOutcome	1:N	Yes
Order	JourneyOutcome	1:N	Yes


AuditEvent.entity_id is a polymorphic UUID reference and is not represented as a conventional FK.
5. Database Constraints and Integrity Rules
5.1 Primary and foreign keys
- Every entity has a UUID primary key.
- Every entity has a unique business identifier.
- All relational FKs reference internal UUID primary keys.
- Use RESTRICT/`NO ACTION` by default for historical records; use SET NULL only when explicitly justified.
- Do not cascade-delete raw events, transaction history, consent, identity, audit, or analytical history.
5.2 Uniqueness
At minimum:
- Every business identifier is unique.
- customer.email is unique where present, subject to the chosen case-normalization policy.
- payment_attempt(order_id, attempt_number) is unique.
- experiment_variant(experiment_id, variant_name) is unique.
- experiment_assignment(experiment_id, assignment_identity_type, assignment_identity_id, config_version) is unique.
- orders(purchase_source, source_purchase_reference) is unique when both values are non-NULL.
- Experiment exposure deduplication uses a stable source exposure reference where available.
- Repeated identity-link requests are idempotent.
5.3 Check constraints
Implement database checks where possible:
- Positive quantities and attempt numbers.
- Non-negative prices, totals, discounts, tax, and shipping.
- Positive refund and payment amounts.
- Allocation percentages between 0 and 100.
- Valid status and type values.
- Valid timestamp ordering (effective_to > effective_from, completion not before request).
- Exactly one applicable subject on ConsentRecord and CustomerJourney.
- Exactly one scope reference on JourneyOutcome, matching scope_type.
- Valid cart-item event quantity semantics.
Cross-row rules such as experiment allocations totalling 100%, order totals reconciling with line items, and cumulative refunds not exceeding refundable balance require transactional service validation or an appropriate database mechanism.
5.4 Historical integrity
- JourneyEvent is immutable.
- CartItemEvent is append-only.
- SupportTicketEvent is append-only.
- AuditEvent is append-only.
- EventQuarantine preserves issue and resolution history.
- PaymentAttempt preserves every attempt.
- Refund preserves each refund request/result.
- ConsentRecord and IdentityLink preserve effective-dated history.
- JourneyOutcome recalculation inserts a new versioned record; prior calculations remain available.
- Experiment assignments and exposures are historical evidence.
6. Indexing Requirements
These are initial candidates; validate them against actual query plans and workloads.
Entity	Candidate index	Purpose
JourneyEvent	(session_id, event_timestamp)	Session timeline
JourneyEvent	(event_type, event_timestamp)	Event analytics
JourneyEvent	(customer_id, event_timestamp)	Customer activity
JourneyEvent	(anonymous_id, event_timestamp)	Anonymous activity
JourneyEvent	(ingestion_timestamp)	Late-arrival analysis
JourneyEvent	(source_event_id) where present	Source duplicate investigation
Session	(client_instance_id, channel, started_at)	Session reconstruction
Session	(customer_id, started_at)	Customer session lookup
Session	(journey_id) where present	Journey membership
CustomerJourney	(customer_id, first_touch_at)	Customer journey lookup
Cart	(status, updated_at)	Abandonment processing
CartItemEvent	(cart_item_id, occurred_at)	Cart-item history
Order	(status, created_at)	Transaction monitoring
Order	unique (purchase_source, source_purchase_reference) where both present	Purchase idempotency
PaymentAttempt	unique (order_id, attempt_number)	Payment history
Refund	(order_id, status)	Refund balance/status
SupportTicket	(status, created_at)	Support analysis
ExperimentAssignment	(experiment_id, assignment_identity_type, assignment_identity_id)	Assignment lookup
ExperimentExposure	(assignment_id, exposed_at)	Exposure analysis
JourneyOutcome	(scope_type, outcome_status)	Outcome analytics
ConsentRecord	(anonymous_id, consent_purpose, effective_from)	Anonymous consent lookup
IdentityLink	(anonymous_id, effective_from)	Identity resolution
EventQuarantine	(resolution_status, detected_at)	Exception queue
AuditEvent	(entity_type, entity_id, occurred_at)	Audit investigation


Avoid indexes without a query use case. Consider partial indexes for nullable source references and frequently queried unresolved exceptions.
7. Derived Data, Ground Truth, and Generation Metadata
7.1 Bot classification
Bot classification is derived metadata, not a raw event attribute. Under the initial 24-entity model, the chosen storage location is the Session classification fields:
- traffic_classification
- classification_reason
- classification_version
- classified_at
This keeps source JourneyEvent immutable and avoids introducing a 25th core entity. If event-level bot labels become necessary, revisit the model explicitly rather than adding raw-event fields silently.
7.2 Anomaly ground truth
Anomaly labels used to evaluate detection must remain separate from analytical input data.

Store ground truth in generator output files with fields such as:
- ground_truth_id
- source_record_id
- entity_type
- anomaly_type
- expected_classification
- injection_version
These are dataset metadata, not a core application table in the initial implementation.
7.3 Generation-run metadata
Generation-run metadata remains in JSON output files initially, not PostgreSQL.

Each manifest should include:
- Generation run ID.
- Random seed.
- Generator version.
- Configuration.
- Start/completion timestamps.
- Entity record counts.
- Output checksums.
- Validation status.
8. Approved Data Model Decisions
All eight decisions are approved and authoritative.
ID	Decision	Approved approach
DD-001	Primary keys	Internal UUID PKs plus unique human-readable business IDs
DD-002	Cart item history	Append-only CartItemEvent entity
DD-003	Journey membership	Direct nullable journey_id UUID FK on Session
DD-004	Refund history	Dedicated Refund entity
DD-005	Bot classification	Derived metadata separate from immutable raw events
DD-006	Quarantine and generation metadata	EventQuarantine in PostgreSQL; generation metadata in output files initially
DD-007	Experiment identity evidence	Preserve identity type/reference, consent evidence, and identity version
DD-008	Purchase idempotency	Preserve authoritative purchase reference and enforce uniqueness for completed purchase processing


8.1 Consolidation notes
- ExperimentAssignment uses only assignment_identity_type and assignment_identity_id; the earlier assignment_unit_type and assignment_unit_id names are not separate fields.
- Bot classification is stored on the derived Session record in the initial implementation.
- Purchase idempotency is enforced on the authoritative Order reference, not by deleting duplicate raw JourneyEvents.
- Session journey membership uses a direct FK; no bridge table is introduced.
- EventQuarantine is a core entity; generation-run metadata is not.
9. Implementation Notes and Explicitly Deferred Details
The following details should be settled while defining API contracts and implementation specifications, without changing the approved domain model silently:
1. Exact business-ID generation format for all 24 entities.
2. Email normalization/case-insensitive uniqueness implementation.
3. Exact REMOVE CartItemEvent representation (new_quantity = 0 or NULL), selected once and used consistently.
4. Exact event-specific JSONB payload schemas and schema-version compatibility.
5. Exact source exposure reference field/constraint if the source supplies one.
6. Whether event-level bot classification is needed beyond session-level classification.
7. Exact enum implementation (PostgreSQL enum types versus VARCHAR plus CHECK constraints).
8. Exact retention/deletion policy for personal data and historical analytical records.
9. Exact partitioning/retention strategy for two million or more JourneyEvents, based on measured query performance.
These are implementation details or future decisions; they do not reopen the eight approved data-model decisions.
10. Data Dictionary Governance
1. This 24-entity inventory supersedes the earlier 21-entity inventory.
2. Internal UUIDs are the only relational primary and foreign-key identifiers.
3. Business identifiers are unique, immutable, and intended for human-facing use.
4. The eight approved decisions in Section 8 are authoritative.
5. Raw events and historical records remain traceable.
6. Derived classifications and analytical outcomes must not mutate raw source events.
7. Any schema change must be reflected in this document, the ERD, API contracts, SQLAlchemy models, and Alembic migrations.
8. Do not add entities, duplicate fields, or change cardinalities without documenting and approving the change.
End of Data Dictionary — Version 0.2