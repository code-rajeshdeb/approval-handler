# Approval Handler — Architecture

> **Version:** v2.4.1  
> **Last updated:** 2026-04-03

---

## 1. Overview

Approval Handler is a **Slack-integrated access management system** that automates GCP IAM permission requests and Vault secret operations. Users submit requests via the Slack slash command `/access-request`, approvals happen in-channel and via direct messages to approvers, and approved workflows execute automatically via **Argo Workflows** on GKE.

### Key Capabilities

- **Slack-native UX** — modal forms, interactive approval buttons, DM notifications
- **Two-level approval** — team lead → final approver flow with configurable rules
- **Rule engine** — priority-based policy matching with wildcard and multi-value conditions
- **Auto-approve** — low-risk requests skip human approval entirely
- **Multi-channel support** — requests post to the channel where `/access-request` was invoked
- **Approver DMs** — each approver receives a personal DM with Approve/Reject buttons, all synced
- **Admin UI** — full CRUD for rules, groups, teams, and IAM role catalog
- **Vault secret management** — request creation/update of OpenBao/Vault secrets through the same approval flow

---

## 2. Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.12+ / FastAPI (single `main.py` ~3087 lines) |
| Database | PostgreSQL via CloudNativePG (on GKE) |
| Slack Integration | Slack Bolt-style (manual HMAC signature verification + `WebClient` from `slack_sdk`) |
| Workflow Engine | Argo Workflows (K8s-native, submitted via K8s API) |
| Container Runtime | GKE (Google Kubernetes Engine) |
| Container Registry | Artifact Registry (`REGION-docker.pkg.dev/YOUR_PROJECT/REPO`) |
| CI/CD | Argo Workflows (`docker-build-push` WorkflowTemplate) |
| GitOps | ArgoCD auto-sync from `git@github.com:your-org/your-gitops-repo.git` path `approval-handler/k8s` |
| Admin UI | Server-side rendered Jinja2 templates + Tailwind CSS + Alpine.js + HTMX |
| Auth | HMAC-signed session cookies for admin UI; Slack signature verification for bot endpoints |
| Secrets | Kubernetes Secrets (via ExternalSecret) mounted as env vars |

### Python Dependencies (`requirements.txt`)

| Package | Version | Purpose |
|---------|---------|---------|
| `fastapi` | 0.115.0 | Web framework |
| `uvicorn[standard]` | 0.30.0 | ASGI server |
| `httpx` | 0.27.0 | Async HTTP client (K8s API calls) |
| `psycopg2-binary` | 2.9.9 | PostgreSQL driver |
| `python-multipart` | 0.0.9 | Form data parsing |
| `jinja2` | 3.1.4 | HTML templating |
| `slack-sdk` | 3.31.0 | Slack API client |
| `pydantic` | 2.9.0 | Data validation |
| `pydantic-settings` | 2.5.0 | Settings from env vars |
| `itsdangerous` | 2.2.0 | Session token signing |

---

## 3. Repository Structure

```
approval-handler/
├── main.py                    # Monolithic application (~3087 lines)
├── Dockerfile                 # Python 3.12-slim build
├── requirements.txt           # Python dependencies (10 packages)
├── qa-tests.sh               # Automated QA test suite
├── QA-CHECKLIST.md           # Manual QA checklist
├── ARCHITECTURE.md           # This file
├── k8s/
│   └── deployment.yaml       # K8s Deployment + Service + ServiceAccount
└── templates/                # Jinja2 HTML templates
    ├── base.html             # Layout with nav, force-modal JS
    ├── login.html            # Admin login page
    ├── dashboard.html        # Admin dashboard with stats + recent requests
    ├── history.html          # Request history with status filters
    ├── request_detail.html   # Full request detail with pipeline visualization
    ├── _detail_fragment.html # HTMX fragment for inline request expansion
    ├── rules.html            # Approval rules list
    ├── rule_edit.html        # Rule create/edit form
    ├── groups.html           # Approver groups management
    ├── teams.html            # Team → approver group mapping
    └── catalog.html          # IAM role catalog management
```

---

## 4. Component Breakdown (with line references in `main.py`)

### A. Configuration & Settings (lines 1–83)

