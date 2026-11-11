from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

ROOT_DIR = Path(__file__).resolve().parents[2]
BACKEND_DIR = ROOT_DIR / "backend"
DATA_GEN_DIR = ROOT_DIR / "data-generation" / "generator"
DATA_VAL_DIR = ROOT_DIR / "data-generation" / "validation"
for p in (BACKEND_DIR, DATA_GEN_DIR, DATA_VAL_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from app.core.config import LIVE_DEMO_OUTPUT_DIR, settings
from app.db.session import (
    Base,
    assert_safe_isolated_database_url,
    get_db,
    get_runtime_database_info,
    resolve_runtime_database_url,
)
from app.main import app
from app.models.entities import (
    Cart,
    ConsentRecord,
    Customer,
    CustomerJourney,
    EventQuarantine,
    Experiment,
    ExperimentVariant,
    IdentityLink,
    Product,
    SupportTicket,
)
from app.services.analytics_service import get_funnel_metrics
from app.services.domain_service import (
    add_or_update_cart_item,
    assign_experiment_variant,
    calculate_journey_outcome,
    create_or_get_authoritative_order,
    evaluate_cart_abandonment,
     get_cart_summary,
    ingest_event,
    record_experiment_exposure,
    record_payment_attempt,
    remove_cart_item,
    resolve_identity_link,
    sessionize_events,
    transition_support_ticket,
    validate_and_activate_experiment,
    validate_checkout_start,
    withdraw_consent,
)
from generate_data import generate_dataset
from validate_data import validate_database

def assert_safe_test_database_url(test_url: str | None, primary_url: str) -> str:
    """Fail-fast guard preventing destructive tests from targeting the primary or sandbox database."""
    if not test_url or not test_url.strip():
        raise RuntimeError(
            "TEST_DATABASE_URL is not configured. Refusing to run destructive database tests."
        )
    try:
        assert_safe_isolated_database_url(test_url, primary_url=primary_url)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    parsed_test = make_url(test_url)
    db_name = (parsed_test.database or "").strip()
    if "test" not in db_name.lower():
        raise RuntimeError(
            f"Unsafe test database name '{db_name}': test database name must explicitly include 'test' (e.g., 'journeyiq_test')."
        )
    if settings.sandbox_database_url:
        parsed_sandbox = make_url(settings.sandbox_database_url)
        if (
            (parsed_sandbox.database or "").strip().lower() == db_name.lower()
            and (parsed_sandbox.host or "") == (parsed_test.host or "")
            and parsed_sandbox.port == parsed_test.port
        ):
            raise RuntimeError(
                f"Unsafe test database '{db_name}': tests cannot reuse the persistent sandbox database."
            )
    return test_url


def assert_safe_test_session(db: Session) -> None:
    """Verify an active SQLAlchemy Session is bound strictly to the safe test database."""
    bind_url = str(db.get_bind().url)
    assert_safe_test_database_url(bind_url, settings.database_url)


TEST_DB_URL = assert_safe_test_database_url(settings.test_database_url, settings.database_url)
test_engine = create_engine(TEST_DB_URL, pool_pre_ping=True, future=True)
TestSessionLocal = sessionmaker(bind=test_engine, autoflush=False, autocommit=False, expire_on_commit=False)


def _clean_db(db: Session) -> None:
    assert_safe_test_session(db)
    Base.metadata.create_all(bind=test_engine)
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


@pytest.fixture()
def db_session() -> Session:
    with TestSessionLocal() as db:
        _clean_db(db)
        yield db


# ============================================================================
# 1. SESSIONIZATION TESTS (SES-001 through SES-011)
# ============================================================================


def test_ses_001_002_inactivity_boundary_and_ses_009_logical_end(db_session: Session) -> None:
    """SES-001, SES-002, SES-009: <=30m stays in same session; >30m starts new session; logical end is +30m."""
    t0 = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)
    # Event 1 at 10:00, Event 2 at 10:30 (exactly 30m -> same session), Event 3 at 11:00:01 (30m01s -> new session)
    ingest_event(
        db_session,
        event_business_id="E-SES-1",
        event_type="SESSION_STARTED",
        channel="WEB",
        event_timestamp=t0,
        ingestion_timestamp=t0,
        client_instance_id="CLI-1",
        anonymous_id="ANON-1",
    )
    ingest_event(
        db_session,
        event_business_id="E-SES-2",
        event_type="PRODUCT_VIEWED",
        channel="WEB",
        event_timestamp=t0 + timedelta(minutes=30),
        ingestion_timestamp=t0 + timedelta(minutes=30),
        client_instance_id="CLI-1",
        anonymous_id="ANON-1",
    )
    ingest_event(
        db_session,
        event_business_id="E-SES-3",
        event_type="PRODUCT_VIEWED",
        channel="WEB",
        event_timestamp=t0 + timedelta(minutes=60, seconds=1),
        ingestion_timestamp=t0 + timedelta(minutes=60, seconds=1),
        client_instance_id="CLI-1",
        anonymous_id="ANON-1",
    )
    sessions = sessionize_events(db_session)
    assert len(sessions) == 2
    assert sessions[0].started_at == t0
    assert sessions[0].last_event_at == t0 + timedelta(minutes=30)
    assert sessions[0].ended_at == t0 + timedelta(minutes=60)
    assert sessions[1].started_at == t0 + timedelta(minutes=60, seconds=1)


def test_ses_003_channel_separation_and_ses_006_quarantine(db_session: Session) -> None:
    """SES-003, SES-006: WEB and APP never merge into one session; missing client_instance_id is quarantined."""
    t0 = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
    ingest_event(
        db_session,
        event_business_id="E-WEB-1",
        event_type="SESSION_STARTED",
        channel="WEB",
        event_timestamp=t0,
        ingestion_timestamp=t0,
        client_instance_id="CLI-SHARED",
        anonymous_id="ANON-1",
    )
    ingest_event(
        db_session,
        event_business_id="E-APP-1",
        event_type="SESSION_STARTED",
        channel="APP",
        event_timestamp=t0 + timedelta(minutes=2),
        ingestion_timestamp=t0 + timedelta(minutes=2),
        client_instance_id="CLI-SHARED",
        anonymous_id="ANON-1",
    )
    _, q = ingest_event(
        db_session,
        event_business_id="E-MISSING-CID",
        event_type="PRODUCT_VIEWED",
        channel="WEB",
        event_timestamp=t0 + timedelta(minutes=3),
        ingestion_timestamp=t0 + timedelta(minutes=3),
        client_instance_id=None,
        anonymous_id="ANON-1",
    )
    assert q is not None
    assert q.issue_type == "MISSING_CLIENT_INSTANCE_ID"

    sessions = sessionize_events(db_session)
    assert len(sessions) == 2
    assert {s.channel for s in sessions} == {"WEB", "APP"}


