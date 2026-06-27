# Approval Handler — QA Test Checklist

> **Version**: v2.0.0  
> **Last Updated**: 2026-04-03  
> **Environment**: Stage (`your-gcp-project-stage`)  
> **Namespace**: `argo-access-control`  
> **Admin UI**: `https://api.example.com/approval-handler/admin`

---

## 1. Automated Tests

Run the automated QA test script before manual testing:

```bash
# Dry-run (validate parameters, no actual workflows)
bash qa-tests.sh --dry-run

# Full test suite (submits workflows, tests API, DB)
bash qa-tests.sh

# Individual sections
bash qa-tests.sh --api-only
bash qa-tests.sh --db-only
bash qa-tests.sh --workflow-only
```

---

## 2. Slack Bot Functional Tests

### 2.1 Slash Command

| # | Test | Steps | Expected Result | ✅/❌ |
|---|------|-------|-----------------|-------|
| 1 | Basic slash command | Type `/access-request` in any Slack channel | Modal form opens | |
| 2 | Command in DM | Type `/access-request` in a DM with the bot | Modal form opens | |
| 3 | Command in thread | Type `/access-request` in a thread | Modal form opens | |

### 2.2 Modal Form

| # | Test | Steps | Expected Result | ✅/❌ |
|---|------|-------|-----------------|-------|
| 4 | Service category dropdown | Open modal → click service category | All 14 categories shown (basic, bigquery, gcs-bucket, compute-vm, gke-cluster, cloud-sql, service-account, pubsub, secret-manager, artifact-registry, cloud-logging, cloud-monitoring, cloud-composer, custom) | |
| 5 | Role dropdown updates | Select "Cloud Storage" category | Roles update to show Storage Object Viewer, Object Creator, Object Admin, Storage Admin, HMAC Key Admin | |
| 6 | BigQuery roles | Select "BigQuery" category | Shows Data Viewer, Data Editor, Data Owner, Job User, User, Admin | |
| 7 | Resource name field | Select a resource-level category (e.g., gcs-bucket) | Resource name text input appears with placeholder | |
| 8 | Resource name hidden | Select a non-resource-level category (e.g., basic) | Resource name field is hidden or shows "global" | |
| 9 | Custom role input | Select "Custom IAM Role" category | Free-text role input appears instead of dropdown | |
| 10 | Who-type selector | Click who-type dropdown | Shows: User, Group, Service Account | |
| 11 | GCP project selector | Click project dropdown | Shows: your-gcp-project-stage, your-gcp-project-prod, your-gcp-project-data | |
| 12 | Access type selector | Click access type | Shows: Temporary, Permanent | |
| 13 | Expiry hours | Select "Temporary" access type | Expiry dropdown appears (1-24h options) | |
| 14 | Expiry hidden for permanent | Select "Permanent" access type | Expiry dropdown hidden | |
| 15 | Reason field | Type in reason field | Accepts free text | |
| 16 | Ticket field | Type ticket ID | Accepts format like ENG-1234 | |
| 17 | Proxy requester | User in proxy-requesters group opens modal | "Request on behalf of" email field appears | |
| 18 | Non-proxy requester | Regular user opens modal | No proxy requester field shown | |

### 2.3 Form Submission — Auto-Approve Flow

| # | Test | Steps | Expected Result | ✅/❌ |
|---|------|-------|-----------------|-------|
| 19 | Auto-approve grant | Submit request matching auto-approve rule (e.g., stage viewer) | ✅ Request auto-approved, workflow submitted, Slack notification posted | |
| 20 | Workflow succeeds | Check Argo UI after auto-approve | Workflow completes successfully | |
| 21 | Slack notification | Check notification channel | Success message with ✅, role details, Argo link | |
| 22 | Temporary access tracking | Submit temporary access request | ConfigMap `iam-temporary-access-grants` updated | |

### 2.4 Form Submission — Manual Approval Flow

