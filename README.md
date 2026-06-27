# 🔐 Argo Access Control - Approval Handler

**A powerful, Slack-integrated approval workflow system for managing GCP IAM permissions through Argo Workflows.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-green.svg)](https://fastapi.tiangolo.com/)
[![Docker](https://img.shields.io/badge/docker-ready-brightgreen.svg)](https://www.docker.com/)

> **Stop giving permanent access. Start approving time-bound, audited permissions through Slack.**

---

## 🌟 Why This Project?

Managing cloud infrastructure access is hard:
- ❌ Manual IAM changes are error-prone
- ❌ Permanent permissions violate least-privilege
- ❌ No audit trail for who approved what
- ❌ Developers wait hours for access approvals

**This tool solves all of that** with Slack-native workflows, multi-level approvals, and automatic Argo Workflow execution.

---

## ✨ Features

### 🎯 **Core Capabilities**
- ✅ **Slack-Native UI**: Request, approve, and manage permissions without leaving Slack
- ✅ **Multi-Level Approvals**: Configure 0-2 approval levels based on risk
- ✅ **Priority-Based Rules**: Smart rule engine matches requests to policies
- ✅ **Argo Workflow Integration**: Automated IAM permission grants via Kubernetes
- ✅ **Time-Bound Access**: Temporary, permanent, or custom duration permissions
- ✅ **Ticket Enforcement**: Require Jira/Linear tickets for production access
- ✅ **Admin Dashboard**: Web UI (HTMX + Jinja2) for managing rules and approvers
- ✅ **Comprehensive Audit Trail**: Every request, approval, and action logged to PostgreSQL

### 🔒 **Security & Compliance**
- 🛡️ **Least Privilege by Default**: Auto-approve low-risk, require approvals for high-risk
- 🎟️ **Ticket Tracking**: Enforce ticket requirements for production environments
- 👥 **Approval Groups**: DevOps leads, security team, final approvers
- 📊 **Full Audit Logs**: WHO requested WHAT access, WHO approved, WHEN it was granted
- 🔐 **HTTP Basic Auth**: Admin UI protected with credentials from secret manager

### 🚀 **DevOps-Friendly**
- 🐳 **Containerized**: Docker + Kubernetes deployment ready
- 📦 **GitOps Compatible**: ArgoCD auto-sync for infrastructure-as-code
- 🏥 **Health Checks**: `/health` and `/health/ready` endpoints
- 📈 **Cloud SQL Backend**: PostgreSQL with automatic backups
- 🔄 **Auto Slack User Sync**: Keeps user directory updated from Slack workspace

---

## 📸 Screenshots

### Slack Request Flow
```
User types: /access-request
  ↓
Modal opens with:
  - GCP Project selector (Stage/Prod/Data)
  - Service Category (VM, GKE, IAM, Storage...)
  - IAM Role dropdown (Viewer/Editor/Admin)
  - Duration picker (Temporary/Permanent)
  - Ticket field (required for prod)
  ↓
Request posted to #access-request channel
  ↓
Approvers click [Approve] or [Reject] buttons
  ↓
Argo Workflow executes IAM changes
  ↓
Requester notified of success/failure
```

### Admin Dashboard
- 📋 **Rules Management**: Create/edit/delete approval rules
- 👥 **Approver Groups**: Manage approval tiers
- 📊 **Request History**: Search and filter past requests
- 🔍 **Rule Testing**: Preview which rule matches a request

---

## 🏗️ Architecture

```
┌─────────────┐
│   Slack     │  User submits /access-request
└──────┬──────┘
       │
       ▼
┌─────────────────────────────────────┐
│   FastAPI Backend (approval-handler)│
│  - Rule Engine (priority matching)  │
│  - Slack Integration                │
│  - Admin UI (Jinja2 + HTMX)         │
└──────┬────────────────┬─────────────┘
       │                │
       ▼                ▼
┌─────────────┐  ┌─────────────┐
│ PostgreSQL  │  │ Argo API    │
│ (Cloud SQL) │  │ (K8s CRD)   │
└─────────────┘  └──────┬──────┘
                        │
                        ▼
                ┌──────────────────┐
                │ Argo Workflow    │
                │ (executes gcloud)│
                └──────────────────┘
```

**Technology Stack:**
- **Backend**: FastAPI (Python 3.11+)
- **Database**: PostgreSQL 16+
- **Slack SDK**: `slack_sdk` for Bot API + interactive components
- **Orchestration**: Argo Workflows (Kubernetes CRDs)
- **Admin UI**: Jinja2 templates + HTMX
- **Deployment**: Docker + Kubernetes + ArgoCD

---

## 🚀 Quick Start

### Prerequisites
- Python 3.11+
- PostgreSQL 16+
- Slack workspace with admin access
- Argo Workflows running on Kubernetes (optional, for execution)

### 1. Clone the Repository
```bash
git clone https://github.com/code-rajeshdeb/approval-handler.git
cd approval-handler
```

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Set Up Slack App

1. Go to https://api.slack.com/apps → **Create New App** → **From Manifest**
2. Paste this manifest (replace `YOUR_DOMAIN`):

```yaml
display_information:
  name: Access Request Bot
  description: Approval workflow for GCP IAM permissions
features:
  bot_user:
    display_name: Access Request Bot
    always_online: true
  slash_commands:
    - command: /access-request
      url: https://YOUR_DOMAIN/slack/slash
      description: Request GCP IAM access
      usage_hint: Opens approval request form
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
  org_deploy_enabled: false
  socket_mode_enabled: false
```

3. **Install to Workspace** → Copy **Bot User OAuth Token**
4. Go to **Basic Information** → Copy **Signing Secret**

### 4. Configure Environment Variables

```bash
# Database
export DB_HOST=localhost
export DB_PORT=5432
export DB_NAME=access_control
export DB_USER=access_control
export DB_PASSWORD=your-secure-password

# Slack
export SLACK_BOT_TOKEN=xoxb-your-bot-token
export SLACK_SIGNING_SECRET=your-signing-secret
export SLACK_CHANNEL=#access-request

# Admin UI
export ADMIN_USERNAME=admin
export ADMIN_PASSWORD=your-admin-password
export SECRET_KEY=$(openssl rand -hex 32)

# Optional: Force approve password (for emergency overrides)
export FORCE_APPROVE_PASSWORD=your-emergency-password

# Argo Workflows
export ARGO_NAMESPACE=argo-access-control
export ARGO_WORKFLOW_TEMPLATE=manage-iam-permissions
export ARGO_UI_URL=https://workflow.example.com
```

### 5. Initialize Database

```bash
# Create database
psql -U postgres -c "CREATE DATABASE access_control;"
psql -U postgres -c "CREATE USER access_control WITH PASSWORD 'your-secure-password';"
psql -U postgres -c "GRANT ALL PRIVILEGES ON DATABASE access_control TO access_control;"

# Load schema
psql -U access_control -d access_control < k8s/db-init-configmap.yaml
```

### 6. Run the Application

```bash
# Development
uvicorn main:app --reload --host 0.0.0.0 --port 8000

# Production
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4
```

### 7. Access Admin UI

Open `http://localhost:8000/admin` (credentials: `ADMIN_USERNAME` / `ADMIN_PASSWORD`)

---

## 🐳 Docker Deployment

### Build Image
```bash
docker build -t approval-handler:latest .
```

### Run Container
```bash
docker run -d \
  -p 8000:8000 \
  -e DB_HOST=your-db-host \
  -e DB_PASSWORD=your-db-password \
  -e SLACK_BOT_TOKEN=xoxb-your-token \
  -e SLACK_SIGNING_SECRET=your-secret \
  -e ADMIN_PASSWORD=your-admin-password \
  approval-handler:latest
```

---

## ☸️ Kubernetes Deployment

Full K8s manifests are in the [`k8s/`](k8s/) directory:

```bash
# Update k8s/external-secret-*.yaml with your secret manager paths
# Update k8s/deployment.yaml with your container registry

kubectl apply -f k8s/
```

**Included manifests:**
- `deployment.yaml` - Main app deployment
- `service.yaml` - ClusterIP service
- `external-httproute.yaml` - Ingress routing
- `pg-cluster.yaml` - CloudNativePG cluster (optional)
- `db-init-job.yaml` - Database schema initialization
- `external-secret-*.yaml` - Secret manager integration
- `rbac.yaml` - ServiceAccount + Role for Argo API access

---

## 📖 How to Use

### As a Requester (Slack User)

1. **Request Access**: Type `/access-request` in Slack
2. **Fill the Form**:
   - Select GCP project (Stage/Prod/Data)
   - Choose service category (VM, GKE, Storage...)
   - Pick IAM role (Viewer/Editor/Admin)
   - Specify duration (Temporary 4h, Permanent, Custom)
   - Add ticket ID (required for prod)
3. **Submit** → Your request goes to `#access-request` channel
4. **Wait for Approval** → Approvers see the request + [Approve]/[Reject] buttons
5. **Get Notified** → Slack DM when approved + Argo Workflow completes

### As an Approver

1. **Monitor** `#access-request` channel
2. **Review** request details (who, what, why, ticket)
3. **Click** `[Approve]` or `[Reject]`
4. **Optionally** add approval comments
5. **Multi-level**: If 2-level approval required, second approver must also approve

### As an Admin

1. **Open** `https://your-domain/admin` (login with `ADMIN_USERNAME`/`ADMIN_PASSWORD`)
2. **Manage Rules**:
   - Create rules with conditions (requester, project, role, action)
   - Set priority (lower number = higher priority)
   - Configure approval levels (0 = auto-approve, 1-2 = manual)
   - Require tickets for prod access
3. **Manage Approver Groups**:
   - Create groups (e.g., `devops-leads`, `security-team`)
   - Add members with emails
4. **View Requests**:
   - Search by requester, project, status
   - See full approval trail

---

## 🎨 Customization

### Adding New GCP Services

Edit [`main.py`](main.py) → `GCP_ROLE_CATALOG`:

```python
GCP_ROLE_CATALOG = {
    "your-service": {
        "label": "Your Service",
        "resource_level": True,  # Supports resource-level scoping?
        "resource_placeholder": "Resource ID or 'global'",
        "roles": [
            {"display": "Viewer", "value": "roles/yourservice.viewer"},
            {"display": "Editor", "value": "roles/yourservice.editor"},
        ]
    },
    # ... existing services
}
```

### Custom Approval Rules

Rules are evaluated **top-to-bottom** by priority. First match wins.

**Example rule** (in Admin UI or `db-init-configmap.yaml`):
- **Name**: `prod-admin-requires-security-approval`
- **Priority**: 50
- **Conditions**:
  - GCP Project: `your-gcp-project-prod`
  - Permission Level: `admin,editor`
- **Approval Levels**: 2
- **2nd Level Group**: `security-team`
- **Require Ticket**: Yes (prefix: `SEC-`)

---

## 🧪 Testing

### Unit Tests
```bash
pytest tests/ -v
```

### QA Test Suite
```bash
# Requires running instance + kubectl access
./qa-tests.sh
```

See [`QA-CHECKLIST.md`](QA-CHECKLIST.md) for manual test scenarios.

---

## 🔒 Security Best Practices

1. ✅ **Rotate Secrets**: Use secret managers (OpenBao, HashiCorp Vault, GCP Secret Manager)
2. ✅ **HTTPS Only**: Always use TLS for Slack webhook URLs
3. ✅ **Network Policies**: Restrict pod egress to Slack API + GCP APIs only
4. ✅ **Least Privilege SA**: Argo Workflow service account should only have IAM Admin
5. ✅ **Audit Logs**: Enable GCP Cloud Audit Logs for IAM changes
6. ✅ **Backup Database**: Automated backups to GCS (see `pg-cluster.yaml`)

---

## 🤝 Contributing

Contributions are welcome! Please follow these steps:

1. **Fork** the repository
2. **Create** a feature branch (`git checkout -b feature/amazing-feature`)
3. **Commit** your changes (`git commit -m 'Add amazing feature'`)
4. **Push** to the branch (`git push origin feature/amazing-feature`)
5. **Open** a Pull Request

### Development Setup
```bash
# Install dev dependencies
pip install -r requirements.txt pytest pytest-cov black flake8

# Run linter
black main.py
flake8 main.py

# Run tests
pytest tests/ --cov=main
```

---

## 📄 License

This project is licensed under the **MIT License** - see the [LICENSE](LICENSE) file for details.

---

## 💖 Support This Project

If you find this tool useful, please consider:

- ⭐ **Star this repo** on GitHub
- 🐛 **Report bugs** via [Issues](https://github.com/code-rajeshdeb/approval-handler/issues)
- 💡 **Suggest features** or improvements
- 💰 **Sponsor** via [GitHub Sponsors](https://github.com/sponsors/code-rajeshdeb)

**Accepting donations via:**
- GitHub Sponsors (coming soon)
- [Ko-fi](https://ko-fi.com/yourname) ☕
- [Buy Me a Coffee](https://buymeacoffee.com/yourname) 💙

---

## 🙏 Acknowledgments

- **FastAPI** - Lightning-fast API framework
- **Slack SDK** - Excellent Python library for Slack integration
- **Argo Workflows** - Kubernetes-native workflow engine
- **HTMX** - Simplicity in interactive UIs

---

## 📞 Contact & Support

- **GitHub Issues**: [Report bugs or request features](https://github.com/code-rajeshdeb/approval-handler/issues)
- **Discussions**: [Ask questions or share ideas](https://github.com/code-rajeshdeb/approval-handler/discussions)
- **Email**: your-email@example.com

---

## 🗺️ Roadmap

- [ ] **v2.0**: Multi-cloud support (AWS IAM, Azure RBAC)
- [ ] **v2.1**: MS Teams integration
- [ ] **v2.2**: Self-service role templates
- [ ] **v2.3**: Compliance reports (SOC2, ISO 27001)
- [ ] **v2.4**: Machine learning for anomaly detection
- [ ] **v3.0**: SaaS version (managed service)

---

**Made with ❤️ by DevOps engineers, for DevOps engineers.**

**⭐ Star this repo if it saves you time!**
