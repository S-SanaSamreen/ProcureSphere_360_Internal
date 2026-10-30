# Approval engine

Every approvable transaction is routed through one engine (`apps/approvals/engine.py`) instead of a
role check inside each module. Routing is **configuration** (policies and steps), evaluated
server-side, and every decision is recorded.

## Transaction types

| Module code | What is approved | Target object | Amount used for routing | Default policy (seeded) |
|---|---|---|---|---|
| `PR` | Purchase requisition | `PurchaseRequisition` | requisition total | Dept Approver, then Procurement Manager |
| `BUDGET_EXCEPTION` | Permission to overspend a budget | requisition (budget review) or PO | the shortfall | Finance / AP |
| `PO` | Purchase order | `PurchaseOrder` | PO total incl. tax | **none** (see below) |
| `PO_AMENDMENT` | PO amendment | `POAmendment` | absolute change in value | Procurement Manager |
| `SOURCING` | Sourcing award | `AwardDecision` | winning bid total | Procurement Manager |
| `CONTRACT` | Contract business approval | `Contract` | contract value | Procurement Manager |
| `INVOICE` | Invoice match exception | `MatchException` | absolute variance | Finance / AP |

`VENDOR` (KYC) is not routed through the engine: KYC approval is gated by document verification.

### PO approval is optional by design
With no active `PO` policy, a PO is issued immediately (the historical behaviour). When an active PO
policy matches (amount band, department, cost center) the PO is created in `APPROVAL`, nothing is
committed against the budget, and it is issued only after the last step is approved. A PO approved
straight from `DRAFT` with no policy still requires a Procurement Manager or Super Admin. All other
types **fail closed**: no matching policy means the transaction cannot be submitted.

## How a policy is chosen

1. Active, not superseded, not an exception route, amount within `[min_amount, max_amount]`.
2. Most specific scope wins: **cost center**, then **department**, then **global**.
3. Within a scope the highest matching `min_amount` wins.

## Versioning

A policy is never edited in place. "Editing" (UI, `PATCH /api/v1/approvals/policies/{id}/`) creates the
next version (same `policy_key`, `version + 1`) and supersedes the old row. A request records the
**exact policy version** it was routed under and a **snapshot of its steps**, so changing, retiring or
replacing a policy never re-routes work already in flight. Once any request has used a version, its
routing fields and steps are immutable at the model level (admin and ORM included). `DELETE` retires a
policy (`is_active = false`); policies are never deleted.

## Exception routing

A policy flagged **exception route** is never chosen as the normal chain. When a request is raised as an
exception its steps are *appended* after the normal chain:

* PR: budget overrun
* PO: pre-tax value above the requisition estimate
* Award: winner is not the lowest-priced compliant bid

If no exception policy matches, the normal chain applies unchanged.

## Budget exceptions

* **Requisition over budget:** the PR goes to `BUDGET_REVIEW` and a `BUDGET_EXCEPTION` request must be
  approved (Finance by default) *before* the budget is reserved and the normal chain starts.
* **PO over budget:** `request_po_budget_exception_service` opens a `BUDGET_EXCEPTION` request for the
  shortfall. Once approved it is a **one-time permission** for that PO and amount, consumed when the PO's
  commitment is posted. It does not cover a larger shortfall and cannot be reused.

## Who may approve

Checked by the engine on every decision, in this order:

1. **Role / named person** for the active step (or an active **delegate** of someone who qualifies; the
   delegate acts with the delegator's authority, including their department and limit). A Super Admin
   may stand in for a step.
2. **Organisation** and, for Department Approvers, **department** must match the request.
3. **Maker-checker:** whoever submitted the request can never approve it - Super Admin included.
4. **Four-eyes:** nobody approves two steps of the same request - Super Admin included.
5. **Approval limit:** `User.approval_limit` (blank = no personal limit) is read fresh from the database.

Rejection requires a justification and a user who appears on the request's chain (or a Super Admin).

## Entry points

* Module screens/APIs (PR, PO, amendment, award, contract, invoice exception) call the owning service.
* **Approvals inbox** (`/approvals/inbox/`): requisitions as before, plus "Other approvals awaiting you"
  for everything else. Decisions post to `approval_request_decide`.
* `POST /api/v1/approvals/actions/{id}/approve|reject/` dispatch only to the transaction types in
  `approvals.services.decision_handlers()`. There is no "set the status" fallback; unknown types are
  refused. `.../comment/` appends a new attributed entry and never edits history.
* Delegation: `/approvals/delegation/` (create, revoke). Dates, self-delegation, inactive or vendor
  delegates and overlapping delegations are validated.

## Known limits (not implemented)

* Overdue-approval reminders and escalations still cover **requisitions only** (now driven by the
  request's current step). Other transaction types have in-app notifications on submission and on each
  step but no SLA timer.
* Contract business approval has an API rejection (`business-reject`) but no button on the contract page.
* The policy screen offers up to 5 steps per policy.
* `DEPT_APPROVER` no longer gives contract business approval by default (the seeded policy names the
  Procurement Manager). Add a step, or a policy per contract-value band, to include other roles.
* An invoice match exception has no maker: the person who captured the invoice may resolve its exception.
