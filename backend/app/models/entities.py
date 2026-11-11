from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON, Uuid

from app.db.session import Base

JSON_TYPE = JSON().with_variant(JSONB(), "postgresql")


class Customer(Base):
    __tablename__ = "customer"

    customer_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    customer_business_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    account_status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "account_status IN ('ACTIVE', 'INACTIVE', 'SUSPENDED')",
            name="ck_customer_account_status",
        ),
    )


class CustomerJourney(Base):
    __tablename__ = "customer_journey"

    journey_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    journey_business_id: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("customer.customer_id", ondelete="RESTRICT"), nullable=True
    )
    anonymous_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_touch_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_touch_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="OPEN")
    identity_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "(customer_id IS NOT NULL AND anonymous_id IS NULL) OR (customer_id IS NULL AND anonymous_id IS NOT NULL)",
            name="ck_customer_journey_exactly_one_subject",
        ),
        CheckConstraint(
            "status IN ('OPEN', 'CLOSED', 'FINALIZED')",
            name="ck_customer_journey_status",
        ),
        CheckConstraint("identity_version > 0", name="ck_customer_journey_identity_version"),
        Index("ix_customer_journey_customer_first_touch", "customer_id", "first_touch_at"),
    )


class SessionRecord(Base):
    __tablename__ = "session"

    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_business_id: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("customer.customer_id", ondelete="RESTRICT"), nullable=True
    )
    anonymous_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    client_instance_id: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    journey_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("customer_journey.journey_id", ondelete="SET NULL"), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    sessionization_version: Mapped[str] = mapped_column(String(30), nullable=False)
    traffic_classification: Mapped[str] = mapped_column(String(20), nullable=False, default="UNKNOWN")
    classification_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    classification_version: Mapped[str | None] = mapped_column(String(30), nullable=True)
    classified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("channel IN ('WEB', 'APP')", name="ck_session_channel"),
        CheckConstraint("status IN ('ACTIVE', 'ENDED', 'FINALIZED')", name="ck_session_status"),
        CheckConstraint(
            "traffic_classification IN ('HUMAN', 'BOT', 'UNKNOWN')",
            name="ck_session_traffic_classification",
        ),
        Index("ix_session_client_channel_started", "client_instance_id", "channel", "started_at"),
        Index("ix_session_customer_started", "customer_id", "started_at"),
        Index("ix_session_journey_id", "journey_id"),
    )


class Product(Base):
    __tablename__ = "product"

    product_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    product_business_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    product_name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("current_price >= 0", name="ck_product_current_price"),
    )


class Cart(Base):
    __tablename__ = "cart"

    cart_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    cart_business_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("customer.customer_id", ondelete="RESTRICT"), nullable=True
    )
    anonymous_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("session.session_id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    abandoned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    converted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "customer_id IS NOT NULL OR anonymous_id IS NOT NULL",
            name="ck_cart_has_owner",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'CHECKOUT', 'ABANDONED', 'CONVERTED')",
            name="ck_cart_status",
        ),
        Index("ix_cart_status_updated", "status", "updated_at"),
    )


class CartItem(Base):
    __tablename__ = "cart_item"

    cart_item_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    cart_item_business_id: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    cart_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("cart.cart_id", ondelete="RESTRICT"), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("product.product_id", ondelete="RESTRICT"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_cart_item_quantity_pos"),
        CheckConstraint("unit_price >= 0", name="ck_cart_item_unit_price_nonneg"),
    )


class Order(Base):
    __tablename__ = "orders"

    order_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    order_business_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("customer.customer_id", ondelete="RESTRICT"), nullable=True
    )
    cart_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("cart.cart_id", ondelete="RESTRICT"), nullable=True
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("session.session_id", ondelete="SET NULL"), nullable=True
    )
    source_purchase_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    purchase_source: Mapped[str | None] = mapped_column(String(50), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    shipping_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(25), nullable=False, default="PENDING")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    purchase_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "subtotal >= 0 AND discount_amount >= 0 AND tax_amount >= 0 AND shipping_amount >= 0 AND total_amount >= 0",
            name="ck_orders_amounts_nonneg",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'CONFIRMED', 'CANCELLED', 'REFUNDED', 'PARTIALLY_REFUNDED')",
            name="ck_orders_status",
        ),
        Index(
            "uq_orders_purchase_source_ref",
            "purchase_source",
            "source_purchase_reference",
            unique=True,
            postgresql_where=text("purchase_source IS NOT NULL AND source_purchase_reference IS NOT NULL"),
        ),
        Index("ix_orders_status_created", "status", "created_at"),
    )