def test_ses_004_007_008_late_arrival_mid_session_auth_and_explicit_end(db_session: Session) -> None:
    """SES-004, SES-007, SES-008: Reconstructs by event_timestamp, mid-session auth does not split, SESSION_ENDED splits."""
    t0 = datetime(2026, 11, 1, 14, 0, 0, tzinfo=timezone.utc)
    cust = Customer(
        customer_id=uuid.uuid4(),
        customer_business_id="CUS-SES-007",
        email="ses007@example.com",
        account_status="ACTIVE",
        created_at=t0,
        updated_at=t0,
    )
    db_session.add(cust)
    db_session.flush()

    # Ingest out-of-order (ANOM-001): login at t0+5m ingested at t0+6m, product view at t0+2m ingested at t0+25m
    ingest_event(
        db_session,
        event_business_id="E-1",
        event_type="SESSION_STARTED",
        channel="WEB",
        event_timestamp=t0,
        ingestion_timestamp=t0,
        client_instance_id="CLI-7",
        anonymous_id="ANON-7",
    )
    ingest_event(
        db_session,
        event_business_id="E-3-LOGIN",
        event_type="AUTH_LOGIN_SUCCESS",
        channel="WEB",
        event_timestamp=t0 + timedelta(minutes=5),
        ingestion_timestamp=t0 + timedelta(minutes=6),
        client_instance_id="CLI-7",
        anonymous_id="ANON-7",
        customer_id=cust.customer_id,
    )
    ev_late, _ = ingest_event(
        db_session,
        event_business_id="E-2-LATE",
        event_type="PRODUCT_VIEWED",
        channel="WEB",
        event_timestamp=t0 + timedelta(minutes=2),
        ingestion_timestamp=t0 + timedelta(minutes=25),
        client_instance_id="CLI-7",
        anonymous_id="ANON-7",
    )
    assert ev_late.properties.get("late_arrival_detected") is True

    # Explicit SESSION_ENDED at t0+10m, followed by another event at t0+12m (must start a 2nd session!)
    ingest_event(
        db_session,
        event_business_id="E-4-END",
        event_type="SESSION_ENDED",
        channel="WEB",
        event_timestamp=t0 + timedelta(minutes=10),
        ingestion_timestamp=t0 + timedelta(minutes=10),
        client_instance_id="CLI-7",
        anonymous_id="ANON-7",
        customer_id=cust.customer_id,
    )
    ingest_event(
        db_session,
        event_business_id="E-5-NEW",
        event_type="SESSION_STARTED",
        channel="WEB",
        event_timestamp=t0 + timedelta(minutes=12),
        ingestion_timestamp=t0 + timedelta(minutes=12),
        client_instance_id="CLI-7",
        anonymous_id="ANON-7",
        customer_id=cust.customer_id,
    )

    sessions = sessionize_events(db_session)
    assert len(sessions) == 2
    assert sessions[0].customer_id == cust.customer_id
    assert sessions[0].status == "FINALIZED"
    assert sessions[0].ended_at == t0 + timedelta(minutes=10)
    assert sessions[1].started_at == t0 + timedelta(minutes=12)


# ============================================================================
# 2. IDENTITY RESOLUTION & CONSENT TESTS (ID-001 through ID-009, ANOM-002)
# ============================================================================


def test_id_002_to_009_consent_gating_conflicts_and_withdrawal(db_session: Session) -> None:
    t0 = datetime(2026, 11, 2, 10, 0, 0, tzinfo=timezone.utc)
    c1 = Customer(
        customer_id=uuid.uuid4(),
        customer_business_id="CUS-ID-1",
        email="c1@example.com",
        account_status="ACTIVE",
        created_at=t0,
        updated_at=t0,
    )
    c2 = Customer(
        customer_id=uuid.uuid4(),
        customer_business_id="CUS-ID-2",
        email="c2@example.com",
        account_status="ACTIVE",
        created_at=t0,
        updated_at=t0,
    )
    db_session.add_all([c1, c2])
    db_session.flush()

    # 1. ID-003/004/005: Attempt stitching without consent (UNKNOWN) -> blocked & quarantined
    link_none, status_none = resolve_identity_link(
        db_session,
        anonymous_id="ANON-DENIED",
        customer_id=c1.customer_id,
        linked_at=t0,
        link_method="AUTH_LOGIN_ONLY",
    )
    assert link_none is None
    assert status_none == "BLOCKED_UNKNOWN"

    # 2. ID-002 & ID-007: Grant consent for ANON-OK -> stitch succeeds and is idempotent
    db_session.add(
        ConsentRecord(
            consent_record_id=uuid.uuid4(),
            consent_business_id="CNS-OK-1",
            customer_id=None,
            anonymous_id="ANON-OK",
            consent_purpose="ANALYTICS",
            consent_status="GRANTED",
            effective_from=t0,
            recorded_at=t0,
            source="WEB",
        )
    )
    db_session.flush()

    link1, st1 = resolve_identity_link(
        db_session,
        anonymous_id="ANON-OK",
        customer_id=c1.customer_id,
        linked_at=t0 + timedelta(minutes=1),
    )
    assert link1 is not None and st1 == "CREATED"

    link_dup, st_dup = resolve_identity_link(
        db_session,
        anonymous_id="ANON-OK",
        customer_id=c1.customer_id,
        linked_at=t0 + timedelta(minutes=2),
    )
    assert st_dup == "IDEMPOTENT_EXISTING"
    assert link_dup and link_dup.identity_link_id == link1.identity_link_id

    # 3. ID-006 / ANOM-002: Conflicting customer c2 tries to claim ANON-OK -> CONFLICTED + quarantined
    link_conf, st_conf = resolve_identity_link(
        db_session,
        anonymous_id="ANON-OK",
        customer_id=c2.customer_id,
        linked_at=t0 + timedelta(minutes=3),
    )
    assert st_conf == "CONFLICTED"
    assert link_conf and link_conf.status == "CONFLICTED"

    # 4. ID-008 / ID-009: Withdraw consent -> revokes active link and recalculates journey outcome version
    journey = CustomerJourney(
        journey_id=uuid.uuid4(),
        journey_business_id="JRN-ID-1",
        customer_id=c1.customer_id,
        anonymous_id=None,
        first_touch_at=t0,
        last_touch_at=t0 + timedelta(minutes=10),
        window_end_at=t0 + timedelta(days=7),
        status="OPEN",
        identity_version=1,
        created_at=t0,
    )
    db_session.add(journey)
    db_session.flush()
    out_v1 = calculate_journey_outcome(db_session, journey_id=journey.journey_id, as_of_timestamp=t0 + timedelta(hours=1))
    assert out_v1.calculation_version == "v1"

    w_res = withdraw_consent(
        db_session,
        customer_id=c1.customer_id,
        anonymous_id="ANON-OK",
        withdrawn_at=t0 + timedelta(hours=2),
    )
    assert len(w_res["revoked_identity_links"]) == 1
    assert w_res["recalculated_journeys"][0]["calculation_version"] == "v2"
    assert journey.identity_version == 2


# ============================================================================
# 3. CART, ORDER, PAYMENT & SUPPORT TESTS (CART, ORD, SUP, ANOM-003)
# ============================================================================


