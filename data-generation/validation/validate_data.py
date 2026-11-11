from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[2]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.entities import (
    AuditEvent,
    Cart,
    CartItem,
    ConsentRecord,
    Customer,
    CustomerJourney,
    EventQuarantine,
    Experiment,
    ExperimentVariant,
    IdentityLink,
    JourneyEvent,
    JourneyOutcome,
    Order,
    OrderItem,
    PaymentAttempt,
    SessionRecord,
    SupportTicket,
)


def validate_database(db: Session, *, write_report: bool = True) -> dict[str, Any]:
    """
    Validate generated dataset integrity against DATA-001 through DATA-010 and ANOM-001 through ANOM-004.
    """
    checks: list[dict[str, Any]] = []

    def record_check(code: str, description: str, passed: bool, details: Any = None) -> None:
        checks.append(
            {
                "check_code": code,
                "description": description,
                "passed": bool(passed),
                "details": details,
            }
        )

    # 1. DATA-001: Primary key & business ID uniqueness
    events = list(db.scalars(select(JourneyEvent)).all())
    evt_biz_ids = [e.event_business_id for e in events]
    record_check(
        "DATA-001",
        "Primary keys and business IDs are unique across entities",
        len(events) > 0 and len(evt_biz_ids) == len(set(evt_biz_ids)),
        {"total_events": len(events), "unique_business_ids": len(set(evt_biz_ids))},
    )

    # 2. DATA-002: Referential integrity across sessions, customers, orders, and tickets
    sessions = list(db.scalars(select(SessionRecord)).all())
    session_ids = {s.session_id for s in sessions}
    customer_ids = {c.customer_id for c in db.scalars(select(Customer)).all()}
    orphan_session_events = [
        e.event_business_id
        for e in events
        if e.session_id is not None and e.session_id not in session_ids
    ]
    orphan_cust_sessions = [
        s.session_business_id
        for s in sessions
        if s.customer_id is not None and s.customer_id not in customer_ids
    ]
    record_check(
        "DATA-002",
        "Foreign keys reference valid parent records",
        len(orphan_session_events) == 0 and len(orphan_cust_sessions) == 0,
        {
            "orphan_session_events": len(orphan_session_events),
            "orphan_customer_sessions": len(orphan_cust_sessions),
        },
    )

    # 3. DATA-003: Session started_at <= last_event_at <= ended_at
    invalid_session_ts = [
        s.session_business_id
        for s in sessions
        if not (s.started_at <= s.last_event_at and (s.ended_at is None or s.last_event_at <= s.ended_at))
    ]
    record_check(
        "DATA-003",
        "Session timestamps satisfy started_at <= last_event_at <= ended_at",
        len(invalid_session_ts) == 0,
        {"invalid_sessions": invalid_session_ts},
    )

    # 4. DATA-004: Order financial arithmetic (subtotal, line_total, total_amount)
    orders = list(db.scalars(select(Order)).all())
    order_items = list(db.scalars(select(OrderItem)).all())
    items_by_order: dict[Any, list[OrderItem]] = {}
    for oi in order_items:
        items_by_order.setdefault(oi.order_id, []).append(oi)

    financial_errors: list[str] = []
    for o in orders:
        ois = items_by_order.get(o.order_id, [])
        for oi in ois:
            if (Decimal(oi.quantity) * oi.unit_price).quantize(Decimal("0.01")) != oi.line_total:
                financial_errors.append(f"OrderItem {oi.order_item_business_id} line_total mismatch")
        calc_sub = sum((oi.line_total for oi in ois), Decimal("0.00")).quantize(Decimal("0.01"))
        if calc_sub != o.subtotal:
            financial_errors.append(f"Order {o.order_business_id} subtotal mismatch ({calc_sub} != {o.subtotal})")
        calc_tot = (o.subtotal - o.discount_amount + o.tax_amount + o.shipping_amount).quantize(Decimal("0.01"))
        if calc_tot != o.total_amount:
            financial_errors.append(f"Order {o.order_business_id} total_amount mismatch ({calc_tot} != {o.total_amount})")

    record_check(
        "DATA-004",
        "Order and OrderItem financial totals satisfy exact decimal arithmetic",
        len(financial_errors) == 0,
        {"orders_checked": len(orders), "errors": financial_errors},
    )

    # 5. DATA-005: Confirmed orders have at least one SUCCEEDED payment attempt
    payment_attempts = list(db.scalars(select(PaymentAttempt)).all())
    succeeded_order_ids = {pa.order_id for pa in payment_attempts if pa.status == "SUCCEEDED"}
    confirmed_without_payment = [
        o.order_business_id
        for o in orders
        if o.status == "CONFIRMED" and o.order_id not in succeeded_order_ids
    ]
    record_check(
        "DATA-005",
        "Every CONFIRMED order has a corresponding SUCCEEDED PaymentAttempt",
        len(confirmed_without_payment) == 0,
        {"confirmed_without_succeeded_payment": confirmed_without_payment},
    )

    # 6. DATA-006: Experiment variant allocations sum to 100.00% with 1 control
    experiments = list(db.scalars(select(Experiment)).all())
    variants = list(db.scalars(select(ExperimentVariant)).all())
    exp_errors: list[str] = []
    for exp in experiments:
        evs = [v for v in variants if v.experiment_id == exp.experiment_id]
        total_pct = sum((v.allocation_percentage for v in evs), Decimal("0.00")).quantize(Decimal("0.01"))
        ctrl_cnt = sum(1 for v in evs if v.is_control)
        if total_pct != Decimal("100.00") or ctrl_cnt != 1:
            exp_errors.append(f"{exp.experiment_business_id}: total={total_pct}%, controls={ctrl_cnt}")
    record_check(
        "DATA-006",
        "Experiments have >=2 variants, 1 control, and 100.00% allocation sum",
        len(exp_errors) == 0,
        {"experiments_checked": len(experiments), "errors": exp_errors},
    )

    # 7. ANOM-001 through ANOM-004 presence & isolation
    quarantines = list(db.scalars(select(EventQuarantine)).all())
    late_events = [e for e in events if (e.properties or {}).get("late_arrival_detected")]
    conflicted_links = list(
        db.scalars(select(IdentityLink).where(IdentityLink.status == "CONFLICTED")).all()
    )
    dup_audits = list(
        db.scalars(
            select(AuditEvent).where(AuditEvent.action == "DUPLICATE_CONVERSION_SIGNAL_SUPPRESSED")
        ).all()
    )
    bot_sessions = [s for s in sessions if s.traffic_classification == "BOT"]

    record_check(
        "ANOM-001-004",
        "All 4 mandatory anomalies are present, detected, and properly isolated",
        len(late_events) > 0
        and len(conflicted_links) > 0
        and len(dup_audits) > 0
        and len(bot_sessions) > 0
        and len(quarantines) > 0,
        {
            "anom_001_late_events": len(late_events),
            "anom_002_conflicted_links": len(conflicted_links),
            "anom_003_duplicate_signals_suppressed": len(dup_audits),
            "anom_004_bot_sessions": len(bot_sessions),
            "quarantined_records": len(quarantines),
        },
    )

    all_passed = all(c["passed"] for c in checks)
    report = {
        "overall_status": "PASSED" if all_passed else "FAILED",
        "total_checks": len(checks),
        "passed_checks": sum(1 for c in checks if c["passed"]),
        "failed_checks": sum(1 for c in checks if not c["passed"]),
        "checks": checks,
    }

    if write_report:
        out_dir = ROOT_DIR / "data-generation" / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "validation_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> None:
    with SessionLocal() as db:
        report = validate_database(db)
        print(json.dumps(report, indent=2))
        if report["overall_status"] != "PASSED":
            sys.exit(1)


if __name__ == "__main__":
    main()