| # | Test | Steps | Expected Result | ✅/❌ |
|---|------|-------|-----------------|-------|
| 23 | Approval message posted | Submit request requiring approval | Approval message posted to channel with Approve/Reject buttons | |
| 24 | Approver DMs sent | Check approver DMs | Each approver in the group gets a DM with the same message + buttons | |
| 25 | Approve button | Click "Approve" on approval message | Status updates to "Approved", workflow submitted | |
| 26 | Reject button | Click "Reject" on approval message | Status updates to "Rejected", no workflow submitted | |
| 27 | Reject reason | Click "Reject" → modal for reason | Rejection reason modal appears | |
| 28 | DM buttons update | Approve via channel message | DM messages for all approvers update to show approved status | |
| 29 | Two-level approval | Submit request matching 2-level rule | First level approve → second level approvers notified → second approve → workflow runs | |
| 30 | Duplicate approve | Click "Approve" twice on same request | Second click shows "already handled" message | |

### 2.5 Notifications

| # | Test | Steps | Expected Result | ✅/❌ |
|---|------|-------|-----------------|-------|
| 31 | Success notification | Wait for workflow to complete | Slack message: ✅ Permission Granted with role, who, project, duration | |
| 32 | Failure notification | Trigger a workflow that fails (e.g., invalid resource) | Slack message: ❌ Permission Request Failed | |
| 33 | Argo UI link | Check notification message | Contains clickable link to Argo workflow | |
| 34 | Temporary duration shown | Grant temporary access | Notification shows "Temporary — Xh (auto-revoke)" | |
| 35 | Permanent shown | Grant permanent access | Notification shows "Permanent" | |

---

## 3. Multi-Channel Verification

| # | Test | Steps | Expected Result | ✅/❌ |
|---|------|-------|-----------------|-------|
| 36 | Request from #devops | `/access-request` in #devops channel | Approval message posted to correct approval channel | |
| 37 | Request from #engineering | `/access-request` in #engineering channel | Approval message routes to correct channel per rules | |
| 38 | DM request | `/access-request` in bot DM | Modal works, approval routed to default channel | |
| 39 | Source channel tracking | Check DB after request | `source_channel` recorded correctly in access_requests | |

---

## 4. Admin UI Checks

### 4.1 Dashboard

| # | Test | Steps | Expected Result | ✅/❌ |
|---|------|-------|-----------------|-------|
| 40 | Dashboard loads | Navigate to `/approval-handler/admin` | Dashboard shows stats: total rules, groups, pending/approved counts | |
| 41 | Recent requests | Check dashboard | Shows 10 most recent access requests | |
| 42 | Login required | Open admin URL without session | Redirected to login page | |
| 43 | Login with valid creds | Enter admin username/password | Dashboard loads | |
| 44 | Login with invalid creds | Enter wrong password | Error shown, not logged in | |

### 4.2 Rules Management

| # | Test | Steps | Expected Result | ✅/❌ |
|---|------|-------|-----------------|-------|
| 45 | Rules list | Navigate to `/admin/rules` | All rules shown sorted by priority | |
| 46 | Create new rule | Click "New Rule" → fill form → save | Rule created, appears in list | |
| 47 | Edit rule | Click edit on existing rule → change → save | Rule updated | |
| 48 | Delete rule | Click delete on a rule | Rule removed (with confirmation) | |
| 49 | Toggle rule enabled | Toggle a rule's enabled state | Rule enabled/disabled, evaluated correctly | |

### 4.3 Groups Management

| # | Test | Steps | Expected Result | ✅/❌ |
|---|------|-------|-----------------|-------|
| 50 | Groups list | Navigate to `/admin/groups` | All approver groups shown with members | |
| 51 | Create group | Add new group | Group created | |
| 52 | Add member | Add member to group (email + display name) | Member added to group | |
| 53 | Remove member | Remove member from group | Member removed | |
| 54 | Delete group | Delete an approver group | Group deleted (check no rules reference it) | |

### 4.4 Teams Management

| # | Test | Steps | Expected Result | ✅/❌ |
|---|------|-------|-----------------|-------|
| 55 | Teams list | Navigate to `/admin/teams` | All teams shown | |
| 56 | Create team | Add new team | Team created | |
| 57 | Edit team | Edit team name/lead group | Team updated | |
| 58 | Delete team | Delete a team | Team removed | |

### 4.5 History Page

| # | Test | Steps | Expected Result | ✅/❌ |
|---|------|-------|-----------------|-------|
| 59 | History loads | Navigate to `/admin/history` | Recent requests listed | |
| 60 | Filter by status | Filter by "approved" / "rejected" / "pending" | Filtered results shown | |
| 61 | Request detail | Click on a request | Detail view shows all request data, timeline, workflow status | |
| 62 | Rerun workflow | Click "Rerun" on a completed request | New workflow submitted with same parameters | |
| 63 | Force approve | Click "Force Approve" on a pending request | Request approved, workflow submitted | |