def test_cart_order_payment_and_support_rules(db_session: Session) -> None:
    t0 = datetime(2026, 11, 3, 9, 0, 0, tzinfo=timezone.utc)
    prod = Product(
        product_id=uuid.uuid4(),
        product_business_id="PRD-TEST-1",
        product_name="Test Product",
        category="Hardware",
        current_price=Decimal("100.00"),
        currency="USD",
        is_active=True,
        created_at=t0,
        updated_at=t0,
    )
    cust = Customer(
        customer_id=uuid.uuid4(),
        customer_business_id="CUS-CART-1",
        email="cart@example.com",
        account_status="ACTIVE",
        created_at=t0,
        updated_at=t0,
    )
    cart = Cart(
        cart_id=uuid.uuid4(),
        cart_business_id="CRT-TEST-1",
        customer_id=cust.customer_id,
        anonymous_id="ANON-CRT-1",
        status="ACTIVE",
        currency="USD",
        created_at=t0,
        updated_at=t0,
    )
    journey = CustomerJourney(
        journey_id=uuid.uuid4(),
        journey_business_id="JRN-CRT-1",
        customer_id=cust.customer_id,
        anonymous_id=None,
        first_touch_at=t0,
        last_touch_at=t0,
        window_end_at=t0 + timedelta(days=7),
        status="OPEN",
        identity_version=1,
        created_at=t0,
    )
    db_session.add_all([prod, cust])
    db_session.flush()
    db_session.add_all([cart, journey])
    db_session.flush()

    # CART-003: Empty cart checkout rejected
    with pytest.raises(ValueError, match="empty cart"):
        validate_checkout_start(db_session, cart_id=cart.cart_id)

    # CART-004: Quantity <= 0 rejected
    with pytest.raises(ValueError, match="greater than zero"):
        add_or_update_cart_item(
            db_session, cart_id=cart.cart_id, product_id=prod.product_id, quantity=0, updated_at=t0
        )

    # CART-005: Price snapshot preserved when product price changes
    item = add_or_update_cart_item(
        db_session, cart_id=cart.cart_id, product_id=prod.product_id, quantity=2, updated_at=t0
    )
    prod.current_price = Decimal("150.00")
    db_session.flush()
    assert item.unit_price == Decimal("100.00")

    # CART-001, CART-002, CART-008: <24h stays ACTIVE; >=24h becomes ABANDONED while 7d journey stays IN_PROGRESS
    evaluate_cart_abandonment(db_session, cart_id=cart.cart_id, as_of_timestamp=t0 + timedelta(hours=23, minutes=59))
    assert cart.status == "ACTIVE"
    evaluate_cart_abandonment(db_session, cart_id=cart.cart_id, as_of_timestamp=t0 + timedelta(hours=24))
    assert cart.status == "ABANDONED"
    j_out = calculate_journey_outcome(db_session, journey_id=journey.journey_id, as_of_timestamp=t0 + timedelta(hours=25))
    assert j_out.outcome_status == "IN_PROGRESS"

    # ORD-001 through ORD-006 & ANOM-003: Authoritative order deduplication & failed payment retry
    order1, is_dup1 = create_or_get_authoritative_order(
        db_session,
        order_business_id="ORD-T-1",
        purchase_source="WEB",
        source_purchase_reference="REF-100",
        placed_at=t0 + timedelta(hours=26),
        customer_id=cust.customer_id,
        cart_id=cart.cart_id,
        items=[{"product_id": prod.product_id, "quantity": 2, "unit_price": Decimal("100.00")}],
        tax_amount=Decimal("20.00"),
    )
    assert is_dup1 is False
    assert order1.total_amount == Decimal("220.00")

    # Duplicate purchase signal for same (purchase_source, source_purchase_reference)
    order_dup, is_dup2 = create_or_get_authoritative_order(
        db_session,
        order_business_id="ORD-T-1-DUP",
        purchase_source="WEB",
        source_purchase_reference="REF-100",
        placed_at=t0 + timedelta(hours=26, seconds=10),
        customer_id=cust.customer_id,
        cart_id=cart.cart_id,
        items=[{"product_id": prod.product_id, "quantity": 2, "unit_price": Decimal("100.00")}],
    )
    assert is_dup2 is True
    assert order_dup.order_id == order1.order_id

    # Payment Attempt #1 FAILED (non-terminal on Order!) -> Attempt #2 SUCCEEDED -> Order CONFIRMED
    pa1 = record_payment_attempt(
        db_session,
        order_id=order1.order_id,
        status="FAILED",
        attempted_at=t0 + timedelta(hours=26, minutes=1),
        failure_reason="DECLINED",
    )
    assert pa1.attempt_number == 1
    assert order1.status == "PENDING"

    pa2 = record_payment_attempt(
        db_session,
        order_id=order1.order_id,
        status="SUCCEEDED",
        attempted_at=t0 + timedelta(hours=26, minutes=5),
    )
    assert pa2.attempt_number == 2
    assert order1.status == "CONFIRMED"
    assert cart.status == "CONVERTED"

    # SUP-001, SUP-002: Support ticket state machine enforcement
    ticket = SupportTicket(
        ticket_id=uuid.uuid4(),
        ticket_business_id="TKT-T-1",
        customer_id=cust.customer_id,
        order_id=order1.order_id,
        journey_id=journey.journey_id,
        issue_type="PAYMENT",
        priority="HIGH",
        status="OPEN",
        origin_channel="WEB",
        support_channel="CHAT",
        created_at=t0 + timedelta(hours=26, minutes=2),
    )
    db_session.add(ticket)
    db_session.flush()

    with pytest.raises(ValueError, match="Invalid SupportTicket state transition"):
        transition_support_ticket(
            db_session, ticket_id=ticket.ticket_id, to_status="CLOSED", changed_at=t0 + timedelta(hours=26, minutes=3)
        )

    transition_support_ticket(
        db_session, ticket_id=ticket.ticket_id, to_status="IN_PROGRESS", changed_at=t0 + timedelta(hours=26, minutes=3)
    )
    transition_support_ticket(
        db_session, ticket_id=ticket.ticket_id, to_status="RESOLVED", changed_at=t0 + timedelta(hours=26, minutes=4)
    )
    assert ticket.status == "RESOLVED"


# ============================================================================
# 4. EXPERIMENTATION TESTS (EXP-001 through EXP-010)
# ============================================================================


