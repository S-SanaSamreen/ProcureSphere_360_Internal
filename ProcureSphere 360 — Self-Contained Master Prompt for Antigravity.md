# PROCURESPHERE 360
# SELF-CONTAINED MASTER IMPLEMENTATION PROMPT
## Enterprise Procurement, Vendor & Contract Management ERP

---

# 0. INSTRUCTION TO ANTIGRAVITY

You are the lead software architect, senior Django backend engineer, frontend engineer, database engineer, DevOps engineer, security engineer, QA engineer and technical documentation engineer for this project.

You are building:

**ProcureSphere 360 — Enterprise Procurement, Vendor & Contract Management ERP**

Development partner:

**VPD Technologies Pvt. Ltd.**

This prompt is the complete functional and technical specification for the project.

## CRITICAL

There is NO external PRD that you should depend on.

Everything required to understand and implement the system is contained in this prompt.

Do not ask the developer to attach the PRD.

Do not assume that a missing requirement exists elsewhere.

Do not replace the requirements below with generic procurement-system assumptions.

If something is genuinely unspecified, make the smallest reasonable extensible implementation and clearly document the assumption.

---

# 1. PROJECT OBJECTIVE

Build a real, production-oriented, database-backed enterprise procurement ERP.

This is NOT:

- a UI prototype
- a presentation-only application
- a static dashboard
- a collection of disconnected CRUD pages
- a fake workflow
- a frontend-only application
- a hardcoded demonstration

Every important business action must:

1. Reach the backend.
2. Be authorized server-side.
3. Validate business rules server-side.
4. Persist real data in PostgreSQL where applicable.
5. Execute within proper transactions.
6. Create audit history where required.
7. Update related business objects.
8. Return meaningful success/error responses.
9. Be reflected in the UI.
10. Be covered by tests.

Never create fake success messages such as:

`"Approved successfully"` 

unless an actual backend approval transaction occurred.

Never create buttons that do nothing.

Never implement RBAC only by hiding frontend controls.

---

# 2. BUSINESS PURPOSE

ProcureSphere 360 manages the complete enterprise procurement lifecycle:

Organization
→ Vendor Onboarding
→ KYC
→ Vendor Master
→ Vendor Risk
→ Purchase Requisition
→ Approval
→ Budget Validation
→ Sourcing
→ RFQ/RFP
→ Vendor Bidding
→ Technical Evaluation
→ Commercial Evaluation
→ Award
→ Purchase Order
→ Vendor Acknowledgement
→ Goods/Service Receipt
→ Inspection
→ Supplier Invoice
→ 3-Way Match
→ Exception Resolution
→ Payment Ready
→ Payment
→ Contract Management
→ Supplier Performance
→ Spend Analysis
→ Notifications
→ Audit
→ Reporting

The system must preserve relationships across this entire lifecycle.

---

# 3. TECHNOLOGY STACK

Use this technical baseline.

## Backend

- Python 3.11+
- Django 4.2+
- Django REST Framework 3.14+
- PostgreSQL 15+
- Redis 7+
- Celery 5+
- REST APIs
- OpenAPI documentation
- Docker
- Docker Compose
- Gunicorn
- Nginx where appropriate
- GitHub Actions

## Frontend

Primary approach:

- Django Templates
- HTMX
- Bootstrap 5 OR Tailwind CSS

React 18+ may ONLY be used for isolated widgets where there is a genuine technical justification.

Do NOT convert the complete frontend into React.

Prefer server-rendered Django Templates + HTMX.

---

# 4. IMPLEMENTATION PRINCIPLE

Build the system incrementally.

Each phase must integrate with the previous phases.

A phase is NOT complete merely because files or screens have been created.

For every applicable phase, implement:

- Models
- Migrations
- Services/domain logic
- Views
- Forms/serializers
- APIs
- Permissions
- Templates
- HTMX interactions
- Validation
- Transactions
- Audit logging
- Error handling
- Tests
- Documentation
- Integration with existing modules

Do not move forward while the current phase contains obvious broken functionality.

---

# 5. PHASE ORDER — MANDATORY

The following phase ordering is intentional.

## VERY IMPORTANT

### PHASE 1 MUST BE AUTHENTICATION.

Do NOT start with Docker.

Do NOT start with Redis.

Do NOT start with Celery.

Do NOT start with dashboards.

Do NOT start with procurement workflows.

Authentication comes first.

### Docker MUST appear in the middle of development.

Docker must NOT be Phase 1.

Docker must NOT be the final step.

Docker should containerize a meaningful existing Django application after the core application foundation and major procurement modules have been built.

### Redis/Celery comes after Docker.

---

# PHASE 1 — AUTHENTICATION & SESSION SECURITY

Start here.

Implement real Django authentication using Django session cookies.

Implement:

- Login
- Logout
- Session creation
- Session expiry
- Password hashing
- Password validation
- Strong password policy
- Authentication middleware
- CSRF protection
- Secure session cookies
- Secure authentication handling
- Authentication error handling
- User persistence
- Login/logout auditing where applicable
- Authentication tests

Configure the initial PostgreSQL database connection required for the Django application.

Do NOT introduce:

- Docker
- Redis
- Celery
- React
- procurement workflows

in this phase.

Authentication must be real and database-backed.

---

# PHASE 2 — APPLICATION FOUNDATION & RBAC

Create the application architecture.

Implement:

- Django project
- Django applications
- settings structure
- environment configuration
- PostgreSQL configuration
- common utilities
- service layer
- validators
- exception handling
- structured errors
- logging
- request/correlation IDs
- health endpoint
- common navigation
- common UI components

Implement server-side RBAC.

Roles:

1. Super Admin
2. Requester
3. Department Approver
4. Procurement Executive
5. Procurement Manager
6. Finance/AP
7. Stores/Receiver
8. Legal/Contract Manager
9. Compliance/Auditor
10. Vendor User

Authorization must be enforced by the backend.

Implement object-level access control where required.

---

# PHASE 3 — ORGANIZATION / COST CENTER

Module 1.

Implement:

- Organization
- Department
- Cost Center
- organizational hierarchy
- users associated with organizations
- department relationships
- cost-center ownership
- active/inactive states
- search
- filtering
- validation
- audit trail

Organization and Cost Center information must later feed:

- Purchase Requisitions
- Approval Policy
- Budget
- Spend
- Reports
- Audit

---

# PHASE 4 — VENDOR ONBOARDING & KYC

Module 2.

Implement complete vendor onboarding.

Vendor lifecycle:

DRAFT
→ SUBMITTED
→ KYC REVIEW
→ APPROVED / REJECTED
→ ACTIVE
→ ON HOLD / SUSPENDED

Implement:

- vendor registration
- vendor profile
- vendor contacts
- vendor categories
- KYC information
- KYC document upload
- document metadata
- document validation
- KYC review
- approval
- rejection
- rejection reason
- vendor status
- audit trail

File uploads must have:

- extension validation
- MIME validation
- file size validation
- sanitized filenames/paths
- secure storage
- authorization checks

Vendor onboarding must connect to Vendor Master.

---

# PHASE 5 — VENDOR MASTER & RISK

Module 3.

Implement:

- Vendor Master
- Vendor Contact
- Vendor Category
- Vendor Risk Record
- Vendor Scorecard foundation
- risk classification
- compliance status
- vendor status
- vendor history
- vendor search
- vendor filtering
- vendor activity
- audit trail

Vendor status must affect procurement eligibility.

Handle:

- vendor suspension
- vendor hold
- vendor reactivation
- vendor risk updates

Edge case:

If a vendor is suspended while an active sourcing event or PO exists, preserve the existing records and apply controlled business behavior rather than deleting or corrupting history.

---

# PHASE 6 — PURCHASE REQUISITION

Module 4.

Purchase Requisition lifecycle:

DRAFT
→ SUBMITTED
→ MANAGER REVIEW
→ BUDGET REVIEW
→ APPROVED / REJECTED
→ SOURCING / PO

Implement:

- Purchase Requisition
- PR Lines
- requester
- department
- cost center
- item/service description
- quantity
- estimated amount
- category
- attachments
- submission
- validation
- comments
- approval state
- rejection
- history
- audit

PRs must be real PostgreSQL records.

---

# PHASE 7 — APPROVAL POLICY ENGINE

Module 5.

Build a configurable approval engine.

Approval routing must be evaluated server-side.

Routing may depend on:

- amount
- department
- cost center
- configured policy
- approval levels

Implement:

- ApprovalPolicy
- ApprovalStep
- ApprovalAction
- approval sequencing
- multi-level approval
- approve
- reject
- return/rework where appropriate
- comments
- approval history
- delegated/unavailable approver handling
- authorization validation

Do NOT hardcode approval routing into frontend JavaScript.

Do NOT make approval a simple boolean field without workflow history.

---

# PHASE 8 — BUDGET / SPEND

Module 13.

Implement:

- Budget
- budget allocation
- budget validation
- budget reservation
- committed amount
- reserved amount
- actual spend
- SpendLedger
- budget consumption
- budget status

Always distinguish:

- Available
- Reserved/Committed
- Actual Spend

Do not silently permit budget overruns.

If an exception process is needed, make it explicit, authorized and auditable.

Connect budget processing to PR approval and procurement transactions.

---

# PHASE 9 — RFQ/RFP SOURCING

Module 6.

Implement:

- SourcingEvent
- RFQ
- RFP
- bid invitation
- invited vendors
- sourcing event configuration
- publication
- bid window
- event closure
- clarifications

Sourcing lifecycle:

DRAFT
→ PUBLISHED
→ BID WINDOW
→ TECHNICAL REVIEW
→ COMMERCIAL REVIEW
→ AWARD APPROVAL
→ AWARDED / CANCELLED

Only eligible vendors should be allowed to participate.

---

# PHASE 10 — VENDOR BID PORTAL

Module 7.

Vendor users must be able to:

- see invited sourcing events
- view eligible event information
- submit bids
- submit bid lines
- provide commercial information
- upload required documents
- submit before deadline
- amend bids where permitted
- withdraw bids where permitted
- view own submission status

CRITICAL SECURITY REQUIREMENT:

Vendor A must NEVER see Vendor B's bid.

Competing bid confidentiality must be enforced server-side.

Do not rely on frontend hiding.

Handle:

- bid withdrawal
- bid amendment
- deadline enforcement
- duplicate submission
- event closure

---

# PHASE 11 — EVALUATION & AWARD

Module 8.

Implement:

- technical evaluation
- commercial evaluation
- bid comparison
- evaluator access
- evaluation records
- clarification
- negotiation records where applicable
- award decision
- award approval
- selected vendor
- unsuccessful vendor handling
- audit trail

Evaluation visibility must follow event stage and authorization.

Award decisions must be persisted.

---

# PHASE 12 — PURCHASE ORDER

Module 9.

Implement:

- PurchaseOrder
- POLine
- DeliverySchedule
- PO approval
- PO issuance
- vendor acknowledgement
- PO status
- PO versioning
- PO amendments
- amendment approval
- cancellation
- history
- audit

PO lifecycle:

DRAFT
→ APPROVAL
→ ISSUED
→ ACKNOWLEDGED
→ PARTIAL RECEIPT
→ COMPLETED / CANCELLED