- **Imports** (lines 14–48): `asyncio`, `hashlib`, `hmac`, `json`, `logging`, `os`, `re`, `time`, `httpx`, `psycopg2`, `fastapi`, `slack_sdk`, `pydantic`, `itsdangerous`
- `class Settings(BaseSettings)` (line 58): Pydantic settings with automatic env var loading
- Key settings: `db_host`, `db_port`, `db_name`, `db_user`, `db_password`, `slack_bot_token`, `slack_signing_secret`, `slack_channel`, `admin_username`, `admin_password`, `secret_key`, `force_approve_password`, `argo_namespace`, `argo_workflow_template`, `argo_ui_url`

### B. IAM Role Catalog (lines 85–328)

- `GCP_ROLE_CATALOG` dict (line 98): Hardcoded fallback catalog with **14 service categories** and their curated IAM roles
- `_load_role_catalog_from_db()` (line 256): DB-first catalog loading — queries `service_categories` and `iam_roles` tables, falls back to `GCP_ROLE_CATALOG` dict
- `_get_role_display()` (line 282): Resolves role value → human display name
- `_get_service_label()` (line 292): Resolves service category key → display label
- `_get_role_description()` (line 299): Looks up role description from catalog
- `_get_team_approvers()` (line 309): Resolves team name → `(lead_group_name, [approver_display_names])`

### C. Database Layer (lines 331–386)

- `_db_pool` global: `psycopg2.pool.ThreadedConnectionPool` (minconn=2, maxconn=10)
- `get_pool()` (line 338): Lazy-init connection pool
- `get_db()` (line 353): Context manager with auto-commit/rollback
- `db_fetchall()` (line 368): Execute query, return list of `RealDictRow`
- `db_fetchone()` (line 375): Execute query, return single `RealDictRow`
- `db_execute()` (line 382): Execute DML, return `rowcount`

### D. Rule Engine (lines 389–451)

- `evaluate_rules()` (line 394): Fetches all enabled rules ordered by `priority ASC`, returns first match
- `_rule_matches()` (line 408): Multi-field matching supporting:
  - Exact match
  - Comma-separated OR match (`"viewer,editor"`)
  - Wildcard prefix match (`"roles/bigquery.*"`)
  - NULL fields match anything
  - Fields checked: `requester_email`, `who_type`, `resource_type`, `permission_level`, `gcp_project`, `action`, `access_type`
- `determine_risk_level()` (line 440): Computes risk (`low`/`medium`/`high`) based on rule and project/permission heuristics

### E. Slack Integration (lines 453–1226)

- `get_slack_client()` (line 460): Singleton `WebClient` factory
- `verify_slack_signature()` (line 467): HMAC-SHA256 signature verification (v0 scheme, 300s replay window)
- `resolve_slack_user_id()` (line 489): Email → Slack user ID lookup via `users.lookupByEmail`
- `_build_who_block()` (line 498): Dynamic "who" section — renders user picker (proxy), read-only display (self), group input, or service account input based on `who_type`
- `_is_proxy_requester()` (line 566): Checks if email belongs to `proxy-requesters` group
- `_build_modal_blocks()` (line 577): ~370-line function building all modal blocks dynamically based on selections:
  - Team selector with approver preview (lines 588–773)
  - Request type selector: User / Group / Service Account / Vault Secret (lines 608–627)
  - Vault Secret mode: operation, path, key, value fields (lines 630–735)
  - IAM mode: service category dropdown, IAM role dropdown with descriptions, resource name, GCP project, access type, expiry (lines 737–947)
- `build_access_request_modal()` (line 950): Constructs Slack modal with `source_channel` + `requester_email` + `is_proxy` in `private_metadata`
- `build_approval_message()` (line 967): Builds approval Slack message blocks (~155 lines) — handles both IAM and Vault request types with role-aware display
- **NEW in v2.4.1**: `_send_approver_dms()` (line 1130): Opens DM with each approver group member, posts approval message with Approve/Reject buttons, stores references in `approval_dm_messages` DB table
- **NEW in v2.4.1**: `_update_all_approval_messages()` (line 1178): Syncs channel message + all DM messages when any action is taken (approve/reject from any location)

### F. Argo Workflow Client (lines 1229–1279)

- `K8S_API_BASE` (line 1233): `https://kubernetes.default.svc`
- `_get_k8s_token()` (line 1238): Reads service account token from `/var/run/secrets/kubernetes.io/serviceaccount/token`
- `_k8s_headers()` (line 1245): Builds auth headers with Bearer token
- `submit_workflow()` (line 1253): Creates Workflow CR via K8s API (`POST /apis/argoproj.io/v1alpha1/namespaces/{ns}/workflows`), using `workflowTemplateRef` to reference named templates