class OrderItem(Base):
    __tablename__ = "order_item"

    order_item_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    order_item_business_id: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    order_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("orders.order_id", ondelete="RESTRICT"), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("product.product_id", ondelete="RESTRICT"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    line_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_order_item_quantity_pos"),
        CheckConstraint("unit_price >= 0 AND line_total >= 0", name="ck_order_item_amounts_nonneg"),
    )


class PaymentAttempt(Base):
    __tablename__ = "payment_attempt"

    payment_attempt_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    payment_attempt_business_id: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    order_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("orders.order_id", ondelete="RESTRICT"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("order_id", "attempt_number", name="uq_payment_attempt_order_seq"),
        CheckConstraint("attempt_number > 0", name="ck_payment_attempt_number_pos"),
        CheckConstraint("amount > 0", name="ck_payment_attempt_amount_pos"),
        CheckConstraint(
            "status IN ('PENDING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name="ck_payment_attempt_status",
        ),
    )


class Refund(Base):
    __tablename__ = "refund"

    refund_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    refund_business_id: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    order_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("orders.order_id", ondelete="RESTRICT"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_refund_amount_pos"),
        CheckConstraint(
            "status IN ('PENDING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name="ck_refund_status",
        ),
    )


class SupportTicket(Base):
    __tablename__ = "support_ticket"

    ticket_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ticket_business_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("customer.customer_id", ondelete="RESTRICT"), nullable=True
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("session.session_id", ondelete="SET NULL"), nullable=True
    )
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("orders.order_id", ondelete="SET NULL"), nullable=True
    )
    journey_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("customer_journey.journey_id", ondelete="SET NULL"), nullable=True
    )
    issue_type: Mapped[str] = mapped_column(String(30), nullable=False)
    priority: Mapped[str] = mapped_column(String(15), nullable=False, default="MEDIUM")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="OPEN")
    origin_channel: Mapped[str] = mapped_column(String(20), nullable=False)
    support_channel: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "issue_type IN ('PAYMENT', 'CHECKOUT', 'DELIVERY', 'PRODUCT', 'OTHER')",
            name="ck_support_ticket_issue_type",
        ),
        CheckConstraint(
            "priority IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')",
            name="ck_support_ticket_priority",
        ),
        CheckConstraint(
            "status IN ('OPEN', 'IN_PROGRESS', 'RESOLVED', 'CLOSED', 'CANCELLED')",
            name="ck_support_ticket_status",
        ),
        Index("ix_support_ticket_status_created", "status", "created_at"),
    )


class SupportTicketEvent(Base):
    __tablename__ = "support_ticket_event"

    ticket_event_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ticket_event_business_id: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("support_ticket.ticket_id", ondelete="RESTRICT"), nullable=False
    )
    previous_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    new_status: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class ConsentRecord(Base):
    __tablename__ = "consent_record"

    consent_record_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    consent_business_id: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("customer.customer_id", ondelete="RESTRICT"), nullable=True
    )
    anonymous_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    consent_purpose: Mapped[str] = mapped_column(String(50), nullable=False, default="ANALYTICS")
    consent_status: Mapped[str] = mapped_column(String(20), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "(customer_id IS NOT NULL AND anonymous_id IS NULL) OR (customer_id IS NULL AND anonymous_id IS NOT NULL)",
            name="ck_consent_record_exactly_one_subject",
        ),
        CheckConstraint(
            "consent_status IN ('GRANTED', 'DENIED', 'UNKNOWN', 'WITHDRAWN')",
            name="ck_consent_record_status",
        ),
        Index("ix_consent_record_anon_purpose", "anonymous_id", "consent_purpose", "effective_from"),
    )


