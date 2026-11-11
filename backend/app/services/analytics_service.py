from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
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
from app.services.domain_service import ensure_utc

FUNNEL_STAGES = [
    ("SESSION_STARTED", "1. Session Started"),
    ("PRODUCT_VIEWED", "2. Product Viewed"),
    ("ADD_TO_CART", "3. Add to Cart"),
    ("CHECKOUT_STARTED", "4. Checkout Started"),
    ("PURCHASE_COMPLETED", "5. Purchase Completed"),
]


def _get_latest_journey_outcomes(db: Session) -> dict[uuid.UUID, JourneyOutcome]:
    all_outcomes = list(
        db.scalars(
            select(JourneyOutcome).where(JourneyOutcome.scope_type == "CUSTOMER_JOURNEY")
        ).all()
    )
    latest: dict[uuid.UUID, JourneyOutcome] = {}
    for o in all_outcomes:
        if o.journey_id is None:
            continue
        prev = latest.get(o.journey_id)
        if prev is None:
            latest[o.journey_id] = o
        else:
            # Compare version number v1, v2, ...
            v_curr = int(str(o.calculation_version).lstrip("vV") or "1")
            v_prev = int(str(prev.calculation_version).lstrip("vV") or "1")
            if v_curr >= v_prev:
                latest[o.journey_id] = o
    return latest


def get_dashboard_metrics(db: Session, *, exclude_bots: bool = True) -> dict[str, Any]:
    sessions = list(db.scalars(select(SessionRecord)).all())
    events = list(db.scalars(select(JourneyEvent)).all())
    quarantines = list(db.scalars(select(EventQuarantine)).all())
    journeys = list(db.scalars(select(CustomerJourney)).all())
    orders = list(db.scalars(select(Order)).all())
    payment_attempts = list(db.scalars(select(PaymentAttempt)).all())
    tickets = list(db.scalars(select(SupportTicket)).all())
    audits = list(db.scalars(select(AuditEvent)).all())
    carts = list(db.scalars(select(Cart)).all())

    latest_outcomes = _get_latest_journey_outcomes(db)

    human_sessions = [s for s in sessions if s.traffic_classification == "HUMAN"]
    bot_sessions = [s for s in sessions if s.traffic_classification == "BOT"]
    eligible_sessions = human_sessions if exclude_bots else sessions
    eligible_session_ids = {s.session_id for s in eligible_sessions}

    confirmed_orders = [o for o in orders if o.status == "CONFIRMED"]
    eligible_confirmed_orders = [
        o for o in confirmed_orders if o.session_id is None or o.session_id in eligible_session_ids
    ]
    total_revenue = sum((o.total_amount for o in eligible_confirmed_orders), Decimal("0.00"))

    converted_session_ids = {
        o.session_id for o in eligible_confirmed_orders if o.session_id in eligible_session_ids
    }
    session_conv_rate = (
        round((len(converted_session_ids) / len(eligible_sessions)) * 100.0, 2)
        if eligible_sessions
        else 0.0
    )

    outcome_counts = {"CONVERTED": 0, "NON_CONVERTED": 0, "IN_PROGRESS": 0, "EXCLUDED": 0}
    assisted_conversions = 0
    direct_conversions = 0
    out_of_window_count = 0

    for j in journeys:
        out = latest_outcomes.get(j.journey_id)
        if not out:
            continue
        outcome_counts[out.outcome_status] = outcome_counts.get(out.outcome_status, 0) + 1
        if out.outcome_status == "CONVERTED":
            if out.attribution_status == "ASSISTED":
                assisted_conversions += 1
            else:
                direct_conversions += 1
        elif out.attribution_status == "OUT_OF_WINDOW":
            out_of_window_count += 1

    eligible_journey_denom = (
        outcome_counts["CONVERTED"] + outcome_counts["NON_CONVERTED"] + outcome_counts["IN_PROGRESS"]
        if exclude_bots
        else len(journeys)
    )
    journey_conv_rate = (
        round((outcome_counts["CONVERTED"] / eligible_journey_denom) * 100.0, 2)
        if eligible_journey_denom > 0
        else 0.0
    )

    failed_payments = [pa for pa in payment_attempts if pa.status == "FAILED"]
    orders_with_failed_payment = {pa.order_id for pa in failed_payments}
    recovered_orders = [
        o for o in confirmed_orders if o.order_id in orders_with_failed_payment
    ]

    duplicate_signals_suppressed = sum(
        1 for a in audits if a.action == "DUPLICATE_CONVERSION_SIGNAL_SUPPRESSED"
    )
    late_events_count = sum(
        1 for e in events if (e.properties or {}).get("late_arrival_detected")
    )
    abandoned_carts_count = sum(1 for c in carts if c.status == "ABANDONED")

    web_sessions = [s for s in eligible_sessions if s.channel == "WEB"]
    app_sessions = [s for s in eligible_sessions if s.channel == "APP"]

    return {
        "exclude_bots": exclude_bots,
        "total_events": len(events),
        "quarantined_events": len(quarantines),
        "total_sessions": len(sessions),
        "human_sessions": len(human_sessions),
        "bot_sessions": len(bot_sessions),
        "eligible_sessions": len(eligible_sessions),
        "web_sessions": len(web_sessions),
        "app_sessions": len(app_sessions),
        "total_journeys": len(journeys),
        "eligible_journeys": eligible_journey_denom,
        "outcome_counts": outcome_counts,
        "session_conversion_rate_pct": session_conv_rate,
        "journey_conversion_rate_pct": journey_conv_rate,
        "confirmed_orders": len(eligible_confirmed_orders),
        "total_revenue": float(total_revenue),
        "assisted_conversions": assisted_conversions,
        "direct_conversions": direct_conversions,
        "out_of_window_purchases": out_of_window_count,
        "failed_payment_attempts": len(failed_payments),
        "recovered_payment_orders": len(recovered_orders),
        "abandoned_carts": abandoned_carts_count,
        "support_tickets_total": len(tickets),
        "duplicate_signals_suppressed": duplicate_signals_suppressed,
        "late_events_count": late_events_count,
    }