PO amendments must preserve previous versions.

Never overwrite historical versions without retaining them.

---

# PHASE 13 — DOCKER & DEPLOYMENT INFRASTRUCTURE

THIS IS THE MIDDLE INFRASTRUCTURE PHASE.

At this point the application already contains:

- authentication
- RBAC
- organization
- vendor
- requisition
- approvals
- budget
- sourcing
- bids
- evaluation
- PO

Now containerize the EXISTING application.

Do NOT recreate the application.

Implement:

- Dockerfile
- Docker Compose
- Django container
- PostgreSQL container
- environment configuration
- service dependencies
- migrations
- static files
- media
- health checks
- startup process
- Gunicorn
- Nginx where applicable
- networking
- development configuration
- production configuration

Validate existing functionality inside Docker.

Never commit secrets.

---

# PHASE 14 — RECEIPT / INSPECTION

Module 10.

Implement:

- GoodsReceipt
- ReceiptLine
- partial receipt
- final receipt
- quantity tracking
- delivery tracking
- inspection
- InspectionRecord
- RejectionRecord
- rejected goods
- returned goods
- quantity corrections
- receipt history

Support multiple receipts against one PO.

Support:

- partial delivery
- multiple deliveries
- rejected goods
- corrected quantities

Never destroy historical receipt information.

Update PO completion based on actual receipt state.

---

# PHASE 15 — INVOICE & 3-WAY MATCH

Module 11.

Implement:

- SupplierInvoice
- InvoiceLine
- invoice validation
- duplicate invoice detection
- MatchResult
- MatchException
- PaymentStatus

Invoice lifecycle:

RECEIVED
→ VALIDATION
→ 3-WAY MATCH
→ EXCEPTION / APPROVAL
→ READY FOR PAYMENT
→ PAID / REJECTED

## 3-WAY MATCH

Compare:

1. Purchase Order
2. Goods Receipt / Service Entry
3. Supplier Invoice

Compare applicable:

- quantity
- price
- amount
- received quantity
- invoice quantity
- invoice amount
- PO values

Implement configured amount/percentage tolerances.

If the difference exceeds tolerance:

Create a real MatchException.

Do not fake a successful match.

Support:

- exception creation
- exception review
- exception resolution
- approval where required
- payment-ready transition

Duplicate invoice numbers must be detected within the relevant vendor context.

---

# PHASE 16 — CONTRACT LIFECYCLE

Module 12.

Implement:

- Contract
- ContractVersion
- ContractMilestone
- ContractObligation
- ContractAlert
- ContractDocument

Contract lifecycle:

DRAFT
→ LEGAL REVIEW
→ BUSINESS APPROVAL
→ ACTIVE
→ RENEWAL DUE
→ RENEWED / EXPIRED / TERMINATED

Implement:

- contract creation
- legal review
- business approval
- activation
- versioning
- amendments
- milestones
- obligations
- renewal
- expiry
- termination
- document management
- audit

Contract amendments must preserve previous versions.

---

# PHASE 17 — REDIS + CELERY

Now integrate:

- Redis
- Celery
- Celery workers
- Celery Beat where needed

Use asynchronous processing for:

- notifications
- escalations
- contract expiry alerts
- renewal reminders
- scheduled jobs
- report/export processing where appropriate

Scheduled jobs must:

- query real records
- execute real business actions
- create execution evidence
- be auditable
- avoid duplicate execution where required
- support retry behavior

Validate Redis/Celery within Docker.

---

# PHASE 18 — SUPPLIER PERFORMANCE

Module 14.

Implement:

- VendorScorecard
- supplier performance metrics
- delivery performance
- rejection performance
- quality indicators
- sourcing participation
- contract compliance indicators where applicable
- performance score
- performance trend
- historical scorecards

Supplier performance must be based on actual procurement/receipt/invoice data wherever the metric depends on system records.

Do not use fake chart values.

---

# PHASE 19 — NOTIFICATIONS & ESCALATION

Module 15.

Implement:

- Notification
- notification types
- user notifications
- approval reminders
- approval escalations
- contract expiry alerts
- renewal alerts
- sourcing deadlines
- invoice exceptions
- relevant workflow notifications

Support:

- unread/read state
- timestamps
- recipient
- related business object
- notification history

Use Celery for asynchronous/scheduled processing where appropriate.

---

# PHASE 20 — REPORTS

Module 16 / Reporting capability.

Implement real reports backed by PostgreSQL.

Mandatory reports:

## 1. Purchase Requisition Aging

Show:

- PR
- requester
- department
- cost center
- current stage
- age
- pending approver
- bottleneck

## 2. Spend Report

Filter by:

- vendor
- category
- department
- cost center
- period

## 3. Sourcing Report

Show:

- sourcing event cycle time
- bid participation
- invited vendors
- submitted bids
- event status

## 4. Purchase Order Report

Show:

- open POs
- partial POs
- completed POs
- cancelled POs

## 5. Receipt / Delivery Report

Show:

- receipts
- rejected quantities
- delivery performance
- partial deliveries

## 6. Invoice Exception Aging

Show:

- invoice
- vendor
- exception
- exception age
- current status

## 7. Contract Report

Show:

- active contracts
- expiry
- renewal
- milestones
- obligations

## 8. Supplier Performance

Show:

- supplier score
- performance trend
- delivery
- quality
- historical performance

## 9. User / Approval Audit Export

Support filters and export.

All reports must use real data.

Do not hardcode metrics.

---

# PHASE 21 — AUDIT & TRACEABILITY

Implement immutable/append-only audit logging for critical activities.

AuditLog must capture where applicable:

- actor
- action
- object
- object ID
- previous state
- updated state
- timestamp
- source IP
- user agent
- relevant request/correlation ID

Audit critical actions including:

- create
- update
- delete where allowed
- approval
- rejection
- login
- sensitive access
- export
- vendor approval
- PR approval
- sourcing actions
- award
- PO issue
- PO amendment
- receipt
- invoice processing
- match exception
- contract changes

Audit history must not be casually editable through the application.

---

# PHASE 22 — TEN ROLE DASHBOARDS

Build all ten role-based dashboards.

Dashboards must be backed by real database queries.

Do not create fake statistics.

---

## DASHBOARD 1 — SUPER ADMIN

Purpose:

System administration and governance.

Show:

- organization overview
- user count
- role count
- vendor count
- active procurement transactions
- pending approvals
- system alerts
- audit activity
- security-related events where applicable

Actions:

- manage users
- manage roles
- manage permissions
- organization configuration
- cost center configuration
- approval policy configuration
- audit access
- system settings

---

## DASHBOARD 2 — REQUESTER

Purpose:

Employee initiating procurement requests.

Show:

- my PRs
- draft PRs
- submitted PRs
- pending approvals
- approved PRs
- rejected PRs
- PR aging
- recent activity
- notifications

Actions:

- create PR
- edit draft
- submit PR
- attach documents
- view approval progress
- respond to returned/rejected requests where allowed
- track resulting sourcing/PO status where permitted

---

## DASHBOARD 3 — DEPARTMENT APPROVER

Purpose:

Review departmental requests.

Show:

- pending approvals
- PR amount
- department
- requester
- cost center
- approval history
- aging
- budget context where authorized

Actions:

- approve
- reject
- return/rework where permitted
- add comments
- view supporting documents

Every action must be backend-authorized and audited.

---

## DASHBOARD 4 — PROCUREMENT EXECUTIVE

Purpose:

Operational procurement processing.

Show:

- approved PRs
- sourcing events
- upcoming deadlines
- bid participation
- evaluations
- pending procurement tasks
- POs
- supplier activity

Actions:

- create sourcing event
- invite vendors
- manage RFQ/RFP
- review bids
- perform evaluations where authorized
- create/prepare PO
- monitor procurement lifecycle

---

## DASHBOARD 5 — PROCUREMENT MANAGER

Purpose:

Procurement oversight and decision management.

Show:

- procurement pipeline
- PR aging
- sourcing events
- pending award approvals
- PO status
- vendor risks
- spend summary
- procurement KPIs

Actions:

- approve awards where authorized
- oversee sourcing
- review vendor performance
- review procurement exceptions
- monitor spend
- oversee procurement operations

---

## DASHBOARD 6 — FINANCE / AP

Purpose:

Budget, invoice and payment readiness.

Show:

- budgets
- available budget
- reserved budget
- actual spend
- invoice queue
- 3-way match status
- match exceptions
- exception aging
- payment-ready invoices
- paid/rejected invoices

Actions:

- budget review
- invoice validation
- match review
- exception resolution/approval where authorized
- move invoice to payment-ready
- payment status updates where permitted

---

## DASHBOARD 7 — STORES / RECEIVER

Purpose:

Goods receipt and inspection.

Show:

- expected deliveries
- open POs
- partial deliveries
- pending inspections
- received quantities
- rejected quantities
- delivery performance

Actions:

- create goods receipt
- record partial receipt
- record final receipt
- perform inspection
- reject goods
- record rejection reason
- correct quantities using controlled historical-safe mechanisms

---

## DASHBOARD 8 — LEGAL / CONTRACT MANAGER

Purpose:

Contract governance.

Show:

- contracts
- contracts under legal review
- active contracts
- renewal due
- expiry
- obligations
- milestones
- alerts
- contract versions

Actions:

- create contract
- legal review
- approve where authorized
- create amendments
- manage milestones
- manage obligations
- renew
- terminate where authorized
- upload contract documents

---

## DASHBOARD 9 — COMPLIANCE / AUDITOR

Purpose:

Audit, compliance and traceability.

Show:

- audit activity
- vendor KYC status
- vendor risk
- approval history
- sourcing activity
- PO changes
- invoice exceptions
- contract changes
- security-sensitive activity
- workflow history

Actions:

- search audit records
- filter audit records
- inspect lifecycle history
- export audit report
- review compliance information

Auditor permissions must not automatically grant mutation privileges.

---

## DASHBOARD 10 — VENDOR USER

Purpose:

Vendor self-service.

Show:

- vendor profile
- KYC status
- documents
- invited sourcing events
- bid status
- submitted bids
- award status where permitted
- POs
- acknowledgements
- receipts/payment status where authorized
- contract information where authorized
- notifications

Actions:

- update permitted vendor information
- upload KYC documents
- participate in invited RFQ/RFP
- submit bids
- amend/withdraw where permitted
- acknowledge PO
- view permitted transaction status

Vendor must only access its own data.

---

# PHASE 23 — CROSS-DASHBOARD INTEGRATION

The dashboards are NOT independent applications.

Implement shared workflow transitions.

Example:

Requester creates PR
→ Department Approver sees pending approval
→ Procurement sees approved PR
→ Finance sees budget impact
→ Procurement creates sourcing event
→ Vendor sees invitation
→ Procurement sees submitted bids
→ Procurement Manager sees evaluation/award
→ Finance/Procurement sees PO
→ Vendor acknowledges PO
→ Stores records receipt
→ Finance sees receipt
→ Finance processes invoice
→ System performs 3-way match
→ Exception appears if tolerance fails
→ Finance resolves exception
→ Invoice becomes payment-ready
→ Supplier performance updates
→ Spend reports update
→ Audit trail records lifecycle