class IdentityLink(Base):
    __tablename__ = "identity_link"

    identity_link_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    identity_link_business_id: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    anonymous_id: Mapped[str] = mapped_column(String(64), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("customer.customer_id", ondelete="RESTRICT"), nullable=False
    )
    client_instance_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    link_method: Mapped[str] = mapped_column(String(30), nullable=False)
    consent_record_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("consent_record.consent_record_id", ondelete="RESTRICT"), nullable=False
    )
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE', 'REVOKED', 'CONFLICTED')",
            name="ck_identity_link_status",
        ),
        Index("ix_identity_link_anon_effective", "anonymous_id", "effective_from"),
    )


class Experiment(Base):
    __tablename__ = "experiment"

    experiment_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    experiment_business_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="DRAFT")
    primary_metric: Mapped[str] = mapped_column(String(100), nullable=False)
    eligibility_rules: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    allocation_config: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    experiment_salt: Mapped[str] = mapped_column(String(128), nullable=False)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('DRAFT', 'SCHEDULED', 'RUNNING', 'PAUSED', 'COMPLETED', 'ARCHIVED')",
            name="ck_experiment_status",
        ),
        CheckConstraint("config_version > 0", name="ck_experiment_config_version"),
    )


class ExperimentVariant(Base):
    __tablename__ = "experiment_variant"

    variant_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    variant_business_id: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("experiment.experiment_id", ondelete="RESTRICT"), nullable=False
    )
    variant_name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_control: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    allocation_percentage: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "experiment_id",
            "config_version",
            "variant_name",
            name="uq_experiment_variant_exp_ver_name",
        ),
        CheckConstraint(
            "allocation_percentage >= 0 AND allocation_percentage <= 100",
            name="ck_experiment_variant_alloc",
        ),
        CheckConstraint("config_version > 0", name="ck_experiment_variant_config_version"),
    )


class ExperimentAssignment(Base):
    __tablename__ = "experiment_assignment"

    assignment_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    assignment_business_id: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("experiment.experiment_id", ondelete="RESTRICT"), nullable=False
    )
    variant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("experiment_variant.variant_id", ondelete="RESTRICT"), nullable=False
    )
    assignment_identity_type: Mapped[str] = mapped_column(String(20), nullable=False)
    assignment_identity_id: Mapped[str] = mapped_column(String(64), nullable=False)
    identity_link_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("identity_link.identity_link_id", ondelete="RESTRICT"), nullable=True
    )
    consent_record_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("consent_record.consent_record_id", ondelete="RESTRICT"), nullable=True
    )
    identity_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    assignment_bucket: Mapped[int] = mapped_column(Integer, nullable=False)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "experiment_id",
            "assignment_identity_type",
            "assignment_identity_id",
            "config_version",
            name="uq_experiment_assignment_unit_ver",
        ),
        CheckConstraint(
            "assignment_identity_type IN ('CUSTOMER', 'ANONYMOUS')",
            name="ck_experiment_assignment_identity_type",
        ),
        CheckConstraint("identity_version > 0", name="ck_experiment_assignment_identity_version"),
        CheckConstraint("config_version > 0", name="ck_experiment_assignment_config_version"),
        Index(
            "ix_experiment_assignment_lookup",
            "experiment_id",
            "assignment_identity_type",
            "assignment_identity_id",
        ),
    )