### 4.6 Catalog Management

| # | Test | Steps | Expected Result | ✅/❌ |
|---|------|-------|-----------------|-------|
| 64 | Catalog page loads | Navigate to `/admin/catalog` | All service categories shown | |
| 65 | Add category | Add new service category | Category created | |
| 66 | Toggle category | Enable/disable a category | Category toggled, Slack form updates | |
| 67 | Edit category | Update label, resource_level, placeholder | Category updated | |
| 68 | Add role to category | Add new IAM role to a category | Role appears in category | |
| 69 | Edit role | Update role display name, value, description | Role updated | |
| 70 | Toggle role | Enable/disable a role | Role toggled | |
| 71 | Delete role | Delete a role from category | Role removed | |
| 72 | Delete category | Delete a category (with cascade check) | Category and its roles removed | |

---

## 5. Workflow Template Tests

### 5.1 Grant Flows (by Service Category)

| # | Test | Category | Role | Resource | Expected | ✅/❌ |
|---|------|----------|------|----------|----------|-------|
| 73 | Basic viewer | basic | roles/viewer | global | Project-level grant | |
| 74 | GCS bucket scoped | gcs-bucket | roles/storage.objectViewer | test-bucket | Bucket-level conditional IAM | |
| 75 | BigQuery dataset | bigquery | roles/bigquery.dataViewer | test_dataset | Dataset-level bq grant | |
| 76 | Compute VM scoped | compute-vm | roles/compute.viewer | test-vm | VM conditional IAM | |
| 77 | Pub/Sub topic | pubsub | roles/pubsub.viewer | test-topic | Topic-level binding | |
| 78 | Secret Manager | secret-manager | roles/secretmanager.secretAccessor | test-secret | Secret-level binding | |
| 79 | Service Account | service-account | roles/iam.serviceAccountUser | test-sa@proj.iam.gserviceaccount.com | SA-level binding | |
| 80 | GKE project-level | gke-cluster | roles/container.viewer | global | Project-level | |
| 81 | Cloud SQL | cloud-sql | roles/cloudsql.client | global | Project-level | |
| 82 | Artifact Registry | artifact-registry | roles/artifactregistry.reader | global | Project-level | |
| 83 | Cloud Logging | cloud-logging | roles/logging.viewer | global | Project-level | |
| 84 | Cloud Monitoring | cloud-monitoring | roles/monitoring.viewer | global | Project-level | |
| 85 | Cloud Composer | cloud-composer | roles/composer.user | global | Project-level | |
| 86 | Custom role | custom | roles/browser | global | Project-level | |

### 5.2 Revoke Flows

| # | Test | Steps | Expected | ✅/❌ |
|---|------|-------|----------|-------|
| 87 | Revoke project-level | Grant then revoke basic viewer | IAM binding removed | |
| 88 | Revoke bucket-level | Grant then revoke on specific bucket | Conditional binding updated | |
| 89 | Revoke nonexistent | Revoke role user doesn't have | Graceful failure / no-op | |

### 5.3 Auto-Revocation

| # | Test | Steps | Expected | ✅/❌ |
|---|------|-------|----------|-------|
| 90 | ConfigMap tracking | Grant temporary access | Entry added to `iam-temporary-access-grants` ConfigMap | |
| 91 | Entry format | Check ConfigMap entry value | Format: `RESOURCE_TYPE\|WHO_TYPE\|WHO_EMAIL\|PROJECT\|ROLE\|EXPIRY_TIME\|RESOURCE_NAME` | |
| 92 | CronWorkflow cleanup | Wait for expiry (or manually trigger) | CronWorkflow revokes expired entries | |

### 5.4 Validation

| # | Test | Steps | Expected | ✅/❌ |
|---|------|-------|----------|-------|
| 93 | Invalid email domain | Submit with `user@gmail.com` | Workflow fails at validation step | |
| 94 | Empty custom-role | Submit `permission-level=custom` with empty custom-role | Workflow fails at validation | |
| 95 | Valid example.com email | Submit with `user@example.com` | Accepted | |
| 96 | Valid example.com | Submit with `user@example.com` | Accepted | |
| 97 | Valid SA email | Submit with `sa@project.iam.gserviceaccount.com` | Accepted | |
| 98 | Multiple comma-separated roles | Submit `roles/viewer,roles/editor` | Both roles granted | |
| 99 | Individual permissions | Submit `storage.objects.get,storage.objects.list` | Temporary custom role created | |