Every dashboard must reflect real state from shared database records.

---

# PHASE 24 — CROSS-MODULE INTEGRATION

Implement these relationships.

## Organization → PR

Department and Cost Center must be associated with PR.

## Organization → Approval

Approval policies must be able to use organization/department/cost-center context.

## Organization → Budget

Budgets must be associated with applicable organizational dimensions.

## Vendor Onboarding → Vendor Master

Approved vendors become eligible vendor master records.

## Vendor Master → Sourcing

Only eligible vendors should participate according to business rules.

## Vendor → PO

Awarded vendors become PO suppliers.

## Vendor → Performance

Vendor performance uses actual transactions.

## PR → Approval

Submitted PR enters approval workflow.

## Approval → Budget

Approved/reviewed PR interacts with budget reservation.

## PR → Sourcing

Approved procurement demand can create sourcing.

## Sourcing → Bid Portal

Invited vendors receive events.

## Bid Portal → Evaluation

Submitted bids become evaluation inputs.

## Evaluation → Award

Evaluation produces award decision.

## Award → PO

Award produces PO.

## PO → Receipt

PO controls expected receipt.

## Receipt → Invoice

Receipt provides one side of 3-way match.

## PO → Invoice

PO provides another side of 3-way match.

## Invoice → 3-Way Match

Invoice is matched against PO and receipt/service entry.

## Contract → Notifications

Milestones/renewals/expiry produce alerts.

## All Modules → Audit

Critical actions create audit records.

## All Transactions → Reports

Reports use real transactional data.

---

# PHASE 25 — CORE DATA ENTITIES

Implement the following core entities.

## ORGANIZATION / ACCESS

- Organization
- Department
- CostCenter
- User
- Role
- Permission
- ApprovalPolicy
- ApprovalStep

## VENDOR

- Vendor
- VendorContact
- VendorDocument
- VendorCategory
- VendorRiskRecord
- VendorScorecard

## REQUISITION

- PurchaseRequisition
- PRLine
- PRAttachment
- ApprovalAction
- BudgetReservation

## SOURCING

- SourcingEvent
- BidInvite
- VendorBid
- BidLine
- BidEvaluation
- Clarification
- AwardDecision

## ORDERING

- PurchaseOrder
- POLine
- POAmendment
- DeliverySchedule

## RECEIPT

- GoodsReceipt
- ReceiptLine
- InspectionRecord
- RejectionRecord

## INVOICE

- SupplierInvoice
- InvoiceLine
- MatchResult
- MatchException
- PaymentStatus

## CONTRACT

- Contract
- ContractVersion
- ContractMilestone
- ContractObligation
- ContractAlert
- ContractDocument

## CONTROL

- Budget
- SpendLedger
- Notification
- AuditLog
- ExportJob

Use proper foreign keys, indexes, constraints and relationships.

---

# PHASE 26 — MANDATORY API GROUPS

Build REST APIs using DRF.

## Vendor APIs

Examples:

`/api/v1/vendors/`

`/api/v1/vendors/{id}/documents/`

`/api/v1/vendors/{id}/scorecard/`

## Requisition APIs

`/api/v1/requisitions/`

`/api/v1/requisitions/{id}/submit/`

`/api/v1/requisitions/{id}/approve/`

## Sourcing APIs

`/api/v1/sourcing-events/`

`/api/v1/bids/`

`/api/v1/evaluations/`

## Purchase Order / Receipt APIs

`/api/v1/purchase-orders/`

`/api/v1/receipts/`

## Invoice APIs

`/api/v1/invoices/`

`/api/v1/invoices/{id}/match/`

`/api/v1/invoices/{id}/exceptions/`

## Contract APIs

`/api/v1/contracts/`

`/api/v1/contracts/{id}/renewals/`

`/api/v1/contracts/{id}/alerts/`

## Reporting APIs

`/api/v1/reports/spend/`

`/api/v1/reports/aging/`

`/api/v1/reports/supplier-performance/`

APIs must support:

- authentication
- authorization
- validation
- object-level access
- filtering
- search where applicable
- pagination
- structured errors
- correct HTTP status codes
- OpenAPI documentation

---

# PHASE 27 — EDGE CASES

Explicitly implement and test the following.

## Vendor

- duplicate vendor
- incomplete KYC
- rejected KYC
- suspended vendor
- vendor reactivation
- vendor suspended during open sourcing
- vendor suspended with active PO

## PR

- incomplete PR
- invalid amount
- missing cost center
- rejection
- return/rework
- approver unavailable
- delegated approver
- duplicate submission

## Approval

- multiple levels
- approval rejection
- approval timeout/escalation
- unauthorized approval attempt
- approver unavailable

## Budget

- insufficient budget
- reserved budget
- committed budget
- actual spend
- explicit exception/override

## Sourcing

- no bids
- one bid
- multiple bids
- bid withdrawal
- bid amendment
- late bid
- closed event
- vendor attempting unauthorized access

## PO

- PO amendment
- multiple amendments
- amendment approval
- cancellation
- partial receipt
- final receipt
- historical version retrieval

## Receipt

- partial delivery
- multiple receipts
- rejected goods
- returned goods
- quantity correction
- over-receipt according to configured rules

## Invoice

- duplicate invoice number
- invalid invoice
- partial invoice
- multiple invoices where permitted
- mismatch
- tolerance match
- tolerance exception
- rejected invoice
- payment-ready transition

## Contract

- amendment
- version history
- renewal
- expiry
- termination
- missed obligation
- multiple milestones

---

# PHASE 28 — SECURITY HARDENING

Implement security according to enterprise standards and OWASP principles.

Must address:

- authentication security
- authorization
- object-level authorization
- CSRF
- session security
- secure cookies
- password hashing
- password policy
- throttling/lockout
- IDOR
- SQL injection
- XSS
- command injection
- unsafe deserialization
- insecure file upload
- path traversal
- sensitive data exposure
- CORS restrictions
- security headers
- secret management
- environment variables
- HTTPS/TLS production configuration
- secure error responses
- audit integrity

Never expose:

- secrets
- passwords
- API keys
- database credentials
- internal sensitive information

Never commit `.env` containing real credentials.

---

# PHASE 29 — FILE SECURITY

All uploaded files must be validated.

Validate:

- extension
- MIME type
- file size
- filename
- path
- user authorization
- object ownership

Prevent:

- executable uploads
- malicious extensions
- path traversal
- unauthorized downloads
- cross-vendor document access

---

# PHASE 30 — DATABASE & TRANSACTION INTEGRITY

Use PostgreSQL correctly.

Implement:

- foreign keys
- indexes
- uniqueness constraints
- check constraints where useful
- transaction boundaries
- atomic operations
- rollback handling
- migration integrity
- referential integrity

Critical workflows must be transactional.

Examples:

Approval + budget reservation

Award + PO creation where applicable

Invoice matching + exception creation

PO amendment + version preservation

Do not leave the database in a partially updated state.

---

# PHASE 31 — TESTING

Create automated tests.

## Unit Tests

Test:

- models
- validators
- calculations
- state transitions
- approval rules
- budget rules
- matching rules
- domain services

## API Tests

Test:

- authentication
- authorization
- positive cases
- negative cases
- validation
- filtering
- pagination
- file upload
- object-level access

## Integration Tests

Test complete workflows.

At minimum:

Vendor
→ KYC
→ Approval
→ Active

PR
→ Approval
→ Budget
→ Approved

PR
→ Sourcing
→ Vendor Bids
→ Evaluation
→ Award
→ PO

PO
→ Partial Receipt
→ Final Receipt

PO
→ Invoice
→ 3-Way Match
→ Exception
→ Resolution
→ Payment Ready

Contract
→ Milestone
→ Renewal Alert

## RBAC Tests

Test both:

- authorized access
- unauthorized access

Do not only test positive cases.

## Database Tests

Test:

- constraints
- transactions
- rollback
- migrations
- referential integrity
- duplicate prevention

## UI Tests

Validate:

- Chrome
- Edge
- Firefox
- desktop
- tablet
- mobile/responsive layouts

## Security Tests

Test:

- CSRF
- IDOR
- permission bypass
- unauthorized object access
- file upload security
- injection
- secret scanning

---

# PHASE 32 — PERFORMANCE & OBSERVABILITY

Review:

- N+1 queries
- unnecessary database queries
- report queries
- list pagination
- API response time
- large dataset behavior
- background job processing

Use:

- select_related
- prefetch_related
- appropriate indexes
- pagination
- efficient queries
- async processing where justified

Do not load huge datasets into memory unnecessarily.

Implement:

- structured logging
- request correlation
- error logging
- health checks
- Celery job monitoring/logging where appropriate

---

# PHASE 33 — CI/CD

Create GitHub Actions workflows for:

- dependency installation
- linting
- formatting checks
- tests
- migrations validation
- security checks
- build validation
- Docker build
- deployment validation where applicable

Do not allow secrets in source control.

---

# PHASE 34 — HPE DAY-90 ACCEPTANCE: VENDOR

The final system must demonstrate:

1. Vendor onboarding
2. KYC document submission
3. KYC review
4. Vendor approval
5. Complete audit trail

Database-backed evidence must be visible.

---

# PHASE 35 — HPE DAY-90 ACCEPTANCE: REQUISITION

Demonstrate:

1. Create PR above configured threshold.
2. Submit PR.
3. Multi-level approval routing.
4. Manager approval.
5. Budget validation.
6. Budget reservation.
7. Final approval.

Prove routing comes from backend policy logic.

---

# PHASE 36 — HPE DAY-90 ACCEPTANCE: SOURCING

Demonstrate:

1. Approved procurement demand.
2. RFQ/RFP creation.
3. At least three simulated vendor bids.
4. Vendor bid isolation.
5. Technical evaluation.
6. Commercial evaluation.
7. Award decision.
8. Award approval.

---

# PHASE 37 — HPE DAY-90 ACCEPTANCE: PO & RECEIPT

Demonstrate:

1. PO generation.
2. PO approval.
3. PO issuance.
4. Vendor acknowledgement.
5. PO amendment.
6. Historical version preservation.
7. Partial receipt.
8. Final receipt.
9. Inspection.
10. Rejection where applicable.

---

# PHASE 38 — HPE DAY-90 ACCEPTANCE: INVOICE & 3-WAY MATCH

Demonstrate:

1. Supplier invoice submission.
2. Invoice validation.
3. PO comparison.
4. Receipt comparison.
5. 3-way match.
6. Tolerance exception.
7. Exception record.
8. Exception resolution.
9. Payment-ready status.

The match must be real.

No fake status changes.

---

# PHASE 39 — HPE DAY-90 ACCEPTANCE: CONTRACT

Demonstrate:

1. Contract creation.
2. Legal review.
3. Business approval.
4. Contract activation.
5. Milestone.
6. Renewal/expiry date.
7. Celery scheduled notification.
8. Notification execution evidence.

---

# PHASE 40 — HPE DAY-90 ACCEPTANCE: DASHBOARDS & REPORTS

Demonstrate:

- all ten dashboards
- supplier performance
- spend dashboard/report
- PR aging
- sourcing participation
- PO status
- receipt/rejection
- invoice exception aging
- contract expiry/renewal
- audit export