class JourneyEvent(Base):
    __tablename__ = "journey_event"

    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    event_business_id: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("session.session_id", ondelete="SET NULL"), nullable=True
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("customer.customer_id", ondelete="RESTRICT"), nullable=True
    )
    anonymous_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    client_instance_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("product.product_id", ondelete="RESTRICT"), nullable=True
    )
    cart_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("cart.cart_id", ondelete="RESTRICT"), nullable=True
    )
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("orders.order_id", ondelete="RESTRICT"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    event_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingestion_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_sequence: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    properties: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    schema_version: Mapped[str] = mapped_column(String(30), nullable=False, default="1.0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "channel IN ('WEB', 'APP', 'SUPPORT', 'PURCHASE', 'SYSTEM')",
            name="ck_journey_event_channel",
        ),
        Index("ix_journey_event_session_ts", "session_id", "event_timestamp"),
        Index("ix_journey_event_type_ts", "event_type", "event_timestamp"),
        Index("ix_journey_event_customer_ts", "customer_id", "event_timestamp"),
        Index("ix_journey_event_anon_ts", "anonymous_id", "event_timestamp"),
        Index("ix_journey_event_ingestion_ts", "ingestion_timestamp"),
    )


class ExperimentExposure(Base):
    __tablename__ = "experiment_exposure"

    exposure_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    exposure_business_id: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    assignment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("experiment_assignment.assignment_id", ondelete="RESTRICT"), nullable=False
    )
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("journey_event.event_id", ondelete="RESTRICT"), nullable=True
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    exposed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exposure_context: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_experiment_exposure_assignment", "assignment_id", "exposed_at"),
    )


class EventQuarantine(Base):
    __tablename__ = "event_quarantine"

    quarantine_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    quarantine_business_id: Mapped[str] = mapped_column(String(48), unique=True, nullable=False)
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("journey_event.event_id", ondelete="RESTRICT"), nullable=True
    )
    issue_type: Mapped[str] = mapped_column(String(40), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolution_status: Mapped[str] = mapped_column(String(20), nullable=False, default="OPEN")
    resolution_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "resolution_status IN ('OPEN', 'RESOLVED', 'DISMISSED')",
            name="ck_event_quarantine_resolution_status",
        ),
        Index("ix_event_quarantine_status_detected", "resolution_status", "detected_at"),
    )


class JourneyOutcome(Base):
    __tablename__ = "journey_outcome"

    outcome_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    outcome_business_id: Mapped[str] = mapped_column(String(48), unique=True, nullable=False)
    scope_type: Mapped[str] = mapped_column(String(20), nullable=False)
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("session.session_id", ondelete="RESTRICT"), nullable=True
    )
    journey_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("customer_journey.journey_id", ondelete="RESTRICT"), nullable=True
    )
    outcome_status: Mapped[str] = mapped_column(String(20), nullable=False)
    outcome_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    conversion_order_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("orders.order_id", ondelete="RESTRICT"), nullable=True
    )
    first_touch_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    converted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attribution_status: Mapped[str] = mapped_column(String(20), nullable=False)
    friction_flags: Mapped[list[str]] = mapped_column(JSON_TYPE, nullable=False, default=list)
    calculation_version: Mapped[str] = mapped_column(String(30), nullable=False)
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "(scope_type = 'SESSION' AND session_id IS NOT NULL AND journey_id IS NULL) OR "
            "(scope_type = 'CUSTOMER_JOURNEY' AND journey_id IS NOT NULL AND session_id IS NULL)",
            name="ck_journey_outcome_scope_ref",
        ),
        CheckConstraint(
            "outcome_status IN ('IN_PROGRESS', 'CONVERTED', 'NON_CONVERTED', 'EXCLUDED')",
            name="ck_journey_outcome_status",
        ),
        CheckConstraint(
            "attribution_status IN ('ATTRIBUTED', 'ASSISTED', 'UNATTRIBUTED', 'OUT_OF_WINDOW', 'EXCLUDED')",
            name="ck_journey_outcome_attribution",
        ),
        Index("ix_journey_outcome_scope_status", "scope_type", "outcome_status"),
    )


class AuditEvent(Base):
    __tablename__ = "audit_event"

    audit_event_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    audit_business_id: Mapped[str] = mapped_column(String(48), unique=True, nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(30), nullable=False)
    actor_reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    previous_state: Mapped[dict[str, Any] | None] = mapped_column(JSON_TYPE, nullable=True)
    new_state: Mapped[dict[str, Any] | None] = mapped_column(JSON_TYPE, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    audit_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSON_TYPE, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_audit_event_entity_occurred", "entity_type", "entity_id", "occurred_at"),
    )

