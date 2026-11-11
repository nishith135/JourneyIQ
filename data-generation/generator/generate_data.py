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

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.session import Base, SessionLocal
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


def generate_dataset(
    db: Session,
    *,
    seed: int = 42,
    scale: str = "mvp",
    as_of: datetime = AS_OF_DEFAULT,
    write_files: bool = True,
) -> dict[str, Any]:
    """
    Deterministic synthetic dataset generator (DATA-001 through DATA-010, ANOM-001 through ANOM-004).
    """
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

        if write_files:
            out_dir = ROOT_DIR / "data-generation" / "output"
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "generation_manifest.json").write_text(
                json.dumps(manifest, indent=2), encoding="utf-8"
            )
            (out_dir / "ground_truth_anomalies.json").write_text(
                json.dumps(ground_truth, indent=2), encoding="utf-8"
            )

        return manifest
    finally:
        uuid.uuid4 = orig_uuid4  # type: ignore[assignment]


def main() -> None:
    parser = argparse.ArgumentParser(description="JourneyIQ Deterministic Synthetic Data Generator")
    parser.add_argument("--seed", type=int, default=42, help="Deterministic random seed")
    parser.add_argument("--scale", choices=["mvp", "full"], default="mvp", help="Dataset scale")
    args = parser.parse_args()

    with SessionLocal() as db:
        manifest = generate_dataset(db, seed=args.seed, scale=args.scale)
        print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()

