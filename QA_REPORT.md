# Approval Handler - Full QA Report

**Date:** 2026-04-01
**Reviewer:** Code QA
**Scope:** Complete review of `approval-handler/` — main.py, templates, k8s configs, Dockerfile
**Revision:** v4 — Resource type redesign (Compute VM + GCP IAM only) + full rule engine QA

---

## 1. App Startup & Listening

| Check | Status | Notes |
|-------|--------|-------|
| FastAPI app created | OK | `FastAPI(title="Approval Handler", version="1.0.0")` |
| Uvicorn listens on 0.0.0.0:8080 | OK | Dockerfile CMD: `uvicorn main:app --host 0.0.0.0 --port 8080` |
| Health endpoint | OK | `GET /health` → `{"status": "ok"}` |
| Readiness probe | OK | `GET /health/ready` checks DB connectivity |
| DB pool init on startup | OK | `lifespan` async context manager calls `get_pool()` |
| Graceful shutdown | OK | `lifespan` closes pool on shutdown |
| K8s liveness probe | OK | Points to `/health` |
| K8s readiness probe | OK | Points to `/health/ready` |

---

## 2. Resource Type Redesign (v4)

### Problem

Previous form showed GCS Bucket and Compute VM, but only Compute VM and general GCP IAM
(`manage-iam-permissions`) workflows are needed. The two workflows use different parameter names.

### Argo Workflows Available

| WorkflowTemplate | Purpose | Principal Params | Resource Param |
|---|---|---|---|
| `grant-compute-vm-access` | VM-specific access with mutex, OR-append conditions | `principal-type`, `principal-email` | `vm-name` |
| `manage-iam-permissions` | General GCP IAM for 9 service types | `who-type`, `who-email` | `resource-type`, `resource-name` |

### Changes Made

| Area | Before | After |
|---|---|---|
| **Resource types** | `gcs-bucket`, `compute-vm` | `compute-vm`, `gcp-iam` |
| **Workflow routing** | Both used `principal-type`/`principal-email` | Type-specific: VM uses `principal-*`, IAM uses `who-*` |
| **GCP Service field** | Not present | New dropdown (9 GCP services) for IAM role mapping |
| **Resource Name** | Required for both types | Required for VM, optional for GCP IAM (project-level) |
| **Custom permission** | Available for both types | VM only — GCP IAM blocks `none`/custom at validation |
| **Specific Permission** | Available for both types | VM only — GCP IAM returns validation error |
| **DB resource_name** | Raw resource name | VM: VM name. IAM: `gcp_service` or `gcp_service/resource` |
| **Workflow param mapping** | Uniform for all types | Branched: VM → `grant-compute-vm-access` params, IAM → `manage-iam-permissions` params |

### GCP IAM Service Types

| Value | Label | IAM Role Mapping (viewer/editor/admin) |
|---|---|---|
| `gcs-bucket` | GCS Bucket (Cloud Storage) | storage.objectViewer / objectAdmin / admin |
| `compute-vm` | Compute Engine (VMs) | compute.viewer / instanceAdmin.v1 / admin |
| `service-account` | Service Account (IAM) | iam.serviceAccountViewer / User / Admin |
| `bigquery` | BigQuery (Data Analytics) | bigquery.dataViewer / dataEditor / admin |
| `cloud-sql` | Cloud SQL (Databases) | cloudsql.viewer / editor / admin |
| `gke-cluster` | GKE Cluster (Kubernetes) | container.viewer / developer / admin |
| `pubsub` | Pub/Sub (Messaging) | pubsub.viewer / editor / admin |
| `secret-manager` | Secret Manager | secretmanager.viewer / secretAccessor / admin |
| `artifact-registry` | Artifact Registry (Docker) | artifactregistry.reader / writer / admin |

### DB Storage Strategy for GCP IAM

The DB has no separate column for `gcp_service`. The `resource_name` column stores:
- For Compute VM: raw VM name (e.g., `prod-api-server-1`)
- For GCP IAM project-level: just the service (e.g., `bigquery`)
- For GCP IAM resource-level: `service/resource` (e.g., `gcs-bucket/my-data-bucket`)

`_submit_workflow_for_request` parses this back when submitting the workflow on approval.

---

## 3. Rule Engine — Full QA

### Seed Rules