def get_funnel_metrics(
    db: Session,
    *,
    channel: str | None = None,
    exclude_bots: bool = True,
) -> dict[str, Any]:
    """
    Compute 5-stage Funnel metrics (FN-001 through FN-010, BR-FUN-001 through BR-FUN-010):
    - Separate Stage Reach (unique eligible sessions reaching stage) from
      Strict Sequential Progression (stages 1..k reached in non-decreasing timestamp order).
    - Skipped-cart direct purchases still count in Stage Reach for Stage 5 without fabricating Stage 3.
    - Deduplicates repeat events within the same session.
    """
    stmt_sess = select(SessionRecord)
    if channel and channel in ("WEB", "APP"):
        stmt_sess = stmt_sess.where(SessionRecord.channel == channel)
    all_sessions = list(db.scalars(stmt_sess).all())

    if exclude_bots:
        eligible_sessions = [s for s in all_sessions if s.traffic_classification == "HUMAN"]
    else:
        eligible_sessions = all_sessions

    eligible_ids = {s.session_id for s in eligible_sessions}
    events = list(
        db.scalars(
            select(JourneyEvent).where(JourneyEvent.session_id.in_(eligible_ids))
        ).all()
    ) if eligible_ids else []

    confirmed_orders = list(
        db.scalars(
            select(Order).where(
                Order.status == "CONFIRMED",
                Order.session_id.in_(eligible_ids),
            )
        ).all()
    ) if eligible_ids else []
    confirmed_order_sessions = {o.session_id for o in confirmed_orders if o.session_id}

    # Map per-session earliest timestamp for each funnel stage event_type
    stage_keys = [k for k, _ in FUNNEL_STAGES]
    session_stage_times: dict[uuid.UUID, dict[str, list[datetime]]] = {
        sid: {} for sid in eligible_ids
    }

    for ev in events:
        if ev.session_id not in session_stage_times:
            continue
        if (ev.properties or {}).get("quarantined"):
            continue
        if ev.event_type in stage_keys:
            # For stage 5 (PURCHASE_COMPLETED), verify authoritative order exists or order_id is confirmed
            if ev.event_type == "PURCHASE_COMPLETED":
                if ev.session_id not in confirmed_order_sessions and ev.order_id is None:
                    continue
            session_stage_times[ev.session_id].setdefault(ev.event_type, []).append(
                ensure_utc(ev.event_timestamp)
            )

    for sid in session_stage_times:
        for st_key in session_stage_times[sid]:
            session_stage_times[sid][st_key].sort()

    # Compute Stage Reach and Strict Sequential Progression
    reach_counts: dict[str, int] = {k: 0 for k in stage_keys}
    sequential_counts: dict[str, int] = {k: 0 for k in stage_keys}
    skipped_cart_purchases = 0

    for sid, st_map in session_stage_times.items():
        # 1. Stage Reach: did the session reach this stage at least once?
        for st_key in stage_keys:
            if st_key in st_map and len(st_map[st_key]) > 0:
                reach_counts[st_key] += 1

        if "PURCHASE_COMPLETED" in st_map and "ADD_TO_CART" not in st_map:
            skipped_cart_purchases += 1

        # 2. Strict Sequential Progression: can we pick timestamps t1 <= t2 <= ... <= tk for stages 1..k?
        current_min_ts: datetime | None = None
        for idx, st_key in enumerate(stage_keys):
            timestamps = st_map.get(st_key, [])
            if not timestamps:
                break
            if idx == 0:
                current_min_ts = timestamps[0]
                sequential_counts[st_key] += 1
            else:
                assert current_min_ts is not None
                valid_next = [t for t in timestamps if t >= current_min_ts]
                if valid_next:
                    current_min_ts = valid_next[0]
                    sequential_counts[st_key] += 1
                else:
                    break

    stages_output: list[dict[str, Any]] = []
    top_reach = reach_counts[stage_keys[0]] if stage_keys else 0
    top_seq = sequential_counts[stage_keys[0]] if stage_keys else 0

    for idx, (st_key, st_label) in enumerate(FUNNEL_STAGES):
        r_cnt = reach_counts[st_key]
        s_cnt = sequential_counts[st_key]

        prev_r = reach_counts[stage_keys[idx - 1]] if idx > 0 else r_cnt
        prev_s = sequential_counts[stage_keys[idx - 1]] if idx > 0 else s_cnt

        reach_step_conv = round((r_cnt / prev_r) * 100.0, 2) if prev_r > 0 else 0.0
        reach_dropoff = round(100.0 - reach_step_conv, 2) if idx > 0 and prev_r > 0 else 0.0
        reach_overall = round((r_cnt / top_reach) * 100.0, 2) if top_reach > 0 else 0.0

        seq_step_conv = round((s_cnt / prev_s) * 100.0, 2) if prev_s > 0 else 0.0
        seq_dropoff = round(100.0 - seq_step_conv, 2) if idx > 0 and prev_s > 0 else 0.0
        seq_overall = round((s_cnt / top_seq) * 100.0, 2) if top_seq > 0 else 0.0

        stages_output.append(
            {
                "stage_number": idx + 1,
                "stage_key": st_key,
                "stage_label": st_label,
                "reach_count": r_cnt,
                "reach_step_conversion_pct": reach_step_conv,
                "reach_dropoff_pct": reach_dropoff,
                "reach_overall_pct": reach_overall,
                "sequential_count": s_cnt,
                "sequential_step_conversion_pct": seq_step_conv,
                "sequential_dropoff_pct": seq_dropoff,
                "sequential_overall_pct": seq_overall,
            }
        )

    dash = get_dashboard_metrics(db, exclude_bots=exclude_bots)

    return {
        "channel_filter": channel or "ALL",
        "exclude_bots": exclude_bots,
        "eligible_sessions_count": len(eligible_sessions),
        "bot_sessions_excluded": len([s for s in all_sessions if s.traffic_classification == "BOT"]) if exclude_bots else 0,
        "skipped_cart_purchases": skipped_cart_purchases,
        "session_conversion_rate_pct": dash["session_conversion_rate_pct"],
        "journey_conversion_rate_pct": dash["journey_conversion_rate_pct"],
        "stages": stages_output,
    }


