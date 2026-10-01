# RFQ/RFP Sourcing Module — Architecture Documentation

**Module:** `src/apps/sourcing/`  
**PRD Ref:** HPE-PRD-2026-PROC01 — Section: RFQ/RFP Sourcing  
**Status:** Implemented (Bid Window Phase) — Evaluation & Award deferred

---

## 1. Purpose

The Sourcing module enables Procurement Executives to create, publish, and manage
RFQ (Request for Quotation) and RFP (Request for Proposal) sourcing events. It
provides the data collection foundation that the Evaluation & Award module will
consume once implemented.

---

## 2. PRD Mapping

| PRD Requirement | Implementation |
|---|---|
| Create event | `create_sourcing_event_service()` |
| Invite eligible vendors | `invite_vendors_to_event_service()` with eligibility checks |
| Bid window | `bid_start_date / bid_end_date`, server-authoritative `is_accepting_bids` property |
| Clarifications | `Clarification` model + `submit_clarification_service()` + `respond_to_clarification_service()` |
| Technical section | `TechnicalRequirement` model + `add_technical_requirement_service()` |
| Commercial section | `CommercialRequirement` model + `add_commercial_requirement_service()` |
| Deadline control | Server-side `is_accepting_bids` check in `submit_vendor_bid_service()` |

---

## 3. Architecture

```
apps/sourcing/
├── models.py       — SourcingEvent, BidInvite, TechnicalRequirement,
│                     CommercialRequirement, Clarification, VendorBid,
│                     BidLine, AwardDecision
├── services.py     — All business logic + state transitions + audit writes
├── selectors.py    — Query logic (access-controlled, efficient ORM)
├── forms.py        — Django Forms for all user-facing input
├── views.py        — Template views with RBAC checks
├── urls.py         — URL patterns (sourcing: namespace)
├── admin.py        — Django admin for all models
├── api_urls.py     — DRF API viewsets for /api/v1/sourcing-events/
├── migrations/     — 0001_initial.py + 0002_clarification_... 
└── tests/
    └── test_sourcing.py  — 31 tests (all passing)
```

---

## 4. Data Model