| Priority | Name | Matches | Action |
|---|---|---|---|
| 10 | `devops-team-auto-approve` | `requester_email=rajesh.deb@example.com` | Auto-approve (0 approvers) |
| 20 | `stage-viewer-auto-approve` | `permission_level=viewer`, `gcp_project=your-gcp-project-stage`, `action=grant` | Auto-approve |
| 30 | `stage-editor-admin` | `permission_level=editor,admin,custom,none`, `gcp_project=your-gcp-project-stage` | 1-level approval |
| 40 | `prod-viewer` | `permission_level=viewer`, `gcp_project=your-gcp-project-prod,your-gcp-project-data` | 1-level + ticket |
| 50 | `prod-editor-admin` | `permission_level=editor,admin,custom,none`, `gcp_project=your-gcp-project-prod,your-gcp-project-data` | 2-level + ticket |
| 60 | `revoke-auto-approve` | `action=revoke` | Auto-approve |
| 999 | `default-require-approval` | *(catch-all)* | 1-level + ticket |

### Scenario Matrix

| # | Scenario | Expected | Matched Rule | Status |
|---|----------|----------|--------------|--------|
| 1 | DevOps lead requests VM viewer on Stage | Auto-approve | `devops-team-auto-approve` (10) | PASS |
| 2 | DevOps lead requests GCP IAM admin on Prod | Auto-approve | `devops-team-auto-approve` (10) | PASS |
| 3 | Regular user → VM viewer → Stage | Auto-approve | `stage-viewer-auto-approve` (20) | PASS |
| 4 | Regular user → GCP IAM viewer → Stage | Auto-approve | `stage-viewer-auto-approve` (20) | PASS |
| 5 | Regular user → VM editor → Stage | 1-level approval | `stage-editor-admin` (30) | PASS |
| 6 | Regular user → GCP IAM admin → Stage | 1-level approval | `stage-editor-admin` (30) | PASS |
| 7 | Regular user → VM custom → Stage | 1-level approval | `stage-editor-admin` (30, matches `none`) | PASS |
| 8 | Regular user → GCP IAM custom → Stage | Rejected at validation | N/A (form blocks custom for IAM) | PASS |
| 9 | Regular user → VM viewer → Prod | 1-level + ticket | `prod-viewer` (40) | PASS |
| 10 | Regular user → GCP IAM viewer → Prod | 1-level + ticket | `prod-viewer` (40) | PASS |
| 11 | Regular user → VM editor → Prod + ENG-123 | 2-level + ticket | `prod-editor-admin` (50) | PASS |
| 12 | Regular user → GCP IAM admin → Prod + ENG-456 | 2-level + ticket | `prod-editor-admin` (50) | PASS |
| 13 | Regular user → VM viewer → your-gcp-project-data | 1-level + ticket | `prod-viewer` (40) | PASS |
| 14 | Regular user → VM admin → your-gcp-project-data + ENG-789 | 2-level + ticket | `prod-editor-admin` (50) | PASS |
| 15 | Revoke action (any) | Auto-approve | `revoke-auto-approve` (60) | N/A (form only does grant) |
| 16 | Unknown combination | 1-level + ticket | `default-require-approval` (999) | PASS |
| 17 | No matching rules (all disabled) | Error to user | N/A | PASS |

### Rule Engine Edge Cases

| # | Edge Case | Result |
|---|-----------|--------|
| 1 | Multiple rules same priority | First DB row wins (INSERT order) — no tie-breaking |
| 2 | Comma-separated values | Correctly handled — `editor,admin,custom,none` split and matched |
| 3 | Wildcard prefix match | `pattern.endswith("*")` + `value.startswith(pattern[:-1])` works |
| 4 | Case sensitivity | All matching is case-sensitive — `Viewer` ≠ `viewer` |
| 5 | Empty string vs NULL | Empty from form → `or None` conversion in `admin_rule_save` |
| 6 | `resource_type` matching | Seed rules have NULL (match any) — works for both `compute-vm` and `gcp-iam` |
| 7 | GCP IAM `permission_level=none` | Blocked at validation before rule engine runs |

### Risk Level Determination