def test_experiment_validation_bucketing_pause_and_exposure_dedup(db_session: Session) -> None:
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    exp = Experiment(
        experiment_id=uuid.uuid4(),
        experiment_business_id="EXP-TEST-01",
        name="Checkout Button Test",
        status="DRAFT",
        primary_metric="conversion_rate",
        experiment_salt="SALT-01",
        config_version=1,
        created_at=t0,
        updated_at=t0,
    )
    db_session.add(exp)
    db_session.flush()

    # EXP-002: <2 variants rejected
    with pytest.raises(ValueError, match="at least 2 variants"):
        validate_and_activate_experiment(db_session, experiment_id=exp.experiment_id, activated_at=t0)

    v_ctrl = ExperimentVariant(
        variant_id=uuid.uuid4(),
        variant_business_id="VAR-T-CTRL",
        experiment_id=exp.experiment_id,
        variant_name="Control",
        is_control=True,
        allocation_percentage=Decimal("50.00"),
        config_version=1,
        created_at=t0,
    )
    v_treat = ExperimentVariant(
        variant_id=uuid.uuid4(),
        variant_business_id="VAR-T-TREAT",
        experiment_id=exp.experiment_id,
        variant_name="Treatment",
        is_control=False,
        allocation_percentage=Decimal("40.00"),
        config_version=1,
        created_at=t0,
    )
    db_session.add_all([v_ctrl, v_treat])
    db_session.flush()

    # EXP-004: Allocation != 100.00% rejected
    with pytest.raises(ValueError, match="100.00%"):
        validate_and_activate_experiment(db_session, experiment_id=exp.experiment_id, activated_at=t0)

    v_treat.allocation_percentage = Decimal("50.00")
    db_session.flush()
    validate_and_activate_experiment(db_session, experiment_id=exp.experiment_id, activated_at=t0)
    assert exp.status == "RUNNING"

    # EXP-005: Deterministic idempotent assignment
    a1 = assign_experiment_variant(
        db_session, experiment_id=exp.experiment_id, assigned_at=t0, anonymous_id="ANON-EXP-1"
    )
    a2 = assign_experiment_variant(
        db_session, experiment_id=exp.experiment_id, assigned_at=t0 + timedelta(minutes=5), anonymous_id="ANON-EXP-1"
    )
    assert a1.assignment_id == a2.assignment_id

    # EXP-007, EXP-008, EXP-009: Exposure recorded separately & deduplicated
    ex1 = record_experiment_exposure(db_session, assignment_id=a1.assignment_id, exposed_at=t0 + timedelta(minutes=1))
    ex2 = record_experiment_exposure(db_session, assignment_id=a1.assignment_id, exposed_at=t0 + timedelta(minutes=2))
    assert ex1.exposure_id == ex2.exposure_id

    # EXP-006: PAUSED experiment rejects new assignments and exposures
    exp.status = "PAUSED"
    db_session.flush()
    with pytest.raises(ValueError, match="PAUSED"):
        assign_experiment_variant(
            db_session, experiment_id=exp.experiment_id, assigned_at=t0, anonymous_id="ANON-EXP-NEW"
        )


# ============================================================================
# 5. REPRODUCIBILITY, FUNNEL & 10-STEP END-TO-END API/UI VERIFICATION
# ============================================================================


def test_data_008_reproducibility_and_full_e2e_application() -> None:
    """
    Verify deterministic generator reproducibility (DATA-008) strictly on the isolated test database,
    validate all 7 data-quality checks, and override FastAPI get_db so E2E API/UI requests use journeyiq_test.
    """
    with TestSessionLocal() as db:
        assert_safe_test_session(db)
        m1 = generate_dataset(db, seed=42, scale="mvp", write_files=False)
        m2 = generate_dataset(db, seed=42, scale="mvp", write_files=False)
        assert m1["dataset_sha256"] == m2["dataset_sha256"]
        assert m1["counts"] == m2["counts"]

        val_report = validate_database(db, write_report=False)
        assert val_report["overall_status"] == "PASSED"

        # Verify FN-001 through FN-005 (Stage Reach >= Strict Sequential Progression, skipped cart purchases tracked)
        funnel = get_funnel_metrics(db, exclude_bots=True)
        assert funnel["skipped_cart_purchases"] > 0
        stage_5 = funnel["stages"][4]
        assert stage_5["reach_count"] > stage_5["sequential_count"]

    def _override_get_db():
        with TestSessionLocal() as test_db:
            yield test_db

    app.dependency_overrides[get_db] = _override_get_db
    try:
        client = TestClient(app)

        # 1. Health API
        r_health = client.get("/api/v1/health")
        assert r_health.status_code == 200
        assert r_health.json()["status"] == "healthy"

        # 2. Dashboard API & View
        r_dash_api = client.get("/api/v1/dashboard?exclude_bots=true")
        assert r_dash_api.status_code == 200
        dash_data = r_dash_api.json()
        assert dash_data["confirmed_orders"] > 0
        assert dash_data["duplicate_signals_suppressed"] == 10
        assert dash_data["bot_sessions"] == 20

        r_dash_ui = client.get("/dashboard")
        assert r_dash_ui.status_code == 200
        assert "Customer Journey &amp; Conversion Overview" in r_dash_ui.text
        assert "Data Quality" in r_dash_ui.text
        assert "Data Quality Guardrails" not in r_dash_ui.text

        # 3. Funnels API & View
        r_fun_api = client.get("/api/v1/funnels?channel=WEB&exclude_bots=true")
        assert r_fun_api.status_code == 200
        assert len(r_fun_api.json()["stages"]) == 5

        r_fun_ui = client.get("/funnels")
        assert r_fun_ui.status_code == 200
        assert "Stage Reach" in r_fun_ui.text
        assert "Strict Sequential Progression" in r_fun_ui.text

        # 4. Canonical 10-Step Hero Journey (JRN-HERO-001) Verification (SUCCESS_CRITERIA.md Sec 16)
        r_hero_api = client.get("/api/v1/journeys/JRN-HERO-001")
        assert r_hero_api.status_code == 200
        hero = r_hero_api.json()
        assert hero["journey"]["customer_business_id"] == "CUS-HERO-001"
        assert hero["current_outcome"]["outcome_status"] == "CONVERTED"
        assert hero["current_outcome"]["attribution_status"] == "ASSISTED"
        assert "PAYMENT_FAILURE" in hero["current_outcome"]["friction_flags"]
        assert "SUPPORT_CONTACTED" in hero["current_outcome"]["friction_flags"]
        assert len(hero["payment_attempts"]) == 2
        assert hero["payment_attempts"][0]["status"] == "FAILED"
        assert hero["payment_attempts"][1]["status"] == "SUCCEEDED"
        assert len(hero["support_tickets"]) == 1
        assert hero["support_tickets"][0]["status"] == "RESOLVED"

        r_hero_ui = client.get("/journeys/JRN-HERO-001")
        assert r_hero_ui.status_code == 200
        assert "JRN-HERO-001" in r_hero_ui.text
        assert "Support-assisted conversion" in r_hero_ui.text
        assert "Card declined (3DS timeout)" in r_hero_ui.text
        assert "Session started" in r_hero_ui.text
        assert "Added to cart" in r_hero_ui.text
        assert "Purchase completed" in r_hero_ui.text
        assert "Event Time" in r_hero_ui.text
        assert "Received Time" in r_hero_ui.text
        assert "CARD_DECLINED_3DS_TIMEOUT" in r_hero_ui.text  # preserved in Technical Details / attributes

        # 5. Versioned Recalculation API on JRN-0001 (in journeyiq_test only)
        r_recalc = client.post("/api/v1/journeys/JRN-0001/recalculate")
        assert r_recalc.status_code == 200
        assert r_recalc.json()["calculation_version"] == "v2"

        # 6. Exceptions API & View
        r_exc_api = client.get("/api/v1/exceptions")
        assert r_exc_api.status_code == 200
        exc_data = r_exc_api.json()
        assert exc_data["summary_counts"]["anom_001_late_events"] == 12
        assert exc_data["summary_counts"]["anom_002_conflicted_links"] == 8
        assert exc_data["summary_counts"]["anom_003_duplicate_conversions_suppressed"] == 10
        assert exc_data["summary_counts"]["anom_004_bot_sessions_isolated"] == 20
        assert len(exc_data["bot_sessions"]) == 20
        assert "resolved_at" in exc_data["quarantined_items"][0]

        r_exc_ui = client.get("/exceptions")
        assert r_exc_ui.status_code == 200
        assert "Data Quality &amp; Exceptions" in r_exc_ui.text
        assert "Identity merge blocked" in r_exc_ui.text
        assert "Identity merge blocked because" in r_exc_ui.text
        assert "Customer identity could not be verified because conflicting identity evidence was detected." in r_exc_ui.text
        assert "client identification was missing." in r_exc_ui.text
        assert "Isolated Bot Sessions (20)" in r_exc_ui.text
        assert "Headless scraper burst" in r_exc_ui.text
        assert "Technical Details" in r_exc_ui.text
        assert "ANOM-001" in r_exc_ui.text  # preserved inside Technical Details disclosure

        # 7. Experiments API & View
        r_exp_api = client.get("/api/v1/experiments")
        assert r_exp_api.status_code == 200
        assert r_exp_api.json()["count"] == 2

        r_exp_ui = client.get("/experiments")
        assert r_exp_ui.status_code == 200
        assert "EXP-CHECKOUT-001" in r_exp_ui.text
        assert "Assigned Participants" in r_exp_ui.text
        assert "Exposed Participants" in r_exp_ui.text
    finally:
        app.dependency_overrides.clear()