### G. Auth & Session (lines 1282–1381)

- `get_serializer()` (line 1289): `itsdangerous.URLSafeTimedSerializer` for session tokens
- `create_session_token()` (line 1296): Generates signed session token
- `verify_session_token()` (line 1300): Validates token with 24h max age
- `require_admin()` (line 1308): FastAPI dependency — checks session cookie first, falls back to HTTP Basic Auth
- **Pydantic Models** (lines 1339–1381): `RuleCreate`, `RuleUpdate`, `GroupCreate`, `MemberAdd`, `EvaluateRequest`

### H. Application Lifecycle (lines 1384–1491)

- `lifespan()` (line 1390): Async context manager — startup: inits DB pool + starts Slack user sync loop; shutdown: cancels sync + closes pool
- `_slack_user_sync_loop()` (line 1411): Background task syncing Slack users every 4 hours
- `_sync_slack_users()` (line 1421): Paginates `users.list` API (200 per page), filters `@example.com` emails, upserts into `slack_users` table
- FastAPI app initialization (line 1466): `FastAPI(title="Approval Handler", version="1.0.0", lifespan=lifespan)`
- `health()` (line 1480): Basic health check
- `readiness()` (line 1486): Readiness probe — verifies DB connectivity

### I. Slack Command & Interaction Handlers (lines 1494–1887)

- `slack_commands()` (line 1500): Handles `/access-request` — resolves requester email, checks proxy status, captures `channel_id` as `source_channel`, opens modal
- `slack_interactions()` (line 1543): Routes interactive payloads:
  - `block_actions` with modal action IDs (`who_type`, `service_category`, `access_type`, `team`, `iam_role`, `vault_operation`) → rebuild modal with `views.update`
  - `view_submission` → `_handle_modal_submission()`
  - `block_actions` with button clicks → `_handle_button_action()`
- `_handle_modal_submission()` (line 1651): ~236-line function processing form submission:
  - Extracts all form values from modal state
  - Handles Vault Secret mode (lines 1692–1715) and IAM mode (lines 1716–1753)
  - Enforces self-request for non-proxy users (line 1721)
  - Evaluates rules → determines risk → generates request ID
  - Resolves team-based lead group (lines 1776–1792)
  - Inserts into `access_requests` (lines 1796–1821)
  - Auto-approve path: submit workflow immediately + poll status (lines 1823–1856)
  - Approval path: post approval message to `source_channel` + send approver DMs (lines 1857–1876)

### J. Workflow Execution & Status (lines 1890–2076)

- `_submit_workflow_for_request()` (line 1890): Maps request data → workflow parameters:
  - Vault requests → `openbao-manage-secrets` workflow template
  - IAM requests → `manage-iam-permissions` workflow template
- `_poll_workflow_status()` (line 1920): Async polling loop (20s intervals, 15 polls = 5min timeout):
  - GETs workflow status from K8s API
  - On `Succeeded`/`Failed`/`Error`: updates DB + posts thread reply
  - On timeout: posts warning message
- `_post_workflow_result_thread()` (line 1995): Posts thread reply with workflow outcome — includes role-aware success details or error messages with Argo UI link

### K. Button Action Handler (lines 2079–2313)

- `_handle_button_action()` (line 2079): Handles Approve/Reject buttons from channel or DM:
  - **Authorization check** (lines 2109–2131): Verifies clicker is member of required approver group, sends ephemeral error if not
  - **L1 Approve** (lines 2143–2203): Updates DB (`lead_approved_by`, `lead_approved_at`), updates all L1 messages (channel + DMs), posts L2 approval message + sends L2 DMs
  - **Final Approve** (lines 2204–2273): Atomic check-and-update to prevent race conditions, submits workflow, updates all messages, spawns background poller
  - **Reject** (lines 2275–2311): Atomic update to `rejected`, updates all messages across both approval levels

### L. Admin REST API (lines 2316–2486)

- **Rules CRUD** (lines 2321–2400): `GET/POST/PUT/DELETE /api/rules`, rule evaluation endpoint
- **Groups CRUD** (lines 2403–2462): `GET/POST/DELETE /api/groups`, member add/remove
- **Requests listing** (lines 2465–2486): `GET /api/requests` with optional status filter and pagination

### M. Admin UI Routes (lines 2489–2862)