### SourcingEvent
- `event_number`: unique, auto-generated (e.g. `RFQ-2026-00001`)
- `event_type`: `RFQ` / `RFP`
- `status`: see lifecycle below
- `bid_start_date`, `bid_end_date`: timezone-aware, server-enforced
- `clarification_deadline`: optional separate deadline for vendor questions
- `technical_section_enabled`, `commercial_section_enabled`, `clarifications_allowed`
- `published_at`, `closed_at`: lifecycle timestamps
- `is_sealed`: always True (vendors cannot see competitors' bids)

### BidInvite
- `event + vendor`: unique constraint (prevents duplicate invitations)
- `status`: INVITED / ACCEPTED / DECLINED
- `is_responded`, `responded_at`

### TechnicalRequirement
- `event`, `title`, `description`, `importance` (MANDATORY/PREFERRED/OPTIONAL), `order`
- Logically separate from commercial section per PRD

### CommercialRequirement
- `event`, `title`, `description`, `requirement_type` (FIXED_PRICE/UNIT_RATE/MILESTONE/OTHER)
- `unit`, `quantity`, `order`

### Clarification
- `event`, `vendor`, `question`, `response`
- `status`: OPEN / ANSWERED / CLOSED
- `visibility`: PRIVATE (one vendor only) / ALL_VENDORS
- `responded_by`, `responded_at`

### VendorBid
- `event + vendor`: unique constraint
- `bid_number`: auto-generated (e.g. `BID-2026-00001`)
- `total_bid_amount`, `status`, `submitted_at`

### BidLine
- `bid`, `commercial_requirement` (FK to link bid response to commercial section)
- `item_description`, `quantity`, `quoted_unit_price`, `quoted_total_price`

---

## 5. Event Lifecycle

```
DRAFT → PUBLISHED (BID_WINDOW) → CLOSED → TECHNICAL_REVIEW →
COMMERCIAL_REVIEW → AWARD_APPROVAL → AWARDED

DRAFT → CANCELLED
PUBLISHED (BID_WINDOW) → CANCELLED
```

All transitions are validated server-side in `services.py` using the
`VALID_TRANSITIONS` dict on the `SourcingEvent` model. Invalid transitions
raise `ValidationError`.

---

## 6. Vendor Invitation Flow

1. Procurement Executive creates event (DRAFT)
2. PE invites vendors via `invite_vendors_to_event_service()`
   - Validates vendor status: ACTIVE or APPROVED only
   - SUSPENDED/other status → `ValidationError`
   - Duplicate invitations → idempotent (no error, no duplicate)
3. Event published → BID_WINDOW opens
4. Vendor users with `role_code == VENDOR_USER` and linked `vendor` FK
   can access only events they are invited to

---

## 7. Bid Window — Deadline Enforcement

The `bid_start_date` and `bid_end_date` are stored as timezone-aware DateTimeFields.
The server determines acceptance via `SourcingEvent.is_accepting_bids`:

```python
@property
def is_accepting_bids(self):
    return (
        self.status == STATUS_BID_WINDOW
        and self.bid_start_date <= timezone.now() <= self.bid_end_date
    )
```

`submit_vendor_bid_service()` checks `is_accepting_bids` server-side.
**No browser-supplied timestamp is trusted.** The frontend may show a countdown
timer as UX, but the backend is the single source of truth.

---

## 8. Technical Section

- Procurement adds `TechnicalRequirement` records while event is DRAFT
- Fields: title, description, importance (mandatory/preferred/optional), order
- Vendors can view requirements read-only once event is published
- Logically separated from commercial section (separate model, separate UI section)
- Intended for consumption by Evaluation & Award module later

---

## 9. Commercial Section

- Procurement adds `CommercialRequirement` records while event is DRAFT
- Fields: title, description, requirement_type, unit, quantity, order
- Vendors read requirements and respond via `BidLine` (linked to `CommercialRequirement`)
- Logically separated from technical section

---

## 10. Clarifications

- Vendor submits question via `submit_clarification_service()`
  - Must be invited to the event
  - Event must be BID_WINDOW
  - Must be within clarification window
- Procurement responds via `respond_to_clarification_service()`
  - Sets response text, visibility (PRIVATE or ALL_VENDORS), timestamp
- **Confidentiality:** PRIVATE clarifications are not exposed to other vendors
  (enforced in `get_clarifications_for_event_and_vendor()` selector via Q filter)

---

## 11. Authorization / Security

| Action | Required Role |
|---|---|
| View event list | PROC_EXEC, PROC_MGR, SUPER_ADMIN |
| Create/Edit/Publish/Close event | PROC_EXEC, PROC_MGR, SUPER_ADMIN |
| Invite vendors / manage requirements | PROC_EXEC, PROC_MGR, SUPER_ADMIN |
| Respond to clarifications | PROC_EXEC, PROC_MGR, SUPER_ADMIN |
| View own invitations | VENDOR_USER |
| Submit clarification | VENDOR_USER (must be invited) |
| Access event detail | VENDOR_USER (must be invited AND event not DRAFT) |

**Object-level IDOR protection:**
- Vendor access to event detail checks `BidInvite.objects.filter(event=event, vendor=user.vendor).exists()`
- Vendor cannot access DRAFT events even if invited
- Vendors cannot access events where they have no invitation
- Vendor bid data is scoped by `vendor_id` in selectors

**CSRF:** All POST actions use Django CSRF protection. The base template injects
the CSRF token into HTMX requests globally.

---

## 12. Audit Trail

The following actions write to `AuditLog` (append-only, immutable):

| Action | AuditLog entry |
|---|---|
| Event created | `ACTION_CREATE` on `SourcingEvent` |
| Draft updated | `ACTION_UPDATE` on `SourcingEvent` |
| Event published | `ACTION_UPDATE` on `SourcingEvent` (prev: DRAFT, new: BID_WINDOW) |
| Event closed | `ACTION_UPDATE` on `SourcingEvent` (prev: BID_WINDOW, new: CLOSED) |
| Vendor invited | `ACTION_CREATE` on `BidInvite` |
| Vendor removed | `ACTION_DELETE` on `BidInvite` |
| Technical req added | `ACTION_CREATE` on `TechnicalRequirement` |
| Commercial req added | `ACTION_CREATE` on `CommercialRequirement` |
| Clarification submitted | `ACTION_CREATE` on `Clarification` |
| Clarification responded | `ACTION_UPDATE` on `Clarification` |
| Bid submitted | `ACTION_CREATE` / `ACTION_UPDATE` on `VendorBid` |
| Award decision | `ACTION_APPROVE` on `AwardDecision` |

---

## 13. API Endpoints

Base: `/api/v1/sourcing-events/`

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/v1/sourcing-events/events/` | List all events |
| POST | `/api/v1/sourcing-events/events/` | Create event |
| GET | `/api/v1/sourcing-events/events/{id}/` | Event detail |
| GET | `/api/v1/sourcing-events/bids/` | List bids (scoped) |

Template UI Endpoints (under `/sourcing/`):

| URL | View |
|---|---|
| `/sourcing/` | Event list |
| `/sourcing/create/` | Create event |
| `/sourcing/{id}/` | Event detail (procurement or vendor) |
| `/sourcing/{id}/edit/` | Edit DRAFT event |
| `/sourcing/{id}/invite-vendors/` | Manage invitations |
| `/sourcing/{id}/publish/` | Publish event |
| `/sourcing/{id}/close/` | Close event |
| `/sourcing/{id}/add-technical-req/` | Add tech requirement |
| `/sourcing/{id}/add-commercial-req/` | Add commercial requirement |
| `/sourcing/{id}/clarifications/{clar_id}/respond/` | Respond to clarification |
| `/sourcing/{id}/submit-clarification/` | Vendor submits clarification |
| `/sourcing/my-invitations/` | Vendor invitation list |

---

## 14. Testing

**31 tests, all passing.** Coverage:

- `SourcingEventModelTests`: event uniqueness, number format (RFQ/RFP), invalid dates, status
- `BidInviteModelTests`: duplicate invitation, suspended vendor blocked
- `PublishEventServiceTests`: publish guards (no vendors, wrong status), successful publish
- `CloseEventServiceTests`: close from BID_WINDOW, cannot close DRAFT
- `TechnicalRequirementTests`: add to draft, blocked on published
- `CommercialRequirementTests`: create with all fields
- `ClarificationTests`: invited vendor can submit, uninvited blocked, PE can respond
- `DeadlineEnforcementTests`: before start, during window, after deadline
- `SourcingViewRBACTests`: unauthenticated redirect, RBAC enforcement, CSRF, IDOR (vendor A/B isolation), vendor draft access blocked

---

## 15. Future: Evaluation & Award Module

This module provides the foundation for Evaluation & Award:

- `VendorBid` records all submitted bids with `BidLine` linked to `CommercialRequirement`
- `TechnicalRequirement` is available for scoring/weighting
- `SourcingEvent` can transition: CLOSED → TECHNICAL_REVIEW → COMMERCIAL_REVIEW → AWARD_APPROVAL → AWARDED
- `AwardDecision` model exists for recording the winning bid
- Evaluation & Award module should implement: weighted technical scoring, commercial comparison, bid evaluation records, award approval workflow

**NOT implemented in this module (deferred):**
- Weighted technical scoring
- Vendor bid ranking
- Commercial comparison engine
- Award recommendation logic
- Award approval routing