def test_journeys_pagination_and_filter_preservation(db_session: Session) -> None:
    """
    Verify database-level pagination on /journeys and /api/v1/journeys strictly on journeyiq_test:
    default UI page size 25, 100% non-duplicate reachability across all pages derived dynamically
    from the verified test dataset, boundary disabled states, filter/search preservation across pages,
    and safe handling of invalid page parameters.
    """
    import math
    from sqlalchemy import func as sa_func

    assert_safe_test_session(db_session)
    generate_dataset(db_session, seed=42, scale="mvp", write_files=False)

    expected_total = int(db_session.scalar(sa_func.count(CustomerJourney.journey_id)) or 0)
    assert expected_total > 50  # Verified test dataset spans >= 3 pages of 25
    expected_pages = math.ceil(expected_total / 25)
    expected_last_start = (expected_pages - 1) * 25 + 1

    def _override_get_db():
        with TestSessionLocal() as test_db:
            yield test_db

    app.dependency_overrides[get_db] = _override_get_db
    try:
        client = TestClient(app)

        # Verify every single journey is reachable across pages 1..expected_pages with zero duplicates
        seen_ids: list[str] = []
        page_counts: list[int] = []
        for page_num in range(1, expected_pages + 1):
            r_api = client.get(f"/api/v1/journeys?page={page_num}&page_size=25")
            assert r_api.status_code == 200
            payload = r_api.json()
            assert payload["total_count"] == expected_total
            assert payload["page"] == page_num
            assert payload["page_size"] == 25
            assert payload["total_pages"] == expected_pages
            page_counts.append(payload["count"])
            seen_ids.extend(item["journey_business_id"] for item in payload["items"])

        expected_last_count = expected_total - (expected_pages - 1) * 25
        assert page_counts == [25] * (expected_pages - 1) + [expected_last_count]
        assert len(seen_ids) == expected_total
        assert len(set(seen_ids)) == expected_total
        assert seen_ids[0] == "JRN-HERO-001"

        # Default /api/v1/journeys contract still supports limit=100 default
        r_api_default = client.get("/api/v1/journeys")
        assert r_api_default.status_code == 200
        assert r_api_default.json()["count"] == min(100, expected_total)

        # First page HTML: Showing 1-25 of expected_total, Previous disabled, Next links to page 2
        r_p1 = client.get("/journeys")
        assert r_p1.status_code == 200
        assert "1&ndash;25" in r_p1.text
        assert f'of <strong class="text-white">{expected_total}</strong> journeys' in r_p1.text
        assert f'Page <strong class="text-white">1</strong> of <strong class="text-white">{expected_pages}</strong>' in r_p1.text
        assert 'href="/journeys?page=2"' in r_p1.text
        assert '<button type="button" disabled aria-disabled="true"' in r_p1.text

        # Middle page HTML (Page 2): Showing 26-50, both Previous and Next enabled
        r_p2 = client.get("/journeys?page=2")
        assert r_p2.status_code == 200
        assert "26&ndash;50" in r_p2.text
        assert f'Page <strong class="text-white">2</strong> of <strong class="text-white">{expected_pages}</strong>' in r_p2.text
        assert 'href="/journeys?page=1"' in r_p2.text
        assert 'href="/journeys?page=3"' in r_p2.text
        assert '<button type="button" disabled' not in r_p2.text

        # Final page HTML: Showing expected_last_start-expected_total, Next disabled
        r_plast = client.get(f"/journeys?page={expected_pages}")
        assert r_plast.status_code == 200
        assert f"{expected_last_start}&ndash;{expected_total}" in r_plast.text
        assert f'Page <strong class="text-white">{expected_pages}</strong> of <strong class="text-white">{expected_pages}</strong>' in r_plast.text
        assert f'href="/journeys?page={expected_pages - 1}"' in r_plast.text
        assert '<button type="button" disabled aria-disabled="true"' in r_plast.text

        # Filter + search query parameter preservation across pages without duplicates
        r_filt_api = client.get("/api/v1/journeys?outcome_status=CONVERTED&search=JRN-&page_size=25")
        assert r_filt_api.status_code == 200
        filt_meta = r_filt_api.json()
        assert filt_meta["total_count"] > 25
        assert filt_meta["total_pages"] >= 2

        r_filt_ui = client.get("/journeys?outcome_status=CONVERTED&search=JRN-&page=2")
        assert r_filt_ui.status_code == 200
        assert 'Page <strong class="text-white">2</strong>' in r_filt_ui.text
        assert "outcome_status=CONVERTED" in r_filt_ui.text
        assert "search=JRN-" in r_filt_ui.text

        # Friction flag JSONB filtering at SQL level
        r_flag_api = client.get("/api/v1/journeys?friction_flag=PAYMENT_FAILURE")
        assert r_flag_api.status_code == 200
        assert r_flag_api.json()["total_count"] > 0
        assert any(item["journey_business_id"] == "JRN-HERO-001" for item in r_flag_api.json()["items"])

        # Invalid / out-of-range page parameters handled safely without errors
        for invalid in ("0", "-5", "abc"):
            r_inv = client.get(f"/journeys?page={invalid}")
            assert r_inv.status_code == 200
            assert "1&ndash;25" in r_inv.text
            assert f'Page <strong class="text-white">1</strong> of <strong class="text-white">{expected_pages}</strong>' in r_inv.text

        r_overflow = client.get("/journeys?page=999")
        assert r_overflow.status_code == 200
        assert f"{expected_last_start}&ndash;{expected_total}" in r_overflow.text
        assert f'Page <strong class="text-white">{expected_pages}</strong> of <strong class="text-white">{expected_pages}</strong>' in r_overflow.text

        # Empty search result renders safely with 0 count and both buttons disabled
        r_empty = client.get("/journeys?search=NO_SUCH_JOURNEY_999")
        assert r_empty.status_code == 200
        assert "No journeys matched the selected filters." in r_empty.text
        assert 'Page <strong class="text-white">1</strong> of <strong class="text-white">1</strong>' in r_empty.text
    finally:
        app.dependency_overrides.clear()