- **Login/Logout** (lines 2494–2519): Session cookie-based authentication
- **Dashboard** (lines 2522–2537): Stats (rules, groups, pending/total requests) + recent requests
- **Rules management** (lines 2540–2641): List, create, edit, save, delete
- **Force-approve and rerun** (lines 2644–2754): Password-protected admin actions with Slack notification
- **Groups management** (lines 2757–2816): CRUD for approver groups and members
- **History** (lines 2819–2862): Request history with status filters, detail fragment with pipeline visualization

### N. Slack User & Team APIs (lines 2865–2953)

- `GET /api/teams` (line 2870): List all teams
- `GET /api/slack-users` (line 2876): Search cached Slack users (for adding group members)
- `POST /api/slack-users/sync` (line 2900): Manually trigger Slack user sync
- **Teams Admin UI** (lines 2907–2953): Teams page, add/edit/delete teams with approver group assignment

### O. Catalog Management (lines 2956–3083)

- `GET /admin/catalog` (line 2960): Admin page for managing IAM Role Catalog
- **Category CRUD** (lines 2984–3032): Add, toggle, update, delete service categories
- **Role CRUD** (lines 3035–3083): Add, toggle, update, delete IAM roles within categories

---

## 5. Database Schema

### `access_requests` — Main request tracking

| Column | Type | Description |
|--------|------|-------------|
| `request_id` | `VARCHAR` PK | Unique ID (`ar-{timestamp}-{user_prefix}`) |
| `request_type` | `VARCHAR` | `iam-permission` or `vault` |
| `requester_email` | `VARCHAR` | Email of the person who submitted |
| `action` | `VARCHAR` | `grant` for IAM; vault operation for vault requests |
| `who_type` | `VARCHAR` | `user`, `group`, `serviceAccount`, `vault-secret` |
| `who_email` | `VARCHAR` | Target user/group/SA email |
| `team` | `VARCHAR` | Selected approval team |
| `resource_type` | `VARCHAR` | Service category key (e.g., `bigquery`, `gcs-bucket`) |
| `resource_name` | `VARCHAR` | Specific resource or `global` |
| `permission_level` | `VARCHAR` | Always `custom` (legacy field) |
| `custom_role` | `VARCHAR` | Actual GCP IAM role value (e.g., `roles/bigquery.dataViewer`) |
| `gcp_project` | `VARCHAR` | Target GCP project ID |
| `access_type` | `VARCHAR` | `temporary` or `permanent` |
| `expiry_hours` | `INTEGER` | Hours until auto-revocation (temporary only) |
| `reason` | `TEXT` | Free-text reason |
| `ticket` | `VARCHAR` | Optional ticket ID |
| `status` | `VARCHAR` | `pending`, `approved`, `auto-approved`, `rejected`, `executed`, `failed` |
| `risk_level` | `VARCHAR` | `low`, `medium`, `high` |
| `matched_rule_id` | `INTEGER` | FK to `approval_rules.id` |
| `matched_rule_name` | `VARCHAR` | Denormalized rule name |
| `lead_approval_required` | `BOOLEAN` | Whether L1 approval is needed |
| `lead_approver_group` | `VARCHAR` | L1 approver group name |
| `lead_approved_by` | `VARCHAR` | Username of L1 approver |
| `lead_approved_at` | `TIMESTAMP` | When L1 approved |
| `final_approval_required` | `BOOLEAN` | Whether final approval is needed |
| `final_approved_by` | `VARCHAR` | Username of final approver |
| `final_approved_at` | `TIMESTAMP` | When finally approved |
| `slack_channel` | `VARCHAR` | Channel where approval message was posted |
| `slack_ts` | `VARCHAR` | Slack message timestamp (for updates) |
| `workflow_name` | `VARCHAR` | Argo Workflow CR name |
| `error_message` | `TEXT` | Error message if workflow failed |
| `executed_at` | `TIMESTAMP` | When workflow completed |
| `vault_operation` | `VARCHAR` | Vault operation type |
| `vault_path` | `VARCHAR` | Vault secret path |
| `vault_key` | `VARCHAR` | Vault secret key |
| `vault_value` | `TEXT` | Vault secret value (stored temporarily) |
| `created_at` | `TIMESTAMP` | Request creation time |
| `updated_at` | `TIMESTAMP` | Last update time |

### `approval_rules` — Rule engine configuration

