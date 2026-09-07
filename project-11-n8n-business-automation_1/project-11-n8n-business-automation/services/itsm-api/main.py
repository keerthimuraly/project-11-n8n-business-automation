"""
Mock external Enterprise IT system for Project 11.

Requirement 4.6 asks the workflow to interact meaningfully with at least one
external API / application / business service.  In a real TCS environment that
system would be ServiceNow, BMC Helix or an internal CMDB.  For a reproducible
assessment we stand up a small, well-behaved stand-in that exposes the same
shape of endpoints and requires an API key, so the n8n workflow performs real
HTTP integration with authentication, pagination, error codes and retries.

Endpoints
    GET    /health                        liveness probe
    GET    /directory/employees/{email}   employee enrichment (dept, manager, VIP)
    GET    /directory/assets              assets assigned to an employee
    GET    /catalog/services              service catalogue with cost estimates
    GET    /kb/search                     knowledge-base lookup for auto-resolution
    POST   /tickets                       create ticket in the system of record
    GET    /tickets/{ref}                 read back a ticket
    PATCH  /tickets/{ref}                 update status / assignee / approval
    GET    /tickets                       list (supports queue + status filters)
    POST   /_test/reset                   clear state between evaluation runs

Fault injection (used by the error-handling tests, requirement 4.9)
    Send header  X-Simulate-Failure: 500 | 429 | timeout | malformed
    or query param ?simulate=500 to force the corresponding failure mode.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import string
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)-5s [itsm-api] %(message)s",
)
log = logging.getLogger("itsm-api")

API_KEY = os.getenv("ITSM_API_KEY", "local-itsm-key")

app = FastAPI(
    title="Mock Enterprise ITSM API",
    description="Stand-in system of record for the n8n Business Automation Pipeline.",
    version="1.0.0",
)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
async def require_api_key(x_api_key: str | None = Header(default=None)) -> str:
    """Every business endpoint requires the shared API key.

    The key is injected into n8n as an environment variable and referenced from
    an n8n credential, never hard-coded in a node (requirement: secure config).
    """
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")
    return x_api_key


# ---------------------------------------------------------------------------
# Fault injection so the workflow's error paths can be exercised on demand
# ---------------------------------------------------------------------------
async def maybe_fail(request: Request) -> None:
    mode = request.headers.get("X-Simulate-Failure") or request.query_params.get("simulate")
    if not mode:
        return
    log.warning("fault injection requested: %s", mode)
    if mode == "500":
        raise HTTPException(status_code=500, detail="Upstream system of record unavailable")
    if mode == "429":
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded, retry after 2s",
            headers={"Retry-After": "2"},
        )
    if mode == "timeout":
        await asyncio.sleep(30)
    if mode == "malformed":
        raise MalformedResponse()


class MalformedResponse(Exception):
    """Signals that we should answer with a body the workflow cannot parse."""


@app.exception_handler(MalformedResponse)
async def malformed_handler(_: Request, __: MalformedResponse) -> JSONResponse:
    return JSONResponse(status_code=200, content="<html>gateway error page</html>")


# ---------------------------------------------------------------------------
# Synthetic reference data
# ---------------------------------------------------------------------------
EMPLOYEES: dict[str, dict[str, Any]] = {
    "arun.mehta@tcs-demo.local": {
        "employee_id": "TCS100241", "full_name": "Arun Mehta",
        "department": "Banking & Financial Services", "location": "Chennai - SEZ 3",
        "job_title": "Senior Business Analyst", "manager_email": "priya.nair@tcs-demo.local",
        "manager_name": "Priya Nair", "cost_center": "CC-BFS-1180", "is_vip": False,
    },
    "priya.nair@tcs-demo.local": {
        "employee_id": "TCS100055", "full_name": "Priya Nair",
        "department": "Banking & Financial Services", "location": "Chennai - SEZ 3",
        "job_title": "Delivery Manager", "manager_email": "raghav.iyer@tcs-demo.local",
        "manager_name": "Raghav Iyer", "cost_center": "CC-BFS-1180", "is_vip": False,
    },
    "raghav.iyer@tcs-demo.local": {
        "employee_id": "TCS100003", "full_name": "Raghav Iyer",
        "department": "Enterprise IT", "location": "Mumbai - Yantra Park",
        "job_title": "Vice President, Enterprise IT", "manager_email": "cio.office@tcs-demo.local",
        "manager_name": "CIO Office", "cost_center": "CC-EIT-0001", "is_vip": True,
    },
    "sneha.rao@tcs-demo.local": {
        "employee_id": "TCS100788", "full_name": "Sneha Rao",
        "department": "Retail & CPG", "location": "Bengaluru - Whitefield",
        "job_title": "Cloud Engineer", "manager_email": "arun.mehta@tcs-demo.local",
        "manager_name": "Arun Mehta", "cost_center": "CC-RCG-2204", "is_vip": False,
    },
    "vikram.singh@tcs-demo.local": {
        "employee_id": "TCS101902", "full_name": "Vikram Singh",
        "department": "Life Sciences", "location": "Hyderabad - Adibatla",
        "job_title": "Validation Lead", "manager_email": "priya.nair@tcs-demo.local",
        "manager_name": "Priya Nair", "cost_center": "CC-LSH-3311", "is_vip": False,
    },
    "meera.krishnan@tcs-demo.local": {
        "employee_id": "TCS100910", "full_name": "Meera Krishnan",
        "department": "Enterprise IT", "location": "Chennai - Siruseri",
        "job_title": "Chief Information Security Officer",
        "manager_email": "cio.office@tcs-demo.local", "manager_name": "CIO Office",
        "cost_center": "CC-EIT-0007", "is_vip": True,
    },
}

ASSETS: dict[str, list[dict[str, Any]]] = {
    "arun.mehta@tcs-demo.local": [
        {"asset_tag": "TCS-LT-44821", "type": "Laptop", "model": "Dell Latitude 5540",
         "os": "Windows 11 Enterprise 23H2", "warranty_until": "2027-03-31", "encrypted": True},
        {"asset_tag": "TCS-MON-9912", "type": "Monitor", "model": "Dell P2422H",
         "os": None, "warranty_until": "2026-11-30", "encrypted": False},
    ],
    "sneha.rao@tcs-demo.local": [
        {"asset_tag": "TCS-LT-51033", "type": "Laptop", "model": "ThinkPad T14 Gen 4",
         "os": "Ubuntu 22.04 LTS", "warranty_until": "2028-01-15", "encrypted": True},
    ],
    "vikram.singh@tcs-demo.local": [
        {"asset_tag": "TCS-LT-33110", "type": "Laptop", "model": "HP EliteBook 840 G9",
         "os": "Windows 11 Enterprise 22H2", "warranty_until": "2025-12-31", "encrypted": True},
    ],
    "raghav.iyer@tcs-demo.local": [
        {"asset_tag": "TCS-LT-10004", "type": "Laptop", "model": "MacBook Pro 14 M3",
         "os": "macOS 14.5", "warranty_until": "2027-09-30", "encrypted": True},
    ],
}

SERVICE_CATALOG = [
    {"sku": "SW-IDE-001",  "name": "JetBrains All Products Pack", "category": "software",
     "unit_cost_inr": 21000,  "requires_approval": False, "fulfilment_queue": "L1_SERVICE_DESK"},
    {"sku": "SW-BI-004",   "name": "Tableau Creator Licence",     "category": "software",
     "unit_cost_inr": 62000,  "requires_approval": True,  "fulfilment_queue": "L1_SERVICE_DESK"},
    {"sku": "HW-LT-014",   "name": "Developer Laptop Refresh",    "category": "hardware",
     "unit_cost_inr": 125000, "requires_approval": True,  "fulfilment_queue": "L2_ENDPOINT"},
    {"sku": "HW-DOCK-002", "name": "USB-C Docking Station",       "category": "hardware",
     "unit_cost_inr": 9500,   "requires_approval": False, "fulfilment_queue": "L2_ENDPOINT"},
    {"sku": "AC-PRD-009",  "name": "Production Database Read Role", "category": "access",
     "unit_cost_inr": 0,      "requires_approval": True,  "fulfilment_queue": "IAM_ACCESS"},
    {"sku": "AC-VPN-001",  "name": "Site-to-Site VPN Profile",    "category": "access",
     "unit_cost_inr": 0,      "requires_approval": False, "fulfilment_queue": "NETWORK_OPS"},
]

KB_ARTICLES = [
    {"kb_id": "KB0001", "title": "Reset your corporate password from the self-service portal",
     "keywords": ["password", "reset", "locked", "account locked", "expired"],
     "resolution": "Use https://sso.tcs-demo.local/reset with your registered mobile number.",
     "auto_resolvable": True},
    {"kb_id": "KB0014", "title": "VPN disconnects every few minutes on Wi-Fi",
     "keywords": ["vpn", "disconnect", "drop", "wifi", "tunnel"],
     "resolution": "Switch the VPN profile to TCP/443 and disable Wi-Fi power saving.",
     "auto_resolvable": False},
    {"kb_id": "KB0022", "title": "Outlook stuck on 'Trying to connect'",
     "keywords": ["outlook", "email", "connect", "mailbox", "exchange"],
     "resolution": "Recreate the Outlook profile and clear the cached credentials.",
     "auto_resolvable": False},
    {"kb_id": "KB0031", "title": "Report a suspected phishing email",
     "keywords": ["phishing", "suspicious", "spoof", "fraud", "scam"],
     "resolution": "Use the Report Phishing add-in. Do not click links. SecOps is paged automatically.",
     "auto_resolvable": False},
    {"kb_id": "KB0045", "title": "Request additional software licence",
     "keywords": ["licence", "license", "software", "install", "purchase"],
     "resolution": "Raise a service request; manager approval is required above INR 25,000.",
     "auto_resolvable": False},
]

# In-memory system of record.  Cleared by POST /_test/reset.
TICKETS: dict[str, dict[str, Any]] = {}
_SEQ = {"n": 0}


def _next_ref() -> str:
    _SEQ["n"] += 1
    year = datetime.now(timezone.utc).year
    return f"ITSM-{year}-{_SEQ['n']:06d}"


def _rand_id(n: int = 8) -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=n))


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
# Deliberately a plain regex rather than pydantic's EmailStr.
#
# Every address in this project uses the @tcs-demo.local domain, which is
# non-routable by design - it guarantees no demo notification can ever reach a
# real inbox. But `.local` is a reserved special-use name under RFC 6762, and
# EmailStr rejects it outright, which would 422 every single ticket. Since the
# workflow's `Validate Request` node already enforces address format before
# anything reaches this service, a shape check here is the right depth.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]{2,}$")


def _valid_email(v: str) -> str:
    v = str(v).strip().lower()
    if not EMAIL_RE.match(v):
        raise ValueError(f"not a valid email address: {v!r}")
    return v


class TicketCreate(BaseModel):
    ticket_id: str = Field(..., description="Internal id minted by the n8n pipeline")
    trace_id: str
    requester_email: str = Field(..., max_length=254)
    requester_name: str | None = None
    subject: str = Field(..., min_length=3, max_length=200)
    description: str = Field(..., min_length=3, max_length=8000)
    category: str
    subcategory: str | None = None
    priority: Literal["P1", "P2", "P3", "P4"]
    queue: str
    assignee_email: str | None = None
    department: str | None = None
    requested_cost: float = 0
    requires_approval: bool = False
    is_security_incident: bool = False
    ai_summary: str | None = None
    sla_due_at: str | None = None
    source: str = "n8n-pipeline"

    @field_validator("requester_email")
    @classmethod
    def _check_email(cls, v: str) -> str:
        return _valid_email(v)


class TicketPatch(BaseModel):
    status: str | None = None
    assignee_email: str | None = None
    approval_status: str | None = None
    approver_email: str | None = None
    resolution_note: str | None = None
    escalated: bool | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health", tags=["ops"])
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "mock-itsm-api",
        "version": app.version,
        "tickets_in_store": len(TICKETS),
        "time": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/directory/employees/{email}", tags=["directory"])
async def get_employee(
    email: str,
    _: None = Depends(maybe_fail),
    __: str = Depends(require_api_key),
) -> dict[str, Any]:
    """Employee enrichment. Unknown addresses return a safe 'external' profile
    rather than a 404, so the workflow can still proceed and route to review."""
    key = email.strip().lower()
    record = EMPLOYEES.get(key)
    if record is None:
        log.info("directory miss for %s - returning unverified profile", key)
        return {
            "found": False, "email": key, "employee_id": None,
            "full_name": None, "department": "Unknown",
            "location": None, "job_title": None,
            "manager_email": os.getenv("IT_OPS_EMAIL", "it-ops@tcs-demo.local"),
            "manager_name": "IT Operations", "cost_center": None,
            "is_vip": False, "verified": False,
        }
    return {"found": True, "email": key, "verified": True, **record}


@app.get("/directory/assets", tags=["directory"])
async def get_assets(
    email: str = Query(..., description="Employee email"),
    _: None = Depends(maybe_fail),
    __: str = Depends(require_api_key),
) -> dict[str, Any]:
    items = ASSETS.get(email.strip().lower(), [])
    return {"email": email.strip().lower(), "count": len(items), "assets": items}


@app.get("/catalog/services", tags=["catalog"])
async def catalog(
    category: str | None = None,
    _: None = Depends(maybe_fail),
    __: str = Depends(require_api_key),
) -> dict[str, Any]:
    items = [s for s in SERVICE_CATALOG if category is None or s["category"] == category]
    return {"count": len(items), "items": items}


@app.get("/kb/search", tags=["catalog"])
async def kb_search(
    q: str = Query(..., min_length=2),
    limit: int = Query(3, ge=1, le=10),
    _: None = Depends(maybe_fail),
    __: str = Depends(require_api_key),
) -> dict[str, Any]:
    """Very small keyword scorer - enough for the workflow to attempt
    knowledge-based auto-resolution before assigning a human."""
    terms = {t for t in q.lower().replace(",", " ").split() if len(t) > 2}
    scored = []
    for art in KB_ARTICLES:
        hits = sum(1 for kw in art["keywords"] if any(kw in t or t in kw for t in terms))
        if hits:
            scored.append({**art, "score": round(hits / len(art["keywords"]), 3)})
    scored.sort(key=lambda a: a["score"], reverse=True)
    top = scored[:limit]
    return {
        "query": q,
        "count": len(top),
        "best_match": top[0] if top else None,
        "auto_resolvable": bool(top and top[0]["auto_resolvable"] and top[0]["score"] >= 0.4),
        "articles": top,
    }


@app.post("/tickets", status_code=201, tags=["tickets"])
async def create_ticket(
    body: TicketCreate,
    _: None = Depends(maybe_fail),
    __: str = Depends(require_api_key),
) -> dict[str, Any]:
    # Idempotency: the pipeline may retry after a transient failure, and the
    # system of record must not end up with duplicates.
    for ref, existing in TICKETS.items():
        if existing["ticket_id"] == body.ticket_id:
            log.info("idempotent replay for %s -> %s", body.ticket_id, ref)
            return {**existing, "external_ref": ref, "idempotent_replay": True}

    ref = _next_ref()
    now = datetime.now(timezone.utc)
    record = {
        **body.model_dump(mode="json"),
        "external_ref": ref,
        "status": "Pending Approval" if body.requires_approval else "Routed",
        "approval_status": "pending" if body.requires_approval else "not_required",
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "history": [{"ts": now.isoformat(), "event": "created", "by": body.source}],
        "correlation": _rand_id(),
    }
    TICKETS[ref] = record
    log.info("created %s (%s / %s / %s)", ref, body.priority, body.queue, body.category)
    return {**record, "idempotent_replay": False}


@app.get("/tickets/{ref}", tags=["tickets"])
async def get_ticket(
    ref: str,
    _: None = Depends(maybe_fail),
    __: str = Depends(require_api_key),
) -> dict[str, Any]:
    if ref not in TICKETS:
        raise HTTPException(status_code=404, detail=f"No ticket with external_ref {ref}")
    return TICKETS[ref]


@app.patch("/tickets/{ref}", tags=["tickets"])
async def patch_ticket(
    ref: str,
    body: TicketPatch,
    _: None = Depends(maybe_fail),
    __: str = Depends(require_api_key),
) -> dict[str, Any]:
    if ref not in TICKETS:
        raise HTTPException(status_code=404, detail=f"No ticket with external_ref {ref}")
    record = TICKETS[ref]
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    record.update(changes)
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    record["history"].append(
        {"ts": record["updated_at"], "event": "updated", "changes": changes}
    )
    log.info("patched %s with %s", ref, changes)
    return record


@app.get("/tickets", tags=["tickets"])
async def list_tickets(
    queue: str | None = None,
    status: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    __: str = Depends(require_api_key),
) -> dict[str, Any]:
    rows = list(TICKETS.values())
    if queue:
        rows = [r for r in rows if r.get("queue") == queue]
    if status:
        rows = [r for r in rows if r.get("status") == status]
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    return {"total": len(rows), "limit": limit, "offset": offset,
            "items": rows[offset: offset + limit]}


@app.get("/sla/policy", tags=["ops"])
async def sla_policy(__: str = Depends(require_api_key)) -> dict[str, Any]:
    return {
        "policy": {
            "P1": {"response_mins": 15,  "resolve_mins": 240},
            "P2": {"response_mins": 60,  "resolve_mins": 480},
            "P3": {"response_mins": 240, "resolve_mins": 1440},
            "P4": {"response_mins": 480, "resolve_mins": 4320},
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }


@app.post("/_test/reset", tags=["ops"])
async def reset(__: str = Depends(require_api_key)) -> dict[str, Any]:
    """Clear the in-memory store so each evaluation run starts clean."""
    count = len(TICKETS)
    TICKETS.clear()
    _SEQ["n"] = 0
    log.warning("store reset - %d tickets discarded", count)
    return {"reset": True, "discarded": count}


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level=LOG_LEVEL.lower())
