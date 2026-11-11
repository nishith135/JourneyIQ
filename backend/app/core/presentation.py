from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jinja2 import Environment

EVENT_TYPE_LABELS: dict[str, str] = {
    "SESSION_STARTED": "Session started",
    "PRODUCT_VIEWED": "Product viewed",
    "ADD_TO_CART": "Added to cart",
    "CHECKOUT_STARTED": "Checkout started",
    "PURCHASE_COMPLETED": "Purchase completed",
    "PAYMENT_FAILED": "Payment failed",
    "PAYMENT_SUCCEEDED": "Payment succeeded",
    "AUTH_LOGIN_SUCCESS": "Signed in",
    "CONSENT_GRANTED": "Consent granted",
    "CONSENT_WITHDRAWN": "Consent withdrawn",
    "SUPPORT_TICKET_CREATED": "Support ticket opened",
    "PAGE_VIEW": "Page viewed",
}

OUTCOME_STATUS_LABELS: dict[str, str] = {
    "CONVERTED": "Converted",
    "IN_PROGRESS": "In progress",
    "NON_CONVERTED": "Did not convert",
    "EXCLUDED": "Excluded (Bot traffic)",
}

ATTRIBUTION_STATUS_LABELS: dict[str, str] = {
    "ATTRIBUTED": "Attributed",
    "ASSISTED": "Support-assisted",
    "UNATTRIBUTED": "Unattributed",
    "OUT_OF_WINDOW": "Outside 7-day window",
    "EXCLUDED": "Excluded",
}

OUTCOME_REASON_LABELS: dict[str, str] = {
    "DIRECT_WINDOW_CONVERSION": "Direct conversion",
    "SUPPORT_ASSISTED_CONVERSION": "Support-assisted conversion",
    "ATTRIBUTION_WINDOW_EXPIRED": "7-day window expired",
    "ATTRIBUTION_WINDOW_OPEN": "Attribution window open",
    "PURCHASE_AFTER_7D_WINDOW": "Purchased after 7-day window",
    "BOT_TRAFFIC": "Bot traffic excluded",
    "SESSION_CONVERSION": "Converted in session",
    "SESSION_ENDED_WITHOUT_PURCHASE": "Ended without purchase",
}

FRICTION_FLAG_LABELS: dict[str, str] = {
    "PAYMENT_FAILURE": "Payment failed",
    "CHECKOUT_RETRY": "Checkout retried",
    "SUPPORT_CONTACTED": "Support contacted",
    "CART_ABANDONED": "Cart abandoned",
    "LATE_EVENT": "Late-arriving event",
    "DUPLICATE_SIGNAL": "Duplicate signal suppressed",
}

QUARANTINE_ISSUE_LABELS: dict[str, str] = {
    "UNAUTHORIZED_IDENTITY_MERGE": "Identity merge blocked",
    "IDENTITY_CONFLICT": "Identity conflict",
    "MISSING_CLIENT_INSTANCE_ID": "Missing client instance",
}

STATUS_LABELS: dict[str, str] = {
    "OPEN": "Open",
    "RESOLVED": "Resolved",
    "DISMISSED": "Dismissed",
    "CONFIRMED": "Confirmed",
    "PENDING": "Pending",
    "CANCELLED": "Cancelled",
    "REFUNDED": "Refunded",
    "PARTIALLY_REFUNDED": "Partially refunded",
    "SUCCEEDED": "Succeeded",
    "FAILED": "Failed",
    "ACTIVE": "Active",
    "CONFLICTED": "Conflicted",
    "REVOKED": "Revoked",
    "GRANTED": "Granted",
    "DENIED": "Denied",
    "WITHDRAWN": "Withdrawn",
    "RUNNING": "Running",
    "PAUSED": "Paused",
    "DRAFT": "Draft",
    "COMPLETED": "Completed",
    "IN_PROGRESS": "In progress",
    "CLOSED": "Closed",
    "FINALIZED": "Finalized",
}

FAILURE_REASON_LABELS: dict[str, str] = {
    "CARD_DECLINED_3DS_TIMEOUT": "Card declined (3DS timeout)",
    "GATEWAY_TIMEOUT": "Payment gateway timeout",
}

CHANNEL_LABELS: dict[str, str] = {
    "WEB": "Web",
    "APP": "App",
    "ALL": "All channels",
    "EMAIL": "Email",
    "CHAT": "Chat",
    "PHONE": "Phone",
}

LINK_METHOD_LABELS: dict[str, str] = {
    "AUTHENTICATED_CONSENT": "Authenticated consent",
    "IMPERSONATED_MERGE": "Blocked unverified merge",
}

PURCHASE_SOURCE_LABELS: dict[str, str] = {
    "WEB_CHECKOUT": "Web checkout",
    "APP_CHECKOUT": "App checkout",
}

TICKET_ISSUE_LABELS: dict[str, str] = {
    "PAYMENT": "Payment support",
    "CHECKOUT": "Checkout support",
    "DELIVERY": "Delivery support",
    "ACCOUNT": "Account support",
}

METRIC_LABELS: dict[str, str] = {
    "checkout_conversion_rate": "Checkout conversion rate",
    "payment_retry_success_rate": "Payment retry success rate",
}


def humanize_code(value: str | None, mapping: dict[str, str], default: str = "—") -> str:
    """Map an internal enum/code string to a concise business label with safe fallback."""
    if not value:
        return default
    if value in mapping:
        return mapping[value]
    return value.replace("_", " ").strip().capitalize()


def format_utc_timestamp(value: str | None) -> str:
    """Format an ISO-8601 timestamp string into a concise human-readable UTC timestamp."""
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value)
        return dt.strftime("%b %d, %Y %H:%M UTC")
    except ValueError:
        return value


def register_presentation_filters(env: Environment) -> None:
    """Register business presentation filters on the shared Jinja2 environment."""
    env.filters["label_event"] = lambda v: humanize_code(v, EVENT_TYPE_LABELS)
    env.filters["label_outcome"] = lambda v: humanize_code(v, OUTCOME_STATUS_LABELS)
    env.filters["label_attribution"] = lambda v: humanize_code(v, ATTRIBUTION_STATUS_LABELS)
    env.filters["label_reason"] = lambda v: humanize_code(v, OUTCOME_REASON_LABELS)
    env.filters["label_friction"] = lambda v: humanize_code(v, FRICTION_FLAG_LABELS)
    env.filters["label_quarantine_issue"] = lambda v: humanize_code(v, QUARANTINE_ISSUE_LABELS)
    env.filters["label_status"] = lambda v: humanize_code(v, STATUS_LABELS)
    env.filters["label_failure"] = lambda v: humanize_code(v, FAILURE_REASON_LABELS)
    env.filters["label_channel"] = lambda v: humanize_code(v, CHANNEL_LABELS)
    env.filters["label_link_method"] = lambda v: humanize_code(v, LINK_METHOD_LABELS)
    env.filters["label_source"] = lambda v: humanize_code(v, PURCHASE_SOURCE_LABELS)
    env.filters["label_ticket_issue"] = lambda v: humanize_code(v, TICKET_ISSUE_LABELS)
    env.filters["label_metric"] = lambda v: humanize_code(v, METRIC_LABELS)
    env.filters["format_ts"] = format_utc_timestamp