def test_quarantine_resolution_validation_and_responsive_header(db_session: Session) -> None:
    """
    Verify /api/v1/exceptions/quarantine/{id}/resolve and /exceptions/quarantine/{id}/resolve-ui
    validate malformed UUIDs, missing records, and invalid statuses safely on journeyiq_test,
    support both Resolve and Dismiss with optional resolution notes (without inventing default notes),
    preserve raw reasons inside Technical Details, and verify presentation helpers.
    """
    from app.core.presentation import summarize_quarantine_issue
    from app.models.entities import EventQuarantine

    assert_safe_test_session(db_session)
    assert "structure or required fields were invalid" in summarize_quarantine_issue("MALFORMED_EVENT")
    assert "conflicting identity evidence" in summarize_quarantine_issue("IDENTITY_CONFLICT")
    assert "consent was denied" in summarize_quarantine_issue(
        "UNAUTHORIZED_IDENTITY_MERGE", "consent_status is 'DENIED'"
    )
    assert "consent was unavailable" in summarize_quarantine_issue("UNAUTHORIZED_IDENTITY_MERGE", None)
    assert "Technical Details" in summarize_quarantine_issue("CUSTOM_UNKNOWN_CODE", "Opaque internal msg")

    t0 = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)
    q1 = EventQuarantine(
        quarantine_id=uuid.uuid4(),
        quarantine_business_id="QUAR-TEST-001",
        issue_type="UNAUTHORIZED_IDENTITY_MERGE",
        reason="Raw internal reason UNAUTH-001 preserved verbatim",
        detected_at=t0,
        resolution_status="OPEN",
    )
    q2 = EventQuarantine(
        quarantine_id=uuid.uuid4(),
        quarantine_business_id="QUAR-TEST-002",
        issue_type="MISSING_CLIENT_INSTANCE_ID",
        reason="WEB event 'EVT-MISS-1' is missing required client_instance_id (SES-006).",
        detected_at=t0 + timedelta(minutes=5),
        resolution_status="OPEN",
    )
    db_session.add_all([q1, q2])
    db_session.commit()

    def _override_get_db():
        with TestSessionLocal() as test_db:
            yield test_db

    app.dependency_overrides[get_db] = _override_get_db
    try:
        client = TestClient(app)

        # Malformed UUID -> 400 on API, 303 safe error redirect on UI (no 500 crash)
        r_bad_api = client.post(
            "/api/v1/exceptions/quarantine/not-a-uuid/resolve",
            json={"resolution_status": "RESOLVED", "resolution_notes": "ok"},
        )
        assert r_bad_api.status_code == 400

        r_bad_ui = client.post(
            "/exceptions/quarantine/not-a-uuid/resolve-ui",
            data={"resolution_status": "RESOLVED", "resolution_notes": "ok"},
            follow_redirects=True,
        )
        assert r_bad_ui.status_code == 200
        assert "Invalid quarantine UUID." in r_bad_ui.text
        assert "bg-rose-500/20" in r_bad_ui.text

        # Missing UUID -> 404 on API, 303 error redirect on UI
        missing_id = str(uuid.uuid4())
        r_missing_api = client.post(
            f"/api/v1/exceptions/quarantine/{missing_id}/resolve",
            json={"resolution_status": "RESOLVED", "resolution_notes": "ok"},
        )
        assert r_missing_api.status_code == 404

        r_missing_ui = client.post(
            f"/exceptions/quarantine/{missing_id}/resolve-ui",
            data={"resolution_status": "RESOLVED", "resolution_notes": "ok"},
            follow_redirects=True,
        )
        assert r_missing_ui.status_code == 200
        assert "Quarantine record not found." in r_missing_ui.text

        # Invalid resolution_status -> 400 on API, 303 error redirect on UI without mutating DB
        r_status_ui = client.post(
            f"/exceptions/quarantine/{q1.quarantine_id}/resolve-ui",
            data={"resolution_status": "BOGUS_STATUS", "resolution_notes": "hack"},
            follow_redirects=True,
        )
        assert r_status_ui.status_code == 200
        assert "Invalid resolution_status." in r_status_ui.text

        db_session.expire_all()
        assert db_session.get(EventQuarantine, q1.quarantine_id).resolution_status == "OPEN"

        # Resolve q1 without submitting a note -> persists None (no invented default note) & human-readable banner
        r_ok_ui = client.post(
            f"/exceptions/quarantine/{q1.quarantine_id}/resolve-ui",
            data={"resolution_status": "RESOLVED", "resolution_notes": ""},
            follow_redirects=True,
        )
        assert r_ok_ui.status_code == 200
        assert "Record QUAR-TEST-001 marked as resolved." in r_ok_ui.text
        assert "Raw internal reason UNAUTH-001 preserved verbatim" in r_ok_ui.text
        assert "flex-wrap" in r_ok_ui.text

        db_session.expire_all()
        updated_q1 = db_session.get(EventQuarantine, q1.quarantine_id)
        assert updated_q1.resolution_status == "RESOLVED"
        assert updated_q1.resolution_notes is None
        assert updated_q1.resolved_at is not None

        # Dismiss q2 with explicit optional note -> persists custom note, timestamp, and displays both in UI
        r_dismiss_ui = client.post(
            f"/exceptions/quarantine/{q2.quarantine_id}/resolve-ui",
            data={"resolution_status": "DISMISSED", "resolution_notes": "Confirmed benign SDK retry"},
            follow_redirects=True,
        )
        assert r_dismiss_ui.status_code == 200
        assert "Record QUAR-TEST-002 marked as dismissed." in r_dismiss_ui.text
        assert "Confirmed benign SDK retry" in r_dismiss_ui.text
        assert "Reviewed at:" in r_dismiss_ui.text

        db_session.expire_all()
        updated_q2 = db_session.get(EventQuarantine, q2.quarantine_id)
        assert updated_q2.resolution_status == "DISMISSED"
        assert updated_q2.resolution_notes == "Confirmed benign SDK retry"
        assert updated_q2.resolved_at is not None
    finally:
        app.dependency_overrides.clear()


