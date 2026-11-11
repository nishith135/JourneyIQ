from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.entities import (
    AuditEvent,
    Cart,
    CartItem,
    ConsentRecord,
    Customer,
    CustomerJourney,
    EventQuarantine,
    Experiment,
    ExperimentAssignment,
    ExperimentExposure,
    ExperimentVariant,
    IdentityLink,
    JourneyEvent,
    JourneyOutcome,
    Order,
    OrderItem,
    PaymentAttempt,
    Product,
    SessionRecord,
    SupportTicket,
    SupportTicketEvent,
)

VALID_EVENT_TYPES = {
    "SESSION_STARTED",
    "SESSION_ENDED",
    "PRODUCT_VIEWED",
    "ADD_TO_CART",
    "REMOVE_FROM_CART",
    "CART_VIEWED",
    "CHECKOUT_STARTED",
    "PAYMENT_ATTEMPTED",
    "PAYMENT_FAILED",
    "PAYMENT_SUCCEEDED",
    "PURCHASE_COMPLETED",
    "SUPPORT_TICKET_CREATED",
    "SUPPORT_TICKET_UPDATED",
    "SUPPORT_TICKET_RESOLVED",
    "CONSENT_GRANTED",
    "CONSENT_DENIED",
    "CONSENT_WITHDRAWN",
    "AUTH_LOGIN_SUCCESS",
    "EXPERIMENT_ASSIGNED",
    "EXPERIMENT_EXPOSED",
}

VALID_CHANNELS = {"WEB", "APP", "SUPPORT", "PURCHASE", "SYSTEM"}

