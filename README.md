# ⚡ Approval Handler - Universal Access Request Platform for DevOps/SRE

**Stop drowning in Slack DMs asking for access. Centralize ALL infrastructure access requests with intelligent approval workflows.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-green.svg)](https://fastapi.tiangolo.com/)
[![Docker](https://img.shields.io/badge/docker-ready-brightgreen.svg)](https://www.docker.com/)

> **One Slack command. All access requests. Smart approvals. Automated execution.**

---

## 🎯 The Problem (Every Startup Faces This)

Your engineering team is scaling. Suddenly your DevOps/SRE team is buried in:

- 💬 "Can I get viewer access to prod GCP?"
- 💬 "Need AWS S3 permissions for data-pipeline project"
- 💬 "Update Vault with new API keys for payment service"
- 💬 "Run the database migration job"
- 💬 "Deploy staging environment"
- 💬 "Give me editor access to Kubernetes namespace"

**Right now:**
- ❌ Requests come via random Slack DMs, emails, or hallway conversations
- ❌ No standardized approval process
- ❌ Manual tracking in spreadsheets or Notion
- ❌ No audit trail for compliance
- ❌ DevOps team manually executes every request
- ❌ Security team has zero visibility
- ❌ Junior engineers wait hours/days for simple access

**This gets messy fast.** You need structure, automation, and accountability.

---

## ✨ The Solution: Approval Handler

**One unified platform for ALL infrastructure access requests:**

```
Developer → Slack /access-request → Smart Rule Engine → Approval Workflow → Auto-Execute
```

### 🚀 **What Makes This Different**

| Feature | Traditional Approach | Approval Handler |
|---------|---------------------|------------------|
| **Request Channel** | Slack DMs, email, tickets | Single Slack command |
| **Approval Flow** | Manual coordination | Automated multi-level workflows |
| **Execution** | DevOps manually runs commands | Argo Workflows auto-execute |
| **Rule Management** | Tribal knowledge | Priority-based rule engine |
| **Audit Trail** | None or manual logs | Full PostgreSQL audit log |
| **Customization** | Hard-coded per request type | Configurable approval catalog |
| **Visibility** | Siloed | Centralized dashboard |

---

## 🎨 How It Works

### **1. User Submits Request (Slack)**

```
/access-request
```

A Slack modal opens with:
- **Request Type**: GCP Access, AWS Access, Vault Update, Run Job, Deploy, etc.
- **Project/Environment**: prod, staging, dev
- **Permission Level**: viewer, editor, admin, custom
- **Duration**: temporary (4h), permanent, custom
- **Justification**: Why do you need this?
- **Ticket ID**: Jira/Linear ticket (required for prod)

### **2. Rule Engine Matches Request**

The system automatically evaluates your rules **by priority**:

| Rule Name | Priority | Conditions | Approval Levels | Auto-Approve? |
|-----------|----------|------------|-----------------|---------------|
| `devops-team-auto` | 10 | requester = DevOps team | 0 | ✅ Yes |
| `staging-viewer` | 20 | env = staging, level = viewer | 0 | ✅ Yes (with alert) |
| `staging-editor` | 30 | env = staging, level = editor/admin | 1 | ❌ No - requires approval |
| `prod-viewer` | 40 | env = prod, level = viewer | 1 + ticket | ❌ No - requires approval + ticket |
| `prod-critical` | 50 | env = prod, level = editor/admin | 2 + ticket | ❌ No - requires 2-level approval |

**First match wins!** Lower priority number = evaluated first.

### **3. Approval Workflow**

Request posted to:
- ✅ **#access-requests** Slack channel (team visibility)
- ✅ **Direct DM** to assigned approvers via Slack bot

Approvers see:
```
🔐 New Access Request from @john.doe

📋 Request Details:
  Type: GCP IAM Access
  Project: prod-api-services
  Role: roles/compute.viewer
  Duration: Temporary (4 hours)
  Ticket: ENG-1234
  Reason: Debug production API latency issue

👤 Requester: John Doe (Backend Engineer)
📊 Risk Level: Medium
✅ Requires: 1-level approval (devops-leads)

[✅ Approve]  [❌ Reject]  [💬 Comment]
```

**Multi-Level Approvals:**
- **Level 1**: DevOps lead approves → triggers Level 2
- **Level 2**: Security team approves → triggers execution

### **4. Automated Execution (Argo Workflows)**

Once approved, the system triggers an **Argo Workflow** that:
- ✅ Grants the IAM permission
- ✅ Updates Vault secrets
- ✅ Runs the requested job
- ✅ Deploys infrastructure
- ✅ Sends confirmation to requester

**Extensible:** Argo Workflows can call ANY automation (gcloud, AWS CLI, Terraform, kubectl, custom scripts)

### **5. Audit Trail**

Every action logged to PostgreSQL:
- WHO requested
- WHAT access/action
- WHY (justification + ticket)
- WHO approved (Level 1 & Level 2)
- WHEN it was granted
- RESULT (success/failure from Argo Workflow)

Export for compliance: SOC2, ISO 27001, GDPR audits.

---

## 🏗️ Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                    Slack Workspace                           │
│  Developer: /access-request → Modal → Submit                 │
│  Approvers: #access-requests channel + DM notifications      │
└────────────────────┬─────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│             FastAPI Backend (Approval Handler)              │
│                                                             │
│  ┌─────────────────┐  ┌──────────────────┐                │
│  │  Rule Engine    │  │ Approval Catalog │                │
│  │ Priority-based  │  │ (Enable/Disable) │                │
│  │ Condition Match │  │ Request Types    │                │
│  └─────────────────┘  └──────────────────┘                │
│                                                             │
│  ┌─────────────────┐  ┌──────────────────┐                │
│  │ Team/Approver   │  │   Admin UI       │                │
│  │ Group Mapping   │  │ (HTMX Dashboard) │                │
│  └─────────────────┘  └──────────────────┘                │
└────────┬────────────────────────┬───────────────────────────┘
         │                        │
         ▼                        ▼
┌─────────────────┐      ┌──────────────────────┐
│  PostgreSQL     │      │  Argo Workflows API  │
│  (Cloud SQL)    │      │  (Kubernetes)        │
│  - Rules        │      │                      │
│  - Approvers    │      │  Triggers workflows: │
│  - Requests     │      │  - gcloud commands   │
│  - Audit Logs   │      │  - AWS CLI           │
└─────────────────┘      │  - Vault updates     │
                         │  - Terraform apply   │
                         │  - kubectl commands  │
                         │  - Custom scripts    │
                         └──────────────────────┘
```

---

## 🎯 Use Cases (Beyond GCP IAM)

This is a **universal approval platform**. Here are real-world use cases:

### **1. Cloud Access Management**
- ✅ GCP IAM permissions (Compute, Storage, BigQuery, GKE, etc.)
- ✅ AWS IAM roles (S3, EC2, RDS, Lambda, etc.)
- ✅ Azure RBAC
- ✅ Kubernetes RBAC (namespace access, ClusterRole bindings)

### **2. Secret Management**
- ✅ Update HashiCorp Vault secrets
- ✅ Rotate API keys
- ✅ Grant database credentials (temporary)
- ✅ Update service account tokens

### **3. Infrastructure Operations**
- ✅ Run database migrations
- ✅ Deploy to staging/production
- ✅ Scale Kubernetes deployments
- ✅ Restart services
- ✅ Execute Terraform plans
- ✅ Run Ansible playbooks

### **4. Emergency Break-Glass**
- ✅ Production SSH access (time-bound)
- ✅ Database write access (temporary)
- ✅ Bypass rate limits
- ✅ Feature flag overrides

### **5. Compliance & Auditing**
- ✅ Require ticket IDs for prod access
- ✅ Enforce multi-level approvals for sensitive operations
- ✅ Auto-expire temporary permissions
- ✅ Generate audit reports for compliance teams

---

## 📦 The Approval Catalog System

**What makes this powerful:** You define what can be requested via a **configurable catalog**.

### **Default Catalog (Included)**

| Category | Request Types | Resource-Level? |
|----------|---------------|-----------------|
| **GCP** | VM, GKE, Storage, BigQuery, IAM, Cloud SQL, Pub/Sub, Artifact Registry | ✅ Yes |
| **AWS** | (Add your own: EC2, S3, RDS, Lambda) | ✅ Extensible |
| **Vault** | (Add your own: Update secrets, rotate keys) | ✅ Extensible |
| **Jobs** | (Add your own: DB migration, deploy, backup) | ✅ Extensible |

### **How to Add New Request Types**

Edit `main.py` → `GCP_ROLE_CATALOG` (or rename to `APPROVAL_CATALOG`):

```python
APPROVAL_CATALOG = {
    "aws-s3": {
        "label": "AWS S3 Bucket Access",
        "resource_level": True,
        "resource_placeholder": "Bucket name (e.g., my-data-bucket)",
        "roles": [
            {"display": "Read-Only", "value": "s3:GetObject"},
            {"display": "Read-Write", "value": "s3:PutObject"},
            {"display": "Full Access", "value": "s3:*"},
        ]
    },
    "vault-update": {
        "label": "Update Vault Secret",
        "resource_level": True,
        "resource_placeholder": "Secret path (e.g., secret/prod/api-keys)",
        "roles": [
            {"display": "Read Secret", "value": "vault:read"},
            {"display": "Write Secret", "value": "vault:write"},
        ]
    },
    "run-job": {
        "label": "Run Infrastructure Job",
        "resource_level": False,
        "resource_placeholder": "N/A",
        "roles": [
            {"display": "DB Migration", "value": "job:db-migration"},
            {"display": "Deploy Staging", "value": "job:deploy-staging"},
            {"display": "Deploy Production", "value": "job:deploy-prod"},
        ]
    },
}
```

**Enable/Disable** catalog items in the Admin UI → Catalog Management.

---

## ⚙️ Rule Engine Deep Dive

Rules are evaluated **top-to-bottom by priority**. First match wins.

### **Rule Conditions (AND logic)**

| Condition Field | Example Values | Matches |
|-----------------|----------------|---------|
| `requester_email` | `john.doe@company.com` | Specific user |
| `requester_domain` | `@devops-team.company.com` | Team email domain |
| `gcp_project` | `prod-api-services` | Specific GCP project |
| `environment` | `prod`, `staging`, `dev` | Environment |
| `permission_level` | `viewer`, `editor`, `admin`, `custom` | Permission risk level |
| `action` | `grant`, `revoke` | Grant vs revoke access |
| `request_type` | `gcp-iam`, `aws-iam`, `vault-update` | Type of request |

### **Rule Actions**

| Field | Values | Behavior |
|-------|--------|----------|
| `approval_levels` | `0`, `1`, `2` | 0 = auto-approve, 1-2 = manual approvals |
| `level_1_group` | `devops-leads` | Who approves at Level 1 |
| `level_2_group` | `security-team` | Who approves at Level 2 (if 2-level) |
| `require_ticket` | `true`/`false` | Enforce ticket ID (Jira, Linear, etc.) |
| `ticket_prefix` | `ENG-`, `SEC-`, `OPS-` | Validate ticket format |
| `alert_channel` | `#security-alerts` | Send alert even if auto-approved |

### **Example Rules**

```sql
-- Rule 1: DevOps team auto-approve for all requests
Priority: 10
Condition: requester_email = 'devops-team@company.com'
Action: approval_levels = 0 (auto-approve)

-- Rule 2: Staging viewer access → auto-approve with alert
Priority: 20
Condition: environment = 'staging' AND permission_level = 'viewer'
Action: approval_levels = 0, alert_channel = '#security-alerts'

-- Rule 3: Staging editor/admin → 1-level approval
Priority: 30
Condition: environment = 'staging' AND permission_level IN ('editor', 'admin')
Action: approval_levels = 1, level_1_group = 'devops-leads'

-- Rule 4: Production viewer → 1-level + ticket required
Priority: 40
Condition: environment = 'prod' AND permission_level = 'viewer'
Action: approval_levels = 1, require_ticket = true, ticket_prefix = 'ENG-'

-- Rule 5: Production critical → 2-level + ticket + security review
Priority: 50
Condition: environment = 'prod' AND permission_level IN ('editor', 'admin')
Action: approval_levels = 2, level_1_group = 'devops-leads', level_2_group = 'security-team', require_ticket = true
```

---

## 👥 Team & Approver Groups

Map approval groups to your team structure:

| Group Name | Members | Used For |
|------------|---------|----------|
| `devops-leads` | DevOps team leads | Level 1 approvals |
| `security-team` | Security engineers | Level 2 approvals for prod |
| `engineering-leads` | Engineering managers | High-risk operations |
| `final-approvers` | CTOs, VPs | Emergency break-glass |
| `on-call` | Current on-call rotation | After-hours requests |

**Dynamic Groups:** Integrate with PagerDuty/OpsGenie to auto-assign to whoever is on-call.

---

## 🚀 Quick Start

### **Prerequisites**
- Python 3.11+
- PostgreSQL 16+
- Slack workspace (admin access)
- Kubernetes cluster with Argo Workflows (optional for execution)

### **1. Clone & Install**
```bash
git clone https://github.com/code-rajeshdeb/approval-handler.git
cd approval-handler
pip install -r requirements.txt
```

### **2. Configure Slack App**

1. Go to https://api.slack.com/apps → **Create New App** → **From Manifest**
2. Paste this manifest:

```yaml
display_information:
  name: Access Request Bot
  description: Universal approval workflow for infrastructure access
features:
  bot_user:
    display_name: Access Request Bot
    always_online: true
  slash_commands:
    - command: /access-request
      url: https://YOUR_DOMAIN/slack/slash
      description: Request infrastructure access
      usage_hint: Opens access request form
oauth_config:
  scopes:
    bot:
      - chat:write
      - commands
      - users:read
      - users:read.email
settings:
  interactivity:
    is_enabled: true
    request_url: https://YOUR_DOMAIN/slack/interactive
```

3. Install to workspace → Copy tokens

### **3. Set Environment Variables**

```bash
export DB_HOST=localhost
export DB_PASSWORD=your-secure-password
export SLACK_BOT_TOKEN=xoxb-your-token
export SLACK_SIGNING_SECRET=your-secret
export SLACK_CHANNEL=#access-requests
export ADMIN_USERNAME=admin
export ADMIN_PASSWORD=your-admin-password
export ARGO_UI_URL=https://workflow.example.com
```

### **4. Initialize Database**

```bash
psql -U postgres -c "CREATE DATABASE access_control;"
psql -U access_control -d access_control < k8s/db-init-configmap.yaml
```

### **5. Run**

```bash
# Development
uvicorn main:app --reload --port 8000

# Production
uvicorn main:app --workers 4 --port 8000
```

### **6. Access Admin UI**

Open `http://localhost:8000/admin` → Configure rules and approver groups

---

## 🐳 Production Deployment

### **Docker**
```bash
docker build -t approval-handler:latest .
docker run -d -p 8000:8000 \
  -e DB_HOST=your-db \
  -e SLACK_BOT_TOKEN=xoxb-token \
  approval-handler:latest
```

### **Kubernetes**
```bash
# Update k8s/deployment.yaml with your config
kubectl apply -f k8s/
```

See [`k8s/`](k8s/) for full manifests (deployment, service, secrets, RBAC).

---

## 📊 Admin Dashboard Features

Access at `/admin` (HTTP Basic Auth):

### **1. Rules Management**
- Create/edit/delete approval rules
- Set priority, conditions, approval levels
- Test rule matching (preview which rule applies to a sample request)

### **2. Approver Groups**
- Create groups (devops-leads, security-team, etc.)
- Add/remove members
- Map groups to approval levels

### **3. Request History**
- Search by requester, project, status, date range
- View full audit trail (who approved, when, execution result)
- Export for compliance reports

### **4. Approval Catalog**
- Enable/disable request types
- Add custom request types
- Configure per-type settings

### **5. Analytics Dashboard**
- Total requests by type, status, environment
- Approval times (SLA tracking)
- Top requesters, top approvers
- Auto-approve vs manual-approve ratio

---

## 🧪 Testing

### **Unit Tests**
```bash
pytest tests/ -v
```

### **QA Test Suite**
```bash
./qa-tests.sh
```

See [`QA-CHECKLIST.md`](QA-CHECKLIST.md) for manual testing scenarios.

---

## 🔒 Security & Compliance

### **Built-In Security Features**
- ✅ **Least Privilege**: Auto-approve low-risk, require approvals for high-risk
- ✅ **Ticket Enforcement**: Require Jira/Linear tickets for prod changes
- ✅ **Temporary Access**: Auto-expire time-bound permissions
- ✅ **Audit Logs**: Full WHO/WHAT/WHEN/WHY trail
- ✅ **Multi-Level Approvals**: Separation of duties (DevOps + Security)
- ✅ **Slack Signature Verification**: Prevent request forgery
- ✅ **HTTP Basic Auth**: Admin UI protected

### **Compliance Use Cases**
- **SOC2**: Audit trail + approval workflows + access reviews
- **ISO 27001**: Access control policies + logging
- **GDPR**: Data access tracking
- **HIPAA**: Healthcare data access approvals

---

## 🤝 Contributing

We welcome contributions! Here's how:

1. **Fork** the repo
2. **Create** a feature branch (`git checkout -b feature/aws-integration`)
3. **Commit** (`git commit -m 'Add AWS IAM support'`)
4. **Push** (`git push origin feature/aws-integration`)
5. **Open** a Pull Request

### **Contribution Ideas**
- Add AWS/Azure support to catalog
- Integrate with MS Teams
- PagerDuty on-call integration
- Terraform plan approval workflow
- Slack thread-based approvals
- Mobile app notifications

---

## 📄 License

MIT License - see [LICENSE](LICENSE) file.

---

## 💖 Support This Project

If this saves your DevOps team hours every week:

- ⭐ **Star this repo**
- 🐛 **Report bugs** via [Issues](https://github.com/code-rajeshdeb/approval-handler/issues)
- 💡 **Request features**
- 💰 **Sponsor** via GitHub Sponsors (coming soon)

---

## 🗺️ Roadmap

- [ ] **v2.0**: AWS & Azure support in catalog
- [ ] **v2.1**: MS Teams integration
- [ ] **v2.2**: PagerDuty on-call auto-assignment
- [ ] **v2.3**: Terraform plan approval workflow
- [ ] **v2.4**: Self-service role templates
- [ ] **v2.5**: SLO/SLA tracking dashboard
- [ ] **v3.0**: SaaS offering

---

## 🙏 Built For DevOps/SRE Teams

This tool was built by a DevOps engineer who was tired of:
- Getting 50+ Slack DMs per day asking for access
- Manually running `gcloud` commands for every request
- No audit trail when security asked "who approved this?"
- Junior engineers waiting hours for simple staging access

**If your team faces the same pain, this is for you.**

---

## 📞 Contact

- **Issues**: https://github.com/code-rajeshdeb/approval-handler/issues
- **Discussions**: https://github.com/code-rajeshdeb/approval-handler/discussions
- **Email**: your-email@example.com

---

**⚡ Stop manual access management. Start smart approval workflows.**

**⭐ Star this repo if it saves you time!**
