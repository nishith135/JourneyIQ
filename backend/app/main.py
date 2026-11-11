from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.models.entities import CustomerJourney, EventQuarantine
from app.services.analytics_service import (
    get_dashboard_metrics,
    get_exceptions_summary,
    get_experiments_summary,
    get_funnel_metrics,
    get_journey_detail,
    list_journeys_paginated,
)
from app.services.domain_service import calculate_journey_outcome, withdraw_consent

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description="JourneyIQ Customer Journey & Conversion Analytics Platform",
)


class ConsentWithdrawRequest(BaseModel):
    reason: str | None = "Customer requested consent withdrawal"


class QuarantineResolveRequest(BaseModel):
    resolution_status: str = "RESOLVED"
    resolution_notes: str = "Reviewed and resolved by data steward"


# ============================================================================
# JSON API ENDPOINTS (/api/v1/...)
# ============================================================================


@app.get("/api/v1/health")
def api_health(db: Session = Depends(get_db)) -> dict[str, Any]:
    db.execute(text("SELECT 1"))
    return {
        "status": "healthy",
        "service": settings.app_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/v1/dashboard")
def api_dashboard(
    exclude_bots: bool = Query(True, description="Exclude bot sessions from human analytical denominators"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return get_dashboard_metrics(db, exclude_bots=exclude_bots)


@app.get("/api/v1/funnels")
def api_funnels(
    channel: str | None = Query(None, description="Optional channel filter: WEB or APP"),
    exclude_bots: bool = Query(True, description="Exclude bot sessions"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ch = channel if channel in ("WEB", "APP") else None
    return get_funnel_metrics(db, channel=ch, exclude_bots=exclude_bots)


def _parse_page_param(raw: str | None, *, default: int = 1, max_val: int | None = None, zero_fallback: int = 1) -> int:
    """Safely parse page/page_size query params without raising 422 on invalid input."""
    if raw is None or not raw.strip():
        return default
    try:
        val = int(raw.strip())
    except ValueError:
        return default
    if val < 1:
        return zero_fallback
    if max_val is not None and val > max_val:
        return max_val
    return val


def _build_journeys_url(
    *,
    page: int,
    page_size: int,
    outcome_status: str,
    friction_flag: str,
    search: str,
) -> str:
    """Build /journeys URL preserving active filter/search query parameters."""
    params: dict[str, str | int] = {"page": page}
    if page_size != 25:
        params["page_size"] = page_size
    if outcome_status and outcome_status != "ALL":
        params["outcome_status"] = outcome_status
    if friction_flag and friction_flag != "ALL":
        params["friction_flag"] = friction_flag
    if search and search.strip():
        params["search"] = search.strip()
    return f"/journeys?{urlencode(params)}"


@app.get("/api/v1/journeys")
def api_list_journeys(
    outcome_status: str | None = Query(None),
    friction_flag: str | None = Query(None),
    search: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int | None = Query(None, ge=1, le=500),
    limit: int | None = Query(None, ge=1, le=500),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    effective_page_size = page_size or limit or 100
    result = list_journeys_paginated(
        db,
        outcome_status=outcome_status,
        friction_flag=friction_flag,
        search=search,
        page=page,
        page_size=effective_page_size,
    )
    return {
        "count": len(result["items"]),
        "total_count": result["total_count"],
        "page": result["page"],
        "page_size": result["page_size"],
        "total_pages": result["total_pages"],
        "start_index": result["start_index"],
        "end_index": result["end_index"],
        "has_prev": result["has_prev"],
        "has_next": result["has_next"],
        "items": result["items"],
    }


@app.get("/api/v1/journeys/{journey_identifier}")
def api_journey_detail(
    journey_identifier: str,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    detail = get_journey_detail(db, journey_identifier)
    if not detail:
        raise HTTPException(status_code=404, detail=f"Journey '{journey_identifier}' not found.")
    return detail


@app.post("/api/v1/journeys/{journey_identifier}/recalculate")
def api_recalculate_journey(
    journey_identifier: str,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    detail = get_journey_detail(db, journey_identifier)
    if not detail:
        raise HTTPException(status_code=404, detail=f"Journey '{journey_identifier}' not found.")
    j_uuid = uuid.UUID(detail["journey"]["journey_id"])
    outcome = calculate_journey_outcome(
        db,
        journey_id=j_uuid,
        as_of_timestamp=datetime.now(timezone.utc),
        reason="MANUAL_RECALCULATION",
    )
    db.commit()
    return {
        "journey_business_id": detail["journey"]["journey_business_id"],
        "outcome_business_id": outcome.outcome_business_id,
        "calculation_version": outcome.calculation_version,
        "outcome_status": outcome.outcome_status,
        "attribution_status": outcome.attribution_status,
        "friction_flags": outcome.friction_flags,
    }


@app.post("/api/v1/journeys/{journey_identifier}/withdraw-consent")
def api_withdraw_journey_consent(
    journey_identifier: str,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    detail = get_journey_detail(db, journey_identifier)
    if not detail:
        raise HTTPException(status_code=404, detail=f"Journey '{journey_identifier}' not found.")
    cust_id_str = detail["journey"]["customer_id"]
    if not cust_id_str:
        raise HTTPException(
            status_code=400,
            detail="Journey has no linked customer_id from which to withdraw consent.",
        )
    res = withdraw_consent(
        db,
        customer_id=uuid.UUID(cust_id_str),
        anonymous_id=detail["journey"]["anonymous_id"],
        withdrawn_at=datetime.now(timezone.utc),
        actor_reference="ui-steward-action",
    )
    db.commit()
    return res


@app.get("/api/v1/exceptions")
def api_exceptions(db: Session = Depends(get_db)) -> dict[str, Any]:
    return get_exceptions_summary(db)


@app.post("/api/v1/exceptions/quarantine/{quarantine_id}/resolve")
def api_resolve_quarantine(
    quarantine_id: str,
    payload: QuarantineResolveRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        q_uuid = uuid.UUID(quarantine_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid quarantine UUID.")
    q = db.get(EventQuarantine, q_uuid)
    if not q:
        raise HTTPException(status_code=404, detail="Quarantine record not found.")
    if payload.resolution_status not in ("OPEN", "RESOLVED", "DISMISSED"):
        raise HTTPException(status_code=400, detail="Invalid resolution_status.")
    q.resolution_status = payload.resolution_status
    q.resolution_notes = payload.resolution_notes
    q.resolved_at = datetime.now(timezone.utc)
    db.commit()
    return {
        "quarantine_id": str(q.quarantine_id),
        "quarantine_business_id": q.quarantine_business_id,
        "resolution_status": q.resolution_status,
        "resolution_notes": q.resolution_notes,
        "resolved_at": q.resolved_at.isoformat() if q.resolved_at else None,
    }


@app.get("/api/v1/experiments")
def api_experiments(db: Session = Depends(get_db)) -> dict[str, Any]:
    items = get_experiments_summary(db)
    return {"count": len(items), "experiments": items}


# ============================================================================
# SERVER-RENDERED JINJA2 + HTMX VIEWS
# ============================================================================


@app.get("/", response_class=HTMLResponse)
def root_redirect() -> RedirectResponse:
    return RedirectResponse(url="/dashboard")


@app.get("/dashboard", response_class=HTMLResponse)
def view_dashboard(
    request: Request,
    exclude_bots: bool = Query(True),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    metrics = get_dashboard_metrics(db, exclude_bots=exclude_bots)
    funnel = get_funnel_metrics(db, exclude_bots=exclude_bots)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "active_nav": "dashboard",
            "metrics": metrics,
            "funnel": funnel,
            "exclude_bots": exclude_bots,
        },
    )


@app.get("/funnels", response_class=HTMLResponse)
def view_funnels(
    request: Request,
    channel: str = Query("ALL"),
    exclude_bots: bool = Query(True),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    ch = channel if channel in ("WEB", "APP") else None
    funnel = get_funnel_metrics(db, channel=ch, exclude_bots=exclude_bots)
    return templates.TemplateResponse(
        request,
        "funnels.html",
        {
            "active_nav": "funnels",
            "funnel": funnel,
            "channel": channel,
            "exclude_bots": exclude_bots,
        },
    )


@app.get("/journeys", response_class=HTMLResponse)
def view_journeys(
    request: Request,
    outcome_status: str = Query("ALL"),
    friction_flag: str = Query("ALL"),
    search: str = Query(""),
    page: str | None = Query(None),
    page_size: str | None = Query(None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    safe_page = _parse_page_param(page, default=1, zero_fallback=1)
    safe_page_size = _parse_page_param(page_size, default=25, max_val=100, zero_fallback=25)
    result = list_journeys_paginated(
        db,
        outcome_status=outcome_status if outcome_status != "ALL" else None,
        friction_flag=friction_flag if friction_flag != "ALL" else None,
        search=search if search.strip() else None,
        page=safe_page,
        page_size=safe_page_size,
    )
    curr_page = result["page"]
    prev_url = (
        _build_journeys_url(
            page=curr_page - 1,
            page_size=safe_page_size,
            outcome_status=outcome_status,
            friction_flag=friction_flag,
            search=search,
        )
        if result["has_prev"]
        else None
    )
    next_url = (
        _build_journeys_url(
            page=curr_page + 1,
            page_size=safe_page_size,
            outcome_status=outcome_status,
            friction_flag=friction_flag,
            search=search,
        )
        if result["has_next"]
        else None
    )
    return templates.TemplateResponse(
        request,
        "journeys.html",
        {
            "active_nav": "journeys",
            "journeys": result["items"],
            "outcome_status": outcome_status,
            "friction_flag": friction_flag,
            "search": search,
            "page": curr_page,
            "page_size": result["page_size"],
            "total_count": result["total_count"],
            "total_pages": result["total_pages"],
            "start_index": result["start_index"],
            "end_index": result["end_index"],
            "has_prev": result["has_prev"],
            "has_next": result["has_next"],
            "prev_url": prev_url,
            "next_url": next_url,
        },
    )


@app.get("/journeys/{journey_identifier}", response_class=HTMLResponse)
def view_journey_detail(
    request: Request,
    journey_identifier: str,
    banner_msg: str | None = Query(None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    detail = get_journey_detail(db, journey_identifier)
    if not detail:
        raise HTTPException(status_code=404, detail=f"Journey '{journey_identifier}' not found.")
    return templates.TemplateResponse(
        request,
        "journey_detail.html",
        {
            "active_nav": "journeys",
            "detail": detail,
            "banner_msg": banner_msg,
        },
    )


@app.post("/journeys/{journey_identifier}/recalculate-ui", response_class=HTMLResponse)
def ui_recalculate_journey(
    journey_identifier: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    detail = get_journey_detail(db, journey_identifier)
    if not detail:
        raise HTTPException(status_code=404, detail="Journey not found.")
    j_uuid = uuid.UUID(detail["journey"]["journey_id"])
    out = calculate_journey_outcome(
        db,
        journey_id=j_uuid,
        as_of_timestamp=datetime.now(timezone.utc),
        reason="UI_STEWARD_RECALCULATION",
    )
    db.commit()
    msg = f"Recalculated outcome -> version {out.calculation_version} ({out.outcome_status})"
    return RedirectResponse(
        url=f"/journeys/{journey_identifier}?banner_msg={msg}",
        status_code=303,
    )


@app.post("/journeys/{journey_identifier}/withdraw-consent-ui", response_class=HTMLResponse)
def ui_withdraw_consent(
    journey_identifier: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    detail = get_journey_detail(db, journey_identifier)
    if not detail:
        raise HTTPException(status_code=404, detail="Journey not found.")
    cust_id_str = detail["journey"]["customer_id"]
    if cust_id_str:
        res = withdraw_consent(
            db,
            customer_id=uuid.UUID(cust_id_str),
            anonymous_id=detail["journey"]["anonymous_id"],
            withdrawn_at=datetime.now(timezone.utc),
            actor_reference="ui-privacy-steward",
        )
        db.commit()
        msg = f"Consent withdrawn ({res['consent_business_id']}); active IdentityLinks revoked and outcome recalculated."
    else:
        msg = "No linked customer on this journey."
    return RedirectResponse(
        url=f"/journeys/{journey_identifier}?banner_msg={msg}",
        status_code=303,
    )


@app.get("/exceptions", response_class=HTMLResponse)
def view_exceptions(
    request: Request,
    banner_msg: str | None = Query(None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    exc = get_exceptions_summary(db)
    return templates.TemplateResponse(
        request,
        "exceptions.html",
        {
            "active_nav": "exceptions",
            "exc": exc,
            "banner_msg": banner_msg,
        },
    )


@app.post("/exceptions/quarantine/{quarantine_id}/resolve-ui")
def ui_resolve_quarantine(
    quarantine_id: str,
    resolution_status: str = Form("RESOLVED"),
    resolution_notes: str = Form("Verified and resolved during data quality review."),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    q = db.get(EventQuarantine, uuid.UUID(quarantine_id))
    if q:
        q.resolution_status = resolution_status
        q.resolution_notes = resolution_notes
        q.resolved_at = datetime.now(timezone.utc)
        db.commit()
    return RedirectResponse(
        url=f"/exceptions?banner_msg=Updated+quarantine+item+to+{resolution_status}",
        status_code=303,
    )


@app.get("/experiments", response_class=HTMLResponse)
def view_experiments(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    experiments = get_experiments_summary(db)
    return templates.TemplateResponse(
        request,
        "experiments.html",
        {
            "active_nav": "experiments",
            "experiments": experiments,
        },
    )