SUPPORT_TRANSITIONS: dict[str, set[str]] = {
    "OPEN": {"IN_PROGRESS", "CANCELLED"},
    "IN_PROGRESS": {"RESOLVED", "OPEN"},
    "RESOLVED": {"CLOSED", "IN_PROGRESS"},
    "CLOSED": set(),
    "CANCELLED": set(),
}


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def log_audit_event(
    db: Session,
    *,
    entity_type: str,
    entity_id: uuid.UUID,
    action: str,
    occurred_at: datetime,
    actor_type: str = "SYSTEM",
    actor_reference: str | None = "journeyiq-engine",
    previous_state: dict[str, Any] | None = None,
    new_state: dict[str, Any] | None = None,
    correlation_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditEvent:
    occurred_utc = ensure_utc(occurred_at)
    audit = AuditEvent(
        audit_event_id=uuid.uuid4(),
        audit_business_id=f"AUD-{uuid.uuid4().hex[:12].upper()}",
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        actor_type=actor_type,
        actor_reference=actor_reference,
        previous_state=previous_state,
        new_state=new_state,
        occurred_at=occurred_utc,
        recorded_at=datetime.now(timezone.utc),
        correlation_id=correlation_id,
        audit_metadata=metadata or {},
    )
    db.add(audit)
    db.flush()
    return audit


def quarantine_event(
    db: Session,
    *,
    issue_type: str,
    reason: str,
    detected_at: datetime,
    event_id: uuid.UUID | None = None,
    resolution_notes: str | None = None,
) -> EventQuarantine:
    q = EventQuarantine(
        quarantine_id=uuid.uuid4(),
        quarantine_business_id=f"QRN-{uuid.uuid4().hex[:12].upper()}",
        event_id=event_id,
        issue_type=issue_type,
        reason=reason,
        detected_at=ensure_utc(detected_at),
        resolution_status="OPEN",
        resolution_notes=resolution_notes,
    )
    db.add(q)
    db.flush()
    return q


def ingest_event(
    db: Session,
    *,
    event_business_id: str,
    event_type: str,
    channel: str,
    event_timestamp: datetime,
    ingestion_timestamp: datetime,
    client_instance_id: str | None = None,
    anonymous_id: str | None = None,
    customer_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
    product_id: uuid.UUID | None = None,
    cart_id: uuid.UUID | None = None,
    order_id: uuid.UUID | None = None,
    source_sequence: int | None = None,
    source_event_id: str | None = None,
    idempotency_key: str | None = None,
    properties: dict[str, Any] | None = None,
) -> tuple[JourneyEvent, EventQuarantine | None]:
    """
    Validate and persist a raw JourneyEvent (BR-EVT-001 through BR-EVT-006, SES-006):
    - Raw events are always stored immutably.
    - WEB and APP events missing client_instance_id are flagged in EventQuarantine and marked quarantined in properties.
    - Late-arriving events (ingestion_timestamp - event_timestamp > 5 minutes) are annotated in properties.
    """
    evt_ts = ensure_utc(event_timestamp)
    ing_ts = ensure_utc(ingestion_timestamp)
    props = dict(properties or {})

    delay_seconds = (ing_ts - evt_ts).total_seconds()
    if delay_seconds > 300:
        props["late_arrival_detected"] = True
        props["ingestion_lag_seconds"] = round(delay_seconds, 2)

    missing_client_id = channel in ("WEB", "APP") and (
        not client_instance_id or not str(client_instance_id).strip()
    )
    invalid_type = event_type not in VALID_EVENT_TYPES

    if missing_client_id or invalid_type:
        props["quarantined"] = True

    event = JourneyEvent(
        event_id=uuid.uuid4(),
        event_business_id=event_business_id,
        session_id=session_id,
        customer_id=customer_id,
        anonymous_id=anonymous_id,
        client_instance_id=client_instance_id if client_instance_id and str(client_instance_id).strip() else None,
        product_id=product_id,
        cart_id=cart_id,
        order_id=order_id,
        event_type=event_type,
        channel=channel if channel in VALID_CHANNELS else "SYSTEM",
        event_timestamp=evt_ts,
        ingestion_timestamp=ing_ts,
        source_sequence=source_sequence,
        source_event_id=source_event_id or event_business_id,
        idempotency_key=idempotency_key or f"IDEMP-{event_business_id}",
        properties=props,
        schema_version="1.0",
        created_at=ing_ts,
    )
    db.add(event)
    db.flush()

    q: EventQuarantine | None = None
    if missing_client_id:
        q = quarantine_event(
            db,
            issue_type="MISSING_CLIENT_INSTANCE_ID",
            reason=f"{channel} event '{event_business_id}' is missing required client_instance_id (SES-006).",
            detected_at=ing_ts,
            event_id=event.event_id,
        )
    elif invalid_type:
        q = quarantine_event(
            db,
            issue_type="MALFORMED_EVENT",
            reason=f"Event '{event_business_id}' has unsupported event_type '{event_type}'.",
            detected_at=ing_ts,
            event_id=event.event_id,
        )

    return event, q


def sessionize_events(
    db: Session,
    *,
    channel: str | None = None,
    client_instance_id: str | None = None,
    bot_client_instances: set[str] | None = None,
) -> list[SessionRecord]:
    """
    Deterministic sessionization (SES-001 through SES-014, BR-SES-001 through BR-SES-012):
    - Scoped strictly by (channel, client_instance_id) for WEB and APP.
    - Ordered by (event_timestamp ASC, source_sequence ASC NULLS LAST, event_business_id ASC).
    - 30-minute inactivity boundary: gap <= 30m stays in same session; gap > 30m starts new session.
    - Explicit SESSION_ENDED event closes the current session so subsequent events start a new session.
    - Mid-session authentication attaches customer_id without splitting the session.
    - Logical session end is last_event_at + 30m unless explicitly ended by SESSION_ENDED.
    """
    stmt = select(JourneyEvent).where(
        JourneyEvent.channel.in_(["WEB", "APP"]),
        JourneyEvent.client_instance_id.is_not(None),
    )
    if channel:
        stmt = stmt.where(JourneyEvent.channel == channel)
    if client_instance_id:
        stmt = stmt.where(JourneyEvent.client_instance_id == client_instance_id)

    all_events = list(db.scalars(stmt).all())
    events = [e for e in all_events if not (e.properties or {}).get("quarantined")]

    events.sort(
        key=lambda e: (
            ensure_utc(e.event_timestamp),
            e.source_sequence if e.source_sequence is not None else 10**12,
            e.event_business_id,
        )
    )

    grouped: dict[tuple[str, str], list[JourneyEvent]] = {}
    for ev in events:
        key = (ev.channel, str(ev.client_instance_id))
        grouped.setdefault(key, []).append(ev)

    created_sessions: list[SessionRecord] = []
    inactivity_limit = timedelta(minutes=30)
    bot_instances = bot_client_instances or set()

    for (ch, cid), stream in grouped.items():
        current_session: SessionRecord | None = None
        prev_event: JourneyEvent | None = None
        session_closed_explicitly = False

        for ev in stream:
            ev_ts = ensure_utc(ev.event_timestamp)
            ev_props = ev.properties or {}
            is_bot = (
                cid in bot_instances
                or bool(ev_props.get("is_bot"))
                or ev_props.get("traffic_classification") == "BOT"
            )

            need_new_session = False
            if current_session is None or prev_event is None:
                need_new_session = True
            elif session_closed_explicitly:
                need_new_session = True
            else:
                gap = ev_ts - ensure_utc(prev_event.event_timestamp)
                if gap > inactivity_limit:
                    need_new_session = True

            if need_new_session:
                session_seq = len(created_sessions) + 1
                sess_biz_id = f"SES-{ch[:3]}-{cid[-8:]}-{session_seq:04d}-{uuid.uuid4().hex[:4].upper()}"
                current_session = SessionRecord(
                    session_id=uuid.uuid4(),
                    session_business_id=sess_biz_id[:40],
                    customer_id=ev.customer_id,
                    anonymous_id=ev.anonymous_id,
                    client_instance_id=cid,
                    channel=ch,
                    journey_id=None,
                    started_at=ev_ts,
                    last_event_at=ev_ts,
                    ended_at=ev_ts + inactivity_limit,
                    status="ENDED",
                    sessionization_version="v1.0",
                    traffic_classification="BOT" if is_bot else "HUMAN",
                    classification_reason=ev_props.get("bot_reason", "HIGH_VELOCITY_SCRAPER") if is_bot else "NORMAL_BEHAVIOR",
                    classification_version="v1.0",
                    classified_at=ev_ts,
                    created_at=ev_ts,
                    updated_at=ev_ts,
                )
                db.add(current_session)
                db.flush()
                created_sessions.append(current_session)
                session_closed_explicitly = False

            assert current_session is not None
            current_session.last_event_at = ev_ts
            current_session.updated_at = ev_ts
            if ev.anonymous_id and not current_session.anonymous_id:
                current_session.anonymous_id = ev.anonymous_id
            if ev.customer_id:
                # SES-007: mid-session authentication updates session.customer_id without splitting
                current_session.customer_id = ev.customer_id
            if is_bot:
                current_session.traffic_classification = "BOT"
                current_session.classification_reason = ev_props.get("bot_reason", "HIGH_VELOCITY_SCRAPER")

            if ev.event_type == "SESSION_ENDED":
                current_session.ended_at = ev_ts
                current_session.status = "FINALIZED"
                session_closed_explicitly = True
            else:
                current_session.ended_at = ev_ts + inactivity_limit
                current_session.status = "ENDED"

            ev.session_id = current_session.session_id
            prev_event = ev

    db.flush()
    return created_sessions


def get_effective_consent(
    db: Session,
    *,
    anonymous_id: str | None = None,
    customer_id: uuid.UUID | None = None,
    as_of: datetime | None = None,
    consent_purpose: str = "ANALYTICS",
) -> ConsentRecord | None:
    """Return the latest effective ConsentRecord for the subject at as_of."""
    stmt = select(ConsentRecord).where(ConsentRecord.consent_purpose == consent_purpose)
    if anonymous_id and customer_id:
        stmt = stmt.where(
            (ConsentRecord.anonymous_id == anonymous_id) | (ConsentRecord.customer_id == customer_id)
        )
    elif anonymous_id:
        stmt = stmt.where(ConsentRecord.anonymous_id == anonymous_id)
    elif customer_id:
        stmt = stmt.where(ConsentRecord.customer_id == customer_id)
    else:
        return None

    records = list(db.scalars(stmt).all())
    if not records:
        return None

    if as_of is not None:
        as_of_utc = ensure_utc(as_of)
        eligible = [r for r in records if ensure_utc(r.effective_from) <= as_of_utc]
        if eligible:
            records = eligible

    records.sort(key=lambda r: ensure_utc(r.effective_from), reverse=True)
    return records[0]


def resolve_identity_link(
    db: Session,
    *,
    anonymous_id: str,
    customer_id: uuid.UUID,
    linked_at: datetime,
    link_method: str = "AUTHENTICATED_CONSENT",
    client_instance_id: str | None = None,
    source_event_id: uuid.UUID | None = None,
) -> tuple[IdentityLink | None, str]:
    """
    Consent-gated identity stitching (ID-001 through ID-012, BR-ID-001 through BR-ID-009):
    - Requires active ConsentRecord with consent_status == 'GRANTED'.
    - Authentication alone (link_method='AUTH_LOGIN_ONLY') without GRANTED consent is blocked & quarantined.
    - Conflicting anonymous_id -> different customer_id is flagged CONFLICTED & quarantined without overwriting.
    - Repeated identical (anonymous_id, customer_id) returns the existing ACTIVE link idempotently.
    """
    linked_utc = ensure_utc(linked_at)

    existing_links = list(
        db.scalars(
            select(IdentityLink).where(
                IdentityLink.anonymous_id == anonymous_id,
                IdentityLink.status == "ACTIVE",
            )
        ).all()
    )

    for link in existing_links:
        if link.customer_id == customer_id:
            # ID-007: Idempotent repeated link
            return link, "IDEMPOTENT_EXISTING"
        else:
            # ID-006 / ANOM-002: Conflicting customer mapping for same anonymous_id
            conflicted = IdentityLink(
                identity_link_id=uuid.uuid4(),
                identity_link_business_id=f"IDL-CNF-{uuid.uuid4().hex[:8].upper()}",
                anonymous_id=anonymous_id,
                customer_id=customer_id,
                client_instance_id=client_instance_id,
                link_method=link_method,
                consent_record_id=link.consent_record_id,
                effective_from=linked_utc,
                effective_to=linked_utc,
                status="CONFLICTED",
                created_at=linked_utc,
            )
            db.add(conflicted)
            quarantine_event(
                db,
                issue_type="IDENTITY_CONFLICT",
                reason=(
                    f"ANOM-002: anonymous_id '{anonymous_id}' is already linked to customer_id '{link.customer_id}'; "
                    f"rejected conflicting merge to customer_id '{customer_id}'."
                ),
                detected_at=linked_utc,
                event_id=source_event_id,
            )
            log_audit_event(
                db,
                entity_type="IdentityLink",
                entity_id=conflicted.identity_link_id,
                action="IDENTITY_MERGE_CONFLICT_BLOCKED",
                occurred_at=linked_utc,
                new_state={
                    "anonymous_id": anonymous_id,
                    "existing_customer_id": str(link.customer_id),
                    "attempted_customer_id": str(customer_id),
                },
            )
            db.flush()
            return conflicted, "CONFLICTED"

    # Evaluate consent status (check anonymous_id first, then customer_id)
    consent = get_effective_consent(
        db, anonymous_id=anonymous_id, customer_id=customer_id, as_of=linked_utc
    )
    if consent is None or consent.consent_status != "GRANTED":
        status_label = consent.consent_status if consent else "UNKNOWN"
        quarantine_event(
            db,
            issue_type="UNAUTHORIZED_IDENTITY_MERGE",
            reason=(
                f"ANOM-002: Identity stitching blocked for anonymous_id='{anonymous_id}' -> customer_id='{customer_id}' "
                f"because consent_status is '{status_label}' (link_method='{link_method}')."
            ),
            detected_at=linked_utc,
            event_id=source_event_id,
        )
        log_audit_event(
            db,
            entity_type="Customer",
            entity_id=customer_id,
            action="IDENTITY_STITCH_BLOCKED_NO_CONSENT",
            occurred_at=linked_utc,
            new_state={
                "anonymous_id": anonymous_id,
                "consent_status": status_label,
                "link_method": link_method,
            },
        )
        db.flush()
        return None, f"BLOCKED_{status_label}"

    new_link = IdentityLink(
        identity_link_id=uuid.uuid4(),
        identity_link_business_id=f"IDL-{uuid.uuid4().hex[:10].upper()}",
        anonymous_id=anonymous_id,
        customer_id=customer_id,
        client_instance_id=client_instance_id,
        link_method=link_method,
        consent_record_id=consent.consent_record_id,
        effective_from=linked_utc,
        effective_to=None,
        status="ACTIVE",
        created_at=linked_utc,
    )
    db.add(new_link)

    # Retroactively stitch eligible sessions for this anonymous_id
    anon_sessions = list(
        db.scalars(
            select(SessionRecord).where(
                SessionRecord.anonymous_id == anonymous_id,
                SessionRecord.customer_id.is_(None),
            )
        ).all()
    )
    for s in anon_sessions:
        s.customer_id = customer_id

    log_audit_event(
        db,
        entity_type="IdentityLink",
        entity_id=new_link.identity_link_id,
        action="IDENTITY_LINK_CREATED",
        occurred_at=linked_utc,
        new_state={
            "anonymous_id": anonymous_id,
            "customer_id": str(customer_id),
            "consent_record_id": str(consent.consent_record_id),
        },
    )
    db.flush()
    return new_link, "CREATED"


def withdraw_consent(
    db: Session,
    *,
    customer_id: uuid.UUID,
    anonymous_id: str | None = None,
    withdrawn_at: datetime,
    actor_reference: str = "customer-self-service",
) -> dict[str, Any]:
    """
    Handle consent withdrawal (ID-008, ID-009):
    - Close previous ConsentRecord and insert WITHDRAWN record.
    - Revoke active IdentityLink rows (status='REVOKED', effective_to=withdrawn_at).
    - Detach unauthenticated pre-login anonymous sessions from customer_id while preserving raw JourneyEvent rows.
    - Increment CustomerJourney.identity_version and recalculate JourneyOutcome with a new calculation_version.
    """
    withdrawn_utc = ensure_utc(withdrawn_at)

    prev_consents = list(
        db.scalars(
            select(ConsentRecord).where(
                (ConsentRecord.customer_id == customer_id)
                | (ConsentRecord.anonymous_id == anonymous_id if anonymous_id else False),
                ConsentRecord.effective_to.is_(None),
            )
        ).all()
    )
    for pc in prev_consents:
        pc.effective_to = withdrawn_utc

    # Note ck_consent_record_exactly_one_subject: set customer_id (and anonymous_id=None)
    new_consent = ConsentRecord(
        consent_record_id=uuid.uuid4(),
        consent_business_id=f"CNS-WDR-{uuid.uuid4().hex[:8].upper()}",
        customer_id=customer_id,
        anonymous_id=None,
        consent_purpose="ANALYTICS",
        consent_status="WITHDRAWN",
        effective_from=withdrawn_utc,
        effective_to=None,
        recorded_at=withdrawn_utc,
        source="CONSENT_WITHDRAWAL_API",
    )
    db.add(new_consent)

    stmt_links = select(IdentityLink).where(
        IdentityLink.customer_id == customer_id,
        IdentityLink.status == "ACTIVE",
    )
    if anonymous_id:
        stmt_links = stmt_links.where(IdentityLink.anonymous_id == anonymous_id)

    active_links = list(db.scalars(stmt_links).all())
    revoked_ids: list[str] = []
    affected_anon_ids: set[str] = set()
    if anonymous_id:
        affected_anon_ids.add(anonymous_id)

    for link in active_links:
        link.status = "REVOKED"
        link.effective_to = withdrawn_utc
        revoked_ids.append(link.identity_link_business_id)
        affected_anon_ids.add(link.anonymous_id)

    # Unlink purely anonymous sessions from customer_id while preserving raw events
    if affected_anon_ids:
        anon_sessions = list(
            db.scalars(
                select(SessionRecord).where(
                    SessionRecord.customer_id == customer_id,
                    SessionRecord.anonymous_id.in_(list(affected_anon_ids)),
                )
            ).all()
        )
        for s in anon_sessions:
            auth_ev = db.scalar(
                select(JourneyEvent).where(
                    JourneyEvent.session_id == s.session_id,
                    JourneyEvent.event_type == "AUTH_LOGIN_SUCCESS",
                )
            )
            if not auth_ev:
                s.customer_id = None
                s.journey_id = None

    log_audit_event(
        db,
        entity_type="ConsentRecord",
        entity_id=new_consent.consent_record_id,
        action="CONSENT_WITHDRAWN",
        actor_type="USER",
        actor_reference=actor_reference,
        occurred_at=withdrawn_utc,
        previous_state={"active_identity_links": revoked_ids},
        new_state={"consent_status": "WITHDRAWN", "revoked_links": revoked_ids},
    )
    db.flush()

    journeys = list(
        db.scalars(select(CustomerJourney).where(CustomerJourney.customer_id == customer_id)).all()
    )
    recalculated_versions: list[dict[str, Any]] = []
    for j in journeys:
        j.identity_version += 1
        outcome = calculate_journey_outcome(
            db,
            journey_id=j.journey_id,
            as_of_timestamp=withdrawn_utc,
            reason="CONSENT_WITHDRAWAL_RECALCULATION",
        )
        recalculated_versions.append(
            {
                "journey_business_id": j.journey_business_id,
                "identity_version": j.identity_version,
                "calculation_version": outcome.calculation_version,
                "outcome_status": outcome.outcome_status,
            }
        )

    db.flush()
    return {
        "consent_business_id": new_consent.consent_business_id,
        "revoked_identity_links": revoked_ids,
        "recalculated_journeys": recalculated_versions,
    }


def add_or_update_cart_item(
    db: Session,
    *,
    cart_id: uuid.UUID,
    product_id: uuid.UUID,
    quantity: int,
    updated_at: datetime,
) -> CartItem:
    """
    Add or update a cart line item (CART-004, CART-005):
    - Rejects quantity <= 0.
    - Snapshots product.current_price into CartItem.unit_price on creation.
    """
    if quantity <= 0:
        raise ValueError("Cart item quantity must be strictly greater than zero (CART-004).")

    ts = ensure_utc(updated_at)
    cart = db.get(Cart, cart_id)
    if not cart:
        raise ValueError(f"Cart {cart_id} not found.")
    product = db.get(Product, product_id)
    if not product:
        raise ValueError(f"Product {product_id} not found.")

    existing = db.scalar(
        select(CartItem).where(
            CartItem.cart_id == cart_id,
            CartItem.product_id == product_id,
        )
    )
    if existing:
        existing.quantity = quantity
        existing.updated_at = ts
        item = existing
    else:
        price_snap = Decimal(str(product.current_price)).quantize(Decimal("0.01"))
        item = CartItem(
            cart_item_id=uuid.uuid4(),
            cart_item_business_id=f"CIT-{uuid.uuid4().hex[:10].upper()}",
            cart_id=cart_id,
            product_id=product_id,
            quantity=quantity,
            unit_price=price_snap,
            added_at=ts,
            updated_at=ts,
        )
        db.add(item)

    cart.updated_at = ts
    db.flush()
    return item


def remove_cart_item(
    db: Session,
    *,
    cart_id: uuid.UUID,
    product_id: uuid.UUID,
    removed_at: datetime,
) -> Cart:
    """Remove an item from the cart and update cart timestamp (CART-006)."""
    ts = ensure_utc(removed_at)
    cart = db.get(Cart, cart_id)
    if not cart:
        raise ValueError(f"Cart {cart_id} not found.")
    existing = db.scalar(
        select(CartItem).where(
            CartItem.cart_id == cart_id,
            CartItem.product_id == product_id,
        )
    )
    if existing:
        db.delete(existing)
    cart.updated_at = ts
    db.flush()
    return cart


def get_cart_summary(db: Session, *, cart_id: uuid.UUID) -> dict[str, Any]:
    cart = db.get(Cart, cart_id)
    if not cart:
        raise ValueError(f"Cart {cart_id} not found.")
    items = list(db.scalars(select(CartItem).where(CartItem.cart_id == cart_id)).all())
    item_count = sum(i.quantity for i in items)
    subtotal = sum((Decimal(i.quantity) * i.unit_price for i in items), Decimal("0.00")).quantize(
        Decimal("0.01")
    )
    return {
        "cart_id": cart.cart_id,
        "cart_business_id": cart.cart_business_id,
        "status": cart.status,
        "item_count": item_count,
        "subtotal": subtotal,
        "items": items,
    }


def validate_checkout_start(db: Session, *, cart_id: uuid.UUID, started_at: datetime | None = None) -> Cart:
    """Reject checkout initiation on an empty cart (CART-003)."""
    summary = get_cart_summary(db, cart_id=cart_id)
    if summary["item_count"] <= 0 or not summary["items"]:
        raise ValueError("Cannot start checkout with an empty cart (CART-003).")
    cart = db.get(Cart, cart_id)
    assert cart is not None
    cart.status = "CHECKOUT"
    if started_at:
        cart.updated_at = ensure_utc(started_at)
    db.flush()
    return cart


def evaluate_cart_abandonment(
    db: Session,
    *,
    cart_id: uuid.UUID,
    as_of_timestamp: datetime,
) -> Cart:
    """
    Evaluate 24-hour cart inactivity abandonment threshold (CART-001, CART-002, CART-008):
    - Marks ACTIVE/CHECKOUT cart as ABANDONED only when >= 24 hours have elapsed since updated_at
      without conversion.
    - Does NOT mark the parent CustomerJourney as NON_CONVERTED (journey has a 7-day window).
    """
    as_of = ensure_utc(as_of_timestamp)
    cart = db.get(Cart, cart_id)
    if not cart:
        raise ValueError(f"Cart {cart_id} not found.")

    if cart.status == "CONVERTED":
        return cart

    summary = get_cart_summary(db, cart_id=cart_id)
    inactivity = as_of - ensure_utc(cart.updated_at)
    if cart.status in ("ACTIVE", "CHECKOUT") and summary["item_count"] > 0 and inactivity >= timedelta(hours=24):
        cart.status = "ABANDONED"
        cart.abandoned_at = ensure_utc(cart.updated_at) + timedelta(hours=24)
        db.flush()
    return cart


def create_or_get_authoritative_order(
    db: Session,
    *,
    order_business_id: str,
    purchase_source: str,
    source_purchase_reference: str,
    placed_at: datetime,
    items: list[dict[str, Any]],
    customer_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
    cart_id: uuid.UUID | None = None,
    discount_amount: Decimal = Decimal("0.00"),
    tax_amount: Decimal = Decimal("0.00"),
    shipping_amount: Decimal = Decimal("0.00"),
    currency: str = "USD",
) -> tuple[Order, bool]:
    """
    Create an Order or deduplicate by (purchase_source, source_purchase_reference) (ORD-005, ORD-006, ANOM-003).
    Returns (order, is_duplicate_signal).
    """
    placed_utc = ensure_utc(placed_at)
    existing = db.scalar(
        select(Order).where(
            Order.purchase_source == purchase_source,
            Order.source_purchase_reference == source_purchase_reference,
        )
    )
    if existing:
        log_audit_event(
            db,
            entity_type="Order",
            entity_id=existing.order_id,
            action="DUPLICATE_CONVERSION_SIGNAL_SUPPRESSED",
            occurred_at=placed_utc,
            new_state={
                "purchase_source": purchase_source,
                "source_purchase_reference": source_purchase_reference,
                "existing_order_business_id": existing.order_business_id,
            },
        )
        return existing, True

    if not items:
        raise ValueError("Order must contain at least one OrderItem.")

    subtotal = Decimal("0.00")
    order_id = uuid.uuid4()
    order_item_rows: list[OrderItem] = []

    for idx, item_spec in enumerate(items, start=1):
        qty = int(item_spec["quantity"])
        if qty <= 0:
            raise ValueError("OrderItem quantity must be > 0.")
        unit_price = Decimal(str(item_spec["unit_price"])).quantize(Decimal("0.01"))
        line_disc = Decimal(str(item_spec.get("discount_amount", "0.00"))).quantize(Decimal("0.01"))
        line_total = (Decimal(qty) * unit_price - line_disc).quantize(Decimal("0.01"))
        if line_total < Decimal("0.00"):
            raise ValueError("OrderItem line_total cannot be negative (ORD-010).")
        subtotal += line_total
        order_item_rows.append(
            OrderItem(
                order_item_id=uuid.uuid4(),
                order_item_business_id=f"OIT-{order_business_id[-6:]}-{idx:02d}-{uuid.uuid4().hex[:4].upper()}",
                order_id=order_id,
                product_id=item_spec["product_id"],
                quantity=qty,
                unit_price=unit_price,
                line_total=line_total,
            )
        )

    disc = Decimal(str(discount_amount)).quantize(Decimal("0.01"))
    tax = Decimal(str(tax_amount)).quantize(Decimal("0.01"))
    ship = Decimal(str(shipping_amount)).quantize(Decimal("0.01"))
    total = (subtotal - disc + tax + ship).quantize(Decimal("0.01"))
    if total < Decimal("0.00"):
        raise ValueError("Order total_amount cannot be negative (ORD-009).")

    order = Order(
        order_id=order_id,
        order_business_id=order_business_id,
        customer_id=customer_id,
        cart_id=cart_id,
        session_id=session_id,
        source_purchase_reference=source_purchase_reference,
        purchase_source=purchase_source,
        currency=currency,
        subtotal=subtotal,
        discount_amount=disc,
        tax_amount=tax,
        shipping_amount=ship,
        total_amount=total,
        status="PENDING",
        created_at=placed_utc,
    )
    db.add(order)
    for oi in order_item_rows:
        db.add(oi)
    db.flush()
    return order, False


def record_payment_attempt(
    db: Session,
    *,
    order_id: uuid.UUID,
    status: str,
    attempted_at: datetime,
    failure_reason: str | None = None,
    provider_reference: str | None = None,
) -> PaymentAttempt:
    """
    Record a PaymentAttempt (ORD-001 through ORD-004):
    - Failed payment attempts keep order.status='PENDING' (non-terminal; payment failure is a PaymentAttempt state,
      NOT a terminal Order state!) and allow subsequent retries.
    - Subsequent SUCCEEDED attempt transitions order.status to 'CONFIRMED' and marks linked cart 'CONVERTED'.
    """
    ts = ensure_utc(attempted_at)
    order = db.get(Order, order_id)
    if not order:
        raise ValueError(f"Order {order_id} not found.")

    existing_count = (
        db.scalar(
            select(func.count()).select_from(PaymentAttempt).where(PaymentAttempt.order_id == order_id)
        )
        or 0
    )
    attempt_num = int(existing_count) + 1

    attempt = PaymentAttempt(
        payment_attempt_id=uuid.uuid4(),
        payment_attempt_business_id=f"PAY-{order.order_business_id[-6:]}-{attempt_num:02d}-{uuid.uuid4().hex[:4].upper()}",
        order_id=order_id,
        attempt_number=attempt_num,
        amount=order.total_amount,
        currency=order.currency,
        status=status,
        failure_reason=failure_reason if status == "FAILED" else None,
        provider_reference=provider_reference or f"PRV-{uuid.uuid4().hex[:10].upper()}",
        attempted_at=ts,
        completed_at=ts,
    )
    db.add(attempt)

    if status == "SUCCEEDED":
        order.status = "CONFIRMED"
        order.purchase_confirmed_at = ts
        if order.cart_id:
            cart = db.get(Cart, order.cart_id)
            if cart:
                cart.status = "CONVERTED"
                cart.converted_at = ts
                cart.updated_at = ts

    db.flush()
    return attempt


def transition_support_ticket(
    db: Session,
    *,
    ticket_id: uuid.UUID,
    to_status: str,
    changed_at: datetime,
    actor_type: str = "AGENT",
    actor_reference: str = "support-agent-01",
    notes: str | None = None,
) -> SupportTicket:
    """
    Enforce support ticket state machine (SUP-001, SUP-002, SUP-005).
    """
    ts = ensure_utc(changed_at)
    ticket = db.get(SupportTicket, ticket_id)
    if not ticket:
        raise ValueError(f"SupportTicket {ticket_id} not found.")

    from_status = ticket.status
    allowed = SUPPORT_TRANSITIONS.get(from_status, set())
    if to_status not in allowed:
        raise ValueError(
            f"Invalid SupportTicket state transition from '{from_status}' to '{to_status}' (SUP-002)."
        )

    ticket.status = to_status
    if to_status == "RESOLVED":
        ticket.resolved_at = ts
    elif to_status == "CLOSED":
        ticket.closed_at = ts

    evt = SupportTicketEvent(
        ticket_event_id=uuid.uuid4(),
        ticket_event_business_id=f"TKE-{uuid.uuid4().hex[:10].upper()}",
        ticket_id=ticket_id,
        previous_status=from_status,
        new_status=to_status,
        actor_type=actor_type,
        actor_reference=actor_reference,
        event_timestamp=ts,
        notes=notes or f"Transitioned from {from_status} to {to_status}",
    )
    db.add(evt)
    db.flush()
    return ticket


def validate_and_activate_experiment(
    db: Session,
    *,
    experiment_id: uuid.UUID,
    activated_at: datetime,
) -> Experiment:
    """
    Validate experiment variants before transitioning to RUNNING (EXP-001 through EXP-004):
    - At least 2 variants for the current config_version.
    - Exactly 1 control variant.
    - Variant allocation_percentage values sum to 100.00%.
    """
    ts = ensure_utc(activated_at)
    exp = db.get(Experiment, experiment_id)
    if not exp:
        raise ValueError(f"Experiment {experiment_id} not found.")

    variants = list(
        db.scalars(
            select(ExperimentVariant).where(
                ExperimentVariant.experiment_id == experiment_id,
                ExperimentVariant.config_version == exp.config_version,
            )
        ).all()
    )
    if len(variants) < 2:
        raise ValueError("Experiment requires at least 2 variants before activation (EXP-002).")

    control_count = sum(1 for v in variants if v.is_control)
    if control_count != 1:
        raise ValueError(
            f"Experiment requires exactly 1 control variant; found {control_count} (EXP-003)."
        )

    total_alloc = sum(
        (Decimal(str(v.allocation_percentage)) for v in variants), Decimal("0.00")
    ).quantize(Decimal("0.01"))
    if total_alloc != Decimal("100.00"):
        raise ValueError(
            f"Variant traffic allocations must total 100.00%; found {total_alloc}% (EXP-004)."
        )

    exp.status = "RUNNING"
    if not exp.start_at:
        exp.start_at = ts
    exp.updated_at = ts
    db.flush()
    return exp


def compute_deterministic_bucket(experiment_business_id: str, identity_key: str, salt: str) -> int:
    """Return deterministic bucket 0..9999 using SHA-256 (EXP-005)."""
    payload = f"{experiment_business_id}:{identity_key}:{salt}".encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    return int(digest[:8], 16) % 10000


def assign_experiment_variant(
    db: Session,
    *,
    experiment_id: uuid.UUID,
    assigned_at: datetime,
    customer_id: uuid.UUID | None = None,
    anonymous_id: str | None = None,
) -> ExperimentAssignment:
    """
    Deterministic, idempotent experiment assignment (EXP-005, EXP-006, EXP-010):
    - Rejects assignment when experiment.status != 'RUNNING' (e.g. PAUSED or DRAFT).
    - Uses consent-authorized customer_id when available, else anonymous_id.
    - Preserves config_version on ExperimentAssignment.
    """
    ts = ensure_utc(assigned_at)
    exp = db.get(Experiment, experiment_id)
    if not exp:
        raise ValueError(f"Experiment {experiment_id} not found.")
    if exp.status != "RUNNING":
        raise ValueError(
            f"Cannot assign variant when experiment status is '{exp.status}' (EXP-006)."
        )

    consent: ConsentRecord | None = None
    id_link: IdentityLink | None = None
    use_customer = False

    if customer_id is not None:
        consent = get_effective_consent(
            db, customer_id=customer_id, anonymous_id=anonymous_id, as_of=ts
        )
        if consent and consent.consent_status == "GRANTED":
            use_customer = True
            if anonymous_id:
                id_link = db.scalar(
                    select(IdentityLink).where(
                        IdentityLink.customer_id == customer_id,
                        IdentityLink.anonymous_id == anonymous_id,
                        IdentityLink.status == "ACTIVE",
                    )
                )

    if use_customer and customer_id is not None:
        id_type = "CUSTOMER"
        id_key = str(customer_id)
    elif anonymous_id:
        id_type = "ANONYMOUS"
        id_key = anonymous_id
    elif customer_id is not None:
        id_type = "CUSTOMER"
        id_key = str(customer_id)
    else:
        raise ValueError("Either customer_id or anonymous_id is required for experiment assignment.")

    existing = db.scalar(
        select(ExperimentAssignment).where(
            ExperimentAssignment.experiment_id == experiment_id,
            ExperimentAssignment.assignment_identity_type == id_type,
            ExperimentAssignment.assignment_identity_id == id_key,
            ExperimentAssignment.config_version == exp.config_version,
        )
    )
    if existing:
        return existing

    variants = list(
        db.scalars(
            select(ExperimentVariant).where(
                ExperimentVariant.experiment_id == experiment_id,
                ExperimentVariant.config_version == exp.config_version,
            )
        ).all()
    )
    variants.sort(key=lambda v: (not v.is_control, v.variant_name))
    if not variants:
        raise ValueError("Experiment has no variants.")

    bucket = compute_deterministic_bucket(exp.experiment_business_id, id_key, exp.experiment_salt)
    cumulative = 0
    chosen_variant = variants[-1]
    for v in variants:
        span = int(Decimal(str(v.allocation_percentage)) * Decimal("100"))
        if bucket < cumulative + span:
            chosen_variant = v
            break
        cumulative += span

    assignment = ExperimentAssignment(
        assignment_id=uuid.uuid4(),
        assignment_business_id=f"ASG-{uuid.uuid4().hex[:10].upper()}",
        experiment_id=experiment_id,
        variant_id=chosen_variant.variant_id,
        assignment_identity_type=id_type,
        assignment_identity_id=id_key,
        identity_link_id=id_link.identity_link_id if id_link else None,
        consent_record_id=consent.consent_record_id if consent else None,
        identity_version=1,
        assignment_bucket=bucket,
        config_version=exp.config_version,
        assigned_at=ts,
    )
    db.add(assignment)
    db.flush()
    return assignment


def record_experiment_exposure(
    db: Session,
    *,
    assignment_id: uuid.UUID,
    exposed_at: datetime,
    channel: str = "WEB",
    event_id: uuid.UUID | None = None,
    exposure_surface: str = "CHECKOUT_FLOW",
) -> ExperimentExposure:
    """
    Record experiment exposure separately from assignment (EXP-006 through EXP-009):
    - Rejects exposure if experiment is PAUSED.
    - Deduplicates repeat exposures per assignment so exposed subject metrics are never inflated.
    """
    ts = ensure_utc(exposed_at)
    assignment = db.get(ExperimentAssignment, assignment_id)
    if not assignment:
        raise ValueError(f"ExperimentAssignment {assignment_id} not found.")

    exp = db.get(Experiment, assignment.experiment_id)
    if not exp or exp.status != "RUNNING":
        status_str = exp.status if exp else "MISSING"
        raise ValueError(f"Cannot record exposure while experiment status is '{status_str}' (EXP-006).")

    existing_first = db.scalar(
        select(ExperimentExposure).where(ExperimentExposure.assignment_id == assignment_id)
    )
    if existing_first:
        # EXP-009: Deduplicate repeat exposure
        return existing_first

    exposure = ExperimentExposure(
        exposure_id=uuid.uuid4(),
        exposure_business_id=f"EXP-{uuid.uuid4().hex[:10].upper()}",
        assignment_id=assignment.assignment_id,
        event_id=event_id,
        channel=channel,
        exposed_at=ts,
        exposure_context={"surface": exposure_surface, "is_first_exposure": True},
    )
    db.add(exposure)
    db.flush()
    return exposure


def calculate_journey_outcome(
    db: Session,
    *,
    journey_id: uuid.UUID,
    as_of_timestamp: datetime,
    reason: str = "EVALUATION",
) -> JourneyOutcome:
    """
    Versioned CustomerJourney outcome calculation (OUT-001 through OUT-014):
    - 7-day attribution window [first_touch_at, window_end_at].
    - Bot traffic excluded (EXCLUDED / BOT_TRAFFIC).
    - Authoritative CONFIRMED orders inside window -> CONVERTED (ASSISTED if support ticket opened
      before purchase on the same journey, else ATTRIBUTED).
    - Purchases after window_end_at -> OUT_OF_WINDOW (does not convert expired journey).
    - Open window without purchase -> IN_PROGRESS.
    - Expired window without purchase -> NON_CONVERTED.
    - Preserves prior JourneyOutcome versions by incrementing calculation_version (v1, v2, ...).
    """
    as_of = ensure_utc(as_of_timestamp)
    journey = db.get(CustomerJourney, journey_id)
    if not journey:
        raise ValueError(f"CustomerJourney {journey_id} not found.")

    first_touch = ensure_utc(journey.first_touch_at)
    window_end = ensure_utc(journey.window_end_at)

    sessions = list(
        db.scalars(select(SessionRecord).where(SessionRecord.journey_id == journey_id)).all()
    )
    session_ids = [s.session_id for s in sessions]

    events: list[JourneyEvent] = []
    if session_ids:
        events = list(
            db.scalars(select(JourneyEvent).where(JourneyEvent.session_id.in_(session_ids))).all()
        )

    # Orders linked via session_id or customer_id within journey timeframe
    orders_stmt = select(Order)
    if session_ids and journey.customer_id:
        orders_stmt = orders_stmt.where(
            (Order.session_id.in_(session_ids)) | (Order.customer_id == journey.customer_id)
        )
    elif session_ids:
        orders_stmt = orders_stmt.where(Order.session_id.in_(session_ids))
    elif journey.customer_id:
        orders_stmt = orders_stmt.where(Order.customer_id == journey.customer_id)
    else:
        orders_stmt = orders_stmt.where(Order.order_id.is_(None))
    orders = list(db.scalars(orders_stmt).all())

    tickets = list(
        db.scalars(select(SupportTicket).where(SupportTicket.journey_id == journey_id)).all()
    )

    carts: list[Cart] = []
    if session_ids:
        carts = list(db.scalars(select(Cart).where(Cart.session_id.in_(session_ids))).all())

    friction_flags: list[str] = []

    human_sessions = [s for s in sessions if s.traffic_classification == "HUMAN"]
    bot_sessions = [s for s in sessions if s.traffic_classification == "BOT"]

    order_ids = [o.order_id for o in orders]
    payment_attempts: list[PaymentAttempt] = []
    if order_ids:
        payment_attempts = list(
            db.scalars(select(PaymentAttempt).where(PaymentAttempt.order_id.in_(order_ids))).all()
        )
    if any(pa.status == "FAILED" for pa in payment_attempts):
        friction_flags.append("PAYMENT_FAILURE")
    if any(pa.attempt_number > 1 for pa in payment_attempts):
        friction_flags.append("CHECKOUT_RETRY")

    for c in carts:
        evaluate_cart_abandonment(db, cart_id=c.cart_id, as_of_timestamp=as_of)
    if any(c.status == "ABANDONED" for c in carts):
        friction_flags.append("CART_ABANDONED")

    if any((e.properties or {}).get("late_arrival_detected") for e in events):
        friction_flags.append("LATE_EVENT")
    purchase_events = [e for e in events if e.event_type == "PURCHASE_COMPLETED"]
    confirmed_orders_all = [
        o for o in orders if o.status == "CONFIRMED" and o.purchase_confirmed_at is not None
    ]
    if len(purchase_events) > len(confirmed_orders_all) and len(purchase_events) > 1:
        friction_flags.append("DUPLICATE_SIGNAL")

    if tickets:
        friction_flags.append("SUPPORT_CONTACTED")

    outcome_reason: str | None = reason
    confirmed_order: Order | None = None
    out_of_window_order: Order | None = None

    confirmed_orders_all.sort(key=lambda o: ensure_utc(o.purchase_confirmed_at))  # type: ignore[arg-type]

    for o in confirmed_orders_all:
        conf_at = ensure_utc(o.purchase_confirmed_at)  # type: ignore[arg-type]
        if conf_at <= as_of:
            if first_touch <= conf_at <= window_end:
                confirmed_order = o
                break
            elif conf_at > window_end:
                out_of_window_order = o

    if sessions and not human_sessions and bot_sessions:
        outcome_status = "EXCLUDED"
        attribution_status = "EXCLUDED"
        outcome_reason = "BOT_TRAFFIC"
    elif confirmed_order is not None:
        outcome_status = "CONVERTED"
        conf_at = ensure_utc(confirmed_order.purchase_confirmed_at)  # type: ignore[arg-type]
        # SUP-003: Support-assisted conversion requires support interaction BEFORE purchase within 7d window
        support_before_purchase = any(
            first_touch <= ensure_utc(t.created_at) <= conf_at for t in tickets
        )
        if support_before_purchase:
            attribution_status = "ASSISTED"
            outcome_reason = "SUPPORT_ASSISTED_CONVERSION"
        else:
            attribution_status = "ATTRIBUTED"
            outcome_reason = "DIRECT_WINDOW_CONVERSION"
    else:
        if out_of_window_order is not None:
            attribution_status = "OUT_OF_WINDOW"
            outcome_reason = "PURCHASE_AFTER_7D_WINDOW"
        else:
            attribution_status = "UNATTRIBUTED"

        if as_of < window_end:
            outcome_status = "IN_PROGRESS"
            if not out_of_window_order:
                outcome_reason = "ATTRIBUTION_WINDOW_OPEN"
        else:
            outcome_status = "NON_CONVERTED"
            if not out_of_window_order:
                outcome_reason = "ATTRIBUTION_WINDOW_EXPIRED"

    journey.status = "FINALIZED" if outcome_status in ("CONVERTED", "NON_CONVERTED", "EXCLUDED") else "OPEN"

    # Versioned persistence (OUT-007, OUT-010)
    existing_outcomes = list(
        db.scalars(
            select(JourneyOutcome).where(
                JourneyOutcome.journey_id == journey_id,
                JourneyOutcome.scope_type == "CUSTOMER_JOURNEY",
            )
        ).all()
    )
    next_num = len(existing_outcomes) + 1
    calc_ver = f"v{next_num}"

    new_outcome = JourneyOutcome(
        outcome_id=uuid.uuid4(),
        outcome_business_id=f"OUT-{journey.journey_business_id[-8:]}-{calc_ver.upper()}-{uuid.uuid4().hex[:4].upper()}",
        scope_type="CUSTOMER_JOURNEY",
        session_id=None,
        journey_id=journey_id,
        outcome_status=outcome_status,
        outcome_reason=outcome_reason,
        conversion_order_id=confirmed_order.order_id if confirmed_order else None,
        first_touch_at=first_touch,
        window_end_at=window_end,
        converted_at=ensure_utc(confirmed_order.purchase_confirmed_at) if confirmed_order and confirmed_order.purchase_confirmed_at else None,
        attribution_status=attribution_status,
        friction_flags=sorted(set(friction_flags)),
        calculation_version=calc_ver,
        calculated_at=as_of,
    )
    db.add(new_outcome)
    db.flush()
    return new_outcome


def calculate_session_outcome(
    db: Session,
    *,
    session_id: uuid.UUID,
    as_of_timestamp: datetime,
) -> JourneyOutcome:
    """Calculate a SESSION-scoped JourneyOutcome (OUT-011)."""
    as_of = ensure_utc(as_of_timestamp)
    sess = db.get(SessionRecord, session_id)
    if not sess:
        raise ValueError(f"SessionRecord {session_id} not found.")

    orders = list(
        db.scalars(
            select(Order).where(
                Order.session_id == session_id,
                Order.status == "CONFIRMED",
            )
        ).all()
    )
    confirmed_order = orders[0] if orders else None

    if sess.traffic_classification == "BOT":
        outcome_status = "EXCLUDED"
        attribution_status = "EXCLUDED"
        outcome_reason = "BOT_TRAFFIC"
    elif confirmed_order is not None:
        outcome_status = "CONVERTED"
        attribution_status = "ATTRIBUTED"
        outcome_reason = "SESSION_CONVERSION"
    elif sess.ended_at and ensure_utc(sess.ended_at) > as_of:
        outcome_status = "IN_PROGRESS"
        attribution_status = "UNATTRIBUTED"
        outcome_reason = "SESSION_ACTIVE"
    else:
        outcome_status = "NON_CONVERTED"
        attribution_status = "UNATTRIBUTED"
        outcome_reason = "SESSION_ENDED_WITHOUT_PURCHASE"

    existing_outcomes = list(
        db.scalars(
            select(JourneyOutcome).where(
                JourneyOutcome.session_id == session_id,
                JourneyOutcome.scope_type == "SESSION",
            )
        ).all()
    )
    calc_ver = f"v{len(existing_outcomes) + 1}"

    outcome = JourneyOutcome(
        outcome_id=uuid.uuid4(),
        outcome_business_id=f"OUT-SES-{sess.session_business_id[-6:]}-{calc_ver.upper()}-{uuid.uuid4().hex[:4].upper()}",
        scope_type="SESSION",
        session_id=session_id,
        journey_id=None,
        outcome_status=outcome_status,
        outcome_reason=outcome_reason,
        conversion_order_id=confirmed_order.order_id if confirmed_order else None,
        first_touch_at=ensure_utc(sess.started_at),
        window_end_at=ensure_utc(sess.ended_at or (sess.last_event_at + timedelta(minutes=30))),
        converted_at=ensure_utc(confirmed_order.purchase_confirmed_at) if confirmed_order and confirmed_order.purchase_confirmed_at else None,
        attribution_status=attribution_status,
        friction_flags=[],
        calculation_version=calc_ver,
        calculated_at=as_of,
    )
    db.add(outcome)
    db.flush()
    return outcome