All values must originate from real data.

---

# PHASE 41 — REQUIREMENT TRACEABILITY

Create a requirement traceability matrix.

For every requirement in this prompt, record:

- requirement
- module
- implementation location
- model/entity
- API
- UI
- test
- status

Do not claim a requirement is implemented without evidence.

---

# PHASE 42 — DASHBOARD TRACEABILITY

Create a dashboard traceability matrix.

For each of the ten dashboards document:

- role
- purpose
- widgets
- data source
- actions
- APIs
- permissions
- related modules
- tests

---

# PHASE 43 — MODULE TRACEABILITY

Create module traceability for all 16 modules:

1. Organization / Cost Center
2. Vendor Onboarding & KYC
3. Vendor Master & Risk
4. Purchase Requisition
5. Approval Policy Engine
6. RFQ/RFP Sourcing
7. Vendor Bid Portal
8. Evaluation & Award
9. Purchase Order
10. Receipt / Inspection
11. Invoice & 3-Way Match
12. Contract Lifecycle
13. Budget / Spend
14. Supplier Performance
15. Notifications / Escalation
16. Reports / Audit

For each module document:

- models
- services
- APIs
- UI
- permissions
- workflows
- state transitions
- audit
- tests
- dependencies

---

# PHASE 44 — FRESH DATABASE VALIDATION

Perform a clean installation from scratch.

Validate:

1. Clone repository.
2. Install dependencies.
3. Configure environment.
4. Start PostgreSQL.
5. Run migrations.
6. Create required initial configuration.
7. Start application.
8. Authenticate.
9. Execute major workflows.
10. Run test suite.

There must be no dependency on developer-local database state.

---

# PHASE 45 — FINAL DOCKER VALIDATION

Build the complete application using Docker.

Validate:

- application starts
- database starts
- migrations run
- authentication works
- RBAC works
- modules work
- APIs work
- Celery works
- Redis works
- scheduled jobs work
- static files work
- media works
- health checks work

The complete system must remain functional in the containerized environment.

---

# PHASE 46 — FINAL QUALITY GATE

Before declaring the project complete, verify all of the following.

## FUNCTIONAL

- all 16 modules implemented
- all 10 dashboards implemented
- workflows connected
- state machines implemented
- database persistence working
- approval engine working
- budget controls working
- sourcing working
- vendor portal working
- evaluation working
- PO working
- receipt working
- invoice working
- 3-way match working
- contract lifecycle working
- supplier performance working
- notifications working
- reports working
- audit working

## SECURITY

- backend RBAC
- object-level authorization
- CSRF
- secure sessions
- password security
- upload validation
- IDOR protection
- injection protection
- secret protection
- secure configuration
- audit integrity

## DATABASE

- migrations clean
- constraints correct
- indexes appropriate
- transactions correct
- rollback tested
- referential integrity verified

## API

- authentication
- authorization
- validation
- filtering
- pagination
- structured errors
- OpenAPI documentation

## FRONTEND

- Django Templates primary
- HTMX interactions
- responsive UI
- role-based navigation
- real data
- meaningful validation
- no fake controls
- no fake metrics

## INFRASTRUCTURE

- Docker
- PostgreSQL
- Redis
- Celery
- Celery Beat where needed
- Gunicorn
- Nginx where applicable
- environment variables
- health checks

## TESTING

- unit tests
- API tests
- integration tests
- RBAC tests
- database tests
- security tests
- UI/responsive validation
- regression tests
- performance checks

## DOCUMENTATION

Provide:

- README
- architecture documentation
- setup instructions
- environment configuration guide
- database/ERD documentation
- API/OpenAPI documentation
- user documentation
- admin documentation
- role/permission documentation
- workflow documentation
- deployment documentation
- Docker documentation
- testing documentation
- troubleshooting documentation
- knowledge-transfer documentation
- requirement traceability matrix

---

# 47. FORBIDDEN IMPLEMENTATION PATTERNS

NEVER:

- hardcode business workflow outcomes
- hardcode dashboard metrics
- create fake API responses
- create fake database records merely to populate dashboards
- create frontend-only RBAC
- trust hidden UI controls for security
- expose one vendor's bids to another vendor
- fake 3-way matching
- overwrite PO history
- overwrite contract history
- destroy receipt history
- silently permit budget overspending
- silently bypass approval
- skip audit logging for critical operations
- store secrets in Git
- commit real `.env` credentials
- create unnecessary React architecture
- rebuild the system in React
- introduce Docker in Phase 1
- introduce Redis/Celery before their designated phase
- mark incomplete modules as complete
- declare success without running tests
- use placeholder implementation for a required workflow
- create disconnected dashboards

---

# 48. STATE MACHINES — MANDATORY REFERENCE

## Vendor

DRAFT
→ SUBMITTED
→ KYC REVIEW
→ APPROVED / REJECTED
→ ACTIVE
→ ON HOLD / SUSPENDED

## Purchase Requisition

DRAFT
→ SUBMITTED
→ MANAGER REVIEW
→ BUDGET REVIEW
→ APPROVED / REJECTED
→ SOURCING / PO

## Sourcing Event

DRAFT
→ PUBLISHED
→ BID WINDOW
→ TECHNICAL REVIEW
→ COMMERCIAL REVIEW
→ AWARD APPROVAL
→ AWARDED / CANCELLED

## Purchase Order

DRAFT
→ APPROVAL
→ ISSUED
→ ACKNOWLEDGED
→ PARTIAL RECEIPT
→ COMPLETED / CANCELLED

## Invoice

RECEIVED
→ VALIDATION
→ 3-WAY MATCH
→ EXCEPTION / APPROVAL
→ READY FOR PAYMENT
→ PAID / REJECTED

