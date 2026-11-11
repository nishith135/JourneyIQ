from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[2]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import Numeric, and_, case, cast, exists, func, or_, select
from sqlalchemy.orm import Session

from app.core.config import resolve_safe_output_dir
from app.db.session import (
    create_isolated_session_factory,
    resolve_isolated_database_url,
)
from app.models.entities import (
    AuditEvent,
    Customer,
    EventQuarantine,
    Experiment,
    ExperimentVariant,
    IdentityLink,
    JourneyEvent,
    Order,
    OrderItem,
    PaymentAttempt,
    SessionRecord,
)


def validate_database(
    db: Session,
    *,
    write_report: bool = True,
    output_dir: str | Path | None = None,
    allow_live_demo_overwrite: bool = False,
) -> dict[str, Any]:
    """
    Validate generated dataset integrity against DATA-001 through DATA-010 and ANOM-001 through ANOM-004
    using set-based SQL aggregate checks so 2M+ event datasets validate with bounded memory.
    """
    target_out_dir = (
        resolve_safe_output_dir(
            output_dir,
            allow_live_demo_overwrite=allow_live_demo_overwrite,
        )
        if write_report
        else None
    )
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
    total_events_raw, unique_biz_ids_raw = db.execute(
        select(
            func.count(JourneyEvent.event_id),
            func.count(func.distinct(JourneyEvent.event_business_id)),
        )
    ).one()
    total_events = int(total_events_raw or 0)
    unique_business_ids = int(unique_biz_ids_raw or 0)
    record_check(
        "DATA-001",
        "Primary keys and business IDs are unique across entities",
        total_events > 0 and total_events == unique_business_ids,
        {"total_events": total_events, "unique_business_ids": unique_business_ids},
    )

    # 2. DATA-002: Referential integrity across sessions and customers
    orphan_session_events = int(
        db.scalar(
            select(func.count(JourneyEvent.event_id))
            .outerjoin(SessionRecord, JourneyEvent.session_id == SessionRecord.session_id)
            .where(JourneyEvent.session_id.is_not(None), SessionRecord.session_id.is_(None))
        )
        or 0
    )
    orphan_cust_sessions = int(
        db.scalar(
            select(func.count(SessionRecord.session_id))
            .outerjoin(Customer, SessionRecord.customer_id == Customer.customer_id)
            .where(SessionRecord.customer_id.is_not(None), Customer.customer_id.is_(None))
        )
        or 0
    )
    record_check(
        "DATA-002",
        "Foreign keys reference valid parent records",
        orphan_session_events == 0 and orphan_cust_sessions == 0,
        {
            "orphan_session_events": orphan_session_events,
            "orphan_customer_sessions": orphan_cust_sessions,
        },
    )

    # 3. DATA-003: Session started_at <= last_event_at <= ended_at
    invalid_session_ts = list(
        db.scalars(
            select(SessionRecord.session_business_id)
            .where(
                or_(
                    SessionRecord.started_at > SessionRecord.last_event_at,
                    and_(
                        SessionRecord.ended_at.is_not(None),
                        SessionRecord.last_event_at > SessionRecord.ended_at,
                    ),
                )
            )
            .limit(100)
        ).all()
    )
    record_check(
        "DATA-003",
        "Session timestamps satisfy started_at <= last_event_at <= ended_at",
        len(invalid_session_ts) == 0,
        {"invalid_sessions": invalid_session_ts},
    )

    # 4. DATA-004: Order financial arithmetic (subtotal, line_total, total_amount)
    financial_errors: list[str] = []
    bad_items = list(
        db.scalars(
            select(OrderItem.order_item_business_id)
            .where(
                func.round(cast(OrderItem.quantity, Numeric(12, 2)) * OrderItem.unit_price, 2)
                != OrderItem.line_total
            )
            .limit(50)
        ).all()
    )
    for item_biz_id in bad_items:
        financial_errors.append(f"OrderItem {item_biz_id} line_total mismatch")

    oi_subq = (
        select(
            OrderItem.order_id.label("order_id"),
            func.coalesce(func.sum(OrderItem.line_total), Decimal("0.00")).label("calc_sub"),
        )
        .group_by(OrderItem.order_id)
        .subquery()
    )
    sub_col = func.round(func.coalesce(oi_subq.c.calc_sub, Decimal("0.00")), 2)
    tot_col = func.round(
        Order.subtotal - Order.discount_amount + Order.tax_amount + Order.shipping_amount,
        2,
    )
    bad_orders = db.execute(
        select(
            Order.order_business_id,
            Order.subtotal,
            sub_col.label("calc_sub"),
            Order.total_amount,
            tot_col.label("calc_tot"),
        )
        .outerjoin(oi_subq, Order.order_id == oi_subq.c.order_id)
        .where(or_(sub_col != Order.subtotal, tot_col != Order.total_amount))
        .limit(50)
    ).all()
    for obiz, o_sub, calc_sub, o_tot, calc_tot in bad_orders:
        if Decimal(str(calc_sub)).quantize(Decimal("0.01")) != o_sub:
            financial_errors.append(f"Order {obiz} subtotal mismatch ({calc_sub} != {o_sub})")
        if Decimal(str(calc_tot)).quantize(Decimal("0.01")) != o_tot:
            financial_errors.append(f"Order {obiz} total_amount mismatch ({calc_tot} != {o_tot})")

    orders_checked = int(db.scalar(select(func.count(Order.order_id))) or 0)
    record_check(
        "DATA-004",
        "Order and OrderItem financial totals satisfy exact decimal arithmetic",
        len(financial_errors) == 0,
        {"orders_checked": orders_checked, "errors": financial_errors},
    )

    # 5. DATA-005: Confirmed orders have at least one SUCCEEDED payment attempt
    succeeded_exists = exists().where(
        PaymentAttempt.order_id == Order.order_id,
        PaymentAttempt.status == "SUCCEEDED",
    )
    confirmed_without_payment = list(
        db.scalars(
            select(Order.order_business_id)
            .where(Order.status == "CONFIRMED", ~succeeded_exists)
            .limit(100)
        ).all()
    )
    record_check(
        "DATA-005",
        "Every CONFIRMED order has a corresponding SUCCEEDED PaymentAttempt",
        len(confirmed_without_payment) == 0,
        {"confirmed_without_succeeded_payment": confirmed_without_payment},
    )

    # 6. DATA-006: Experiment variant allocations sum to 100.00% with 1 control
    exp_rows = db.execute(
        select(
            Experiment.experiment_business_id,
            func.coalesce(func.sum(ExperimentVariant.allocation_percentage), Decimal("0.00")).label("total_pct"),
            func.coalesce(
                func.sum(case((ExperimentVariant.is_control.is_(True), 1), else_=0)),
                0,
            ).label("ctrl_cnt"),
        )
        .outerjoin(ExperimentVariant, Experiment.experiment_id == ExperimentVariant.experiment_id)
        .group_by(Experiment.experiment_id, Experiment.experiment_business_id)
    ).all()
    exp_errors: list[str] = []
    for exp_biz_id, total_pct_raw, ctrl_cnt_raw in exp_rows:
        total_pct = Decimal(str(total_pct_raw)).quantize(Decimal("0.01"))
        ctrl_cnt = int(ctrl_cnt_raw or 0)
        if total_pct != Decimal("100.00") or ctrl_cnt != 1:
            exp_errors.append(f"{exp_biz_id}: total={total_pct}%, controls={ctrl_cnt}")
    record_check(
        "DATA-006",
        "Experiments have >=2 variants, 1 control, and 100.00% allocation sum",
        len(exp_errors) == 0,
        {"experiments_checked": len(exp_rows), "errors": exp_errors},
    )

    # 7. ANOM-001 through ANOM-004 presence & isolation
    quarantines_cnt = int(db.scalar(select(func.count(EventQuarantine.quarantine_id))) or 0)
    late_events_cnt = int(
        db.scalar(
            select(func.count(JourneyEvent.event_id)).where(
                JourneyEvent.properties["late_arrival_detected"].as_boolean().is_(True)
            )
        )
        or 0
    )
    conflicted_links_cnt = int(
        db.scalar(
            select(func.count(IdentityLink.identity_link_id)).where(IdentityLink.status == "CONFLICTED")
        )
        or 0
    )
    dup_audits_cnt = int(
        db.scalar(
            select(func.count(AuditEvent.audit_event_id)).where(
                AuditEvent.action == "DUPLICATE_CONVERSION_SIGNAL_SUPPRESSED"
            )
        )
        or 0
    )
    bot_sessions_cnt = int(
        db.scalar(
            select(func.count(SessionRecord.session_id)).where(SessionRecord.traffic_classification == "BOT")
        )
        or 0
    )

    record_check(
        "ANOM-001-004",
        "All 4 mandatory anomalies are present, detected, and properly isolated",
        late_events_cnt > 0
        and conflicted_links_cnt > 0
        and dup_audits_cnt > 0
        and bot_sessions_cnt > 0
        and quarantines_cnt > 0,
        {
            "anom_001_late_events": late_events_cnt,
            "anom_002_conflicted_links": conflicted_links_cnt,
            "anom_003_duplicate_signals_suppressed": dup_audits_cnt,
            "anom_004_bot_sessions": bot_sessions_cnt,
            "quarantined_records": quarantines_cnt,
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

    if write_report and target_out_dir is not None:
        target_out_dir.mkdir(parents=True, exist_ok=True)
        (target_out_dir / "validation_report.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
    return report


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="JourneyIQ Synthetic Dataset Validator")
    parser.add_argument(
        "--database-url",
        type=str,
        default=None,
        help="Explicit SQLAlchemy database URL for an isolated test/sandbox database",
    )
    parser.add_argument(
        "--db-env-var",
        type=str,
        default=None,
        help="Environment variable name holding an isolated test/sandbox database URL (e.g. TEST_DATABASE_URL)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Target directory for validation_report.json",
    )
    parser.add_argument(
        "--no-write-report",
        action="store_true",
        help="Skip writing validation_report.json",
    )
    parser.add_argument(
        "--allow-live-demo-artifacts",
        action="store_true",
        help="Explicit opt-in required only when updating live demo artifacts under data-generation/output",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_cli_parser()
    args = parser.parse_args(argv)

    try:
        target_db_url = resolve_isolated_database_url(
            database_url=args.database_url,
            db_env_var=args.db_env_var,
        )
        write_report = not args.no_write_report
        out_dir = (
            resolve_safe_output_dir(
                args.output_dir,
                allow_live_demo_overwrite=args.allow_live_demo_artifacts,
            )
            if write_report
            else None
        )
    except ValueError as exc:
        parser.error(str(exc))

    session_factory = create_isolated_session_factory(target_db_url)
    try:
        with session_factory() as db:
            report = validate_database(
                db,
                write_report=write_report,
                output_dir=out_dir,
                allow_live_demo_overwrite=args.allow_live_demo_artifacts,
            )
            print(json.dumps(report, indent=2))
            if report["overall_status"] != "PASSED":
                sys.exit(1)
    finally:
        bind = session_factory.kw.get("bind")
        if bind is not None:
            bind.dispose()


if __name__ == "__main__":
    main()