def list_journeys(
    db: Session,
    *,
    outcome_status: str | None = None,
    friction_flag: str | None = None,
    search: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    journeys = list(db.scalars(select(CustomerJourney)).all())
    customers = {c.customer_id: c for c in db.scalars(select(Customer)).all()}
    sessions = list(db.scalars(select(SessionRecord)).all())
    orders = list(db.scalars(select(Order)).all())
    latest_outcomes = _get_latest_journey_outcomes(db)

    sessions_by_journey: dict[uuid.UUID, list[SessionRecord]] = {}
    for s in sessions:
        if s.journey_id:
            sessions_by_journey.setdefault(s.journey_id, []).append(s)

    orders_by_session: dict[uuid.UUID, list[Order]] = {}
    for o in orders:
        if o.session_id:
            orders_by_session.setdefault(o.session_id, []).append(o)

    rows: list[dict[str, Any]] = []
    for j in journeys:
        out = latest_outcomes.get(j.journey_id)
        out_status = out.outcome_status if out else "IN_PROGRESS"
        attr_status = out.attribution_status if out else "UNATTRIBUTED"
        flags = list(out.friction_flags or []) if out else []
        calc_ver = out.calculation_version if out else "v1"

        if outcome_status and outcome_status != "ALL" and out_status != outcome_status:
            continue
        if friction_flag and friction_flag != "ALL" and friction_flag not in flags:
            continue

        cust = customers.get(j.customer_id) if j.customer_id else None
        cust_biz = cust.customer_business_id if cust else None
        j_sessions = sessions_by_journey.get(j.journey_id, [])

        if search and search.strip():
            q = search.strip().lower()
            haystack = " ".join(
                filter(
                    None,
                    [
                        j.journey_business_id,
                        cust_biz,
                        j.anonymous_id,
                        *(s.session_business_id for s in j_sessions),
                    ],
                )
            ).lower()
            if q not in haystack:
                continue

        j_orders: list[Order] = []
        for s in j_sessions:
            j_orders.extend(orders_by_session.get(s.session_id, []))
        confirmed_rev = sum(
            (o.total_amount for o in j_orders if o.status == "CONFIRMED"), Decimal("0.00")
        )

        rows.append(
            {
                "journey_id": str(j.journey_id),
                "journey_business_id": j.journey_business_id,
                "customer_business_id": cust_biz,
                "customer_email": cust.email if cust else None,
                "anonymous_id": j.anonymous_id,
                "first_touch_at": ensure_utc(j.first_touch_at).isoformat(),
                "last_touch_at": ensure_utc(j.last_touch_at).isoformat(),
                "window_end_at": ensure_utc(j.window_end_at).isoformat(),
                "journey_status": j.status,
                "identity_version": j.identity_version,
                "outcome_status": out_status,
                "attribution_status": attr_status,
                "outcome_reason": out.outcome_reason if out else None,
                "calculation_version": calc_ver,
                "friction_flags": flags,
                "session_count": len(j_sessions),
                "channels": sorted({s.channel for s in j_sessions}),
                "confirmed_revenue": float(confirmed_rev),
            }
        )

    # Sort with hero journey first, then by first_touch_at DESC
    rows.sort(
        key=lambda r: (
            0 if "HERO" in r["journey_business_id"] else 1,
            r["journey_business_id"],
        )
    )
    return rows[:limit]


def get_journey_detail(db: Session, journey_identifier: str) -> dict[str, Any] | None:
    """Lookup journey by journey_business_id or UUID and return full end-to-end traceable timeline."""
    journey: CustomerJourney | None = db.scalar(
        select(CustomerJourney).where(CustomerJourney.journey_business_id == journey_identifier)
    )
    if journey is None:
        try:
            j_uuid = uuid.UUID(journey_identifier)
            journey = db.get(CustomerJourney, j_uuid)
        except ValueError:
            return None
    if journey is None:
        return None

    customer = db.get(Customer, journey.customer_id) if journey.customer_id else None
    sessions = list(
        db.scalars(
            select(SessionRecord).where(SessionRecord.journey_id == journey.journey_id)
        ).all()
    )
    sessions.sort(key=lambda s: ensure_utc(s.started_at))
    session_ids = [s.session_id for s in sessions]

    events: list[JourneyEvent] = []
    if session_ids:
        events = list(
            db.scalars(select(JourneyEvent).where(JourneyEvent.session_id.in_(session_ids))).all()
        )
    events.sort(
        key=lambda e: (
            ensure_utc(e.event_timestamp),
            e.source_sequence if e.source_sequence is not None else 10**12,
            e.event_business_id,
        )
    )

    anon_ids = {s.anonymous_id for s in sessions if s.anonymous_id}
    if journey.anonymous_id:
        anon_ids.add(journey.anonymous_id)

    consents: list[ConsentRecord] = []
    if journey.customer_id or anon_ids:
        stmt_c = select(ConsentRecord)
        if journey.customer_id and anon_ids:
            stmt_c = stmt_c.where(
                (ConsentRecord.customer_id == journey.customer_id)
                | (ConsentRecord.anonymous_id.in_(list(anon_ids)))
            )
        elif journey.customer_id:
            stmt_c = stmt_c.where(ConsentRecord.customer_id == journey.customer_id)
        else:
            stmt_c = stmt_c.where(ConsentRecord.anonymous_id.in_(list(anon_ids)))
        consents = list(db.scalars(stmt_c).all())
        consents.sort(key=lambda c: ensure_utc(c.effective_from))

    identity_links: list[IdentityLink] = []
    if journey.customer_id or anon_ids:
        stmt_l = select(IdentityLink)
        if journey.customer_id and anon_ids:
            stmt_l = stmt_l.where(
                (IdentityLink.customer_id == journey.customer_id)
                | (IdentityLink.anonymous_id.in_(list(anon_ids)))
            )
        elif journey.customer_id:
            stmt_l = stmt_l.where(IdentityLink.customer_id == journey.customer_id)
        else:
            stmt_l = stmt_l.where(IdentityLink.anonymous_id.in_(list(anon_ids)))
        identity_links = list(db.scalars(stmt_l).all())

    carts: list[Cart] = []
    if session_ids:
        carts = list(db.scalars(select(Cart).where(Cart.session_id.in_(session_ids))).all())
    cart_ids = [c.cart_id for c in carts]
    cart_items = (
        list(db.scalars(select(CartItem).where(CartItem.cart_id.in_(cart_ids))).all())
        if cart_ids
        else []
    )

    orders: list[Order] = []
    if session_ids or journey.customer_id:
        stmt_o = select(Order)
        if session_ids and journey.customer_id:
            stmt_o = stmt_o.where(
                (Order.session_id.in_(session_ids)) | (Order.customer_id == journey.customer_id)
            )
        elif session_ids:
            stmt_o = stmt_o.where(Order.session_id.in_(session_ids))
        else:
            stmt_o = stmt_o.where(Order.customer_id == journey.customer_id)
        orders = list(db.scalars(stmt_o).all())

    order_ids = [o.order_id for o in orders]
    payment_attempts = (
        list(db.scalars(select(PaymentAttempt).where(PaymentAttempt.order_id.in_(order_ids))).all())
        if order_ids
        else []
    )
    payment_attempts.sort(key=lambda pa: (pa.order_id, pa.attempt_number))

    tickets = list(
        db.scalars(
            select(SupportTicket).where(SupportTicket.journey_id == journey.journey_id)
        ).all()
    )
    ticket_ids = [t.ticket_id for t in tickets]
    ticket_events = (
        list(
            db.scalars(
                select(SupportTicketEvent).where(SupportTicketEvent.ticket_id.in_(ticket_ids))
            ).all()
        )
        if ticket_ids
        else []
    )
    ticket_events.sort(key=lambda te: ensure_utc(te.event_timestamp))

    outcomes = list(
        db.scalars(
            select(JourneyOutcome).where(
                JourneyOutcome.journey_id == journey.journey_id,
                JourneyOutcome.scope_type == "CUSTOMER_JOURNEY",
            )
        ).all()
    )
    outcomes.sort(key=lambda o: int(str(o.calculation_version).lstrip("vV") or "1"), reverse=True)
    current_outcome = outcomes[0] if outcomes else None

    return {
        "journey": {
            "journey_id": str(journey.journey_id),
            "journey_business_id": journey.journey_business_id,
            "customer_id": str(journey.customer_id) if journey.customer_id else None,
            "customer_business_id": customer.customer_business_id if customer else None,
            "customer_email": customer.email if customer else None,
            "anonymous_id": journey.anonymous_id or (next(iter(anon_ids), None)),
            "first_touch_at": ensure_utc(journey.first_touch_at).isoformat(),
            "last_touch_at": ensure_utc(journey.last_touch_at).isoformat(),
            "window_end_at": ensure_utc(journey.window_end_at).isoformat(),
            "status": journey.status,
            "identity_version": journey.identity_version,
        },
        "current_outcome": {
            "outcome_business_id": current_outcome.outcome_business_id,
            "outcome_status": current_outcome.outcome_status,
            "attribution_status": current_outcome.attribution_status,
            "outcome_reason": current_outcome.outcome_reason,
            "calculation_version": current_outcome.calculation_version,
            "friction_flags": current_outcome.friction_flags,
            "converted_at": ensure_utc(current_outcome.converted_at).isoformat() if current_outcome and current_outcome.converted_at else None,
            "calculated_at": ensure_utc(current_outcome.calculated_at).isoformat(),
        }
        if current_outcome
        else None,
        "outcome_history": [
            {
                "outcome_business_id": o.outcome_business_id,
                "calculation_version": o.calculation_version,
                "outcome_status": o.outcome_status,
                "attribution_status": o.attribution_status,
                "outcome_reason": o.outcome_reason,
                "friction_flags": o.friction_flags,
                "calculated_at": ensure_utc(o.calculated_at).isoformat(),
            }
            for o in outcomes
        ],
        "consents": [
            {
                "consent_business_id": c.consent_business_id,
                "consent_status": c.consent_status,
                "consent_purpose": c.consent_purpose,
                "subject": c.anonymous_id or (customer.customer_business_id if customer else str(c.customer_id)),
                "effective_from": ensure_utc(c.effective_from).isoformat(),
                "effective_to": ensure_utc(c.effective_to).isoformat() if c.effective_to else None,
                "source": c.source,
            }
            for c in consents
        ],
        "identity_links": [
            {
                "identity_link_business_id": l.identity_link_business_id,
                "anonymous_id": l.anonymous_id,
                "customer_id": str(l.customer_id),
                "link_method": l.link_method,
                "status": l.status,
                "effective_from": ensure_utc(l.effective_from).isoformat(),
                "effective_to": ensure_utc(l.effective_to).isoformat() if l.effective_to else None,
            }
            for l in identity_links
        ],
        "sessions": [
            {
                "session_id": str(s.session_id),
                "session_business_id": s.session_business_id,
                "channel": s.channel,
                "client_instance_id": s.client_instance_id,
                "anonymous_id": s.anonymous_id,
                "started_at": ensure_utc(s.started_at).isoformat(),
                "last_event_at": ensure_utc(s.last_event_at).isoformat(),
                "ended_at": ensure_utc(s.ended_at).isoformat() if s.ended_at else None,
                "status": s.status,
                "traffic_classification": s.traffic_classification,
                "classification_reason": s.classification_reason,
            }
            for s in sessions
        ],
        "events": [
            {
                "event_business_id": e.event_business_id,
                "event_type": e.event_type,
                "channel": e.channel,
                "event_timestamp": ensure_utc(e.event_timestamp).isoformat(),
                "ingestion_timestamp": ensure_utc(e.ingestion_timestamp).isoformat(),
                "late_arrival_detected": bool((e.properties or {}).get("late_arrival_detected")),
                "ingestion_lag_seconds": (e.properties or {}).get("ingestion_lag_seconds", 0),
                "client_instance_id": e.client_instance_id,
                "anonymous_id": e.anonymous_id,
                "properties": e.properties,
            }
            for e in events
        ],
        "carts": [
            {
                "cart_business_id": c.cart_business_id,
                "status": c.status,
                "created_at": ensure_utc(c.created_at).isoformat(),
                "updated_at": ensure_utc(c.updated_at).isoformat(),
                "abandoned_at": ensure_utc(c.abandoned_at).isoformat() if c.abandoned_at else None,
                "converted_at": ensure_utc(c.converted_at).isoformat() if c.converted_at else None,
                "items_count": sum(ci.quantity for ci in cart_items if ci.cart_id == c.cart_id),
            }
            for c in carts
        ],
        "orders": [
            {
                "order_id": str(o.order_id),
                "order_business_id": o.order_business_id,
                "purchase_source": o.purchase_source,
                "source_purchase_reference": o.source_purchase_reference,
                "status": o.status,
                "subtotal": float(o.subtotal),
                "discount_amount": float(o.discount_amount),
                "tax_amount": float(o.tax_amount),
                "shipping_amount": float(o.shipping_amount),
                "total_amount": float(o.total_amount),
                "created_at": ensure_utc(o.created_at).isoformat(),
                "purchase_confirmed_at": ensure_utc(o.purchase_confirmed_at).isoformat() if o.purchase_confirmed_at else None,
            }
            for o in orders
        ],
        "payment_attempts": [
            {
                "payment_attempt_business_id": pa.payment_attempt_business_id,
                "attempt_number": pa.attempt_number,
                "amount": float(pa.amount),
                "status": pa.status,
                "failure_reason": pa.failure_reason,
                "provider_reference": pa.provider_reference,
                "attempted_at": ensure_utc(pa.attempted_at).isoformat(),
            }
            for pa in payment_attempts
        ],
        "support_tickets": [
            {
                "ticket_business_id": t.ticket_business_id,
                "issue_type": t.issue_type,
                "priority": t.priority,
                "status": t.status,
                "origin_channel": t.origin_channel,
                "support_channel": t.support_channel,
                "created_at": ensure_utc(t.created_at).isoformat(),
                "resolved_at": ensure_utc(t.resolved_at).isoformat() if t.resolved_at else None,
                "events": [
                    {
                        "ticket_event_business_id": te.ticket_event_business_id,
                        "previous_status": te.previous_status,
                        "new_status": te.new_status,
                        "actor_type": te.actor_type,
                        "event_timestamp": ensure_utc(te.event_timestamp).isoformat(),
                        "notes": te.notes,
                    }
                    for te in ticket_events
                    if te.ticket_id == t.ticket_id
                ],
            }
            for t in tickets
        ],
    }


def get_exceptions_summary(db: Session) -> dict[str, Any]:
    """
    Summarize and list all 4 mandatory anomalies (ANOM-001 through ANOM-004) and quarantined records:
    - ANOM-001: Late-arriving events reconstructed by event_timestamp.
    - ANOM-002: Unauthorized / conflicting identity merges blocked & quarantined.
    - ANOM-003: Duplicate conversion signals deduplicated against authoritative order reference.
    - ANOM-004: Bot traffic sessions classified & excluded from human analytical denominators.
    """
    quarantines = list(db.scalars(select(EventQuarantine)).all())
    quarantines.sort(key=lambda q: ensure_utc(q.detected_at), reverse=True)

    events = list(db.scalars(select(JourneyEvent)).all())
    late_events = [
        e for e in events if (e.properties or {}).get("late_arrival_detected")
    ]
    late_events.sort(
        key=lambda e: float((e.properties or {}).get("ingestion_lag_seconds", 0)),
        reverse=True,
    )

    conflicted_links = list(
        db.scalars(select(IdentityLink).where(IdentityLink.status == "CONFLICTED")).all()
    )
    identity_quarantines = [
        q for q in quarantines if q.issue_type in ("IDENTITY_CONFLICT", "UNAUTHORIZED_IDENTITY_MERGE")
    ]

    duplicate_audits = list(
        db.scalars(
            select(AuditEvent).where(AuditEvent.action == "DUPLICATE_CONVERSION_SIGNAL_SUPPRESSED")
        ).all()
    )

    bot_sessions = list(
        db.scalars(select(SessionRecord).where(SessionRecord.traffic_classification == "BOT")).all()
    )

    # Check if ground_truth_anomalies.json exists to show verification match
    gt_path = (
        Path(__file__).resolve().parents[3]
        / "data-generation"
        / "output"
        / "ground_truth_anomalies.json"
    )
    ground_truth: dict[str, Any] | None = None
    if gt_path.exists():
        try:
            ground_truth = json.loads(gt_path.read_text(encoding="utf-8"))
        except Exception:
            ground_truth = None

    return {
        "summary_counts": {
            "total_quarantined": len(quarantines),
            "open_quarantined": sum(1 for q in quarantines if q.resolution_status == "OPEN"),
            "anom_001_late_events": len(late_events),
            "anom_002_identity_exceptions": len(identity_quarantines),
            "anom_002_conflicted_links": len(conflicted_links),
            "anom_003_duplicate_conversions_suppressed": len(duplicate_audits),
            "anom_004_bot_sessions_isolated": len(bot_sessions),
        },
        "ground_truth_verification": ground_truth,
        "quarantined_items": [
            {
                "quarantine_id": str(q.quarantine_id),
                "quarantine_business_id": q.quarantine_business_id,
                "event_id": str(q.event_id) if q.event_id else None,
                "issue_type": q.issue_type,
                "reason": q.reason,
                "detected_at": ensure_utc(q.detected_at).isoformat(),
                "resolution_status": q.resolution_status,
                "resolution_notes": q.resolution_notes,
            }
            for q in quarantines
        ],
        "late_events": [
            {
                "event_business_id": e.event_business_id,
                "event_type": e.event_type,
                "channel": e.channel,
                "event_timestamp": ensure_utc(e.event_timestamp).isoformat(),
                "ingestion_timestamp": ensure_utc(e.ingestion_timestamp).isoformat(),
                "lag_seconds": (e.properties or {}).get("ingestion_lag_seconds", 0),
                "session_id": str(e.session_id) if e.session_id else None,
            }
            for e in late_events[:50]
        ],
        "duplicate_signals": [
            {
                "audit_business_id": a.audit_business_id,
                "order_id": str(a.entity_id),
                "occurred_at": ensure_utc(a.occurred_at).isoformat(),
                "details": a.new_state,
            }
            for a in duplicate_audits
        ],
        "bot_sessions": [
            {
                "session_business_id": s.session_business_id,
                "channel": s.channel,
                "client_instance_id": s.client_instance_id,
                "anonymous_id": s.anonymous_id,
                "started_at": ensure_utc(s.started_at).isoformat(),
                "classification_reason": s.classification_reason,
            }
            for s in bot_sessions[:50]
        ],
    }


def get_experiments_summary(db: Session) -> list[dict[str, Any]]:
    """
    Compute Experiment Analytics (EXP-001 through EXP-010):
    - Separate Assigned subjects vs. Exposed subjects (EXP-007, EXP-008).
    - Compute conversion rate and revenue per variant for both Assigned and Exposed populations.
    - Calculate lift % vs. Control variant.
    """
    experiments = list(db.scalars(select(Experiment)).all())
    variants = list(db.scalars(select(ExperimentVariant)).all())
    assignments = list(db.scalars(select(ExperimentAssignment)).all())
    exposures = list(db.scalars(select(ExperimentExposure)).all())
    orders = list(db.scalars(select(Order).where(Order.status == "CONFIRMED")).all())
    sessions = list(db.scalars(select(SessionRecord)).all())

    # Map customer_id and anonymous_id to confirmed orders
    orders_by_customer: dict[str, list[Order]] = {}
    Session_by_id = {s.session_id: s for s in sessions}
    orders_by_anon: dict[str, list[Order]] = {}

    for o in orders:
        if o.customer_id:
            orders_by_customer.setdefault(str(o.customer_id), []).append(o)
        if o.session_id and o.session_id in Session_by_id:
            anon = Session_by_id[o.session_id].anonymous_id
            if anon:
                orders_by_anon.setdefault(anon, []).append(o)

    exposed_assignment_ids = {ex.assignment_id for ex in exposures}

    result: list[dict[str, Any]] = []
    for exp in experiments:
        exp_variants = [v for v in variants if v.experiment_id == exp.experiment_id]
        exp_variants.sort(key=lambda v: (not v.is_control, v.variant_name))

        exp_assignments = [a for a in assignments if a.experiment_id == exp.experiment_id]

        variant_rows: list[dict[str, Any]] = []
        control_exposed_conv_rate: float | None = None

        for v in exp_variants:
            v_assigns = [a for a in exp_assignments if a.variant_id == v.variant_id]
            v_exposed = [a for a in v_assigns if a.assignment_id in exposed_assignment_ids]

            assigned_converted = 0
            assigned_revenue = Decimal("0.00")
            exposed_converted = 0
            exposed_revenue = Decimal("0.00")

            for a in v_assigns:
                subj_orders = (
                    orders_by_customer.get(a.assignment_identity_id, [])
                    if a.assignment_identity_type == "CUSTOMER"
                    else orders_by_anon.get(a.assignment_identity_id, [])
                )
                # Filter orders confirmed on or after assignment
                valid_orders = [
                    o
                    for o in subj_orders
                    if o.purchase_confirmed_at
                    and ensure_utc(o.purchase_confirmed_at) >= ensure_utc(a.assigned_at)
                ]
                if valid_orders:
                    assigned_converted += 1
                    rev = sum((o.total_amount for o in valid_orders), Decimal("0.00"))
                    assigned_revenue += rev
                    if a.assignment_id in exposed_assignment_ids:
                        exposed_converted += 1
                        exposed_revenue += rev

            assign_cnt = len(v_assigns)
            exp_cnt = len(v_exposed)
            assign_conv_rate = round((assigned_converted / assign_cnt) * 100.0, 2) if assign_cnt > 0 else 0.0
            exposed_conv_rate = round((exposed_converted / exp_cnt) * 100.0, 2) if exp_cnt > 0 else 0.0

            if v.is_control:
                control_exposed_conv_rate = exposed_conv_rate

            variant_rows.append(
                {
                    "variant_id": str(v.variant_id),
                    "variant_business_id": v.variant_business_id,
                    "variant_name": v.variant_name,
                    "is_control": v.is_control,
                    "allocation_percentage": float(v.allocation_percentage),
                    "assigned_count": assign_cnt,
                    "exposed_count": exp_cnt,
                    "unexposed_count": assign_cnt - exp_cnt,
                    "exposure_rate_pct": round((exp_cnt / assign_cnt) * 100.0, 2) if assign_cnt > 0 else 0.0,
                    "assigned_conversions": assigned_converted,
                    "assigned_conversion_rate_pct": assign_conv_rate,
                    "assigned_revenue": float(assigned_revenue),
                    "exposed_conversions": exposed_converted,
                    "exposed_conversion_rate_pct": exposed_conv_rate,
                    "exposed_revenue": float(exposed_revenue),
                    "lift_vs_control_pct": None,
                }
            )

        for vr in variant_rows:
            if not vr["is_control"] and control_exposed_conv_rate and control_exposed_conv_rate > 0:
                vr["lift_vs_control_pct"] = round(
                    ((vr["exposed_conversion_rate_pct"] - control_exposed_conv_rate) / control_exposed_conv_rate)
                    * 100.0,
                    2,
                )
            elif vr["is_control"]:
                vr["lift_vs_control_pct"] = 0.0

        result.append(
            {
                "experiment_id": str(exp.experiment_id),
                "experiment_business_id": exp.experiment_business_id,
                "name": exp.name,
                "description": exp.description,
                "status": exp.status,
                "primary_metric": exp.primary_metric,
                "config_version": exp.config_version,
                "experiment_salt": exp.experiment_salt,
                "start_at": ensure_utc(exp.start_at).isoformat() if exp.start_at else None,
                "variants": variant_rows,
            }
        )

    return result
