from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

# Add backend to sys.path
ROOT_DIR = Path(__file__).resolve().parents[2]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import JSON, func, inspect as sa_inspect, insert as sa_insert, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.core.config import resolve_safe_output_dir
from app.db.session import (
    Base,
    create_isolated_session_factory,
    resolve_isolated_database_url,
)
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
    Refund,
    SessionRecord,
    SupportTicket,
    SupportTicketEvent,
)
from app.services.domain_service import (
    add_or_update_cart_item,
    assign_experiment_variant,
    calculate_journey_outcome,
    calculate_session_outcome,
    compute_deterministic_bucket,
    create_or_get_authoritative_order,
    evaluate_cart_abandonment,
    ingest_event,
    record_experiment_exposure,
    record_payment_attempt,
    resolve_identity_link,
    sessionize_events,
    transition_support_ticket,
    validate_and_activate_experiment,
    validate_checkout_start,
)

AS_OF_DEFAULT = datetime(2026, 11, 10, 0, 0, 0, tzinfo=timezone.utc)
FULL_SCALE_DEFAULT_JOURNEYS = 98_000
FULL_SCALE_DEFAULT_CHUNK_SIZE = 2_000

CATALOG_SPECS: list[tuple[str, str, str, Decimal]] = [
    ("PRD-001", "AeroPulse Wireless ANC Headphones", "Audio", Decimal("249.00")),
    ("PRD-002", "Chronos Pro Smart Fitness Watch", "Wearables", Decimal("329.00")),
    ("PRD-003", "LumaDesk 4K UltraWide Monitor", "Displays", Decimal("599.00")),
    ("PRD-004", "KeyCraft Mechanical Tactile Keyboard", "Peripherals", Decimal("149.00")),
    ("PRD-005", "GlidePro Ergonomic Vertical Mouse", "Peripherals", Decimal("89.00")),
    ("PRD-006", "ThunderDock USB-C 12-in-1 Hub", "Accessories", Decimal("129.00")),
    ("PRD-007", "SonicBeam Compact Desktop Soundbar", "Audio", Decimal("179.00")),
    ("PRD-008", "VaultSSD 2TB NVMe Portable Drive", "Storage", Decimal("199.00")),
    ("PRD-009", "ClearCast 4K AI Webcam", "Video", Decimal("159.00")),
    ("PRD-010", "PowerCore 140W GaN Travel Charger", "Accessories", Decimal("79.00")),
]


class DeterministicUUIDFactory:
    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)

    def next_uuid(self) -> uuid.UUID:
        return uuid.UUID(int=self.rng.getrandbits(128), version=4)


def reset_database(db: Session) -> None:
    Base.metadata.create_all(bind=db.get_bind())
    tables = [
        "audit_event",
        "event_quarantine",
        "experiment_exposure",
        "experiment_assignment",
        "experiment_variant",
        "experiment",
        "journey_outcome",
        "journey_event",
        "support_ticket_event",
        "support_ticket",
        "refund",
        "payment_attempt",
        "order_item",
        "orders",
        "cart_item",
        "cart",
        "identity_link",
        "consent_record",
        "session",
        "customer_journey",
        "product",
        "customer",
    ]
    db.execute(text(f"TRUNCATE TABLE {', '.join(tables)} RESTART IDENTITY CASCADE"))
    db.commit()


def parse_as_of_timestamp(raw_value: str | None) -> datetime:
    """Parse optional ISO-8601 CLI as-of timestamp into timezone-aware UTC datetime."""
    if not raw_value or not raw_value.strip():
        return AS_OF_DEFAULT
    normalized = raw_value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _bulk_insert_table(db: Session, model_cls: type[Base], rows: list[dict[str, Any]]) -> None:
    """Insert batch rows using PostgreSQL COPY FROM STDIN (or Core multi-row insert fallback)."""
    if not rows:
        return

    bind = db.get_bind()
    mapper = sa_inspect(model_cls)
    col_specs = [
        (
            attr.key,
            attr.columns[0].name,
            isinstance(attr.columns[0].type, (JSON, JSONB)),
        )
        for attr in mapper.column_attrs
    ]

    if bind.dialect.name == "postgresql":
        from psycopg import sql

        sa_conn = db.connection()
        raw_driver_conn = sa_conn.connection.driver_connection
        copy_stmt = sql.SQL("COPY public.{tbl} ({cols}) FROM STDIN").format(
            tbl=sql.Identifier(model_cls.__tablename__),
            cols=sql.SQL(", ").join(sql.Identifier(col_name) for _, col_name, _ in col_specs),
        )
        with raw_driver_conn.cursor() as cur:
            with cur.copy(copy_stmt) as copy:
                for row in rows:
                    values = []
                    for key, _, is_json in col_specs:
                        val = row.get(key)
                        if val is not None and is_json:
                            val = json.dumps(val, sort_keys=True)
                        values.append(val)
                    copy.write_row(values)
    else:
        db.execute(sa_insert(model_cls), rows)


def _compute_streaming_sha256(db: Session) -> str:
    """Compute a deterministic SHA-256 digest over large tables without loading all rows into memory."""
    hasher = hashlib.sha256()

    hasher.update(b"EVENTS\n")
    for eid in db.scalars(
        select(JourneyEvent.event_business_id)
        .order_by(JourneyEvent.event_business_id)
        .execution_options(yield_per=50_000)
    ):
        hasher.update(eid.encode("utf-8"))
        hasher.update(b"\n")

    hasher.update(b"SESSIONS\n")
    for sid in db.scalars(
        select(SessionRecord.session_business_id)
        .order_by(SessionRecord.session_business_id)
        .execution_options(yield_per=50_000)
    ):
        hasher.update(sid.encode("utf-8"))
        hasher.update(b"\n")

    hasher.update(b"ORDERS\n")
    for oid in db.scalars(
        select(Order.order_business_id)
        .order_by(Order.order_business_id)
        .execution_options(yield_per=50_000)
    ):
        hasher.update(oid.encode("utf-8"))
        hasher.update(b"\n")

    hasher.update(b"OUTCOMES\n")
    for obiz, ostatus, oattr, oflags in db.execute(
        select(
            JourneyOutcome.outcome_business_id,
            JourneyOutcome.outcome_status,
            JourneyOutcome.attribution_status,
            JourneyOutcome.friction_flags,
        )
        .order_by(JourneyOutcome.outcome_business_id)
        .execution_options(yield_per=50_000)
    ):
        flags_str = ",".join(oflags or [])
        hasher.update(f"{obiz}:{ostatus}:{oattr}:{flags_str}\n".encode("utf-8"))

    return hasher.hexdigest()


def _collect_manifest_counts(db: Session) -> dict[str, int]:
    """Collect entity row counts using aggregate queries."""
    entity_models: list[tuple[str, type[Base], Any]] = [
        ("customers", Customer, Customer.customer_id),
        ("products", Product, Product.product_id),
        ("journeys", CustomerJourney, CustomerJourney.journey_id),
        ("sessions", SessionRecord, SessionRecord.session_id),
        ("events", JourneyEvent, JourneyEvent.event_id),
        ("carts", Cart, Cart.cart_id),
        ("orders", Order, Order.order_id),
        ("payment_attempts", PaymentAttempt, PaymentAttempt.payment_attempt_id),
        ("support_tickets", SupportTicket, SupportTicket.ticket_id),
        ("consents", ConsentRecord, ConsentRecord.consent_record_id),
        ("identity_links", IdentityLink, IdentityLink.identity_link_id),
        ("experiments", Experiment, Experiment.experiment_id),
        ("experiment_assignments", ExperimentAssignment, ExperimentAssignment.assignment_id),
        ("experiment_exposures", ExperimentExposure, ExperimentExposure.exposure_id),
        ("quarantined_events", EventQuarantine, EventQuarantine.quarantine_id),
        ("journey_outcomes", JourneyOutcome, JourneyOutcome.outcome_id),
        ("audit_events", AuditEvent, AuditEvent.audit_event_id),
    ]
    return {
        label: int(db.scalar(select(func.count(pk_col))) or 0)
        for label, _, pk_col in entity_models
    }


def _export_dataset_csv(db: Session, target_out_dir: Path) -> None:
    """Stream entity tables to CSV files inside <output_dir>/csv/ without materializing full tables in RAM."""
    import csv

    csv_dir = target_out_dir / "csv"
    csv_dir.mkdir(parents=True, exist_ok=True)
    export_models: list[type[Base]] = [
        Customer,
        Product,
        CustomerJourney,
        SessionRecord,
        JourneyEvent,
        Cart,
        CartItem,
        Order,
        OrderItem,
        PaymentAttempt,
        SupportTicket,
        SupportTicketEvent,
        ConsentRecord,
        IdentityLink,
        Experiment,
        ExperimentVariant,
        ExperimentAssignment,
        ExperimentExposure,
        EventQuarantine,
        JourneyOutcome,
        AuditEvent,
    ]
    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        from psycopg import sql

        sa_conn = db.connection()
        raw_driver_conn = sa_conn.connection.driver_connection
        with raw_driver_conn.cursor() as cur:
            for model_cls in export_models:
                out_file = csv_dir / f"{model_cls.__tablename__}.csv"
                copy_stmt = sql.SQL("COPY public.{tbl} TO STDOUT WITH CSV HEADER").format(
                    tbl=sql.Identifier(model_cls.__tablename__)
                )
                with out_file.open("wb") as fh:
                    with cur.copy(copy_stmt) as copy:
                        for chunk in copy:
                            fh.write(chunk)
        return

    for model_cls in export_models:
        out_file = csv_dir / f"{model_cls.__tablename__}.csv"
        col_names = [c.name for c in model_cls.__table__.columns]
        with out_file.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(col_names)
            for row in db.execute(select(model_cls.__table__).execution_options(yield_per=20_000)):
                writer.writerow(list(row))


def _write_generation_artifacts(
    db: Session,
    *,
    target_out_dir: Path,
    manifest: dict[str, Any],
    ground_truth: dict[str, Any],
    export_csv: bool,
) -> None:
    target_out_dir.mkdir(parents=True, exist_ok=True)
    (target_out_dir / "generation_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    (target_out_dir / "ground_truth_anomalies.json").write_text(
        json.dumps(ground_truth, indent=2), encoding="utf-8"
    )
    if export_csv:
        _export_dataset_csv(db, target_out_dir)