## Contract

DRAFT
→ LEGAL REVIEW
→ BUSINESS APPROVAL
→ ACTIVE
→ RENEWAL DUE
→ RENEWED / EXPIRED / TERMINATED

Do not allow arbitrary invalid state transitions.

Every transition must be validated server-side and audited.

---

# 49. COMPLETE BUSINESS WORKFLOW

The final application must support this integrated scenario:

### STEP 1 — Organization

Admin creates:

- Organization
- Department
- Cost Center
- Users
- Roles

### STEP 2 — Vendor

Vendor registers.

Vendor uploads KYC documents.

Compliance reviews documents.

Vendor is approved.

Vendor becomes ACTIVE.

### STEP 3 — Purchase Request

Requester creates PR.

PR contains:

- department
- cost center
- items/services
- quantity
- estimated amount
- attachments

Requester submits PR.

### STEP 4 — Approval

Approval Policy Engine determines the required approval chain.

Approvers review.

Approval actions are persisted.

Budget is checked.

Budget is reserved.

PR becomes APPROVED.

### STEP 5 — Sourcing

Procurement creates RFQ/RFP.

Eligible vendors are invited.

Vendors access only their own event information.

At least three vendors can submit simulated bids.

### STEP 6 — Evaluation

Procurement evaluates technical criteria.

Commercial evaluation follows.

Clarifications/negotiation may be recorded.

Award decision is created.

Award approval occurs.

### STEP 7 — Purchase Order

PO is generated from the award.

PO is approved.

PO is issued.

Vendor acknowledges it.

PO amendment may occur.

Original PO version remains preserved.

### STEP 8 — Receipt

Stores receives part of the order.

Partial GoodsReceipt is created.

Inspection occurs.

Rejected goods are recorded where applicable.

Remaining quantity is received later.

PO becomes completed only according to actual receipt/business rules.

### STEP 9 — Invoice

Vendor submits invoice.

System validates invoice.

Duplicate invoice detection runs.

Invoice is matched against:

PO
+
Receipt
+
Invoice

### STEP 10 — Exception

If tolerance fails:

MatchException is created.

Finance reviews.

Exception is resolved/approved according to policy.

Invoice becomes READY FOR PAYMENT.

### STEP 11 — Contract

Contract is created.

Legal reviews.

Business approves.

Contract becomes ACTIVE.

Milestone and renewal dates are stored.

Celery schedules notification.

### STEP 12 — Performance

Supplier performance is calculated from actual transactions.

Spend is reflected in reporting.

### STEP 13 — Audit

The complete lifecycle is traceable:

Vendor
→ KYC
→ PR
→ Approval
→ Budget
→ Sourcing
→ Bids
→ Evaluation
→ Award
→ PO
→ Amendment
→ Receipt
→ Inspection
→ Invoice
→ Match
→ Exception
→ Payment Ready
→ Contract
→ Performance

---

# 50. FINAL DEVELOPMENT BEHAVIOR

Work phase-by-phase.

At the beginning of each phase:

1. Inspect the existing implementation.
2. Identify what previous phases already provide.
3. Do not duplicate existing functionality.
4. Identify dependencies.
5. Implement the phase.
6. Integrate it with previous phases.
7. Run migrations.
8. Run relevant tests.
9. Fix failures.
10. Update documentation.
11. Update traceability.
12. Only then continue.

If existing code conflicts with this specification:

- do not blindly delete it
- inspect it
- determine its purpose
- preserve valid work where possible
- refactor safely
- maintain backward consistency
- update tests

---

# 51. DEFINITION OF DONE

A feature is DONE only when:

- backend exists
- database persistence exists where applicable
- validation exists
- authorization exists
- UI exists
- API exists where applicable
- audit exists where required
- errors are handled
- tests exist
- integration works
- documentation is updated

A module is DONE only when:

- all required entities exist
- workflow exists
- state transitions exist
- permissions exist
- UI exists
- API exists
- audit exists
- edge cases are handled
- tests exist
- integration with dependent modules works

The project is DONE only when:

- all 16 modules are implemented
- all 10 dashboards are implemented
- all major cross-module workflows work
- Docker works
- Redis works
- Celery works
- APIs work
- security validation passes
- tests pass
- fresh database setup works
- HPE Day-90 acceptance scenarios can be demonstrated
- documentation is complete
- no Critical/High defects remain

---

# 52. FINAL OBJECTIVE

The final product must behave as one integrated enterprise procurement platform.

The architecture must demonstrate:

AUTHENTICATION
→ RBAC
→ ORGANIZATION
→ VENDOR
→ KYC
→ RISK
→ PR
→ APPROVAL
→ BUDGET
→ SOURCING
→ BIDS
→ EVALUATION
→ AWARD
→ PO
→ RECEIPT
→ INSPECTION
→ INVOICE
→ 3-WAY MATCH
→ EXCEPTION
→ PAYMENT READY
→ CONTRACT
→ NOTIFICATION
→ PERFORMANCE
→ SPEND
→ REPORTING
→ AUDIT

All data must be real.

All important workflows must be database-backed.

All sensitive actions must be backend-authorized.

All critical lifecycle actions must be auditable.

All dashboards must reflect actual system data.

The final system must be production-oriented, testable, secure, maintainable, containerized and demonstrable as a complete enterprise procurement ERP.

## START NOW

Begin with:

### PHASE 1 — AUTHENTICATION & SESSION SECURITY

Do not begin with Docker.

Do not begin with Redis/Celery.

Do not skip ahead to dashboards.

Build Phase 1 completely, validate it, and then proceed sequentially through the phases above.