def test_generator_and_validator_cli_safety_before_connection(monkeypatch, tmp_path: Path, db_session: Session) -> None:
    """
    Prove that generate_data.main and validate_data.main reject missing, ambiguous, malformed,
    primary, non-isolated DB configs, missing --confirm-reset, and unsafe live demo output paths
    BEFORE create_engine or any database connection attempt occurs.
    """
    import app.db.session as session_mod
    import generate_data
    import validate_data

    def _forbid_engine(*args, **kwargs):
        raise AssertionError("create_engine must not be called when safety validation fails!")

    monkeypatch.setattr(session_mod, "create_engine", _forbid_engine)
    monkeypatch.delenv("MISSING_ISOLATED_DB_URL", raising=False)
    monkeypatch.setenv("PRIMARY_LEAK_DB_URL", settings.database_url)
    monkeypatch.setenv("SAFE_ISOLATED_DB_URL", TEST_DB_URL)

    safe_out = str(tmp_path / "safe_out")
    live_out_rel = str(LIVE_DEMO_OUTPUT_DIR / ".." / "output")

    unsafe_gen_cases = [
        # Missing --confirm-reset
        ["--database-url", TEST_DB_URL, "--output-dir", safe_out],
        # Missing DB config
        ["--confirm-reset", "--output-dir", safe_out],
        # Missing env var value
        ["--confirm-reset", "--db-env-var", "MISSING_ISOLATED_DB_URL", "--output-dir", safe_out],
        # Ambiguous config (both --database-url and --db-env-var)
        ["--confirm-reset", "--database-url", TEST_DB_URL, "--db-env-var", "SAFE_ISOLATED_DB_URL", "--output-dir", safe_out],
        # Malformed URL
        ["--confirm-reset", "--database-url", "postgresql://:bad_port_here/journeyiq_test", "--output-dir", safe_out],
        # Missing DB name
        ["--confirm-reset", "--database-url", "postgresql+psycopg://postgres@127.0.0.1:5439", "--output-dir", safe_out],
        # Primary journeyiq DB directly and via env var
        ["--confirm-reset", "--database-url", settings.database_url, "--output-dir", safe_out],
        ["--confirm-reset", "--db-env-var", "PRIMARY_LEAK_DB_URL", "--output-dir", safe_out],
        # Non-test / non-sandbox DB name
        ["--confirm-reset", "--database-url", "postgresql+psycopg://postgres@127.0.0.1:5439/production_db", "--output-dir", safe_out],
        # Missing --output-dir without --no-write-files
        ["--confirm-reset", "--database-url", TEST_DB_URL],
        # Live demo output directory (exact and relative variant)
        ["--confirm-reset", "--database-url", TEST_DB_URL, "--output-dir", str(LIVE_DEMO_OUTPUT_DIR)],
        ["--confirm-reset", "--database-url", TEST_DB_URL, "--output-dir", live_out_rel],
    ]
    for argv in unsafe_gen_cases:
        with pytest.raises(SystemExit) as exc_info:
            generate_data.main(argv)
        assert exc_info.value.code != 0

    unsafe_val_cases = [
        ["--output-dir", safe_out],
        ["--db-env-var", "MISSING_ISOLATED_DB_URL", "--output-dir", safe_out],
        ["--database-url", TEST_DB_URL, "--db-env-var", "SAFE_ISOLATED_DB_URL", "--output-dir", safe_out],
        ["--database-url", "postgresql://:bad_port_here/journeyiq_test", "--output-dir", safe_out],
        ["--database-url", "postgresql+psycopg://postgres@127.0.0.1:5439", "--output-dir", safe_out],
        ["--database-url", settings.database_url, "--output-dir", safe_out],
        ["--database-url", "postgresql+psycopg://postgres@127.0.0.1:5439/analytics_prod", "--output-dir", safe_out],
        ["--database-url", TEST_DB_URL],
        ["--database-url", TEST_DB_URL, "--output-dir", str(LIVE_DEMO_OUTPUT_DIR)],
    ]
    for argv in unsafe_val_cases:
        with pytest.raises(SystemExit) as exc_info:
            validate_data.main(argv)
        assert exc_info.value.code != 0

    # Programmatic artifact safety also blocks implicit overwrites before touching DB
    with pytest.raises(ValueError):
        generate_dataset(db_session, seed=42, scale="mvp", write_files=True)
    with pytest.raises(ValueError):
        generate_dataset(db_session, seed=42, scale="mvp", write_files=True, output_dir=LIVE_DEMO_OUTPUT_DIR)
    with pytest.raises(ValueError):
        validate_database(db_session, write_report=True)
    with pytest.raises(ValueError):
        validate_database(db_session, write_report=True, output_dir=LIVE_DEMO_OUTPUT_DIR)


@pytest.fixture()
def disposable_cli_db_url() -> str:
    """Create and tear down an isolated temporary database strictly for CLI subprocess execution tests."""
    import re

    db_name = f"journeyiq_cli_test_{uuid.uuid4().hex[:8]}"
    assert re.fullmatch(r"journeyiq_cli_test_[0-9a-f]{8}", db_name)
    disposable_url = make_url(TEST_DB_URL).set(database=db_name).render_as_string(hide_password=False)
    assert_safe_test_database_url(disposable_url, settings.database_url)

    admin_url = make_url(TEST_DB_URL).set(database="postgres")
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT", future=True)
    created_by_fixture = False
    try:
        with admin_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :db_name"),
                {"db_name": db_name},
            ).scalar()
            if exists:
                raise RuntimeError(f"Disposable CLI test database '{db_name}' already exists.")
            conn.execute(text(f'CREATE DATABASE "{db_name}"'))
            created_by_fixture = True
        yield disposable_url
    finally:
        if created_by_fixture:
            with admin_engine.connect() as conn:
                conn.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) "
                        "FROM pg_stat_activity "
                        "WHERE datname = :db_name AND pid <> pg_backend_pid()"
                    ),
                    {"db_name": db_name},
                )
                conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        admin_engine.dispose()