def _generate_full_scale_dataset(
    db: Session,
    *,
    seed: int,
    as_of: datetime,
    ground_truth: dict[str, Any],
    target_out_dir: Path | None,
    write_files: bool,
    export_csv: bool,
    total_journeys: int,
    chunk_size: int,
) -> dict[str, Any]:
    """
    Deterministic chunked bulk generator targeting >=100k sessions and >=2M events
    while keeping Python memory bounded and preserving all mandatory rules & anomalies.
    """
    rng = random.Random(seed)
    base_time = as_of - timedelta(days=10)

    # 1. Seed Products (10 canonical products) & Experiments
    products: list[Product] = []
    for p_biz, p_name, p_cat, p_price in CATALOG_SPECS:
        p = Product(
            product_id=uuid.uuid4(),
            product_business_id=p_biz,
            product_name=p_name,
            category=p_cat,
            description=f"Enterprise-grade {p_cat.lower()} hardware ({p_name})",
            current_price=p_price,
            currency="USD",
            is_active=True,
            created_at=base_time,
            updated_at=base_time,
        )
        db.add(p)
        products.append(p)
    db.flush()

    exp1 = Experiment(
        experiment_id=uuid.uuid4(),
        experiment_business_id="EXP-CHECKOUT-001",
        name="One-Click Express Checkout Flow",
        description="Tests simplified single-page checkout vs. multi-step control.",
        status="DRAFT",
        primary_metric="checkout_conversion_rate",
        eligibility_rules={"channels": ["WEB", "APP"], "exclude_bots": True},
        allocation_config={"control": 50.0, "treatment": 50.0},
        experiment_salt="SALT-EXP-CHECKOUT-001",
        config_version=1,
        created_at=base_time,
        updated_at=base_time,
    )
    db.add(exp1)
    db.flush()
    exp1_ctrl = ExperimentVariant(
        variant_id=uuid.uuid4(),
        variant_business_id="VAR-CHK-CTRL",
        experiment_id=exp1.experiment_id,
        variant_name="Control_MultiStep",
        is_control=True,
        allocation_percentage=Decimal("50.00"),
        configuration={"layout": "multi_step"},
        config_version=1,
        created_at=base_time,
    )
    exp1_treat = ExperimentVariant(
        variant_id=uuid.uuid4(),
        variant_business_id="VAR-CHK-TREAT",
        experiment_id=exp1.experiment_id,
        variant_name="Treatment_ExpressOneClick",
        is_control=False,
        allocation_percentage=Decimal("50.00"),
        configuration={"layout": "express_one_click"},
        config_version=1,
        created_at=base_time,
    )
    db.add_all([exp1_ctrl, exp1_treat])
    db.flush()
    validate_and_activate_experiment(db, experiment_id=exp1.experiment_id, activated_at=base_time)

    exp2 = Experiment(
        experiment_id=uuid.uuid4(),
        experiment_business_id="EXP-BANNER-002",
        name="Free Shipping Threshold Promo Banner",
        description="Paused experiment demonstrating pause enforcement (EXP-006).",
        status="PAUSED",
        primary_metric="add_to_cart_rate",
        eligibility_rules={"channels": ["WEB"]},
        allocation_config={"control": 50.0, "treatment": 50.0},
        experiment_salt="SALT-EXP-BANNER-002",
        config_version=1,
        start_at=base_time,
        created_at=base_time,
        updated_at=base_time + timedelta(days=2),
    )
    db.add(exp2)
    db.flush()
    db.add_all(
        [
            ExperimentVariant(
                variant_id=uuid.uuid4(),
                variant_business_id="VAR-BNR-CTRL",
                experiment_id=exp2.experiment_id,
                variant_name="Control_NoBanner",
                is_control=True,
                allocation_percentage=Decimal("50.00"),
                configuration={"banner": False},
                config_version=1,
                created_at=base_time,
            ),
            ExperimentVariant(
                variant_id=uuid.uuid4(),
                variant_business_id="VAR-BNR-TREAT",
                experiment_id=exp2.experiment_id,
                variant_name="Treatment_PromoBanner",
                is_control=False,
                allocation_percentage=Decimal("50.00"),
                configuration={"banner": True},
                config_version=1,
                created_at=base_time,
            ),
        ]
    )
    db.flush()

    # 2. Seed Canonical Hero Journey (JRN-HERO-001)
    hero_t0 = as_of - timedelta(days=3, hours=4)
    hero_cust = Customer(
        customer_id=uuid.uuid4(),
        customer_business_id="CUS-HERO-001",
        email="elena.rostova@example.com",
        first_name="Elena",
        last_name="Rostova",
        account_status="ACTIVE",
        created_at=hero_t0 - timedelta(days=30),
        updated_at=hero_t0,
    )
    db.add(hero_cust)
    db.flush()

    hero_anon_id = "ANON-HERO-001"
    hero_client_id = "CLI-WEB-HERO-001"
    db.add(
        ConsentRecord(
            consent_record_id=uuid.uuid4(),
            consent_business_id="CNS-HERO-001",
            customer_id=None,
            anonymous_id=hero_anon_id,
            consent_purpose="ANALYTICS",
            consent_status="GRANTED",
            effective_from=hero_t0,
            effective_to=None,
            recorded_at=hero_t0,
            source="COOKIE_BANNER_WEB",
        )
    )
    hero_journey = CustomerJourney(
        journey_id=uuid.uuid4(),
        journey_business_id="JRN-HERO-001",
        customer_id=hero_cust.customer_id,
        anonymous_id=None,
        first_touch_at=hero_t0,
        last_touch_at=hero_t0 + timedelta(hours=2),
        window_end_at=hero_t0 + timedelta(days=7),
        status="OPEN",
        identity_version=1,
        created_at=hero_t0,
    )
    db.add(hero_journey)
    db.flush()

    hero_cart = Cart(
        cart_id=uuid.uuid4(),
        cart_business_id="CRT-HERO-001",
        customer_id=hero_cust.customer_id,
        anonymous_id=hero_anon_id,
        session_id=None,
        status="ACTIVE",
        currency="USD",
        created_at=hero_t0 + timedelta(minutes=5),
        updated_at=hero_t0 + timedelta(minutes=5),
    )
    db.add(hero_cart)
    db.flush()
    add_or_update_cart_item(db, cart_id=hero_cart.cart_id, product_id=products[0].product_id, quantity=1, updated_at=hero_t0 + timedelta(minutes=5))
    add_or_update_cart_item(db, cart_id=hero_cart.cart_id, product_id=products[5].product_id, quantity=1, updated_at=hero_t0 + timedelta(minutes=6))
    validate_checkout_start(db, cart_id=hero_cart.cart_id, started_at=hero_t0 + timedelta(minutes=12))

    hero_events_spec = [
        ("EVT-HERO-001", "SESSION_STARTED", "WEB", hero_t0, hero_t0, None, None),
        ("EVT-HERO-002", "CONSENT_GRANTED", "WEB", hero_t0 + timedelta(minutes=1), hero_t0 + timedelta(minutes=1), None, None),
        ("EVT-HERO-003", "PRODUCT_VIEWED", "WEB", hero_t0 + timedelta(minutes=3), hero_t0 + timedelta(minutes=3), products[0].product_id, None),
        ("EVT-HERO-004", "ADD_TO_CART", "WEB", hero_t0 + timedelta(minutes=5), hero_t0 + timedelta(minutes=5), products[0].product_id, hero_cart.cart_id),
        ("EVT-HERO-005", "AUTH_LOGIN_SUCCESS", "WEB", hero_t0 + timedelta(minutes=8), hero_t0 + timedelta(minutes=8), None, None),
        ("EVT-HERO-006", "CHECKOUT_STARTED", "WEB", hero_t0 + timedelta(minutes=12), hero_t0 + timedelta(minutes=12), None, hero_cart.cart_id),
        ("EVT-HERO-007", "PAYMENT_FAILED", "WEB", hero_t0 + timedelta(minutes=15), hero_t0 + timedelta(minutes=15), None, hero_cart.cart_id),
        ("EVT-HERO-008", "SUPPORT_TICKET_CREATED", "WEB", hero_t0 + timedelta(minutes=18), hero_t0 + timedelta(minutes=18), None, hero_cart.cart_id),
        ("EVT-HERO-009", "PAYMENT_SUCCEEDED", "WEB", hero_t0 + timedelta(minutes=26), hero_t0 + timedelta(minutes=26), None, hero_cart.cart_id),
        ("EVT-HERO-010", "PURCHASE_COMPLETED", "WEB", hero_t0 + timedelta(minutes=27), hero_t0 + timedelta(minutes=27), products[0].product_id, hero_cart.cart_id),
    ]
    for seq_idx, (ebiz, etype, ech, ets, its, pid, cid) in enumerate(hero_events_spec, start=1):
        ingest_event(
            db,
            event_business_id=ebiz,
            event_type=etype,
            channel=ech,
            event_timestamp=ets,
            ingestion_timestamp=its,
            client_instance_id=hero_client_id,
            anonymous_id=hero_anon_id,
            customer_id=hero_cust.customer_id if seq_idx >= 5 else None,
            product_id=pid,
            cart_id=cid,
            source_sequence=seq_idx,
            properties={"scenario": "HERO_JOURNEY"},
        )

    resolve_identity_link(
        db,
        anonymous_id=hero_anon_id,
        customer_id=hero_cust.customer_id,
        linked_at=hero_t0 + timedelta(minutes=8),
        link_method="AUTHENTICATED_CONSENT",
        client_instance_id=hero_client_id,
    )
    hero_asg = assign_experiment_variant(
        db,
        experiment_id=exp1.experiment_id,
        assigned_at=hero_t0 + timedelta(minutes=10),
        customer_id=hero_cust.customer_id,
        anonymous_id=hero_anon_id,
    )
    record_experiment_exposure(
        db,
        assignment_id=hero_asg.assignment_id,
        exposed_at=hero_t0 + timedelta(minutes=12),
        channel="WEB",
        exposure_surface="CHECKOUT_PAGE",
    )
    hero_order, _ = create_or_get_authoritative_order(
        db,
        order_business_id="ORD-HERO-001",
        purchase_source="WEB_CHECKOUT",
        source_purchase_reference="PUR-REF-HERO-001",
        placed_at=hero_t0 + timedelta(minutes=14),
        customer_id=hero_cust.customer_id,
        cart_id=hero_cart.cart_id,
        items=[
            {"product_id": products[0].product_id, "quantity": 1, "unit_price": products[0].current_price},
            {"product_id": products[5].product_id, "quantity": 1, "unit_price": products[5].current_price},
        ],
        tax_amount=Decimal("30.24"),
        shipping_amount=Decimal("0.00"),
    )
    record_payment_attempt(
        db,
        order_id=hero_order.order_id,
        status="FAILED",
        attempted_at=hero_t0 + timedelta(minutes=15),
        failure_reason="CARD_DECLINED_3DS_TIMEOUT",
        provider_reference="STRIPE-FAIL-HERO-01",
    )
    hero_ticket = SupportTicket(
        ticket_id=uuid.uuid4(),
        ticket_business_id="TKT-HERO-001",
        customer_id=hero_cust.customer_id,
        session_id=None,
        order_id=hero_order.order_id,
        journey_id=hero_journey.journey_id,
        issue_type="PAYMENT",
        priority="HIGH",
        status="OPEN",
        origin_channel="WEB",
        support_channel="LIVE_CHAT",
        created_at=hero_t0 + timedelta(minutes=18),
    )
    db.add(hero_ticket)
    db.flush()
    transition_support_ticket(db, ticket_id=hero_ticket.ticket_id, to_status="IN_PROGRESS", changed_at=hero_t0 + timedelta(minutes=20), actor_reference="agent-marcus", notes="Verified 3DS challenge reset for customer card.")
    transition_support_ticket(db, ticket_id=hero_ticket.ticket_id, to_status="RESOLVED", changed_at=hero_t0 + timedelta(minutes=24), actor_reference="agent-marcus", notes="Customer retried payment successfully after gateway reset.")
    record_payment_attempt(db, order_id=hero_order.order_id, status="SUCCEEDED", attempted_at=hero_t0 + timedelta(minutes=26), provider_reference="STRIPE-SUCC-HERO-02")

    hero_sessions = sessionize_events(db, channel="WEB", client_instance_id=hero_client_id)
    for hs in hero_sessions:
        hs.journey_id = hero_journey.journey_id
    hero_cart.session_id = hero_sessions[0].session_id
    hero_order.session_id = hero_sessions[0].session_id
    hero_ticket.session_id = hero_sessions[0].session_id
    db.flush()
    calculate_journey_outcome(db, journey_id=hero_journey.journey_id, as_of_timestamp=as_of)
    for hs in hero_sessions:
        calculate_session_outcome(db, session_id=hs.session_id, as_of_timestamp=as_of)
    db.commit()
    db.expunge_all()

    # Cache plain product tuples before expunged session resets
    product_refs = [(p.product_id, p.current_price) for p in products]
    exp1_id = exp1.experiment_id
    exp1_biz = exp1.experiment_business_id
    exp1_salt = exp1.experiment_salt
    exp1_ctrl_id = exp1_ctrl.variant_id
    exp1_treat_id = exp1_treat.variant_id
    inactivity_limit = timedelta(minutes=30)

    # 3. Stream deterministic bulk chunks
    session_counter = len(hero_sessions)
    q_missing_counter = 0

    for chunk_start in range(1, total_journeys + 1, chunk_size):
        chunk_end = min(total_journeys, chunk_start + chunk_size - 1)
        customer_rows: list[dict[str, Any]] = []
        consent_rows: list[dict[str, Any]] = []
        id_link_rows: list[dict[str, Any]] = []
        journey_rows: list[dict[str, Any]] = []
        session_rows: list[dict[str, Any]] = []
        cart_rows: list[dict[str, Any]] = []
        cart_item_rows: list[dict[str, Any]] = []
        order_rows: list[dict[str, Any]] = []
        order_item_rows: list[dict[str, Any]] = []
        payment_rows: list[dict[str, Any]] = []
        assignment_rows: list[dict[str, Any]] = []
        event_rows: list[dict[str, Any]] = []
        exposure_rows: list[dict[str, Any]] = []
        quarantine_rows: list[dict[str, Any]] = []
        outcome_rows: list[dict[str, Any]] = []
        audit_rows: list[dict[str, Any]] = []

        for idx in range(chunk_start, chunk_end + 1):
            bucket = (idx - 1) % 100
            if bucket <= 2:
                archetype = "ANOM_001_LATE_EVENT_CONVERTED"
            elif bucket <= 4:
                archetype = "ANOM_002_IDENTITY_CONFLICT"
            elif bucket <= 6:
                archetype = "ANOM_002_UNAUTHORIZED_NO_CONSENT"
            elif bucket <= 9:
                archetype = "ANOM_003_DUPLICATE_PURCHASE"
            elif bucket <= 14:
                archetype = "ANOM_004_BOT_TRAFFIC"
            elif bucket <= 19:
                archetype = "SKIPPED_CART_DIRECT_PURCHASE"
            elif bucket <= 29:
                archetype = "ABANDONED_CART_OPEN_JOURNEY"
            elif bucket <= 34:
                archetype = "OUT_OF_WINDOW_PURCHASE"
            elif bucket <= 54:
                archetype = "STANDARD_CONVERTED"
            else:
                archetype = "BROWSE_NON_CONVERTED"

            channel = "WEB" if idx % 3 != 0 else "APP"
            cid = f"CLI-{channel}-{idx:06d}"
            anon_id = f"ANON-{idx:06d}"

            # Anchor journey time deterministically within the 10-day window before as_of
            slot_hours = (idx % 168) * 1.1
            j_start = base_time + timedelta(hours=slot_hours, minutes=rng.randint(0, 25))
            if archetype == "ABANDONED_CART_OPEN_JOURNEY":
                j_start = as_of - timedelta(days=2, hours=(idx % 12) + 2)
            elif archetype in ("BROWSE_NON_CONVERTED", "OUT_OF_WINDOW_PURCHASE"):
                j_start = as_of - timedelta(days=9, hours=(idx % 10))
            window_end = j_start + timedelta(days=7)

            has_customer = archetype not in ("ANOM_004_BOT_TRAFFIC", "BROWSE_NON_CONVERTED")
            cust_id: uuid.UUID | None = None
            cust_biz_id: str | None = None
            if has_customer:
                cust_id = uuid.uuid4()
                cust_biz_id = f"CUS-{idx:06d}"
                customer_rows.append(
                    {
                        "customer_id": cust_id,
                        "customer_business_id": cust_biz_id,
                        "email": f"user{idx:06d}@journeyiq-demo.com",
                        "first_name": f"Customer{idx}",
                        "last_name": "Demo",
                        "account_status": "ACTIVE",
                        "created_at": j_start - timedelta(days=15),
                        "updated_at": j_start,
                    }
                )

            if archetype == "ANOM_002_UNAUTHORIZED_NO_CONSENT":
                consent_status = "DENIED" if idx % 2 == 0 else "UNKNOWN"
            else:
                consent_status = "GRANTED"

            consent_id = uuid.uuid4()
            consent_rows.append(
                {
                    "consent_record_id": consent_id,
                    "consent_business_id": f"CNS-{idx:06d}",
                    "customer_id": None,
                    "anonymous_id": anon_id,
                    "consent_purpose": "ANALYTICS",
                    "consent_status": consent_status,
                    "effective_from": j_start,
                    "effective_to": None,
                    "recorded_at": j_start,
                    "source": f"{channel}_CONSENT_BANNER",
                }
            )

            eff_cust_id = cust_id if (cust_id and consent_status == "GRANTED") else None
            journey_id = uuid.uuid4()
            j_biz_id = f"JRN-{idx:06d}"

            # Identity linking & ANOM-002 records
            active_link_id: uuid.UUID | None = None
            if cust_id and archetype == "ANOM_002_IDENTITY_CONFLICT":
                active_link_id = uuid.uuid4()
                linked_at_1 = j_start + timedelta(minutes=2)
                id_link_rows.append(
                    {
                        "identity_link_id": active_link_id,
                        "identity_link_business_id": f"IDL-{active_link_id.hex[:10].upper()}",
                        "anonymous_id": anon_id,
                        "customer_id": cust_id,
                        "client_instance_id": cid,
                        "link_method": "AUTHENTICATED_CONSENT",
                        "consent_record_id": consent_id,
                        "effective_from": linked_at_1,
                        "effective_to": None,
                        "status": "ACTIVE",
                        "created_at": linked_at_1,
                    }
                )
                other_cust_id = uuid.uuid4()
                other_cust_biz = f"CUS-CNF-{idx:06d}"
                customer_rows.append(
                    {
                        "customer_id": other_cust_id,
                        "customer_business_id": other_cust_biz,
                        "email": f"conflict{idx:06d}@journeyiq-demo.com",
                        "first_name": "Conflict",
                        "last_name": f"User{idx}",
                        "account_status": "ACTIVE",
                        "created_at": j_start,
                        "updated_at": j_start,
                    }
                )
                conf_link_id = uuid.uuid4()
                linked_at_2 = j_start + timedelta(minutes=4)
                id_link_rows.append(
                    {
                        "identity_link_id": conf_link_id,
                        "identity_link_business_id": f"IDL-CNF-{conf_link_id.hex[:8].upper()}",
                        "anonymous_id": anon_id,
                        "customer_id": other_cust_id,
                        "client_instance_id": cid,
                        "link_method": "IMPERSONATED_MERGE",
                        "consent_record_id": consent_id,
                        "effective_from": linked_at_2,
                        "effective_to": linked_at_2,
                        "status": "CONFLICTED",
                        "created_at": linked_at_2,
                    }
                )
                q_id = uuid.uuid4()
                quarantine_rows.append(
                    {
                        "quarantine_id": q_id,
                        "quarantine_business_id": f"QUAR-{q_id.hex[:10].upper()}",
                        "event_id": None,
                        "issue_type": "IDENTITY_CONFLICT",
                        "reason": (
                            f"ANOM-002: anonymous_id '{anon_id}' is already linked to customer_id '{cust_id}'; "
                            f"rejected conflicting merge to customer_id '{other_cust_id}'."
                        ),
                        "detected_at": linked_at_2,
                        "resolution_status": "OPEN",
                        "resolution_notes": None,
                        "resolved_at": None,
                    }
                )
                audit_rows.append(
                    {
                        "audit_event_id": uuid.uuid4(),
                        "audit_business_id": f"AUD-{uuid.uuid4().hex[:10].upper()}",
                        "entity_type": "IdentityLink",
                        "entity_id": conf_link_id,
                        "action": "IDENTITY_MERGE_CONFLICT_BLOCKED",
                        "actor_type": "SYSTEM",
                        "actor_reference": "domain_service",
                        "previous_state": None,
                        "new_state": {
                            "anonymous_id": anon_id,
                            "existing_customer_id": str(cust_id),
                            "attempted_customer_id": str(other_cust_id),
                        },
                        "occurred_at": linked_at_2,
                        "recorded_at": linked_at_2,
                        "correlation_id": None,
                        "audit_metadata": {},
                    }
                )
                ground_truth["anom_002_identity_conflicts"].append(
                    {
                        "anonymous_id": anon_id,
                        "legitimate_customer": cust_biz_id,
                        "conflicting_customer": other_cust_biz,
                    }
                )
            elif cust_id and archetype == "ANOM_002_UNAUTHORIZED_NO_CONSENT":
                blocked_at = j_start + timedelta(minutes=3)
                q_id = uuid.uuid4()
                quarantine_rows.append(
                    {
                        "quarantine_id": q_id,
                        "quarantine_business_id": f"QUAR-{q_id.hex[:10].upper()}",
                        "event_id": None,
                        "issue_type": "UNAUTHORIZED_IDENTITY_MERGE",
                        "reason": (
                            f"ANOM-002: Identity stitching blocked for anonymous_id='{anon_id}' -> customer_id='{cust_id}' "
                            f"because consent_status is '{consent_status}' (link_method='AUTH_LOGIN_ONLY')."
                        ),
                        "detected_at": blocked_at,
                        "resolution_status": "OPEN",
                        "resolution_notes": None,
                        "resolved_at": None,
                    }
                )
                audit_rows.append(
                    {
                        "audit_event_id": uuid.uuid4(),
                        "audit_business_id": f"AUD-{uuid.uuid4().hex[:10].upper()}",
                        "entity_type": "IdentityLink",
                        "entity_id": uuid.uuid4(),
                        "action": "UNAUTHORIZED_IDENTITY_MERGE_BLOCKED",
                        "actor_type": "SYSTEM",
                        "actor_reference": "domain_service",
                        "previous_state": None,
                        "new_state": {
                            "anonymous_id": anon_id,
                            "customer_id": str(cust_id),
                            "consent_status": consent_status,
                        },
                        "occurred_at": blocked_at,
                        "recorded_at": blocked_at,
                        "correlation_id": None,
                        "audit_metadata": {},
                    }
                )
                ground_truth["anom_002_unauthorized_merges"].append(
                    {
                        "anonymous_id": anon_id,
                        "customer_business_id": cust_biz_id,
                        "consent_status": consent_status,
                    }
                )
            elif eff_cust_id is not None:
                active_link_id = uuid.uuid4()
                linked_at = j_start + timedelta(minutes=2)
                id_link_rows.append(
                    {
                        "identity_link_id": active_link_id,
                        "identity_link_business_id": f"IDL-{active_link_id.hex[:10].upper()}",
                        "anonymous_id": anon_id,
                        "customer_id": eff_cust_id,
                        "client_instance_id": cid,
                        "link_method": "AUTHENTICATED_CONSENT",
                        "consent_record_id": consent_id,
                        "effective_from": linked_at,
                        "effective_to": None,
                        "status": "ACTIVE",
                        "created_at": linked_at,
                    }
                )

            # Experiment assignment & exposure for human cohorts
            is_bot = archetype == "ANOM_004_BOT_TRAFFIC"
            asg_id: uuid.UUID | None = None
            expose_event_idx_target = not is_bot and (
                idx % 4 != 0
                or archetype in ("STANDARD_CONVERTED", "ANOM_001_LATE_EVENT_CONVERTED", "ANOM_003_DUPLICATE_PURCHASE")
            )
            if not is_bot:
                id_type = "CUSTOMER" if eff_cust_id is not None else "ANONYMOUS"
                id_key = str(eff_cust_id) if eff_cust_id is not None else anon_id
                exp_bucket = compute_deterministic_bucket(exp1_biz, id_key, exp1_salt)
                variant_id = exp1_ctrl_id if exp_bucket < 5000 else exp1_treat_id
                asg_id = uuid.uuid4()
                assignment_rows.append(
                    {
                        "assignment_id": asg_id,
                        "assignment_business_id": f"ASG-{asg_id.hex[:10].upper()}",
                        "experiment_id": exp1_id,
                        "variant_id": variant_id,
                        "assignment_identity_type": id_type,
                        "assignment_identity_id": id_key,
                        "identity_link_id": active_link_id if eff_cust_id is not None else None,
                        "consent_record_id": consent_id if eff_cust_id is not None else None,
                        "identity_version": 1,
                        "assignment_bucket": exp_bucket,
                        "config_version": 1,
                        "assigned_at": j_start + timedelta(minutes=1),
                    }
                )
                if expose_event_idx_target:
                    exposure_rows.append(
                        {
                            "exposure_id": uuid.uuid4(),
                            "exposure_business_id": f"EXP-{uuid.uuid4().hex[:10].upper()}",
                            "assignment_id": asg_id,
                            "event_id": None,
                            "channel": channel,
                            "exposed_at": j_start + timedelta(minutes=4),
                            "exposure_context": {"surface": "CHECKOUT_PAGE", "is_first_exposure": True},
                        }
                    )

            # Session plan: ~10% of non-bot journeys span 2 sessions to cleanly exceed 100k sessions
            multi_session = (not is_bot) and (idx % 10 == 9)
            prod_id, prod_price = product_refs[idx % len(product_refs)]
            props_base = {"is_bot": True, "bot_reason": "HEADLESS_SCRAPER_BURST"} if is_bot else {}
            if is_bot:
                ground_truth["anom_004_bot_sessions"].append(
                    {"client_instance_id": cid, "anonymous_id": anon_id, "channel": channel}
                )

            sess1_id = uuid.uuid4()
            session_counter += 1
            sess1_biz = f"SES-{channel[:3]}-{idx:06d}-1-{sess1_id.hex[:4].upper()}"[:40]
            sess2_id: uuid.UUID | None = None
            sess2_biz: str | None = None
            if multi_session:
                sess2_id = uuid.uuid4()
                session_counter += 1
                sess2_biz = f"SES-{channel[:3]}-{idx:06d}-2-{sess2_id.hex[:4].upper()}"[:40]

            # Construct events for session 1 (and session 2 if multi_session)
            s1_events: list[dict[str, Any]] = []
            s2_events: list[dict[str, Any]] = []
            ev_seq = 0

            def _make_event(
                *,
                sid: uuid.UUID,
                etype: str,
                ets: datetime,
                its: datetime,
                p_id: uuid.UUID | None = None,
                c_id: uuid.UUID | None = None,
                o_id: uuid.UUID | None = None,
                extra_props: dict[str, Any] | None = None,
                suffix: str = "",
            ) -> dict[str, Any]:
                nonlocal ev_seq
                ev_seq += 1
                ebiz = f"EVT-{idx:06d}-{ev_seq:03d}{suffix}"
                props = dict(props_base)
                if extra_props:
                    props.update(extra_props)
                lag = (its - ets).total_seconds()
                if lag > 300:
                    props["late_arrival_detected"] = True
                    props["ingestion_lag_seconds"] = round(lag, 2)
                return {
                    "event_id": uuid.uuid4(),
                    "event_business_id": ebiz,
                    "session_id": sid,
                    "customer_id": eff_cust_id,
                    "anonymous_id": anon_id,
                    "client_instance_id": cid,
                    "product_id": p_id,
                    "cart_id": c_id,
                    "order_id": o_id,
                    "event_type": etype,
                    "channel": channel,
                    "event_timestamp": ets,
                    "ingestion_timestamp": its,
                    "source_sequence": ev_seq,
                    "source_event_id": ebiz,
                    "idempotency_key": f"IDEMP-{ebiz}",
                    "properties": props,
                    "schema_version": "1.0",
                    "created_at": its,
                }

            # Session 1 events: SESSION_STARTED + rich browsing PRODUCT_VIEWED sequence
            s1_events.append(
                _make_event(sid=sess1_id, etype="SESSION_STARTED", ets=j_start, its=j_start + timedelta(seconds=1))
            )
            num_s1_views = 10 if multi_session else (21 if is_bot else (16 if archetype not in ("BROWSE_NON_CONVERTED",) else 20 + (idx % 3)))
            late_view_ev: dict[str, Any] | None = None
            for v_i in range(num_s1_views):
                pv_ts = j_start + timedelta(seconds=20 * (v_i + 1))
                is_late = archetype == "ANOM_001_LATE_EVENT_CONVERTED" and v_i == 0
                pv_ing = pv_ts + timedelta(minutes=45) if is_late else pv_ts + timedelta(seconds=2)
                vp_id = product_refs[(idx + v_i) % len(product_refs)][0]
                ev_row = _make_event(sid=sess1_id, etype="PRODUCT_VIEWED", ets=pv_ts, its=pv_ing, p_id=vp_id)
                s1_events.append(ev_row)
                if is_late:
                    late_view_ev = ev_row

            if late_view_ev is not None:
                ground_truth["anom_001_late_events"].append(
                    {
                        "event_business_id": late_view_ev["event_business_id"],
                        "event_timestamp": late_view_ev["event_timestamp"].isoformat(),
                        "ingestion_timestamp": late_view_ev["ingestion_timestamp"].isoformat(),
                        "journey_business_id": j_biz_id,
                    }
                )

            if multi_session and sess2_id is not None:
                s1_end_ts = s1_events[-1]["event_timestamp"] + timedelta(seconds=15)
                s1_events.append(
                    _make_event(sid=sess1_id, etype="SESSION_ENDED", ets=s1_end_ts, its=s1_end_ts + timedelta(seconds=1))
                )
                s2_start = j_start + timedelta(hours=2)
                s2_events.append(
                    _make_event(sid=sess2_id, etype="SESSION_STARTED", ets=s2_start, its=s2_start + timedelta(seconds=1))
                )
                for v_i in range(9):
                    pv2_ts = s2_start + timedelta(seconds=25 * (v_i + 1))
                    vp2_id = product_refs[(idx + v_i + 3) % len(product_refs)][0]
                    s2_events.append(
                        _make_event(sid=sess2_id, etype="PRODUCT_VIEWED", ets=pv2_ts, its=pv2_ts + timedelta(seconds=2), p_id=vp2_id)
                    )
                active_sess_id = sess2_id
                active_events = s2_events
                funnel_t0 = s2_start + timedelta(minutes=4)
            else:
                active_sess_id = sess1_id
                active_events = s1_events
                funnel_t0 = j_start + timedelta(minutes=8)

            # Cart / Checkout / Order / Outcome logic
            friction_flags: list[str] = []
            if archetype == "ANOM_001_LATE_EVENT_CONVERTED":
                friction_flags.append("LATE_EVENT")

            cart_id: uuid.UUID | None = None
            confirmed_order_id: uuid.UUID | None = None
            purchase_time: datetime | None = None
            out_of_window_order = False

            if not is_bot and archetype != "BROWSE_NON_CONVERTED":
                cart_id = uuid.uuid4()
                qty = 1 + (idx % 2)
                cart_created = funnel_t0 + timedelta(seconds=30)
                cart_updated = funnel_t0 + timedelta(minutes=1)
                is_abandoned_only = archetype in (
                    "ABANDONED_CART_OPEN_JOURNEY",
                    "ANOM_002_IDENTITY_CONFLICT",
                    "ANOM_002_UNAUTHORIZED_NO_CONSENT",
                )
                cart_rows.append(
                    {
                        "cart_id": cart_id,
                        "cart_business_id": f"CRT-{idx:06d}",
                        "customer_id": eff_cust_id,
                        "anonymous_id": anon_id,
                        "session_id": active_sess_id,
                        "status": "ABANDONED" if is_abandoned_only else "CONVERTED",
                        "currency": "USD",
                        "created_at": cart_created,
                        "updated_at": cart_updated,
                        "abandoned_at": cart_updated + timedelta(hours=24) if is_abandoned_only else None,
                        "converted_at": None if is_abandoned_only else (
                            j_start + timedelta(days=8, hours=2) if archetype == "OUT_OF_WINDOW_PURCHASE" else funnel_t0 + timedelta(minutes=6)
                        ),
                    }
                )
                cart_item_rows.append(
                    {
                        "cart_item_id": uuid.uuid4(),
                        "cart_item_business_id": f"CIT-{cart_id.hex[:8].upper()}-{prod_id.hex[:4].upper()}",
                        "cart_id": cart_id,
                        "product_id": prod_id,
                        "quantity": qty,
                        "unit_price": prod_price,
                        "added_at": cart_updated,
                        "updated_at": cart_updated,
                    }
                )
                if archetype != "SKIPPED_CART_DIRECT_PURCHASE":
                    active_events.append(
                        _make_event(
                            sid=active_sess_id,
                            etype="ADD_TO_CART",
                            ets=cart_updated,
                            its=cart_updated + timedelta(seconds=2),
                            p_id=prod_id,
                            c_id=cart_id,
                        )
                    )
                if is_abandoned_only:
                    friction_flags.append("CART_ABANDONED")
                else:
                    chk_ts = funnel_t0 + timedelta(minutes=3)
                    active_events.append(
                        _make_event(
                            sid=active_sess_id,
                            etype="CHECKOUT_STARTED",
                            ets=chk_ts,
                            its=chk_ts + timedelta(seconds=1),
                            p_id=prod_id,
                            c_id=cart_id,
                        )
                    )
                    purchase_time = (
                        j_start + timedelta(days=8, hours=2)
                        if archetype == "OUT_OF_WINDOW_PURCHASE"
                        else funnel_t0 + timedelta(minutes=6)
                    )
                    if archetype == "OUT_OF_WINDOW_PURCHASE":
                        out_of_window_order = True
                    order_id = uuid.uuid4()
                    order_biz_id = f"ORD-{idx:06d}"
                    pur_ref = f"PUR-REF-{idx:06d}"
                    line_total = (Decimal(qty) * prod_price).quantize(Decimal("0.01"))
                    tax_amt = Decimal("12.50")
                    total_amt = (line_total + tax_amt).quantize(Decimal("0.01"))
                    placed_at = purchase_time - timedelta(minutes=1)
                    order_rows.append(
                        {
                            "order_id": order_id,
                            "order_business_id": order_biz_id,
                            "customer_id": eff_cust_id,
                            "cart_id": cart_id,
                            "session_id": active_sess_id,
                            "source_purchase_reference": pur_ref,
                            "purchase_source": f"{channel}_CHECKOUT",
                            "currency": "USD",
                            "subtotal": line_total,
                            "discount_amount": Decimal("0.00"),
                            "tax_amount": tax_amt,
                            "shipping_amount": Decimal("0.00"),
                            "total_amount": total_amt,
                            "status": "CONFIRMED",
                            "created_at": placed_at,
                            "purchase_confirmed_at": purchase_time,
                            "cancelled_at": None,
                        }
                    )
                    order_item_rows.append(
                        {
                            "order_item_id": uuid.uuid4(),
                            "order_item_business_id": f"OIT-{order_biz_id[-6:]}-01-{uuid.uuid4().hex[:4].upper()}",
                            "order_id": order_id,
                            "product_id": prod_id,
                            "quantity": qty,
                            "unit_price": prod_price,
                            "line_total": line_total,
                        }
                    )
                    attempt_num = 1
                    if idx % 7 == 0:
                        friction_flags.extend(["PAYMENT_FAILURE", "CHECKOUT_RETRY"])
                        pa_fail_ts = purchase_time - timedelta(seconds=30)
                        payment_rows.append(
                            {
                                "payment_attempt_id": uuid.uuid4(),
                                "payment_attempt_business_id": f"PAY-{order_id.hex[:8].upper()}-01",
                                "order_id": order_id,
                                "attempt_number": 1,
                                "amount": total_amt,
                                "currency": "USD",
                                "status": "FAILED",
                                "failure_reason": "GATEWAY_TIMEOUT",
                                "provider_reference": None,
                                "attempted_at": pa_fail_ts,
                                "completed_at": pa_fail_ts,
                            }
                        )
                        attempt_num = 2
                    payment_rows.append(
                        {
                            "payment_attempt_id": uuid.uuid4(),
                            "payment_attempt_business_id": f"PAY-{order_id.hex[:8].upper()}-{attempt_num:02d}",
                            "order_id": order_id,
                            "attempt_number": attempt_num,
                            "amount": total_amt,
                            "currency": "USD",
                            "status": "SUCCEEDED",
                            "failure_reason": None,
                            "provider_reference": None,
                            "attempted_at": purchase_time,
                            "completed_at": purchase_time,
                        }
                    )
                    confirmed_order_id = order_id
                    # Keep PURCHASE_COMPLETED inside active session unless OUT_OF_WINDOW (>7d after j_start would violate <=30m gap if on same session, so for OUT_OF_WINDOW emit event at checkout_ts + 2m)
                    pur_evt_ts = chk_ts + timedelta(minutes=3) if out_of_window_order else purchase_time
                    active_events.append(
                        _make_event(
                            sid=active_sess_id,
                            etype="PURCHASE_COMPLETED",
                            ets=pur_evt_ts,
                            its=pur_evt_ts + timedelta(seconds=1),
                            p_id=prod_id,
                            c_id=cart_id,
                            o_id=order_id,
                        )
                    )
                    if archetype == "ANOM_003_DUPLICATE_PURCHASE":
                        friction_flags.append("DUPLICATE_SIGNAL")
                        dup_ts = pur_evt_ts + timedelta(seconds=5)
                        dup_ev = _make_event(
                            sid=active_sess_id,
                            etype="PURCHASE_COMPLETED",
                            ets=dup_ts,
                            its=dup_ts + timedelta(seconds=1),
                            p_id=prod_id,
                            c_id=cart_id,
                            o_id=order_id,
                            extra_props={"duplicate_retry_webhook": True},
                            suffix="-DUP",
                        )
                        active_events.append(dup_ev)
                        audit_rows.append(
                            {
                                "audit_event_id": uuid.uuid4(),
                                "audit_business_id": f"AUD-{uuid.uuid4().hex[:10].upper()}",
                                "entity_type": "Order",
                                "entity_id": order_id,
                                "action": "DUPLICATE_CONVERSION_SIGNAL_SUPPRESSED",
                                "actor_type": "SYSTEM",
                                "actor_reference": "domain_service",
                                "previous_state": None,
                                "new_state": {
                                    "purchase_source": f"{channel}_CHECKOUT",
                                    "source_purchase_reference": pur_ref,
                                    "existing_order_business_id": order_biz_id,
                                },
                                "occurred_at": dup_ts,
                                "recorded_at": dup_ts,
                                "correlation_id": None,
                                "audit_metadata": {},
                            }
                        )
                        ground_truth["anom_003_duplicate_purchase_signals"].append(
                            {
                                "order_business_id": order_biz_id,
                                "source_purchase_reference": pur_ref,
                                "duplicate_event_business_id": dup_ev["event_business_id"],
                            }
                        )

            # Finalize session rows & session outcomes cleanly
            def _append_session(
                sid: uuid.UUID,
                sbiz: str,
                ev_list: list[dict[str, Any]],
                sess_order_id: uuid.UUID | None,
                sess_purchase_ts: datetime | None,
            ) -> None:
                s_start = ev_list[0]["event_timestamp"]
                s_last = ev_list[-1]["event_timestamp"]
                explicit_end = ev_list[-1]["event_type"] == "SESSION_ENDED"
                s_ended = s_last if explicit_end else (s_last + inactivity_limit)
                s_status = "FINALIZED" if explicit_end else "ENDED"
                session_rows.append(
                    {
                        "session_id": sid,
                        "session_business_id": sbiz,
                        "customer_id": eff_cust_id,
                        "anonymous_id": anon_id,
                        "client_instance_id": cid,
                        "channel": channel,
                        "journey_id": journey_id,
                        "started_at": s_start,
                        "last_event_at": s_last,
                        "ended_at": s_ended,
                        "status": s_status,
                        "sessionization_version": "v1.0",
                        "traffic_classification": "BOT" if is_bot else "HUMAN",
                        "classification_reason": "HEADLESS_SCRAPER_BURST" if is_bot else "NORMAL_BEHAVIOR",
                        "classification_version": "v1.0",
                        "classified_at": s_start,
                        "created_at": s_start,
                        "updated_at": s_last,
                    }
                )
                if is_bot:
                    so_status, so_attr, so_reason = "EXCLUDED", "EXCLUDED", "BOT_TRAFFIC"
                elif sess_order_id is not None:
                    so_status, so_attr, so_reason = "CONVERTED", "ATTRIBUTED", "SESSION_CONVERSION"
                elif s_ended > as_of:
                    so_status, so_attr, so_reason = "IN_PROGRESS", "UNATTRIBUTED", "SESSION_ACTIVE"
                else:
                    so_status, so_attr, so_reason = "NON_CONVERTED", "UNATTRIBUTED", "SESSION_ENDED_WITHOUT_PURCHASE"
                outcome_rows.append(
                    {
                        "outcome_id": uuid.uuid4(),
                        "outcome_business_id": f"OUT-SES-{sbiz[-6:]}-V1-{uuid.uuid4().hex[:4].upper()}",
                        "scope_type": "SESSION",
                        "session_id": sid,
                        "journey_id": None,
                        "outcome_status": so_status,
                        "outcome_reason": so_reason,
                        "conversion_order_id": sess_order_id,
                        "first_touch_at": s_start,
                        "window_end_at": s_ended,
                        "converted_at": sess_purchase_ts if sess_order_id is not None else None,
                        "attribution_status": so_attr,
                        "friction_flags": [],
                        "calculation_version": "v1",
                        "calculated_at": as_of,
                    }
                )

            if multi_session and sess2_id is not None and sess2_biz is not None:
                _append_session(sess1_id, sess1_biz, s1_events, None, None)
                _append_session(sess2_id, sess2_biz, s2_events, confirmed_order_id, purchase_time)
                all_j_events = s1_events + s2_events
            else:
                _append_session(sess1_id, sess1_biz, s1_events, confirmed_order_id, purchase_time)
                all_j_events = s1_events

            event_rows.extend(all_j_events)
            last_touch = all_j_events[-1]["event_timestamp"]

            # Journey-level outcome
            if is_bot:
                jo_status, jo_attr, jo_reason = "EXCLUDED", "EXCLUDED", "BOT_TRAFFIC"
                jo_order_id, jo_conv_at = None, None
            elif confirmed_order_id is not None and not out_of_window_order:
                jo_status, jo_attr, jo_reason = "CONVERTED", "ATTRIBUTED", "DIRECT_WINDOW_CONVERSION"
                jo_order_id, jo_conv_at = confirmed_order_id, purchase_time
            elif out_of_window_order:
                jo_attr, jo_reason = "OUT_OF_WINDOW", "PURCHASE_AFTER_7D_WINDOW"
                jo_status = "IN_PROGRESS" if as_of < window_end else "NON_CONVERTED"
                jo_order_id, jo_conv_at = None, None
            elif as_of < window_end:
                jo_status, jo_attr, jo_reason = "IN_PROGRESS", "UNATTRIBUTED", "ATTRIBUTION_WINDOW_OPEN"
                jo_order_id, jo_conv_at = None, None
            else:
                jo_status, jo_attr, jo_reason = "NON_CONVERTED", "UNATTRIBUTED", "ATTRIBUTION_WINDOW_EXPIRED"
                jo_order_id, jo_conv_at = None, None

            journey_rows.append(
                {
                    "journey_id": journey_id,
                    "journey_business_id": j_biz_id,
                    "customer_id": eff_cust_id,
                    "anonymous_id": None if eff_cust_id is not None else anon_id,
                    "first_touch_at": j_start,
                    "last_touch_at": last_touch,
                    "window_end_at": window_end,
                    "status": "FINALIZED" if jo_status in ("CONVERTED", "NON_CONVERTED", "EXCLUDED") else "OPEN",
                    "identity_version": 1,
                    "created_at": j_start,
                }
            )
            outcome_rows.append(
                {
                    "outcome_id": uuid.uuid4(),
                    "outcome_business_id": f"OUT-{j_biz_id[-8:]}-V1-{uuid.uuid4().hex[:4].upper()}",
                    "scope_type": "CUSTOMER_JOURNEY",
                    "session_id": None,
                    "journey_id": journey_id,
                    "outcome_status": jo_status,
                    "outcome_reason": jo_reason,
                    "conversion_order_id": jo_order_id,
                    "first_touch_at": j_start,
                    "window_end_at": window_end,
                    "converted_at": jo_conv_at,
                    "attribution_status": jo_attr,
                    "friction_flags": sorted(set(friction_flags)),
                    "calculation_version": "v1",
                    "calculated_at": as_of,
                }
            )

            # Quarantine missing client_instance_id event periodically (SES-006)
            if idx % 200 == 0 or (total_journeys < 200 and idx <= 6):
                q_missing_counter += 1
                q_ts = j_start + timedelta(minutes=1)
                q_ebiz = f"EVT-MISSING-CID-{q_missing_counter:06d}"
                q_eid = uuid.uuid4()
                q_ch = "WEB" if q_missing_counter % 2 == 0 else "APP"
                event_rows.append(
                    {
                        "event_id": q_eid,
                        "event_business_id": q_ebiz,
                        "session_id": None,
                        "customer_id": None,
                        "anonymous_id": f"ANON-BAD-{q_missing_counter:06d}",
                        "client_instance_id": None,
                        "product_id": None,
                        "cart_id": None,
                        "order_id": None,
                        "event_type": "PRODUCT_VIEWED",
                        "channel": q_ch,
                        "event_timestamp": q_ts,
                        "ingestion_timestamp": q_ts + timedelta(seconds=2),
                        "source_sequence": 1,
                        "source_event_id": q_ebiz,
                        "idempotency_key": f"IDEMP-{q_ebiz}",
                        "properties": {"quarantined": True},
                        "schema_version": "1.0",
                        "created_at": q_ts + timedelta(seconds=2),
                    }
                )
                qid = uuid.uuid4()
                quarantine_rows.append(
                    {
                        "quarantine_id": qid,
                        "quarantine_business_id": f"QUAR-{qid.hex[:10].upper()}",
                        "event_id": q_eid,
                        "issue_type": "MISSING_CLIENT_INSTANCE_ID",
                        "reason": f"{q_ch} event '{q_ebiz}' is missing required client_instance_id (SES-006).",
                        "detected_at": q_ts + timedelta(seconds=2),
                        "resolution_status": "OPEN",
                        "resolution_notes": None,
                        "resolved_at": None,
                    }
                )
                ground_truth["missing_client_instance_events"].append(q_ebiz)

        # Persist chunk in strict FK topological order & clear SQLAlchemy session state
        _bulk_insert_table(db, Customer, customer_rows)
        _bulk_insert_table(db, ConsentRecord, consent_rows)
        _bulk_insert_table(db, IdentityLink, id_link_rows)
        _bulk_insert_table(db, CustomerJourney, journey_rows)
        _bulk_insert_table(db, SessionRecord, session_rows)
        _bulk_insert_table(db, Cart, cart_rows)
        _bulk_insert_table(db, CartItem, cart_item_rows)
        _bulk_insert_table(db, Order, order_rows)
        _bulk_insert_table(db, OrderItem, order_item_rows)
        _bulk_insert_table(db, PaymentAttempt, payment_rows)
        _bulk_insert_table(db, ExperimentAssignment, assignment_rows)
        _bulk_insert_table(db, JourneyEvent, event_rows)
        _bulk_insert_table(db, ExperimentExposure, exposure_rows)
        _bulk_insert_table(db, EventQuarantine, quarantine_rows)
        _bulk_insert_table(db, JourneyOutcome, outcome_rows)
        _bulk_insert_table(db, AuditEvent, audit_rows)
        db.commit()
        db.expunge_all()

    dataset_checksum = _compute_streaming_sha256(db)
    manifest = {
        "seed": seed,
        "scale": "full",
        "as_of_timestamp": as_of.isoformat(),
        "dataset_sha256": dataset_checksum,
        "counts": _collect_manifest_counts(db),
        "anomaly_counts": {
            "anom_001_late_events": len(ground_truth["anom_001_late_events"]),
            "anom_002_identity_conflicts": len(ground_truth["anom_002_identity_conflicts"]),
            "anom_002_unauthorized_merges": len(ground_truth["anom_002_unauthorized_merges"]),
            "anom_003_duplicate_purchase_signals": len(ground_truth["anom_003_duplicate_purchase_signals"]),
            "anom_004_bot_sessions": len(ground_truth["anom_004_bot_sessions"]),
            "missing_client_instance_events": len(ground_truth["missing_client_instance_events"]),
        },
    }
    if write_files and target_out_dir is not None:
        _write_generation_artifacts(
            db,
            target_out_dir=target_out_dir,
            manifest=manifest,
            ground_truth=ground_truth,
            export_csv=export_csv,
        )
    return manifest