| Column | Type | Description |
|--------|------|-------------|
| `id` | `SERIAL` PK | Auto-increment ID |
| `name` | `VARCHAR` | Human-readable rule name |
| `priority` | `INTEGER` | Lower = higher priority (first match wins) |
| `enabled` | `BOOLEAN` | Whether rule is active |
| `requester_email` | `VARCHAR` | Match condition (NULL = any) |
| `who_type` | `VARCHAR` | Match condition (NULL = any) |
| `resource_type` | `VARCHAR` | Match condition (NULL = any) |
| `permission_level` | `VARCHAR` | Match condition (NULL = any) |
| `gcp_project` | `VARCHAR` | Match condition — supports comma-separated and wildcard |
| `action` | `VARCHAR` | Match condition (NULL = any) |
| `access_type` | `VARCHAR` | Match condition (NULL = any) |
| `approvers_required` | `INTEGER` | 0 = auto-approve, 1+ = require approval |
| `approval_levels` | `INTEGER` | 1 = single level, 2 = two-level |
| `lead_group` | `VARCHAR` | L1 approver group (or `__selected_team__` for team-based) |
| `second_approval_group` | `VARCHAR` | L2 approver group |
| `require_reason` | `BOOLEAN` | Whether reason field is required |
| `require_ticket` | `BOOLEAN` | Whether ticket field is required |
| `ticket_pattern` | `VARCHAR` | Expected ticket prefix |
| `description` | `TEXT` | Rule description |
| `created_by` | `VARCHAR` | Admin who created the rule |
| `created_at` | `TIMESTAMP` | Creation time |
| `updated_at` | `TIMESTAMP` | Last update time |

### `approver_groups` — Named groups of approvers

| Column | Type | Description |
|--------|------|-------------|
| `id` | `SERIAL` PK | Auto-increment ID |
| `group_name` | `VARCHAR` UNIQUE | Group identifier (e.g., `devops-leads`) |
| `description` | `TEXT` | Group description |
| `created_at` | `TIMESTAMP` | Creation time |

### `approver_group_members` — Members of approver groups

| Column | Type | Description |
|--------|------|-------------|
| `id` | `SERIAL` PK | Auto-increment ID |
| `group_id` | `INTEGER` FK | References `approver_groups.id` (CASCADE) |
| `email` | `VARCHAR` | Member email |
| `display_name` | `VARCHAR` | Display name |
| `slack_user_id` | `VARCHAR` | Resolved Slack user ID |
| UNIQUE | | `(group_id, email)` |

### `teams` — Team definitions with approver group assignments

| Column | Type | Description |
|--------|------|-------------|
| `id` | `SERIAL` PK | Auto-increment ID |
| `team_name` | `VARCHAR` UNIQUE | Team name (shown in modal dropdown) |
| `lead_group` | `VARCHAR` | References `approver_groups.group_name` |
| `description` | `TEXT` | Team description |
| `created_at` | `TIMESTAMP` | Creation time |
| `updated_at` | `TIMESTAMP` | Last update time |

### `slack_users` — Cached Slack user directory

| Column | Type | Description |
|--------|------|-------------|
| `slack_user_id` | `VARCHAR` PK | Slack user ID |
| `email` | `VARCHAR` | User email (`@example.com` only) |
| `display_name` | `VARCHAR` | Slack display name |
| `real_name` | `VARCHAR` | Real name |
| `avatar_url` | `VARCHAR` | Profile image URL |
| `is_active` | `BOOLEAN` | Whether user is active |
| `synced_at` | `TIMESTAMP` | Last sync time |

### `service_categories` — IAM service categories (DB-managed catalog)

| Column | Type | Description |
|--------|------|-------------|
| `category_key` | `VARCHAR` PK | Slug key (e.g., `bigquery`) |
| `label` | `VARCHAR` | Display name |
| `resource_level` | `BOOLEAN` | Whether resource-level scoping is supported |
| `resource_placeholder` | `VARCHAR` | Placeholder text for resource name field |
| `enabled` | `BOOLEAN` | Whether category is active |
| `sort_order` | `INTEGER` | Display order |
| `updated_at` | `TIMESTAMP` | Last update time |

### `iam_roles` — Individual roles within categories

| Column | Type | Description |
|--------|------|-------------|
| `id` | `SERIAL` PK | Auto-increment ID |
| `category_key` | `VARCHAR` FK | References `service_categories.category_key` |
| `display_name` | `VARCHAR` | Human display name |
| `role_value` | `VARCHAR` | GCP IAM role value (e.g., `roles/bigquery.dataViewer`) |
| `description` | `TEXT` | Role description |
| `enabled` | `BOOLEAN` | Whether role is active |
| `sort_order` | `INTEGER` | Display order |
| UNIQUE | | `(category_key, role_value)` |

