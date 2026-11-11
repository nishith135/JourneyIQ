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

from app.core.config import settings
from app.db.session import Base, get_db
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
    """Fail-fast guard preventing destructive tests from targeting the primary database."""
    if not test_url or not test_url.strip():
        raise RuntimeError(
            "TEST_DATABASE_URL is not configured. Refusing to run destructive database tests."
        )
    parsed_test = make_url(test_url)
    parsed_primary = make_url(primary_url)
    db_name = (parsed_test.database or "").strip()
    primary_db_name = (parsed_primary.database or "").strip()

    if not db_name or db_name == "journeyiq" or db_name == primary_db_name:
        raise RuntimeError(
            f"Unsafe test database '{db_name}': destructive tests cannot target primary database '{primary_db_name}'."
        )
    if "test" not in db_name.lower():
        raise RuntimeError(
            f"Unsafe test database name '{db_name}': test database name must explicitly include 'test' (e.g., 'journeyiq_test')."
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
        assert "Executive Journey" in r_dash_ui.text

        # 3. Funnels API & View
        r_fun_api = client.get("/api/v1/funnels?channel=WEB&exclude_bots=true")
        assert r_fun_api.status_code == 200
        assert len(r_fun_api.json()["stages"]) == 5

        r_fun_ui = client.get("/funnels")
        assert r_fun_ui.status_code == 200
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
        assert "CARD_DECLINED_3DS_TIMEOUT" in r_hero_ui.text

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

        r_exc_ui = client.get("/exceptions")
        assert r_exc_ui.status_code == 200
        assert "ANOM-001" in r_exc_ui.text

        # 7. Experiments API & View
        r_exp_api = client.get("/api/v1/experiments")
        assert r_exp_api.status_code == 200
        assert r_exp_api.json()["count"] == 2

        r_exp_ui = client.get("/experiments")
        assert r_exp_ui.status_code == 200
        assert "EXP-CHECKOUT-001" in r_exp_ui.text
    finally:
        app.dependency_overrides.clear()