def generate_dataset(
    db: Session,
    *,
    seed: int = 42,
    scale: str = "mvp",
    as_of: datetime = AS_OF_DEFAULT,
    write_files: bool = True,
    output_dir: str | Path | None = None,
    allow_live_demo_overwrite: bool = False,
    export_csv: bool = False,
    full_journeys: int = FULL_SCALE_DEFAULT_JOURNEYS,
    full_chunk_size: int = FULL_SCALE_DEFAULT_CHUNK_SIZE,
) -> dict[str, Any]:
    """
    Deterministic synthetic dataset generator (DATA-001 through DATA-010, ANOM-001 through ANOM-004).
    """
    target_out_dir = (
        resolve_safe_output_dir(
            output_dir,
            allow_live_demo_overwrite=allow_live_demo_overwrite,
        )
        if write_files
        else None
    )

    # Monkeypatch uuid.uuid4 during deterministic generation so all PKs and business IDs are 100% reproducible
    orig_uuid4 = uuid.uuid4
    ufac = DeterministicUUIDFactory(seed)
    uuid.uuid4 = ufac.next_uuid  # type: ignore[assignment]
    rng = random.Random(seed)

    ground_truth: dict[str, Any] = {
        "seed": seed,
        "scale": scale,
        "as_of_timestamp": as_of.isoformat(),
        "anom_001_late_events": [],
        "anom_002_identity_conflicts": [],
        "anom_002_unauthorized_merges": [],
        "anom_003_duplicate_purchase_signals": [],
        "anom_004_bot_sessions": [],
        "missing_client_instance_events": [],
        "hero_journey_business_id": "JRN-HERO-001",
    }

    try:
        reset_database(db)
        if scale == "full":
            return _generate_full_scale_dataset(
                db,
                seed=seed,
                as_of=as_of,
                ground_truth=ground_truth,
                target_out_dir=target_out_dir,
                write_files=write_files,
                export_csv=export_csv,
                total_journeys=full_journeys,
                chunk_size=full_chunk_size,
            )

        base_time = as_of - timedelta(days=10)

        # 1. Seed Products (10 canonical products)
        catalog_specs = [
            ("PRD-001", "AeroPulse Wireless ANC Headphones", "Audio", Decimal("249.00")),
            ("PRD-002", "Chronos Pro Smart Fitness Watch", "Wearables", Decimal("329.00")),
            ("PRD-003", "LumaDesk 4K UltraWide Monitor", "Displays", Decimal("599.00")),
            ("PRD-004", "KeyCraft Mechanical Tactile Keyboard", "Peripherals", Decimal("149.00")),
            ("PRD-005", "GlidePro Ergonomic Vertical Mouse", "Peripherals", Decimal("89.00")),
            ("PRD-006", "ThunderDock USB-C 12-in-1 Hub", "Accessories", Decimal("129.00")),
            ("PRD-007", "SonicBeam Compact Desktop Soundbar", "Audio", Decimal("179.00")),
            ("PRD-008", "VaultSSD 2TB NVMe Portable Drive", "Storage", Decimal("199.00")),
            ("PRD-009", "ClearCast 4K AI Webcam", "Video", Decimal("159.00")),
            ("PRD-010", "PowerCore 140W GaN Travel Charger", "Accessories", Decimal("79.00")),
        ]
        products: list[Product] = []
        for p_biz, p_name, p_cat, p_price in catalog_specs:
            p = Product(
                product_id=uuid.uuid4(),
                product_business_id=p_biz,
                product_name=p_name,
                category=p_cat,
                description=f"Enterprise-grade {p_cat.lower()} hardware ({p_name})",
                current_price=p_price,
                currency="USD",
                is_active=True,
                created_at=base_time,
                updated_at=base_time,
            )
            db.add(p)
            products.append(p)
        db.flush()

        # 2. Seed Experiments (EXP-CHECKOUT-001 RUNNING, EXP-BANNER-002 PAUSED)
        exp1 = Experiment(
            experiment_id=uuid.uuid4(),
            experiment_business_id="EXP-CHECKOUT-001",
            name="One-Click Express Checkout Flow",
            description="Tests simplified single-page checkout vs. multi-step control.",
            status="DRAFT",
            primary_metric="checkout_conversion_rate",
            eligibility_rules={"channels": ["WEB", "APP"], "exclude_bots": True},
            allocation_config={"control": 50.0, "treatment": 50.0},
            experiment_salt="SALT-EXP-CHECKOUT-001",
            config_version=1,
            created_at=base_time,
            updated_at=base_time,
        )
        db.add(exp1)
        db.flush()

        exp1_ctrl = ExperimentVariant(
            variant_id=uuid.uuid4(),
            variant_business_id="VAR-CHK-CTRL",
            experiment_id=exp1.experiment_id,
            variant_name="Control_MultiStep",
            is_control=True,
            allocation_percentage=Decimal("50.00"),
            configuration={"layout": "multi_step"},
            config_version=1,
            created_at=base_time,
        )
        exp1_treat = ExperimentVariant(
            variant_id=uuid.uuid4(),
            variant_business_id="VAR-CHK-TREAT",
            experiment_id=exp1.experiment_id,
            variant_name="Treatment_ExpressOneClick",
            is_control=False,
            allocation_percentage=Decimal("50.00"),
            configuration={"layout": "express_one_click"},
            config_version=1,
            created_at=base_time,
        )
        db.add_all([exp1_ctrl, exp1_treat])
        db.flush()
        validate_and_activate_experiment(db, experiment_id=exp1.experiment_id, activated_at=base_time)

        exp2 = Experiment(
            experiment_id=uuid.uuid4(),
            experiment_business_id="EXP-BANNER-002",
            name="Free Shipping Threshold Promo Banner",
            description="Paused experiment demonstrating pause enforcement (EXP-006).",
            status="PAUSED",
            primary_metric="add_to_cart_rate",
            eligibility_rules={"channels": ["WEB"]},
            allocation_config={"control": 50.0, "treatment": 50.0},
            experiment_salt="SALT-EXP-BANNER-002",
            config_version=1,
            start_at=base_time,
            created_at=base_time,
            updated_at=base_time + timedelta(days=2),
        )
        db.add(exp2)
        db.flush()
        db.add_all(
            [
                ExperimentVariant(
                    variant_id=uuid.uuid4(),
                    variant_business_id="VAR-BNR-CTRL",
                    experiment_id=exp2.experiment_id,
                    variant_name="Control_NoBanner",
                    is_control=True,
                    allocation_percentage=Decimal("50.00"),
                    configuration={"banner": False},
                    config_version=1,
                    created_at=base_time,
                ),
                ExperimentVariant(
                    variant_id=uuid.uuid4(),
                    variant_business_id="VAR-BNR-TREAT",
                    experiment_id=exp2.experiment_id,
                    variant_name="Treatment_PromoBanner",
                    is_control=False,
                    allocation_percentage=Decimal("50.00"),
                    configuration={"banner": True},
                    config_version=1,
                    created_at=base_time,
                ),
            ]
        )
        db.flush()

        # =====================================================================
        # 3. CANONICAL HERO JOURNEY (JRN-HERO-001 / CUS-HERO-001)
        # End-to-End 10-step scenario from SUCCESS_CRITERIA.md Sec 16:
        # Anonymous discovery -> Consent GRANTED -> Product View -> Add to Cart ->
        # Login & Identity Stitch -> Experiment Assigned & Exposed -> Checkout ->
        # Failed Payment #1 -> Support Ticket Opened & Resolved ->
        # Succeeded Payment #2 -> Authoritative Order Confirmed -> Assisted Conversion!
        # =====================================================================
        hero_t0 = as_of - timedelta(days=3, hours=4)
        hero_cust = Customer(
            customer_id=uuid.uuid4(),
            customer_business_id="CUS-HERO-001",
            email="elena.rostova@example.com",
            first_name="Elena",
            last_name="Rostova",
            account_status="ACTIVE",
            created_at=hero_t0 - timedelta(days=30),
            updated_at=hero_t0,
        )
        db.add(hero_cust)
        db.flush()

        hero_anon_id = "ANON-HERO-001"
        hero_client_id = "CLI-WEB-HERO-001"

        hero_consent = ConsentRecord(
            consent_record_id=uuid.uuid4(),
            consent_business_id="CNS-HERO-001",
            customer_id=None,
            anonymous_id=hero_anon_id,
            consent_purpose="ANALYTICS",
            consent_status="GRANTED",
            effective_from=hero_t0,
            effective_to=None,
            recorded_at=hero_t0,
            source="COOKIE_BANNER_WEB",
        )
        db.add(hero_consent)
        db.flush()

        hero_journey = CustomerJourney(
            journey_id=uuid.uuid4(),
            journey_business_id="JRN-HERO-001",
            customer_id=hero_cust.customer_id,
            anonymous_id=None,
            first_touch_at=hero_t0,
            last_touch_at=hero_t0 + timedelta(hours=2),
            window_end_at=hero_t0 + timedelta(days=7),
            status="OPEN",
            identity_version=1,
            created_at=hero_t0,
        )
        db.add(hero_journey)
        db.flush()

        hero_cart = Cart(
            cart_id=uuid.uuid4(),
            cart_business_id="CRT-HERO-001",
            customer_id=hero_cust.customer_id,
            anonymous_id=hero_anon_id,
            session_id=None,
            status="ACTIVE",
            currency="USD",
            created_at=hero_t0 + timedelta(minutes=5),
            updated_at=hero_t0 + timedelta(minutes=5),
        )
        db.add(hero_cart)
        db.flush()
        add_or_update_cart_item(
            db,
            cart_id=hero_cart.cart_id,
            product_id=products[0].product_id,
            quantity=1,
            updated_at=hero_t0 + timedelta(minutes=5),
        )
        add_or_update_cart_item(
            db,
            cart_id=hero_cart.cart_id,
            product_id=products[5].product_id,
            quantity=1,
            updated_at=hero_t0 + timedelta(minutes=6),
        )
        validate_checkout_start(db, cart_id=hero_cart.cart_id, started_at=hero_t0 + timedelta(minutes=12))

        # Ingest Hero Journey events
        hero_events_spec = [
            ("EVT-HERO-001", "SESSION_STARTED", "WEB", hero_t0, hero_t0, None, None),
            ("EVT-HERO-002", "CONSENT_GRANTED", "WEB", hero_t0 + timedelta(minutes=1), hero_t0 + timedelta(minutes=1), None, None),
            ("EVT-HERO-003", "PRODUCT_VIEWED", "WEB", hero_t0 + timedelta(minutes=3), hero_t0 + timedelta(minutes=3), products[0].product_id, None),
            ("EVT-HERO-004", "ADD_TO_CART", "WEB", hero_t0 + timedelta(minutes=5), hero_t0 + timedelta(minutes=5), products[0].product_id, hero_cart.cart_id),
            ("EVT-HERO-005", "AUTH_LOGIN_SUCCESS", "WEB", hero_t0 + timedelta(minutes=8), hero_t0 + timedelta(minutes=8), None, None),
            ("EVT-HERO-006", "CHECKOUT_STARTED", "WEB", hero_t0 + timedelta(minutes=12), hero_t0 + timedelta(minutes=12), None, hero_cart.cart_id),
            ("EVT-HERO-007", "PAYMENT_FAILED", "WEB", hero_t0 + timedelta(minutes=15), hero_t0 + timedelta(minutes=15), None, hero_cart.cart_id),
            ("EVT-HERO-008", "SUPPORT_TICKET_CREATED", "WEB", hero_t0 + timedelta(minutes=18), hero_t0 + timedelta(minutes=18), None, hero_cart.cart_id),
            ("EVT-HERO-009", "PAYMENT_SUCCEEDED", "WEB", hero_t0 + timedelta(minutes=26), hero_t0 + timedelta(minutes=26), None, hero_cart.cart_id),
            ("EVT-HERO-010", "PURCHASE_COMPLETED", "WEB", hero_t0 + timedelta(minutes=27), hero_t0 + timedelta(minutes=27), products[0].product_id, hero_cart.cart_id),
        ]
        for seq_idx, (ebiz, etype, ech, ets, its, pid, cid) in enumerate(hero_events_spec, start=1):
            ingest_event(
                db,
                event_business_id=ebiz,
                event_type=etype,
                channel=ech,
                event_timestamp=ets,
                ingestion_timestamp=its,
                client_instance_id=hero_client_id,
                anonymous_id=hero_anon_id,
                customer_id=hero_cust.customer_id if seq_idx >= 5 else None,
                product_id=pid,
                cart_id=cid,
                source_sequence=seq_idx,
                properties={"scenario": "HERO_JOURNEY"},
            )

        # Stitch Hero Identity
        resolve_identity_link(
            db,
            anonymous_id=hero_anon_id,
            customer_id=hero_cust.customer_id,
            linked_at=hero_t0 + timedelta(minutes=8),
            link_method="AUTHENTICATED_CONSENT",
            client_instance_id=hero_client_id,
        )

        # Assign & Expose Hero Customer in EXP-CHECKOUT-001
        hero_asg = assign_experiment_variant(
            db,
            experiment_id=exp1.experiment_id,
            assigned_at=hero_t0 + timedelta(minutes=10),
            customer_id=hero_cust.customer_id,
            anonymous_id=hero_anon_id,
        )
        record_experiment_exposure(
            db,
            assignment_id=hero_asg.assignment_id,
            exposed_at=hero_t0 + timedelta(minutes=12),
            channel="WEB",
            exposure_surface="CHECKOUT_PAGE",
        )

        # Create Hero Order, Failed Payment #1, Linked Support Ticket, Succeeded Payment #2
        hero_order, _ = create_or_get_authoritative_order(
            db,
            order_business_id="ORD-HERO-001",
            purchase_source="WEB_CHECKOUT",
            source_purchase_reference="PUR-REF-HERO-001",
            placed_at=hero_t0 + timedelta(minutes=14),
            customer_id=hero_cust.customer_id,
            cart_id=hero_cart.cart_id,
            items=[
                {"product_id": products[0].product_id, "quantity": 1, "unit_price": products[0].current_price},
                {"product_id": products[5].product_id, "quantity": 1, "unit_price": products[5].current_price},
            ],
            tax_amount=Decimal("30.24"),
            shipping_amount=Decimal("0.00"),
        )
        # Attempt 1: FAILED
        record_payment_attempt(
            db,
            order_id=hero_order.order_id,
            status="FAILED",
            attempted_at=hero_t0 + timedelta(minutes=15),
            failure_reason="CARD_DECLINED_3DS_TIMEOUT",
            provider_reference="STRIPE-FAIL-HERO-01",
        )
        # Linked Support Ticket opened BEFORE purchase confirmation
        hero_ticket = SupportTicket(
            ticket_id=uuid.uuid4(),
            ticket_business_id="TKT-HERO-001",
            customer_id=hero_cust.customer_id,
            session_id=None,
            order_id=hero_order.order_id,
            journey_id=hero_journey.journey_id,
            issue_type="PAYMENT",
            priority="HIGH",
            status="OPEN",
            origin_channel="WEB",
            support_channel="LIVE_CHAT",
            created_at=hero_t0 + timedelta(minutes=18),
        )
        db.add(hero_ticket)
        db.flush()
        transition_support_ticket(
            db,
            ticket_id=hero_ticket.ticket_id,
            to_status="IN_PROGRESS",
            changed_at=hero_t0 + timedelta(minutes=20),
            actor_reference="agent-marcus",
            notes="Verified 3DS challenge reset for customer card.",
        )
        transition_support_ticket(
            db,
            ticket_id=hero_ticket.ticket_id,
            to_status="RESOLVED",
            changed_at=hero_t0 + timedelta(minutes=24),
            actor_reference="agent-marcus",
            notes="Customer retried payment successfully after gateway reset.",
        )
        # Attempt 2: SUCCEEDED
        record_payment_attempt(
            db,
            order_id=hero_order.order_id,
            status="SUCCEEDED",
            attempted_at=hero_t0 + timedelta(minutes=26),
            provider_reference="STRIPE-SUCC-HERO-02",
        )

        # =====================================================================
        # 4. GENERATE COHORTS COVERING ALL MANDATORY BUSINESS RULES & ANOMALIES
        # =====================================================================
        num_regular_journeys = 140 if scale == "mvp" else 2000
        bot_client_ids: set[str] = set()

        for idx in range(1, num_regular_journeys + 1):
            j_start = base_time + timedelta(hours=idx * 1.2, minutes=rng.randint(0, 25))
            channel = "WEB" if idx % 3 != 0 else "APP"
            cid = f"CLI-{channel}-{idx:04d}"
            anon_id = f"ANON-{idx:04d}"

            # Determine archetype for this journey
            if idx <= 12:
                archetype = "ANOM_001_LATE_EVENT_CONVERTED"
            elif idx <= 20:
                archetype = "ANOM_002_IDENTITY_CONFLICT"
            elif idx <= 30:
                archetype = "ANOM_002_UNAUTHORIZED_NO_CONSENT"
            elif idx <= 40:
                archetype = "ANOM_003_DUPLICATE_PURCHASE"
            elif idx <= 60:
                archetype = "ANOM_004_BOT_TRAFFIC"
                bot_client_ids.add(cid)
            elif idx <= 68:
                archetype = "SKIPPED_CART_DIRECT_PURCHASE"
            elif idx <= 82:
                archetype = "ABANDONED_CART_OPEN_JOURNEY"
            elif idx <= 90:
                archetype = "OUT_OF_WINDOW_PURCHASE"
            elif idx <= 112:
                archetype = "STANDARD_CONVERTED"
            else:
                archetype = "BROWSE_NON_CONVERTED"

            has_customer = archetype not in ("ANOM_004_BOT_TRAFFIC", "BROWSE_NON_CONVERTED")
            cust: Customer | None = None
            if has_customer:
                cust = Customer(
                    customer_id=uuid.uuid4(),
                    customer_business_id=f"CUS-{idx:04d}",
                    email=f"user{idx:04d}@journeyiq-demo.com",
                    first_name=f"Customer{idx}",
                    last_name="Demo",
                    account_status="ACTIVE",
                    created_at=j_start - timedelta(days=15),
                    updated_at=j_start,
                )
                db.add(cust)
                db.flush()

            # Create ConsentRecord according to archetype
            if archetype == "ANOM_002_UNAUTHORIZED_NO_CONSENT":
                consent_status = "DENIED" if idx % 2 == 0 else "UNKNOWN"
            else:
                consent_status = "GRANTED"

            consent_rec = ConsentRecord(
                consent_record_id=uuid.uuid4(),
                consent_business_id=f"CNS-{idx:04d}",
                customer_id=None,
                anonymous_id=anon_id,
                consent_purpose="ANALYTICS",
                consent_status=consent_status,
                effective_from=j_start,
                effective_to=None,
                recorded_at=j_start,
                source=f"{channel}_CONSENT_BANNER",
            )
            db.add(consent_rec)
            db.flush()

            # For OPEN_JOURNEY archetypes, make sure j_start is within the last 3 days before as_of
            if archetype == "ABANDONED_CART_OPEN_JOURNEY":
                j_start = as_of - timedelta(days=2, hours=(idx % 12) + 2)
            elif archetype in ("BROWSE_NON_CONVERTED", "OUT_OF_WINDOW_PURCHASE"):
                j_start = as_of - timedelta(days=9, hours=(idx % 10))

            journey = CustomerJourney(
                journey_id=uuid.uuid4(),
                journey_business_id=f"JRN-{idx:04d}",
                customer_id=cust.customer_id if (cust and consent_status == "GRANTED") else None,
                anonymous_id=None if (cust and consent_status == "GRANTED") else anon_id,
                first_touch_at=j_start,
                last_touch_at=j_start + timedelta(minutes=25),
                window_end_at=j_start + timedelta(days=7),
                status="OPEN",
                identity_version=1,
                created_at=j_start,
            )
            db.add(journey)
            db.flush()

            prod = products[idx % len(products)]

            # Identity linking & ANOM-002 handling
            if cust and archetype == "ANOM_002_IDENTITY_CONFLICT":
                # First link anon_id legitimately to cust
                resolve_identity_link(
                    db,
                    anonymous_id=anon_id,
                    customer_id=cust.customer_id,
                    linked_at=j_start + timedelta(minutes=2),
                    link_method="AUTHENTICATED_CONSENT",
                    client_instance_id=cid,
                )
                # Now create a second conflicting customer attempting to claim the same anonymous_id
                other_cust = Customer(
                    customer_id=uuid.uuid4(),
                    customer_business_id=f"CUS-CNF-{idx:04d}",
                    email=f"conflict{idx:04d}@journeyiq-demo.com",
                    first_name="Conflict",
                    last_name=f"User{idx}",
                    account_status="ACTIVE",
                    created_at=j_start,
                    updated_at=j_start,
                )
                db.add(other_cust)
                db.flush()
                resolve_identity_link(
                    db,
                    anonymous_id=anon_id,
                    customer_id=other_cust.customer_id,
                    linked_at=j_start + timedelta(minutes=4),
                    link_method="IMPERSONATED_MERGE",
                    client_instance_id=cid,
                )
                ground_truth["anom_002_identity_conflicts"].append(
                    {
                        "anonymous_id": anon_id,
                        "legitimate_customer": cust.customer_business_id,
                        "conflicting_customer": other_cust.customer_business_id,
                    }
                )
            elif cust and archetype == "ANOM_002_UNAUTHORIZED_NO_CONSENT":
                resolve_identity_link(
                    db,
                    anonymous_id=anon_id,
                    customer_id=cust.customer_id,
                    linked_at=j_start + timedelta(minutes=3),
                    link_method="AUTH_LOGIN_ONLY",
                    client_instance_id=cid,
                )
                ground_truth["anom_002_unauthorized_merges"].append(
                    {
                        "anonymous_id": anon_id,
                        "customer_business_id": cust.customer_business_id,
                        "consent_status": consent_status,
                    }
                )
            elif cust and consent_status == "GRANTED":
                resolve_identity_link(
                    db,
                    anonymous_id=anon_id,
                    customer_id=cust.customer_id,
                    linked_at=j_start + timedelta(minutes=2),
                    link_method="AUTHENTICATED_CONSENT",
                    client_instance_id=cid,
                )

            # Experiment assignment & exposure for human sessions
            if archetype != "ANOM_004_BOT_TRAFFIC":
                asg = assign_experiment_variant(
                    db,
                    experiment_id=exp1.experiment_id,
                    assigned_at=j_start + timedelta(minutes=1),
                    customer_id=cust.customer_id if (cust and consent_status == "GRANTED") else None,
                    anonymous_id=anon_id,
                )
                # Expose ~75% of assigned users (those who reach product/cart/checkout) so Assigned != Exposed (EXP-007/008)
                if idx % 4 != 0 or archetype in ("STANDARD_CONVERTED", "ANOM_001_LATE_EVENT_CONVERTED", "ANOM_003_DUPLICATE_PURCHASE"):
                    record_experiment_exposure(
                        db,
                        assignment_id=asg.assignment_id,
                        exposed_at=j_start + timedelta(minutes=4),
                        channel=channel,
                        exposure_surface="CHECKOUT_PAGE",
                    )

            # Emit events per archetype
            is_bot = archetype == "ANOM_004_BOT_TRAFFIC"
            props_base = {"is_bot": True, "bot_reason": "HEADLESS_SCRAPER_BURST"} if is_bot else {}
            if is_bot:
                ground_truth["anom_004_bot_sessions"].append(
                    {"client_instance_id": cid, "anonymous_id": anon_id, "channel": channel}
                )

            # Stage 1: SESSION_STARTED
            ingest_event(
                db,
                event_business_id=f"EVT-{idx:04d}-01",
                event_type="SESSION_STARTED",
                channel=channel,
                event_timestamp=j_start,
                ingestion_timestamp=j_start + timedelta(seconds=1),
                client_instance_id=cid,
                anonymous_id=anon_id,
                customer_id=cust.customer_id if (cust and consent_status == "GRANTED") else None,
                source_sequence=1,
                properties=props_base,
            )

            # Stage 2: PRODUCT_VIEWED (for ANOM_001, ingestion_timestamp is delayed 45 minutes!)
            pv_occ = j_start + timedelta(minutes=3)
            pv_ing = pv_occ + timedelta(minutes=45) if archetype == "ANOM_001_LATE_EVENT_CONVERTED" else pv_occ + timedelta(seconds=2)
            ev_pv, _ = ingest_event(
                db,
                event_business_id=f"EVT-{idx:04d}-02",
                event_type="PRODUCT_VIEWED",
                channel=channel,
                event_timestamp=pv_occ,
                ingestion_timestamp=pv_ing,
                client_instance_id=cid,
                anonymous_id=anon_id,
                customer_id=cust.customer_id if (cust and consent_status == "GRANTED") else None,
                product_id=prod.product_id,
                source_sequence=2,
                properties=props_base,
            )
            if archetype == "ANOM_001_LATE_EVENT_CONVERTED":
                ground_truth["anom_001_late_events"].append(
                    {
                        "event_business_id": ev_pv.event_business_id,
                        "event_timestamp": pv_occ.isoformat(),
                        "ingestion_timestamp": pv_ing.isoformat(),
                        "journey_business_id": journey.journey_business_id,
                    }
                )

            if archetype == "BROWSE_NON_CONVERTED" or is_bot:
                continue

            cart = Cart(
                cart_id=uuid.uuid4(),
                cart_business_id=f"CRT-{idx:04d}",
                customer_id=cust.customer_id if (cust and consent_status == "GRANTED") else None,
                anonymous_id=anon_id,
                session_id=None,
                status="ACTIVE",
                currency="USD",
                created_at=j_start + timedelta(minutes=5),
                updated_at=j_start + timedelta(minutes=5),
            )
            db.add(cart)
            db.flush()
            add_or_update_cart_item(
                db,
                cart_id=cart.cart_id,
                product_id=prod.product_id,
                quantity=1 + (idx % 2),
                updated_at=j_start + timedelta(minutes=6),
            )

            # Stage 3: ADD_TO_CART (skipped in SKIPPED_CART_DIRECT_PURCHASE to test FN-002!)
            if archetype != "SKIPPED_CART_DIRECT_PURCHASE":
                ingest_event(
                    db,
                    event_business_id=f"EVT-{idx:04d}-03",
                    event_type="ADD_TO_CART",
                    channel=channel,
                    event_timestamp=j_start + timedelta(minutes=6),
                    ingestion_timestamp=j_start + timedelta(minutes=6, seconds=2),
                    client_instance_id=cid,
                    anonymous_id=anon_id,
                    customer_id=cust.customer_id if (cust and consent_status == "GRANTED") else None,
                    product_id=prod.product_id,
                    cart_id=cart.cart_id,
                    source_sequence=3,
                    properties=props_base,
                )

            if archetype in ("ABANDONED_CART_OPEN_JOURNEY", "ANOM_002_IDENTITY_CONFLICT", "ANOM_002_UNAUTHORIZED_NO_CONSENT"):
                evaluate_cart_abandonment(db, cart_id=cart.cart_id, as_of_timestamp=as_of)
                continue

            # Stage 4: CHECKOUT_STARTED
            validate_checkout_start(db, cart_id=cart.cart_id, started_at=j_start + timedelta(minutes=10))
            ingest_event(
                db,
                event_business_id=f"EVT-{idx:04d}-04",
                event_type="CHECKOUT_STARTED",
                channel=channel,
                event_timestamp=j_start + timedelta(minutes=10),
                ingestion_timestamp=j_start + timedelta(minutes=10, seconds=1),
                client_instance_id=cid,
                anonymous_id=anon_id,
                customer_id=cust.customer_id if (cust and consent_status == "GRANTED") else None,
                product_id=prod.product_id,
                cart_id=cart.cart_id,
                source_sequence=4,
                properties=props_base,
            )

            # Stage 5: Order & Purchase
            purchase_time = (
                j_start + timedelta(days=8, hours=2)
                if archetype == "OUT_OF_WINDOW_PURCHASE"
                else j_start + timedelta(minutes=15)
            )
            pur_ref = f"PUR-REF-{idx:04d}"
            order, _ = create_or_get_authoritative_order(
                db,
                order_business_id=f"ORD-{idx:04d}",
                purchase_source=f"{channel}_CHECKOUT",
                source_purchase_reference=pur_ref,
                placed_at=purchase_time - timedelta(minutes=1),
                customer_id=cust.customer_id if (cust and consent_status == "GRANTED") else None,
                cart_id=cart.cart_id,
                items=[
                    {
                        "product_id": prod.product_id,
                        "quantity": 1 + (idx % 2),
                        "unit_price": prod.current_price,
                    }
                ],
                tax_amount=Decimal("12.50"),
            )
            if idx % 7 == 0:
                # Inject a failed payment attempt before succeeding
                record_payment_attempt(
                    db,
                    order_id=order.order_id,
                    status="FAILED",
                    attempted_at=purchase_time - timedelta(seconds=30),
                    failure_reason="GATEWAY_TIMEOUT",
                )
            record_payment_attempt(
                db,
                order_id=order.order_id,
                status="SUCCEEDED",
                attempted_at=purchase_time,
            )

            ingest_event(
                db,
                event_business_id=f"EVT-{idx:04d}-05",
                event_type="PURCHASE_COMPLETED",
                channel=channel,
                event_timestamp=purchase_time,
                ingestion_timestamp=purchase_time + timedelta(seconds=1),
                client_instance_id=cid,
                anonymous_id=anon_id,
                customer_id=cust.customer_id if (cust and consent_status == "GRANTED") else None,
                product_id=prod.product_id,
                cart_id=cart.cart_id,
                order_id=order.order_id,
                source_sequence=5,
                properties=props_base,
            )

            # ANOM-003: Duplicate conversion signals for the same authoritative order reference!
            if archetype == "ANOM_003_DUPLICATE_PURCHASE":
                _, is_dup = create_or_get_authoritative_order(
                    db,
                    order_business_id=f"ORD-DUP-{idx:04d}",
                    purchase_source=f"{channel}_CHECKOUT",
                    source_purchase_reference=pur_ref,
                    placed_at=purchase_time + timedelta(seconds=5),
                    customer_id=cust.customer_id if (cust and consent_status == "GRANTED") else None,
                    cart_id=cart.cart_id,
                    items=[
                        {
                            "product_id": prod.product_id,
                            "quantity": 1 + (idx % 2),
                            "unit_price": prod.current_price,
                        }
                    ],
                )
                ingest_event(
                    db,
                    event_business_id=f"EVT-{idx:04d}-05-DUP",
                    event_type="PURCHASE_COMPLETED",
                    channel=channel,
                    event_timestamp=purchase_time + timedelta(seconds=5),
                    ingestion_timestamp=purchase_time + timedelta(seconds=6),
                    client_instance_id=cid,
                    anonymous_id=anon_id,
                    customer_id=cust.customer_id if (cust and consent_status == "GRANTED") else None,
                    product_id=prod.product_id,
                    cart_id=cart.cart_id,
                    order_id=order.order_id,
                    source_sequence=6,
                    properties={"duplicate_retry_webhook": True},
                )
                if is_dup:
                    ground_truth["anom_003_duplicate_purchase_signals"].append(
                        {
                            "order_business_id": order.order_business_id,
                            "source_purchase_reference": pur_ref,
                            "duplicate_event_business_id": f"EVT-{idx:04d}-05-DUP",
                        }
                    )

        # 5. Quarantine events missing client_instance_id on WEB/APP (SES-006)
        for q_idx in range(1, 7):
            q_ts = base_time + timedelta(hours=q_idx * 3)
            ev_q, _ = ingest_event(
                db,
                event_business_id=f"EVT-MISSING-CID-{q_idx:03d}",
                event_type="PRODUCT_VIEWED",
                channel="WEB" if q_idx % 2 == 0 else "APP",
                event_timestamp=q_ts,
                ingestion_timestamp=q_ts + timedelta(seconds=2),
                client_instance_id=None,
                anonymous_id=f"ANON-BAD-{q_idx:03d}",
            )
            ground_truth["missing_client_instance_events"].append(ev_q.event_business_id)

        db.flush()

        # 6. Run deterministic Sessionization and link sessions to journeys, carts, orders, and tickets
        created_sessions = sessionize_events(db, bot_client_instances=bot_client_ids)

        # Map sessions back to journeys by customer_id or anonymous_id
        all_journeys = list(db.query(CustomerJourney).all())
        journey_by_cust = {j.customer_id: j for j in all_journeys if j.customer_id}
        journey_by_anon = {j.anonymous_id: j for j in all_journeys if j.anonymous_id}

        session_by_anon: dict[str, SessionRecord] = {}
        session_by_cust: dict[uuid.UUID, SessionRecord] = {}

        for s in created_sessions:
            if s.customer_id and s.customer_id in journey_by_cust:
                s.journey_id = journey_by_cust[s.customer_id].journey_id
            elif s.anonymous_id and s.anonymous_id in journey_by_anon:
                s.journey_id = journey_by_anon[s.anonymous_id].journey_id
            elif s.anonymous_id == hero_anon_id:
                s.journey_id = hero_journey.journey_id

            if s.anonymous_id:
                session_by_anon[s.anonymous_id] = s
            if s.customer_id:
                session_by_cust[s.customer_id] = s

        # Attach session_id to Carts, Orders, and SupportTickets
        for c in db.query(Cart).all():
            if c.anonymous_id and c.anonymous_id in session_by_anon:
                c.session_id = session_by_anon[c.anonymous_id].session_id
            elif c.customer_id and c.customer_id in session_by_cust:
                c.session_id = session_by_cust[c.customer_id].session_id

        for o in db.query(Order).all():
            if o.customer_id and o.customer_id in session_by_cust:
                o.session_id = session_by_cust[o.customer_id].session_id
            elif o.cart_id:
                cart_obj = db.get(Cart, o.cart_id)
                if cart_obj and cart_obj.session_id:
                    o.session_id = cart_obj.session_id

        for t in db.query(SupportTicket).all():
            if t.customer_id and t.customer_id in session_by_cust:
                t.session_id = session_by_cust[t.customer_id].session_id

        db.flush()

        # 7. Calculate JourneyOutcome & SessionOutcome for all journeys and sessions
        for j in all_journeys:
            calculate_journey_outcome(db, journey_id=j.journey_id, as_of_timestamp=as_of)
        for s in created_sessions:
            calculate_session_outcome(db, session_id=s.session_id, as_of_timestamp=as_of)

        db.commit()

        # 8. Build deterministic checksum & manifest
        event_ids = sorted(e.event_business_id for e in db.query(JourneyEvent).all())
        session_ids = sorted(s.session_business_id for s in db.query(SessionRecord).all())
        order_ids = sorted(o.order_business_id for o in db.query(Order).all())
        outcome_sigs = sorted(
            f"{o.outcome_business_id}:{o.outcome_status}:{o.attribution_status}:{','.join(o.friction_flags or [])}"
            for o in db.query(JourneyOutcome).all()
        )
        hash_payload = json.dumps(
            {
                "events": event_ids,
                "sessions": session_ids,
                "orders": order_ids,
                "outcomes": outcome_sigs,
            },
            sort_keys=True,
        ).encode("utf-8")
        dataset_checksum = hashlib.sha256(hash_payload).hexdigest()

        manifest = {
            "seed": seed,
            "scale": scale,
            "as_of_timestamp": as_of.isoformat(),
            "dataset_sha256": dataset_checksum,
            "counts": {
                "customers": db.query(Customer).count(),
                "products": db.query(Product).count(),
                "journeys": db.query(CustomerJourney).count(),
                "sessions": db.query(SessionRecord).count(),
                "events": db.query(JourneyEvent).count(),
                "carts": db.query(Cart).count(),
                "orders": db.query(Order).count(),
                "payment_attempts": db.query(PaymentAttempt).count(),
                "support_tickets": db.query(SupportTicket).count(),
                "consents": db.query(ConsentRecord).count(),
                "identity_links": db.query(IdentityLink).count(),
                "experiments": db.query(Experiment).count(),
                "experiment_assignments": db.query(ExperimentAssignment).count(),
                "experiment_exposures": db.query(ExperimentExposure).count(),
                "quarantined_events": db.query(EventQuarantine).count(),
                "journey_outcomes": db.query(JourneyOutcome).count(),
                "audit_events": db.query(AuditEvent).count(),
            },
            "anomaly_counts": {
                "anom_001_late_events": len(ground_truth["anom_001_late_events"]),
                "anom_002_identity_conflicts": len(ground_truth["anom_002_identity_conflicts"]),
                "anom_002_unauthorized_merges": len(ground_truth["anom_002_unauthorized_merges"]),
                "anom_003_duplicate_purchase_signals": len(ground_truth["anom_003_duplicate_purchase_signals"]),
                "anom_004_bot_sessions": len(ground_truth["anom_004_bot_sessions"]),
                "missing_client_instance_events": len(ground_truth["missing_client_instance_events"]),
            },
        }

        if write_files and target_out_dir is not None:
            _write_generation_artifacts(
                db,
                target_out_dir=target_out_dir,
                manifest=manifest,
                ground_truth=ground_truth,
                export_csv=export_csv,
            )

        return manifest
    finally:
        uuid.uuid4 = orig_uuid4  # type: ignore[assignment]


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="JourneyIQ Deterministic Synthetic Data Generator")
    parser.add_argument("--seed", type=int, default=42, help="Deterministic random seed")
    parser.add_argument("--scale", choices=["mvp", "full"], default="mvp", help="Dataset scale")
    parser.add_argument("--as-of", type=str, default=None, help="Optional UTC ISO-8601 as-of timestamp")
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
        help="Target directory for generation_manifest.json and ground_truth_anomalies.json",
    )
    parser.add_argument(
        "--no-write-files",
        action="store_true",
        help="Skip writing generation_manifest.json and ground_truth_anomalies.json",
    )
    parser.add_argument(
        "--export-csv",
        action="store_true",
        help="Also stream generated tables as CSV files into <output-dir>/csv/",
    )
    parser.add_argument(
        "--allow-live-demo-artifacts",
        action="store_true",
        help="Explicit opt-in required only when updating live demo artifacts under data-generation/output",
    )
    parser.add_argument(
        "--confirm-reset",
        action="store_true",
        help="Explicit confirmation required before truncating and regenerating the isolated target database",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_cli_parser()
    args = parser.parse_args(argv)

    # Fail-fast safety validation BEFORE any engine creation or database connection
    if not args.confirm_reset:
        parser.error("Destructive reset requires --confirm-reset. Refusing to connect or truncate database.")

    try:
        target_db_url = resolve_isolated_database_url(
            database_url=args.database_url,
            db_env_var=args.db_env_var,
        )
        write_files = not args.no_write_files
        out_dir = (
            resolve_safe_output_dir(
                args.output_dir,
                allow_live_demo_overwrite=args.allow_live_demo_artifacts,
            )
            if write_files
            else None
        )
        as_of_dt = parse_as_of_timestamp(args.as_of)
    except ValueError as exc:
        parser.error(str(exc))

    session_factory = create_isolated_session_factory(target_db_url)
    try:
        with session_factory() as db:
            manifest = generate_dataset(
                db,
                seed=args.seed,
                scale=args.scale,
                as_of=as_of_dt,
                write_files=write_files,
                output_dir=out_dir,
                allow_live_demo_overwrite=args.allow_live_demo_artifacts,
                export_csv=args.export_csv,
            )
            print(json.dumps(manifest, indent=2))
    finally:
        bind = session_factory.kw.get("bind")
        if bind is not None:
            bind.dispose()


if __name__ == "__main__":
    main()