### `approval_dm_messages` — Tracks DM messages sent to approvers *(NEW in v2.4.1)*

| Column | Type | Description |
|--------|------|-------------|
| `id` | `SERIAL` PK | Auto-increment ID |
| `request_id` | `VARCHAR` FK | References `access_requests.request_id` |
| `approver_email` | `VARCHAR` | Approver email |
| `approver_slack_id` | `VARCHAR` | Approver Slack user ID |
| `dm_channel` | `VARCHAR` | DM channel ID |
| `dm_ts` | `VARCHAR` | DM message timestamp |
| `approval_level` | `INTEGER` | 1 (L1) or 2 (L2) |

---

## 6. Flow Diagrams

### A. Access Request Submission Flow

```
User types /access-request in Slack channel
    │
    ▼
slack_commands() [line 1500] captures channel_id as source_channel
    │
    ▼
Resolves requester email + checks proxy-requesters group
    │
    ▼
build_access_request_modal() [line 950] opens modal
    │  source_channel stored in private_metadata
    ▼
User fills form (team, who, service, role, project, etc.)
    │  Modal rebuilds on selection changes via views.update [line 1565]
    ▼
_handle_modal_submission() [line 1651] processes form
    │
    ├─► evaluate_rules() [line 394] → match found?
    │       │
    │       ├─ Yes: Apply rule (approval groups, risk, ticket/reason requirements)
    │       └─ No:  Return error to modal
    │
    ├─► determine_risk_level() [line 440]
    │
    ├─► Resolve team-based lead group (if rule uses __selected_team__)
    │
    ├─► INSERT into access_requests [line 1796]
    │
    ├─► Auto-approve? (rule says approvers_required = 0)
    │       │
    │       ├─ Yes: _submit_workflow_for_request() [line 1890]
    │       │       Post auto-approve text to source_channel
    │       │       └─► _poll_workflow_status() [line 1920]
    │       │
    │       └─ No:  build_approval_message() [line 967]
    │               Post to source_channel
    │               Store slack_channel + slack_ts in DB
    │               │
    │               └─► _send_approver_dms() [line 1130]
    │                   DM each approver with Approve/Reject buttons
    │
    └─► Return { "response_action": "clear" } to Slack
```

### B. Approval Flow (Two-Level)

```
Approver clicks Approve/Reject button (in channel OR DM)
    │
    ▼
_handle_button_action() [line 2079]
    │
    ├─► Resolve clicker's email from Slack profile
    │
    ├─► Authorization check [line 2110]: Is clicker in approver group?
    │       │
    │       ├─ No:  chat_postEphemeral() → "❌ You are not authorized..."
    │       └─ Yes: Continue
    │
    ├─► APPROVE (action_id = "approve_request"):
    │   │
    │   ├─► L1 Approve (level=1, total_levels=2):
    │   │       ├─ UPDATE DB: lead_approved_by, lead_approved_at
    │   │       ├─ chat_update() on triggering message (remove buttons, show ✅)
    │   │       ├─ _update_all_approval_messages() [line 1178] → sync channel + all L1 DMs
    │   │       ├─ build_approval_message() for L2 → post to source_channel
    │   │       └─ _send_approver_dms() for L2 approvers
    │   │
    │   └─► Final Approve (L2 or single-level):
    │           ├─ Atomic UPDATE: status='approved' WHERE status='pending' (race prevention)
    │           ├─ _submit_workflow_for_request() [line 1890]
    │           ├─ Store workflow_name in DB
    │           ├─ chat_update() on triggering message
    │           ├─ _update_all_approval_messages() → sync all messages for current level
    │           └─ _poll_workflow_status() [line 1920] → thread reply with result
    │
    └─► REJECT (action_id = "reject_request"):
            ├─ Atomic UPDATE: status='rejected' WHERE status='pending'
            ├─ chat_update() on triggering message
            └─ _update_all_approval_messages() for both L1 and L2 → mark all as ❌ rejected
```

### C. Workflow Execution Flow