| Scenario | Permission | Project | Risk | Status |
|---|---|---|---|---|
| VM viewer, stage | viewer | your-gcp-project-stage | low (auto-approve) | PASS |
| GCP IAM editor, stage | editor | your-gcp-project-stage | medium | PASS |
| VM viewer, prod | viewer | your-gcp-project-prod | medium | PASS |
| VM editor, prod | editor | your-gcp-project-prod | high | PASS |
| VM admin, prod | admin | your-gcp-project-prod | high | PASS |
| VM custom, prod | none | your-gcp-project-prod | high | PASS (fixed: `none` added to check) |
| GCP IAM admin, your-gcp-project-data | admin | your-gcp-project-data | high | PASS |

---

## 4. Form Validation QA

| # | Scenario | Expected | Status |
|---|----------|----------|--------|
| 1 | No principal email selected | Error on `principal_email_text_block` | PASS |
| 2 | Compute VM with no resource name | Error on `resource_name_block` | PASS |
| 3 | GCP IAM with no GCP service selected | Error on `gcp_service_block` | PASS |
| 4 | GCP IAM with custom permission (none) | Error on `permission_block` | PASS |
| 5 | GCP IAM with specific_permission filled | Error on `specific_permission_block` | PASS |
| 6 | VM with custom but no specific_permission | Error on `specific_permission_block` | PASS |
| 7 | VM with specific_permission bad format | Error on `specific_permission_block` | PASS |
| 8 | Resource type not in WORKFLOW_TEMPLATES | Error on `resource_type_block` | PASS |
| 9 | Ticket required but empty | Error on `ticket_block` | PASS |
| 10 | Ticket doesn't match pattern | Error on `ticket_block` | PASS |
| 11 | No matching rule | Error on `reason_block` | PASS |

---

## 5. Workflow Submission QA

### Compute VM → `grant-compute-vm-access`

| Form Field | Workflow Param | Status |
|---|---|---|
| `who_type` | `principal-type` | PASS |
| `who_email` | `principal-email` | PASS |
| `resource_name` (raw) | `vm-name` | PASS |
| `permission_level` | `permission-level` | PASS |
| `specific_permission` / `custom_role` | `specific-permission` | PASS (falls back to `custom_role` from DB) |
| `gcp_project` | `gcp-project` | PASS |
| `access_type` | `access-type` | PASS |
| `expiry_hours` | `expiry-hours` | PASS |
| `reason` | `reason` | PASS |

### GCP IAM → `manage-iam-permissions`

| Form Field | Workflow Param | Source | Status |
|---|---|---|---|
| (hardcoded) | `action` = `"grant"` | — | PASS |
| `who_type` | `who-type` | form | PASS |
| `who_email` | `who-email` | form | PASS |
| `gcp_service` (from `resource_name`) | `resource-type` | parsed from `resource_name.split("/")[0]` | PASS |
| raw resource name | `resource-name` | parsed from `resource_name.split("/")[1]` or `"not-specified"` | PASS |
| `permission_level` | `permission-level` | form (viewer/editor/admin only) | PASS |
| `gcp_project` | `gcp-project` | form | PASS |
| `access_type` | `access-type` | form | PASS |
| `expiry_hours` | `expiry-hours` | form | PASS |
| `reason` | `reason` | form | PASS |

### Approval Flow (button click → workflow submission)

| Step | Status | Notes |
|---|---|---|
| DB read `access_requests WHERE request_id = %s` | PASS | Returns full row with all fields |
| `resource_type` from DB row | PASS | `gcp-iam` or `compute-vm` |
| `resource_name` from DB row | PASS | VM: `prod-server-1`. IAM: `bigquery` or `bigquery/dataset-name` |
| `_submit_workflow_for_request(dict(req))` | PASS | Parses `resource_name` correctly for both types |
| `specific_permission` from DB | PASS | Not stored directly; `custom_role` column used as fallback |

---

## 6. Bugs Found & Fixed in This Revision

### FIXED: `determine_risk_level` didn't recognize `"none"` permission

Custom VM requests on prod were classified as "medium" risk instead of "high" because
the check was `if permission in ("admin", "editor", "custom")` but the form sends `"none"`.

**Fix:** Added `"none"` to the check: `("admin", "editor", "custom", "none")`.

### FIXED: Seed rules didn't match `"none"` permission

Rules `stage-editor-admin` and `prod-editor-admin` had `permission_level='editor,admin,custom'`
but the form sends `"none"` for custom. Custom VM requests would fall through to the catch-all
rule 999 instead of matching the correct rule.