---

## 6. Edge Cases

| # | Test | Steps | Expected | ✅/❌ |
|---|------|-------|----------|-------|
| 100 | Duplicate grant | Grant same role to same user twice | "Already has access" — skips duplicate | |
| 101 | Concurrent requests | Submit two requests for same user rapidly | Mutex prevents race condition, second queues | |
| 102 | Large reason text | Submit with very long reason (>500 chars) | Handled gracefully (truncated or accepted) | |
| 103 | Special chars in reason | Submit reason with quotes, backticks, newlines | No injection, displayed correctly | |
| 104 | Bucket not found | Grant GCS access to nonexistent bucket | Error: "Bucket not found" | |
| 105 | VM not found | Grant compute access to nonexistent VM | Error: "VM not found" | |
| 106 | Service down | Stop approval-handler pod → submit command | Error message to user, no crash | |
| 107 | DB connection lost | Kill DB connection | `/health/ready` returns 503 | |
| 108 | Invalid Slack signature | Send request with wrong signature | HTTP 401 returned | |
| 109 | Expired session | Access admin UI with expired cookie | Redirected to login | |
| 110 | Prod project safety | Submit prod project request | Higher risk level, requires approval | |
| 111 | Mixed case email | Submit with `User@Arcana.IO` | Normalized to lowercase before processing | |
| 112 | Workflow timeout | Submit workflow that takes too long | TTL strategy cleans up after 24h | |

---

## 7. API Endpoint Verification

| # | Endpoint | Method | Auth | Expected | ✅/❌ |
|---|----------|--------|------|----------|-------|
| 113 | `/health` | GET | None | `{"status": "ok"}` | |
| 114 | `/health/ready` | GET | None | `{"status": "ready", "db": "connected"}` | |
| 115 | `/api/rules` | GET | Basic Auth | List of rules (JSON array) | |
| 116 | `/api/rules` | POST | Basic Auth | Create new rule | |
| 117 | `/api/rules/{id}` | PUT | Basic Auth | Update rule | |
| 118 | `/api/rules/{id}` | DELETE | Basic Auth | Delete rule | |
| 119 | `/api/rules/evaluate` | POST | Basic Auth | Evaluate rules against request data | |
| 120 | `/api/groups` | GET | Basic Auth | List groups with members | |
| 121 | `/api/teams` | GET | Basic Auth | List teams | |
| 122 | `/api/requests` | GET | Basic Auth | List access requests | |
| 123 | `/api/slack-users` | GET | Basic Auth | List cached Slack users | |
| 124 | `/api/rules` (no auth) | GET | None | HTTP 401 | |

---

## 8. Pre-Deployment Checklist

Before deploying a new version:

- [ ] Run `bash qa-tests.sh --dry-run` — all parameter checks pass
- [ ] Run `bash qa-tests.sh --api-only` — all API endpoints respond correctly
- [ ] Run `bash qa-tests.sh --db-only` — all DB tables exist with expected schema
- [ ] Manually test Slack `/access-request` command → modal opens
- [ ] Submit a test grant (stage, temporary, 1h) → verify workflow succeeds
- [ ] Verify Slack notification received with correct details
- [ ] Check Argo UI link in notification works
- [ ] Verify temporary access ConfigMap entry created
- [ ] Test approve/reject buttons in Slack
- [ ] Check admin UI dashboard loads with correct stats
- [ ] Verify catalog management page shows all categories and roles

---

## 9. Quick Smoke Test (5 minutes)

For a fast verification after deployment:

1. **Health check**: `curl https://api.example.com/approval-handler/health`
2. **Readiness**: `curl https://api.example.com/approval-handler/health/ready`
3. **Slash command**: Type `/access-request` in Slack → modal opens
4. **Submit test**: Request stage basic viewer (temporary, 1h) → auto-approved → workflow succeeds
5. **Admin UI**: Open `https://api.example.com/approval-handler/admin` → dashboard loads

---

*Generated by QA test framework. Update this checklist when new features are added.*