```
_submit_workflow_for_request() [line 1890]
    │
    ├─► Request type = "vault"?
    │   ├─ Yes: Map to openbao-manage-secrets template
    │   │       params: operation, path, key, value
    │   └─ No:  Map to manage-iam-permissions template
    │           params: action, who-type, who-email, resource-type,
    │           resource-name, permission-level, custom-role, gcp-project,
    │           access-type, expiry-hours, reason, ticket
    │
    ├─► submit_workflow() [line 1253] → POST to K8s API
    │   Creates Workflow CR in argo-access-control namespace
    │   Uses workflowTemplateRef (not inline spec)
    │
    └─► _poll_workflow_status() [line 1920] (async background task)
            │
            ├─► Poll every 20s for up to 5 minutes (15 polls)
            │   GET /apis/argoproj.io/v1alpha1/namespaces/{ns}/workflows/{name}
            │
            ├─► Phase = "Succeeded":
            │   ├─ UPDATE DB: status='executed', executed_at=NOW()
            │   └─ _post_workflow_result_thread() [line 1995]
            │       Thread reply: ✅ Access Granted (role-aware details)
            │
            ├─► Phase = "Failed" / "Error":
            │   ├─ UPDATE DB: status='failed', error_message=...
            │   └─ _post_workflow_result_thread()
            │       Thread reply: ❌ Workflow Failed (error details + Argo UI link)
            │
            └─► Timeout (5 min):
                └─ _post_workflow_result_thread()
                    Thread reply: ⏱️ Workflow Status Unknown
```

### D. DM Notification Sync Flow (NEW in v2.4.1)

```
Action taken on ANY message (channel message OR approver DM)
    │
    ▼
_update_all_approval_messages() [line 1178]
    │
    │  (request_id, approval_level, updated_blocks, updated_text,
    │   exclude_channel, exclude_ts)
    │
    ├─► 1. Update channel message (chat.update)
    │   Query access_requests for slack_channel + slack_ts
    │   Skip if this IS the triggering message (exclude_channel/exclude_ts)
    │
    └─► 2. Update all DM messages
        Query approval_dm_messages WHERE request_id AND approval_level
        │
        └─► For each DM row:
            Skip if this IS the triggering message
            └─ chat.update(channel=dm_channel, ts=dm_ts, blocks=updated_blocks)
```

---

## 7. Deployment Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                        GKE Cluster                           │
│                                                              │
│  ┌─────────────────────────┐   ┌──────────────────────────┐ │
│  │  argo-access-control    │   │  argo-access-control     │ │
│  │  namespace              │   │  namespace (workflows)   │ │
│  │                         │   │                          │ │
│  │  ┌───────────────────┐  │   │  ┌────────────────────┐  │ │
│  │  │ approval-handler  │  │   │  │ Argo Workflow      │  │ │
│  │  │ Deployment        │  │   │  │ Controller         │  │ │
│  │  │ (FastAPI/uvicorn) │──┼───┼─►│                    │  │ │
│  │  │ Port 8080         │  │   │  └────────────────────┘  │ │
│  │  │ Replicas: 1       │  │   │         │                │ │
│  │  └────────┬──────────┘  │   │         ▼                │ │
│  │           │             │   │  ┌────────────────────┐  │ │
│  │  ┌────────▼──────────┐  │   │  │ manage-iam-        │  │ │
│  │  │ CloudNativePG     │  │   │  │ permissions &      │  │ │
│  │  │ PostgreSQL        │  │   │  │ openbao-manage-    │  │ │
│  │  │ (access-control-  │  │   │  │ secrets            │  │ │
│  │  │  db-rw Service)   │  │   │  │ workflow pods      │  │ │
│  │  └───────────────────┘  │   │  └────────────────────┘  │ │
│  └─────────────────────────┘   └──────────────────────────┘ │
│                                                              │
│  ┌──────────────────────────────────────────────────────────┐│
│  │                  ArgoCD (GitOps)                         ││
│  │  Syncs from: infra-devops/approval-handler/k8s          ││
│  │  Auto-sync enabled                                      ││
│  └──────────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────┘
          │                              │
          ▼                              ▼
   ┌─────────────┐              ┌──────────────┐
   │  Slack API  │              │  GCP IAM API │
   │  (Bot)      │              │  (via gcloud)│
   └─────────────┘              └──────────────┘