def test_generator_and_validator_cli_isolated_execution(
    disposable_cli_db_url: str,
    tmp_path: Path,
) -> None:
    """
    Verify valid isolated CLI subprocess execution against a dedicated disposable database
    (journeyiq_cli_test_<hex>), leaving both journeyiq and journeyiq_test untouched, along with
    custom --output-dir artifact generation/validation formats, --as-of support, and programmatic compatibility.
    """
    import json
    import os
    import subprocess

    assert make_url(disposable_cli_db_url).database != make_url(TEST_DB_URL).database
    assert make_url(disposable_cli_db_url).database != (make_url(settings.database_url).database or "journeyiq")

    gen_out = tmp_path / "gen_artifacts"
    val_out = tmp_path / "val_artifacts"
    cli_env = os.environ.copy()
    cli_env["ISOLATED_GEN_TEST_URL"] = disposable_cli_db_url

    gen_proc = subprocess.run(
        [
            sys.executable,
            str(DATA_GEN_DIR / "generate_data.py"),
            "--db-env-var",
            "ISOLATED_GEN_TEST_URL",
            "--output-dir",
            str(gen_out),
            "--seed",
            "42",
            "--scale",
            "mvp",
            "--as-of",
            "2026-11-10T00:00:00Z",
            "--confirm-reset",
        ],
        cwd=str(ROOT_DIR),
        env=cli_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert gen_proc.returncode == 0, gen_proc.stderr
    gen_stdout = json.loads(gen_proc.stdout)
    assert gen_stdout["seed"] == 42
    assert gen_stdout["scale"] == "mvp"
    assert gen_stdout["counts"]["sessions"] == 149
    assert gen_stdout["counts"]["events"] == 510

    manifest_file = gen_out / "generation_manifest.json"
    gt_file = gen_out / "ground_truth_anomalies.json"
    assert manifest_file.exists()
    assert gt_file.exists()
    assert json.loads(manifest_file.read_text(encoding="utf-8"))["dataset_sha256"] == gen_stdout["dataset_sha256"]

    # Non-destructive validation CLI subprocess does not require --confirm-reset
    val_proc = subprocess.run(
        [
            sys.executable,
            str(DATA_VAL_DIR / "validate_data.py"),
            "--database-url",
            disposable_cli_db_url,
            "--output-dir",
            str(val_out),
        ],
        cwd=str(ROOT_DIR),
        env=cli_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert val_proc.returncode == 0, val_proc.stderr
    val_stdout = json.loads(val_proc.stdout)
    assert val_stdout["overall_status"] == "PASSED"
    assert val_stdout["total_checks"] == 7
    val_file = val_out / "validation_report.json"
    assert val_file.exists()
    assert json.loads(val_file.read_text(encoding="utf-8"))["overall_status"] == "PASSED"

    # Programmatic calls with isolated explicit output_dir also run strictly on the disposable DB
    cli_engine = create_engine(disposable_cli_db_url, pool_pre_ping=True, future=True)
    cli_session_local = sessionmaker(bind=cli_engine, autoflush=False, autocommit=False, expire_on_commit=False)
    try:
        with cli_session_local() as db:
            assert_safe_test_session(db)
            prog_out = tmp_path / "prog_artifacts"
            m_prog = generate_dataset(db, seed=42, scale="mvp", write_files=True, output_dir=prog_out)
            r_prog = validate_database(db, write_report=True, output_dir=prog_out)
            assert m_prog["dataset_sha256"] == gen_stdout["dataset_sha256"]
            assert r_prog["overall_status"] == "PASSED"
            assert (prog_out / "generation_manifest.json").exists()
            assert (prog_out / "ground_truth_anomalies.json").exists()
            assert (prog_out / "validation_report.json").exists()
    finally:
        cli_engine.dispose()


def test_full_scale_bulk_pipeline_determinism_and_sql_validator(
    disposable_cli_db_url: str,
    tmp_path: Path,
) -> None:
    """
    Verify scaled bulk generation (`scale="full"`), streaming SHA-256 determinism across chunked runs,
    CSV export, and set-based SQL validation (`DATA-001`..`DATA-006`, `ANOM-001-004`) on an isolated DB.
    """
    import json

    full_engine = create_engine(disposable_cli_db_url, pool_pre_ping=True, future=True)
    full_session_local = sessionmaker(bind=full_engine, autoflush=False, autocommit=False, expire_on_commit=False)
    try:
        out_one = tmp_path / "full_run_one"
        out_two = tmp_path / "full_run_two"
        with full_session_local() as db:
            assert_safe_test_session(db)
            manifest_one = generate_dataset(
                db,
                seed=42,
                scale="full",
                write_files=True,
                output_dir=out_one,
                export_csv=True,
                full_journeys=220,
                full_chunk_size=75,
            )
            report_one = validate_database(db, write_report=True, output_dir=out_one)

        with full_session_local() as db:
            assert_safe_test_session(db)
            manifest_two = generate_dataset(
                db,
                seed=42,
                scale="full",
                write_files=True,
                output_dir=out_two,
                export_csv=False,
                full_journeys=220,
                full_chunk_size=75,
            )

        assert manifest_one["scale"] == "full"
        assert manifest_one["dataset_sha256"] == manifest_two["dataset_sha256"]
        assert manifest_one["counts"] == manifest_two["counts"]
        assert manifest_one["counts"]["journeys"] == 221
        assert manifest_one["counts"]["sessions"] > manifest_one["counts"]["journeys"]
        assert manifest_one["counts"]["events"] > manifest_one["counts"]["journeys"] * 18

        assert report_one["overall_status"] == "PASSED"
        assert len(report_one["checks"]) == 7
        assert all(check["passed"] for check in report_one["checks"])

        gt = json.loads((out_one / "ground_truth_anomalies.json").read_text(encoding="utf-8"))
        assert len(gt["anom_001_late_events"]) >= 1
        assert len(gt["anom_002_identity_conflicts"]) >= 1
        assert len(gt["anom_002_unauthorized_merges"]) >= 1
        assert len(gt["anom_003_duplicate_purchase_signals"]) >= 1
        assert len(gt["anom_004_bot_sessions"]) >= 1

        csv_dir = out_one / "csv"
        assert (csv_dir / "journey_event.csv").exists()
        assert (csv_dir / "session.csv").exists()
        assert (csv_dir / "orders.csv").exists()
    finally:
        full_engine.dispose()


def test_runtime_database_target_selection_and_isolation() -> None:
    """Verify explicit primary vs. sandbox runtime DB selection, test/sandbox isolation, and credential redaction."""
    primary_url = "postgresql+psycopg://postgres:secret_pw@127.0.0.1:5439/journeyiq"
    sandbox_url = "postgresql+psycopg://postgres:secret_sbx@127.0.0.1:5432/journeyiq_sandbox"

    assert resolve_runtime_database_url("primary", primary_url=primary_url, sandbox_url=sandbox_url) == (
        "primary",
        primary_url,
    )
    assert resolve_runtime_database_url("sandbox", primary_url=primary_url, sandbox_url=sandbox_url) == (
        "sandbox",
        sandbox_url,
    )

    with pytest.raises(ValueError):
        resolve_runtime_database_url("sandbox", primary_url=primary_url, sandbox_url="")
    with pytest.raises(ValueError):
        resolve_runtime_database_url("sandbox", primary_url=primary_url, sandbox_url=primary_url)
    with pytest.raises(ValueError):
        resolve_runtime_database_url("unknown_target", primary_url=primary_url, sandbox_url=sandbox_url)

    # Pytest destructive guard must reject primary DB and sandbox DB
    with pytest.raises(RuntimeError):
        assert_safe_test_database_url(primary_url, primary_url)
    with pytest.raises(RuntimeError):
        assert_safe_test_database_url(sandbox_url, primary_url)
    if settings.sandbox_database_url:
        with pytest.raises(RuntimeError):
            assert_safe_test_database_url(settings.sandbox_database_url, settings.database_url)

    redacted = get_runtime_database_info(sandbox_url, target="sandbox")
    assert redacted["target"] == "sandbox"
    assert redacted["database"] == "journeyiq_sandbox"
    assert "secret_sbx" not in redacted["url_redacted"]
    assert "secret_sbx" not in repr(redacted)