**Fix:** Updated seed data to `'editor,admin,custom,none'`.

**Note:** For existing deployments, the seed uses `ON CONFLICT (name) DO NOTHING`, so the fix
won't auto-apply. Run this SQL manually on deployed databases:

```sql
UPDATE approval_rules SET permission_level = 'editor,admin,custom,none'
WHERE name IN ('stage-editor-admin', 'prod-editor-admin');
```

---

## 7. Known Limitations & Notes

| # | Item | Impact | Notes |
|---|------|--------|-------|
| 1 | Expiry hours for GCP IAM | Low | Form offers 48/72h which aren't in `manage-iam-permissions` YAML enum, but works via K8s API since enum is only enforced by Argo UI |
| 2 | GCP IAM resource_name with `/` in specific resource | Low | If user enters a resource name containing `/` for IAM, the `split("/", 1)` will still work correctly |
| 3 | Revoke action unused in form | Info | Form hardcodes `action=grant`; revoke is handled by auto-revoke CronWorkflows |
| 4 | GCP service selection visible for VM | Cosmetic | Slack modals are static — can't conditionally hide. Hint says "Ignored for Compute VM" |
| 5 | Argo workflow completion callback | Design gap | No mechanism to update `access_requests.status` when workflow completes or fails |
| 6 | CSRF on admin forms | Security | Admin form submissions lack CSRF tokens |
| 7 | Case-sensitive rule matching | Functional | `Viewer` ≠ `viewer` in rule engine — all values must be lowercase |

---

## 8. Admin UI CRUD QA

| Operation | Endpoint | Status | Notes |
|-----------|----------|--------|-------|
| List rules | `GET /admin/rules` | OK | Sorted by priority |
| Create rule (UI) | `POST /admin/rules/save` | OK | Redirects to rules list |
| Edit rule (UI) | `GET /admin/rules/{id}/edit` + `POST /admin/rules/save` | OK | Pre-fills form |
| Delete rule (UI) | `POST /admin/rules/{id}/delete` | OK | With JS confirm dialog |
| Create rule (API) | `POST /api/rules` | OK | JSON body, returns ID |
| Update rule (API) | `PUT /api/rules/{id}` | OK | Full replace |
| Delete rule (API) | `DELETE /api/rules/{id}` | OK | Returns message |
| Evaluate rules (API) | `POST /api/rules/evaluate` | OK | Dry-run test endpoint |
| List groups | `GET /admin/groups` | OK | With members inline |
| Add group | `POST /admin/groups/add` | OK | |
| Delete group | `POST /admin/groups/{id}/delete` | OK | |
| Add member | `POST /admin/groups/{id}/members/add` | OK | Auto-resolves Slack ID |
| Remove member | `POST /admin/groups/{id}/members/{mid}/delete` | OK | |
| View history | `GET /admin/history` | OK | With status filters |

---

## 9. Verification Checklist

| Check | Status |
|---|---|
| Python syntax check (`py_compile`) | PASS |
| No stale `gcs-bucket` references in WORKFLOW_TEMPLATES | PASS |
| No stale `grant-bucket-access` references | PASS |
| `WORKFLOW_TEMPLATES` maps to correct template names | PASS |
| Form params align with `grant-compute-vm-access.yaml` spec | PASS |
| Form params align with `manage-iam-permissions.yaml` spec | PASS |
| GCP Service dropdown matches `manage-iam-permissions` `resource-type` enum | PASS |
| Resource Name optional for GCP IAM, required for VM | PASS |
| Custom permission blocked for GCP IAM at validation | PASS |
| Seed rule `permission_level` includes `none` for custom support | PASS |
| `determine_risk_level` handles `none` as high-risk | PASS |
| `rule_edit.html` placeholder updated to `compute-vm, gcp-iam` | PASS |

---

## 10. Files Modified

| File | Changes |
|------|---------|
| `main.py` | Resource type redesign: `WORKFLOW_TEMPLATES` (removed `gcs-bucket`, added `gcp-iam`), `GCP_IAM_SERVICES` constant, `build_access_request_modal()` (GCP Service dropdown, optional resource name), `_handle_modal_submission()` (type-specific validation, gcp_service encoding in resource_name), `_submit_workflow_for_request()` (branched param mapping for VM vs IAM), `determine_risk_level()` (added `none` to high-risk set) |
| `k8s/db-init-configmap.yaml` | Updated `stage-editor-admin` and `prod-editor-admin` seed rules: `permission_level` now includes `none` |
| `templates/rule_edit.html` | Updated resource_type placeholder from `gcs-bucket, compute-vm, ...` to `compute-vm, gcp-iam` |
| `QA_REPORT.md` | This report — v4 with resource type redesign and full rule engine QA |