```

### Container Image

- **Registry:** `REGION-docker.pkg.dev/YOUR_PROJECT/REPO/approval-handler`
- **Current tag:** `v2.4.1`
- **Base image:** `python:3.12-slim`
- **Runs as:** non-root user `appuser` (UID 1001)
- **Entrypoint:** `uvicorn main:app --host 0.0.0.0 --port 8080`

### K8s Resources (from `deployment.yaml`)

- **Namespace:** `argo-access-control`
- **ServiceAccount:** `approval-handler` (with `automountServiceAccountToken: true` for K8s API access)
- **Security:** `runAsNonRoot`, `readOnlyRootFilesystem`, all capabilities dropped
- **Resources:** 100m–500m CPU, 128Mi–512Mi memory
- **Probes:**
  - Readiness: `GET /health/ready` (checks DB connectivity)
  - Liveness: `GET /health`

---

## 8. Key Environment Variables

| Variable | Source | Description |
|----------|--------|-------------|
| `SLACK_BOT_TOKEN` | Secret `approval-handler-slack-app` → `bot-token` | Slack Bot OAuth token |
| `SLACK_SIGNING_SECRET` | Secret `approval-handler-slack-app` → `signing-secret` | Slack request verification |
| `SLACK_CHANNEL` | ConfigMap value `#devops-access` | Default channel (fallback) |
| `DB_HOST` | ConfigMap value | PostgreSQL host (`access-control-db-rw.argo-access-control.svc.cluster.local`) |
| `DB_PORT` | ConfigMap value `5432` | PostgreSQL port |
| `DB_NAME` | ConfigMap value `access_control` | Database name |
| `DB_USER` | Secret `access-control-db-credentials` → `username` | Database username |
| `DB_PASSWORD` | Secret `access-control-db-credentials` → `password` | Database password |
| `ADMIN_USERNAME` | Secret `approval-handler-admin-auth` → `username` | Admin UI username |
| `ADMIN_PASSWORD` | Secret `approval-handler-admin-auth` → `password` | Admin UI password |
| `SECRET_KEY` | Settings default `change-me-in-production` | Session token signing key |
| `FORCE_APPROVE_PASSWORD` | Secret `approval-handler-admin-auth` → `force-password` | Password for force-approve/rerun |
| `ARGO_NAMESPACE` | ConfigMap value `argo-access-control` | Namespace for Argo Workflow CRs |
| `ARGO_UI_URL` | ConfigMap value | Argo Workflows UI URL (`https://workflow.example.com`) |
| `ARGO_WORKFLOW_TEMPLATE` | Settings default `manage-iam-permissions` | Default WorkflowTemplate name |

---

## 9. Supported Service Categories

The IAM role catalog ships with **14 service categories** (hardcoded fallback in `GCP_ROLE_CATALOG`, DB-managed via `service_categories` + `iam_roles` tables):

| Key | Label | Resource-Level | # Roles |
|-----|-------|---------------|---------|
| `basic` | Basic | No | 4 |
| `bigquery` | BigQuery | Yes | 6 |
| `gcs-bucket` | Cloud Storage | Yes | 5 |
| `compute-vm` | Compute Engine | Yes | 7 |
| `gke-cluster` | Kubernetes Engine | No | 5 |
| `cloud-sql` | Cloud SQL | Yes | 5 |
| `service-account` | IAM / Service Accounts | Yes | 6 |
| `pubsub` | Pub/Sub | Yes | 5 |
| `secret-manager` | Secret Manager | Yes | 4 |
| `artifact-registry` | Artifact Registry | Yes | 4 |
| `cloud-logging` | Cloud Logging | No | 4 |
| `cloud-monitoring` | Cloud Monitoring | No | 4 |
| `cloud-composer` | Cloud Composer | No | 3 |
| `custom` | Custom IAM Role | Yes | 0 (free-text) |

---

## 10. Version History

| Version | Key Changes |
|---------|-------------|
| **v2.4.1** | Multi-channel support (requests post to `source_channel`); Approver DM notifications with cross-sync (`approval_dm_messages` table); Vault secret management (OpenBao integration) |
| **v2.4.0** | Vault Secret request type; `vault_operation` modal dispatch; Team selector with approver preview; Optional reason/ticket fields |
| **v2.3.x** | Admin UI (Jinja2 + Tailwind + HTMX); Rule engine with priority-based matching; Teams management; IAM role catalog management (DB-driven with fallback) |
| **v2.2.x** | Workflow status polling with thread replies; Role-aware display in approval messages and success notifications |
| **v2.1.x** | Rerun and force-approve with password protection; Admin catalog CRUD; Proxy requester support (request on behalf of others) |
| **v2.0.x** | Two-level approval flow; Risk assessment; Service category → IAM role catalog; Resource-level scoping |
| **v1.x** | Initial Slack bot + Argo Workflow integration; Single-level approval; Basic IAM permission grant/revoke |