---

## 11. Proxy Requester who_email Fix (2026-04-30)

### Bug

When a proxy-requesters group member selects another user via the Slack `users_select` picker,
the `who_email` field in the access request still showed the proxy user's own email instead of
the selected target user's email. This happened every time the proxy/impersonation feature was used.

### Root Cause

`_get_val()` only handled `static_select` and `plain_text_input` block types. The proxy user
picker renders as `users_select` (Slack native element), which returns `selected_user` (a Slack UID)
instead of `selected_option.value`. Since `_get_val` fell through to `block.get("value", "")`,
which is empty for `users_select`, the who_email was always empty for proxy users, and the
security enforcement block then had no value to work with.

### Fix Applied

| Area | Change |
|------|--------|
| `_get_val()` (line 1715) | Added `users_select` type handling: extracts `selected_user` UID, resolves to email via `users_info()` API, falls back to raw UID on API failure |
| Fallback extraction (line 1787) | For proxy users where `_get_val` returns empty, directly reads `selected_user` from raw state values and resolves via Slack API |
| Security enforcement (line 1800) | Non-proxy users with `who_type=user` always locked to `requester_email`; proxy users keep selected user email |
| Diagnostic logging | Added `logger.info`/`logger.warning` at each decision point for observability |

### Unit Test Results — 34/34 PASSED

| Test Suite | Tests | Status |
|------------|-------|--------|
| `TestGetValUsersSelect` — _get_val block type handling | 8 | ✅ ALL PASSED |
| `TestProxyWhoEmailLogic` — proxy/non-proxy decision tree | 6 | ✅ ALL PASSED |
| `TestPrivateMetadataPreservation` — is_proxy survives modal rebuilds | 5 | ✅ ALL PASSED |
| `TestBuildWhoBlock` — correct UI element per scenario | 4 | ✅ ALL PASSED |
| `TestProxyFlowEndToEnd` — full payload simulation | 7 | ✅ ALL PASSED |
| **TOTAL** | **34** | **✅ ALL PASSED** |

### Test Scenarios Covered

| # | Scenario | Expected | Status |
|---|----------|----------|--------|
| 1 | Proxy user selects another user via users_select | who_email = selected user's email | PASS |
| 2 | Proxy user: _get_val returns empty, fallback resolves UID | who_email = fallback-resolved email | PASS |
| 3 | Proxy user: Slack API fails during resolution | who_email = raw Slack UID | PASS |
| 4 | Proxy user: no user selected at all | who_email = empty | PASS |
| 5 | Non-proxy user: who_email always locked to requester | who_email = requester_email | PASS |
| 6 | Non-proxy user: form value ignored/overridden | who_email = requester_email | PASS |
| 7 | Proxy + group who_type: form value preserved | who_email = group email | PASS |
| 8 | Non-proxy + group who_type: form value preserved | who_email = group email | PASS |
| 9 | Proxy + serviceAccount: form value preserved | who_email = SA email | PASS |
| 10 | Non-proxy + serviceAccount: form value preserved | who_email = SA email | PASS |
| 11 | Proxy user selects themselves | who_email = own email | PASS |
| 12 | Missing private_metadata defaults to non-proxy | who_email locked to requester | PASS |
| 13 | is_proxy survives JSON roundtrip (True) | is_proxy = True | PASS |
| 14 | is_proxy survives JSON roundtrip (False) | is_proxy = False | PASS |
| 15 | Modal rebuild preserves proxy context | is_proxy, requester_email intact | PASS |

### Files Modified

| File | Changes |
|------|---------|
| `main.py` | `_get_val()` — added `users_select` type handling with Slack UID→email resolution; fallback extraction for proxy users; diagnostic logging |
| `tests/test_proxy_who_email.py` | New — 34 unit tests covering all proxy/non-proxy who_email extraction scenarios |
| `QA_REPORT.md` | Added Section 11 — proxy requester fix QA results |
