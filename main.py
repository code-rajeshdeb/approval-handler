"""
Approval Handler Service
========================
FastAPI backend for the Argo Access Control approval system.

Components:
  - Rule Engine: priority-based policy evaluator
  - Slack Integration: slash commands, interactive buttons, modals
  - Argo API Client: submit/resume/stop workflows
  - Admin UI: Jinja2 + HTMX for rule/group/request management
  - Auth: HTTP Basic Auth (credentials from OpenBao)
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Optional

import base64

import httpx
import psycopg2
import psycopg2.extras
import psycopg2.pool
from fastapi import (
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel
from pydantic_settings import BaseSettings
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("approval-handler")

# ============================================================
# Settings
# ============================================================


class Settings(BaseSettings):
    db_host: str = "10.0.0.100"  # Cloud SQL (migrated from CNPG 2026-05-28)
    db_port: int = 5432
    db_name: str = "access_control"
    db_user: str = "access_control"
    db_password: str = ""

    slack_bot_token: str = ""
    slack_signing_secret: str = ""
    slack_channel: str = "#access-request"

    admin_username: str = "admin"
    admin_password: str = ""
    secret_key: str = "change-me-in-production"

    force_approve_password: str = ""

    argo_namespace: str = "argo-access-control"
    argo_workflow_template: str = "manage-iam-permissions"

    argo_ui_url: str = "https://workflow.example.com"

    model_config = {"env_prefix": "", "case_sensitive": False}


settings = Settings()

# ============================================================
# GCP Role Catalog — Service Category → IAM Roles
# ============================================================
# Each service category maps to a list of curated GCP IAM roles.
# The modal dynamically populates the "IAM Role" dropdown based on
# the selected service category.
#
# Fields:
#   label:              Display name in Slack dropdown
#   resource_level:     Whether resource-level scoping is supported
#   resource_placeholder: Placeholder text for resource name field
#   roles:              List of {display, value} for each IAM role

GCP_ROLE_CATALOG = {
    "basic": {
        "label": "Basic",
        "resource_level": False,
        "roles": [
            {"display": "Browser", "value": "roles/browser"},
            {"display": "Viewer", "value": "roles/viewer"},
            {"display": "Editor", "value": "roles/editor"},
            {"display": "Owner", "value": "roles/owner"},
        ],
    },
    "bigquery": {
        "label": "BigQuery",
        "resource_level": True,
        "resource_placeholder": "Dataset name (e.g., my_dataset) — leave 'global' for project-level",
        "roles": [
            {"display": "BigQuery Data Viewer", "value": "roles/bigquery.dataViewer"},
            {"display": "BigQuery Data Editor", "value": "roles/bigquery.dataEditor"},
            {"display": "BigQuery Data Owner", "value": "roles/bigquery.dataOwner"},
            {"display": "BigQuery Job User", "value": "roles/bigquery.jobUser"},
            {"display": "BigQuery User", "value": "roles/bigquery.user"},
            {"display": "BigQuery Admin", "value": "roles/bigquery.admin"},
        ],
    },
    "gcs-bucket": {
        "label": "Cloud Storage",
        "resource_level": True,
        "resource_placeholder": "Bucket name (e.g., my-data-bucket) — leave 'global' for project-level",
        "roles": [
            {"display": "Storage Object Viewer", "value": "roles/storage.objectViewer"},
            {"display": "Storage Object Creator", "value": "roles/storage.objectCreator"},
            {"display": "Storage Object Admin", "value": "roles/storage.objectAdmin"},
            {"display": "Storage Admin", "value": "roles/storage.admin"},
            {"display": "Storage HMAC Key Admin", "value": "roles/storage.hmacKeyAdmin"},
        ],
    },
    "compute-vm": {
        "label": "Compute Engine",
        "resource_level": True,
        "resource_placeholder": "VM instance name (e.g., prod-api-server) — leave 'global' for project-level",
        "roles": [
            {"display": "Compute Viewer", "value": "roles/compute.viewer"},
            {"display": "Compute Instance Admin", "value": "roles/compute.instanceAdmin.v1"},
            {"display": "Compute Network Viewer", "value": "roles/compute.networkViewer"},
            {"display": "Compute Network Admin", "value": "roles/compute.networkAdmin"},
            {"display": "Compute OS Login", "value": "roles/compute.osLogin"},
            {"display": "Compute OS Admin Login", "value": "roles/compute.osAdminLogin"},
            {"display": "Compute Admin", "value": "roles/compute.admin"},
        ],
    },
    "gke-cluster": {
        "label": "Kubernetes Engine",
        "resource_level": False,
        "roles": [
            {"display": "GKE Cluster Viewer", "value": "roles/container.clusterViewer"},
            {"display": "GKE Viewer", "value": "roles/container.viewer"},
            {"display": "GKE Developer", "value": "roles/container.developer"},
            {"display": "GKE Cluster Admin", "value": "roles/container.clusterAdmin"},
            {"display": "GKE Admin", "value": "roles/container.admin"},
        ],
    },
    "cloud-sql": {
        "label": "Cloud SQL",
        "resource_level": True,
        "resource_placeholder": "Instance name (e.g., prod-db) — leave 'global' for project-level",
        "roles": [
            {"display": "Cloud SQL Viewer", "value": "roles/cloudsql.viewer"},
            {"display": "Cloud SQL Client", "value": "roles/cloudsql.client"},
            {"display": "Cloud SQL Instance User", "value": "roles/cloudsql.instanceUser"},
            {"display": "Cloud SQL Editor", "value": "roles/cloudsql.editor"},
            {"display": "Cloud SQL Admin", "value": "roles/cloudsql.admin"},
        ],
    },
    "service-account": {
        "label": "IAM / Service Accounts",
        "resource_level": True,
        "resource_placeholder": "SA email (e.g., my-sa@project.iam.gserviceaccount.com) — leave 'global' for project-level",
        "roles": [
            {"display": "Service Account Viewer", "value": "roles/iam.serviceAccountViewer"},
            {"display": "Service Account User", "value": "roles/iam.serviceAccountUser"},
            {"display": "Service Account Token Creator", "value": "roles/iam.serviceAccountTokenCreator"},
            {"display": "Service Account Key Admin", "value": "roles/iam.serviceAccountKeyAdmin"},
            {"display": "Service Account Admin", "value": "roles/iam.serviceAccountAdmin"},
            {"display": "Workload Identity User", "value": "roles/iam.workloadIdentityUser"},
        ],
    },
    "pubsub": {
        "label": "Pub/Sub",
        "resource_level": True,
        "resource_placeholder": "Topic or subscription name (e.g., order-events) — leave 'global' for project-level",
        "roles": [
            {"display": "Pub/Sub Viewer", "value": "roles/pubsub.viewer"},
            {"display": "Pub/Sub Publisher", "value": "roles/pubsub.publisher"},
            {"display": "Pub/Sub Subscriber", "value": "roles/pubsub.subscriber"},
            {"display": "Pub/Sub Editor", "value": "roles/pubsub.editor"},
            {"display": "Pub/Sub Admin", "value": "roles/pubsub.admin"},
        ],
    },
    "secret-manager": {
        "label": "Secret Manager",
        "resource_level": True,
        "resource_placeholder": "Secret name (e.g., api-credentials) — leave 'global' for project-level",
        "roles": [
            {"display": "Secret Manager Viewer", "value": "roles/secretmanager.viewer"},
            {"display": "Secret Manager Accessor", "value": "roles/secretmanager.secretAccessor"},
            {"display": "Secret Manager Version Adder", "value": "roles/secretmanager.secretVersionAdder"},
            {"display": "Secret Manager Admin", "value": "roles/secretmanager.admin"},
        ],
    },
    "artifact-registry": {
        "label": "Artifact Registry",
        "resource_level": True,
        "resource_placeholder": "Repository name (e.g., docker-images) — leave 'global' for project-level",
        "roles": [
            {"display": "Artifact Registry Reader", "value": "roles/artifactregistry.reader"},
            {"display": "Artifact Registry Writer", "value": "roles/artifactregistry.writer"},
            {"display": "Artifact Registry Repo Admin", "value": "roles/artifactregistry.repoAdmin"},
            {"display": "Artifact Registry Admin", "value": "roles/artifactregistry.admin"},
        ],
    },
    "cloud-logging": {
        "label": "Cloud Logging",
        "resource_level": False,
        "roles": [
            {"display": "Logs Viewer", "value": "roles/logging.viewer"},
            {"display": "Logs Writer", "value": "roles/logging.logWriter"},
            {"display": "Logging Config Writer", "value": "roles/logging.configWriter"},
            {"display": "Logging Admin", "value": "roles/logging.admin"},
        ],
    },
    "cloud-monitoring": {
        "label": "Cloud Monitoring",
        "resource_level": False,
        "roles": [
            {"display": "Monitoring Viewer", "value": "roles/monitoring.viewer"},
            {"display": "Monitoring Metric Writer", "value": "roles/monitoring.metricWriter"},
            {"display": "Monitoring Editor", "value": "roles/monitoring.editor"},
            {"display": "Monitoring Admin", "value": "roles/monitoring.admin"},
        ],
    },
    "cloud-composer": {
        "label": "Cloud Composer",
        "resource_level": False,
        "roles": [
            {"display": "Composer User", "value": "roles/composer.user"},
            {"display": "Composer Worker", "value": "roles/composer.worker"},
            {"display": "Composer Admin", "value": "roles/composer.admin"},
        ],
    },
    "custom": {
        "label": "Custom IAM Role",
        "resource_level": True,
        "resource_placeholder": "Resource name (optional) — leave 'global' for project-level",
        "roles": [],  # Custom uses free-text input
    },
}


def _load_role_catalog_from_db() -> dict:
    """Load the IAM role catalog from DB, falling back to GCP_ROLE_CATALOG dict."""
    try:
        cats = db_fetchall(
            "SELECT category_key, label, resource_level, resource_placeholder FROM service_categories WHERE enabled = TRUE ORDER BY sort_order, label"
        )
        if not cats:
            return GCP_ROLE_CATALOG  # DB not seeded yet — use fallback
        catalog = {}
        for c in cats:
            roles = db_fetchall(
                "SELECT display_name, role_value, description FROM iam_roles WHERE category_key = %s AND enabled = TRUE ORDER BY sort_order, display_name",
                (c["category_key"],),
            )
            catalog[c["category_key"]] = {
                "label": c["label"],
                "resource_level": c["resource_level"],
                "resource_placeholder": c.get("resource_placeholder"),
                "roles": [{"display": r["display_name"], "value": r["role_value"], "description": r.get("description", "")} for r in roles],
            }
        return catalog
    except Exception:
        logger.warning("Failed to load role catalog from DB, using fallback")
        return GCP_ROLE_CATALOG


def _get_role_display(role_value: str) -> str:
    """Look up the display name for a GCP IAM role value from the catalog."""
    catalog = _load_role_catalog_from_db()
    for svc in catalog.values():
        for r in svc.get("roles", []):
            if r["value"] == role_value:
                return r["display"]
    return role_value  # fallback: show the raw role


def _get_service_label(service_key: str) -> str:
    """Get display label for a service category key."""
    catalog = _load_role_catalog_from_db()
    svc = catalog.get(service_key)
    return svc["label"] if svc else service_key


def _get_role_description(role_value: str) -> str:
    """Look up the description for a GCP IAM role value from the catalog."""
    catalog = _load_role_catalog_from_db()
    for svc in catalog.values():
        for r in svc.get("roles", []):
            if r["value"] == role_value:
                return r.get("description", "")
    return ""


def _get_team_approvers(team_name: str) -> tuple:
    """Resolve team → lead_group → approver members.
    Returns (group_name, [display_names]) or (None, []).
    """
    if not team_name:
        return None, []
    team_row = db_fetchone(
        "SELECT lead_group FROM teams WHERE team_name = %s", (team_name,)
    )
    if not team_row or not team_row.get("lead_group"):
        return None, []
    group_name = team_row["lead_group"]
    members = db_fetchall(
        """SELECT display_name, email FROM approver_group_members m
           JOIN approver_groups g ON m.group_id = g.id
           WHERE g.group_name = %s ORDER BY display_name""",
        (group_name,),
    )
    names = [m.get("display_name") or m["email"] for m in members]
    return group_name, names


# ============================================================
# Database
# ============================================================

_db_pool = None


def get_pool():
    global _db_pool
    if _db_pool is None:
        _db_pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=10,
            host=settings.db_host,
            port=settings.db_port,
            dbname=settings.db_name,
            user=settings.db_user,
            password=settings.db_password,
        )
    return _db_pool


@contextmanager
def get_db():
    pool = get_pool()
    conn = pool.getconn()
    try:
        conn.autocommit = False
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def db_fetchall(query, params=None):
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            return cur.fetchall()


def db_fetchone(query, params=None):
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            return cur.fetchone()


def db_execute(query, params=None):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.rowcount


# ============================================================
# Rule Engine
# ============================================================


def evaluate_rules(request_data: dict) -> Optional[dict]:
    """Evaluate request against enabled rules in priority order.
    Returns the first matching rule, or None if no rules match.
    """
    rules = db_fetchall(
        "SELECT * FROM approval_rules WHERE enabled = TRUE ORDER BY priority ASC"
    )

    for rule in rules:
        if _rule_matches(rule, request_data):
            return dict(rule)
    return None


def _rule_matches(rule: dict, req: dict) -> bool:
    """Check if a single rule matches the request. NULL fields match anything."""
    checks = [
        ("requester_email", req.get("requester_email", "")),
        ("who_type", req.get("who_type", "")),
        ("resource_type", req.get("resource_type", "")),
        ("permission_level", req.get("permission_level", "")),
        ("gcp_project", req.get("gcp_project", "")),
        ("action", req.get("action", "")),
        ("access_type", req.get("access_type", "")),
    ]

    for field, value in checks:
        rule_val = rule.get(field)
        if rule_val is None:
            continue  # NULL = match any
        # Support comma-separated OR match and wildcard prefix match
        allowed = [v.strip() for v in rule_val.split(",")]
        matched = False
        for pattern in allowed:
            if pattern.endswith("*"):
                if value.startswith(pattern[:-1]):
                    matched = True
                    break
            elif value == pattern:
                matched = True
                break
        if not matched:
            return False
    return True


def determine_risk_level(rule: Optional[dict], req: dict) -> str:
    """Determine risk level based on matched rule and request."""
    if rule and rule.get("approvers_required", 0) == 0:
        return "low"
    project = req.get("gcp_project", "")
    permission = req.get("permission_level", "")
    if "prod" in project or project == "your-gcp-project-data":
        if permission in ("admin", "editor", "custom"):
            return "high"
        return "medium"
    return "medium"


# ============================================================
# Slack Helpers
# ============================================================

_slack_client = None


def get_slack_client() -> WebClient:
    global _slack_client
    if _slack_client is None:
        _slack_client = WebClient(token=settings.slack_bot_token)
    return _slack_client


def verify_slack_signature(timestamp: str, body: bytes, signature: str) -> bool:
    """Verify Slack request signature using signing secret."""
    if not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - ts) > 300:
        return False
    sig_basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
    my_sig = (
        "v0="
        + hmac.new(
            settings.slack_signing_secret.encode(),
            sig_basestring.encode(),
            hashlib.sha256,
        ).hexdigest()
    )
    return hmac.compare_digest(my_sig, signature)


def resolve_slack_user_id(email: str) -> Optional[str]:
    """Lookup Slack user ID from email address."""
    try:
        resp = get_slack_client().users_lookupByEmail(email=email)
        return resp["user"]["id"]
    except SlackApiError:
        return None


def _build_who_block(who_type: str = "user", requester_email: str = "", is_proxy: bool = False) -> list:
    """Return the appropriate 'who' input block(s) based on who_type and proxy status.

    Returns a list of blocks (1 for most cases, 1 section for locked user).
    """
    if who_type == "group":
        return [{
            "type": "input",
            "block_id": "who_email_block",
            "label": {"type": "plain_text", "text": "Group"},
            "element": {
                "type": "plain_text_input",
                "action_id": "who_email",
                "placeholder": {
                    "type": "plain_text",
                    "text": "my-team@example.com",
                },
            },
        }]
    elif who_type == "serviceAccount":
        return [{
            "type": "input",
            "block_id": "who_email_block",
            "label": {"type": "plain_text", "text": "Service Account"},
            "element": {
                "type": "plain_text_input",
                "action_id": "who_email",
                "placeholder": {
                    "type": "plain_text",
                    "text": "sa-name@project.iam.gserviceaccount.com",
                },
            },
        }]
    else:  # "user"
        if is_proxy:
            # Privileged user — show Slack user picker dropdown
            return [{
                "type": "input",
                "block_id": "who_email_block",
                "label": {"type": "plain_text", "text": "User"},
                "element": {
                    "type": "users_select",
                    "action_id": "who_email",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Select a user",
                    },
                },
            }]
        else:
            # Non-privileged user — show locked read-only display
            display_email = requester_email or "your-email@example.com"
            return [{
                "type": "section",
                "block_id": "who_email_display_block",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*User*\n📧 {display_email} _(auto-selected)_",
                },
            }]


def _get_team_options() -> list:
    """Fetch teams from DB and return Slack option list."""
    teams = db_fetchall("SELECT team_name FROM teams ORDER BY team_name")
    return [_slack_option(t["team_name"], t["team_name"]) for t in teams]


def _is_proxy_requester(email: str) -> bool:
    """Check if email belongs to the proxy-requesters group."""
    row = db_fetchone(
        """SELECT 1 FROM approver_group_members m
           JOIN approver_groups g ON g.id = m.group_id
           WHERE g.group_name = 'proxy-requesters' AND m.email = %s""",
        (email,),
    )
    return row is not None


def _build_modal_blocks(who_type: str = "user", service_category: str = "basic", access_type: str = "temporary", requester_email: str = "", is_proxy: bool = False, selected_team: str = "", selected_role: str = "", vault_operation: str = "add-or-update-key") -> list:
    """Build all modal blocks for the access request form.

    The service_category parameter drives the IAM Role dropdown dynamically:
    when the user picks a service category, the modal rebuilds with the
    corresponding roles from the DB-driven catalog (falling back to GCP_ROLE_CATALOG).
    """
    who_type_labels = {
        "user": "User", "group": "Group",
        "serviceAccount": "Service Account",
    }
    team_options = _get_team_options()
    team_block = {
        "type": "input",
        "block_id": "team_block",
        "dispatch_action": True,
        "label": {"type": "plain_text", "text": "Approval Team"},
        "element": {
            "type": "static_select",
            "action_id": "team",
            "placeholder": {"type": "plain_text", "text": "Select approval team"},
            "options": team_options if team_options else [_slack_option("Other", "Other")],
        },
    }
    # Set initial_option for team if selected_team is provided
    if selected_team:
        for opt in (team_options or []):
            if opt["value"] == selected_team:
                team_block["element"]["initial_option"] = _slack_option(opt["text"]["text"], selected_team)
                break

    who_type_block = {
        "type": "input",
        "block_id": "who_type_block",
        "dispatch_action": True,
        "label": {"type": "plain_text", "text": "Request Type"},
        "element": {
            "type": "static_select",
            "action_id": "who_type",
            "options": [
                _slack_option("User", "user"),
                _slack_option("Group", "group"),
                _slack_option("Service Account", "serviceAccount"),
            ],
            "initial_option": _slack_option(
                who_type_labels.get(who_type, "User"),
                who_type,
            ),
        },
    }

    # ── Vault Secret mode: show vault-specific fields ──
    if who_type == "vault-secret":
        # Map vault_operation to display label for initial_option
        _vault_op_labels = {
            "create-new-path": "Create New Path",
            "add-or-update-key": "Add or Update Key",
            "read-keys-only": "Read Keys Only",
        }
        vault_op_label = _vault_op_labels.get(vault_operation, "Add or Update Key")

        blocks = [
            team_block,
            who_type_block,
            {
                "type": "input",
                "block_id": "vault_operation_block",
                "dispatch_action": True,
                "label": {"type": "plain_text", "text": "Operation"},
                "element": {
                    "type": "static_select",
                    "action_id": "vault_operation",
                    "options": [
                        _slack_option("Create New Path", "create-new-path"),
                        _slack_option("Add or Update Key", "add-or-update-key"),
                        _slack_option("Read Keys Only", "read-keys-only"),
                    ],
                    "initial_option": _slack_option(vault_op_label, vault_operation),
                },
            },
            {
                "type": "input",
                "block_id": "vault_path_block",
                "label": {"type": "plain_text", "text": "Secret Path"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "vault_path",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "env/prod/my-service/db-credentials",
                    },
                },
            },
        ]

        # Key and Value fields only for write operations (up to 5 pairs)
        if vault_operation != "read-keys-only":
            for i in range(1, 6):
                suffix = "" if i == 1 else str(i)
                label_num = f" #{i}"
                is_first = i == 1
                blocks.append({
                    "type": "input",
                    "block_id": f"vault_key{suffix}_block",
                    "optional": True,
                    "label": {"type": "plain_text", "text": f"Key{label_num}"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": f"vault_key{suffix}",
                        "placeholder": {
                            "type": "plain_text",
                            "text": "DB_PASSWORD" if is_first else f"(optional) e.g. DB_HOST",
                        },
                    },
                })
                blocks.append({
                    "type": "input",
                    "block_id": f"vault_value{suffix}_block",
                    "optional": True,
                    "label": {"type": "plain_text", "text": f"Value{label_num}"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": f"vault_value{suffix}",
                        "placeholder": {
                            "type": "plain_text",
                            "text": "Enter secret value" if is_first else "(optional)",
                        },
                    },
                })

        blocks.extend([
            {
                "type": "input",
                "block_id": "reason_block",
                "optional": True,
                "label": {"type": "plain_text", "text": "Reason"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "reason",
                    "multiline": True,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Why is this secret change needed?",
                    },
                },
            },
            {
                "type": "input",
                "block_id": "ticket_block",
                "label": {"type": "plain_text", "text": "Ticket ID"},
                "optional": True,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "ticket",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "ARC-1234 (optional)",
                    },
                },
            },
        ])
        return blocks

    # ── IAM mode: GCP Console-style Service Category + IAM Role ──

    catalog = _load_role_catalog_from_db()

    # If the requested service_category is not in the (enabled) catalog,
    # fall back to the first available category to avoid Slack
    # initial_option-not-in-options errors.
    if service_category not in catalog and catalog:
        service_category = next(iter(catalog))
    svc_info = catalog.get(service_category, catalog.get("basic", GCP_ROLE_CATALOG["basic"]))

    # Build service category dropdown options from DB catalog
    svc_options = [
        _slack_option(info["label"], key)
        for key, info in catalog.items()
    ]
    svc_initial = _slack_option(svc_info["label"], service_category)

    blocks = [
        team_block,
    ]

    # Approver preview: show who will approve when a team is selected
    if selected_team:
        group_name, approver_names = _get_team_approvers(selected_team)
        if approver_names:
            blocks.append({
                "type": "context",
                "block_id": "approver_preview_block",
                "elements": [{"type": "mrkdwn", "text": f"👥 *Approvers:* {', '.join(approver_names)}\n_Group: {group_name}_"}],
            })
        else:
            blocks.append({
                "type": "context",
                "block_id": "approver_preview_block",
                "elements": [{"type": "mrkdwn", "text": "👥 *Approvers:* Will be determined by matching approval rule"}],
            })

    blocks.extend([
        who_type_block,
        *_build_who_block(who_type, requester_email=requester_email, is_proxy=is_proxy),
        {
            "type": "input",
            "block_id": "service_category_block",
            "dispatch_action": True,
            "label": {"type": "plain_text", "text": "Service Category"},
            "element": {
                "type": "static_select",
                "action_id": "service_category",
                "options": svc_options,
                "initial_option": svc_initial,
            },
        },
    ])

    # IAM Role: dropdown for catalog services, free-text for custom
    if service_category == "custom":
        blocks.append({
            "type": "input",
            "block_id": "custom_role_block",
            "label": {"type": "plain_text", "text": "Custom IAM Role / Permission"},
            "element": {
                "type": "plain_text_input",
                "action_id": "custom_role",
                "placeholder": {
                    "type": "plain_text",
                    "text": "roles/storage.objectViewer, roles/bigquery.jobUser, or cloudsql.instances.executeSql",
                },
            },
        })
    else:
        role_options = [
            _slack_option(r["display"], r["value"])
            for r in svc_info.get("roles", [])
        ]
        if role_options:
            # Determine initial selection for IAM role
            initial_role = role_options[0]
            if selected_role:
                for opt in role_options:
                    if opt["value"] == selected_role:
                        initial_role = opt
                        break
            blocks.append({
                "type": "input",
                "block_id": "iam_role_block",
                "dispatch_action": True,
                "label": {"type": "plain_text", "text": "IAM Role"},
                "element": {
                    "type": "static_select",
                    "action_id": "iam_role",
                    "options": role_options,
                    "initial_option": initial_role,
                },
            })
            # Show role description for selected role
            active_role_value = initial_role["value"]
            role_desc = _get_role_description(active_role_value)
            if role_desc:
                blocks.append({
                    "type": "context",
                    "block_id": "role_description_block",
                    "elements": [{"type": "mrkdwn", "text": f"ℹ️ {role_desc}"}],
                })

    # Resource Name: shown for all who_types when service supports resource-level scoping
    if svc_info.get("resource_level"):
        blocks.append({
            "type": "input",
            "block_id": "resource_name_block",
            "label": {"type": "plain_text", "text": "Resource Name"},
            "optional": True,
            "element": {
                "type": "plain_text_input",
                "action_id": "resource_name",
                "placeholder": {
                    "type": "plain_text",
                    "text": svc_info.get("resource_placeholder", "e.g. my-dataset, my-bucket (leave blank for project-level)"),
                },
            },
        })

    # GCP Project, Access Type, Reason, Ticket
    blocks.extend([
        {
            "type": "input",
            "block_id": "project_block",
            "label": {"type": "plain_text", "text": "GCP Project"},
            "element": {
                "type": "static_select",
                "action_id": "gcp_project",
                "options": [
                    _slack_option("Stage (example-company-stage)", "your-gcp-project-stage"),
                    _slack_option("Prod (example-company-prod)", "your-gcp-project-prod"),
                    _slack_option("Data (your-gcp-project-data)", "your-gcp-project-data"),
                ],
                "initial_option": _slack_option(
                    "Stage (example-company-stage)", "your-gcp-project-stage"
                ),
            },
        },
        {
            "type": "input",
            "block_id": "access_type_block",
            "dispatch_action": True,
            "label": {"type": "plain_text", "text": "Access Type"},
            "element": {
                "type": "static_select",
                "action_id": "access_type",
                "options": [
                    _slack_option("Temporary (auto-revoked)", "temporary"),
                    _slack_option("Permanent", "permanent"),
                ],
                "initial_option": _slack_option(
                    {"temporary": "Temporary (auto-revoked)", "permanent": "Permanent"}.get(access_type, "Temporary (auto-revoked)"),
                    access_type,
                ),
            },
        },
    ])

    # Conditionally add expiry hours when access_type=temporary
    if access_type == "temporary":
        blocks.append({
            "type": "input",
            "block_id": "expiry_block",
            "label": {"type": "plain_text", "text": "Expiry Hours"},
            "element": {
                "type": "static_select",
                "action_id": "expiry_hours",
                "options": [
                    _slack_option(f"{h} hours", str(h))
                    for h in [1, 2, 3, 4, 5, 6, 8, 12, 16, 24]
                ],
                "initial_option": _slack_option("4 hours", "4"),
            },
        })

    blocks.extend([
        {
            "type": "input",
            "block_id": "reason_block",
            "optional": True,
            "label": {"type": "plain_text", "text": "Reason"},
            "element": {
                "type": "plain_text_input",
                "action_id": "reason",
                "multiline": True,
                "placeholder": {
                    "type": "plain_text",
                    "text": "Why is this access needed?",
                },
            },
        },
        {
            "type": "input",
            "block_id": "ticket_block",
            "label": {"type": "plain_text", "text": "Ticket ID"},
            "optional": True,
            "element": {
                "type": "plain_text_input",
                "action_id": "ticket",
                "placeholder": {
                    "type": "plain_text",
                    "text": "ARC-1234 (optional)",
                },
            },
        },
    ])

    return blocks


def build_access_request_modal(trigger_id: str, who_type: str = "user", requester_email: str = "", is_proxy: bool = False, source_channel: str = ""):
    """Open Slack modal for /access-request slash command."""
    modal = {
        "type": "modal",
        "callback_id": "access_request_modal",
        "private_metadata": json.dumps({"requester_email": requester_email, "is_proxy": is_proxy, "source_channel": source_channel}),
        "title": {"type": "plain_text", "text": "Access Request"},
        "submit": {"type": "plain_text", "text": "Submit"},
        "blocks": _build_modal_blocks(who_type, requester_email=requester_email, is_proxy=is_proxy),
    }
    get_slack_client().views_open(trigger_id=trigger_id, view=modal)


def _slack_option(text: str, value: str) -> dict:
    return {"text": {"type": "plain_text", "text": text}, "value": value}


def build_approval_message(
    request_data: dict, rule: dict, risk: str, request_id: str, level: int = 1, total_levels: int = 1, channel: str = "", show_secrets: bool = False
) -> dict:
    """Build Slack Block Kit message for approval request."""
    if level == 1:
        approver_group = rule.get("lead_group")
    else:
        approver_group = rule.get("second_approval_group") or rule.get("lead_group")
    group_label = approver_group or "final-approvers"

    # Resolve approver mentions
    members = db_fetchall(
        """SELECT email, slack_user_id, display_name
           FROM approver_group_members m
           JOIN approver_groups g ON m.group_id = g.id
           WHERE g.group_name = %s""",
        (group_label,),
    )
    mentions = []
    for m in members:
        if m["slack_user_id"]:
            mentions.append(f"<@{m['slack_user_id']}>")
        else:
            mentions.append(m["email"])
    approvers_text = " ".join(mentions) if mentions else group_label

    risk_emoji = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(risk, "⚪")

    level_text = f"Level {level} of {total_levels}"
    level_label = "Team Lead Approval" if level == 1 and total_levels == 2 else "Final Approval"

    is_vault = request_data.get("request_type") == "vault"
    is_delete_op = request_data.get("vault_operation") == "delete-keys"

    if is_vault:
        # Vault secret approval message (supports up to 10 KV pairs or delete-keys)
        vault_keys = request_data.get("vault_keys", [])
        if not vault_keys and request_data.get("vault_key"):
            # Try JSON array first (v2.4.5+), then fallback to comma-split (legacy)
            try:
                vault_keys = json.loads(request_data["vault_key"])
            except (json.JSONDecodeError, TypeError):
                vault_keys = [k.strip() for k in request_data["vault_key"].split(",") if k.strip()]
        kv_count = len(vault_keys) if vault_keys else 0

        if is_delete_op:
            # Delete-keys: show keys with 🗑️ badge, no values
            keys_display = ", ".join(f"🗑️ `{k}`" for k in vault_keys) if vault_keys else "`N/A`"
            values_display = "N/A (delete operation)"
        elif show_secrets:
            # DM to approvers: show actual keys and values
            keys_display = ", ".join(f"`{k}`" for k in vault_keys) if vault_keys else "`N/A`"
            # Parse actual values from vault_values list or vault_value JSON
            vault_values = request_data.get("vault_values", [])
            if not vault_values and request_data.get("vault_value"):
                try:
                    vault_values = json.loads(request_data["vault_value"])
                except (json.JSONDecodeError, TypeError):
                    vault_values = []
            values_display = ", ".join(f"`{v}`" for v in vault_values) if vault_values else "••••••••"
        else:
            # Channel message: show keys, mask values only
            keys_display = ", ".join(f"`{k}`" for k in vault_keys) if vault_keys else "`N/A`"
            values_display = "••••••••" if kv_count > 0 else "N/A"

        op_display = request_data.get('vault_operation', 'N/A')
        if is_delete_op:
            op_display = "🗑️ delete-keys"

        detail_fields = [
            {"type": "mrkdwn", "text": f"*Requester:*\n{request_data.get('requester_email', 'N/A')}"},
            {"type": "mrkdwn", "text": f"*Type:*\n🔑 Vault Secret"},
            {"type": "mrkdwn", "text": f"*Operation:*\n{op_display}"},
            {"type": "mrkdwn", "text": f"*Path:*\n`{request_data.get('vault_path', 'N/A')}`"},
            {"type": "mrkdwn", "text": f"*Keys ({kv_count}):*\n{keys_display}"},
            {"type": "mrkdwn", "text": f"*Values:*\n{values_display}"},
            {"type": "mrkdwn", "text": f"*Risk:*\n{risk_emoji} {risk.title()}"},
        ]
        header_text = "🗑️ Vault Key Deletion - Approval Required" if is_delete_op else "🔑 Vault Secret Request - Approval Required"
    else:
        # Standard IAM approval message — v2.0.0 role-aware display
        duration = (
            f"Temporary - {request_data.get('expiry_hours', 4)} hours"
            if request_data.get("access_type") == "temporary"
            else "Permanent"
        )

        # Resolve human-friendly labels from the catalog
        svc_key = request_data.get("resource_type", "basic")
        svc_label = _get_service_label(svc_key)
        custom_role = request_data.get("custom_role", "")
        role_display = _get_role_display(custom_role) if custom_role else request_data.get("permission_level", "viewer")
        resource_name = request_data.get("resource_name", "global")
        resource_text = f"{svc_label}: {resource_name}" if resource_name and resource_name != "global" else f"{svc_label} (project-level)"

        detail_fields = [
            {"type": "mrkdwn", "text": f"*Requester:*\n{request_data.get('requester_email', 'N/A')}"},
            {"type": "mrkdwn", "text": f"*Action:*\n{request_data.get('action', 'grant').title()}"},
            {"type": "mrkdwn", "text": f"*Target:*\n{request_data.get('who_type', 'user')}: {request_data.get('who_email', 'N/A')}"},
            {"type": "mrkdwn", "text": f"*Service:*\n{resource_text}"},
            {"type": "mrkdwn", "text": f"*IAM Role:*\n{role_display}\n`{custom_role}`" if custom_role else f"*Permission:*\n{role_display}"},
            {"type": "mrkdwn", "text": f"*Project:*\n{request_data.get('gcp_project', 'N/A')}"},
            {"type": "mrkdwn", "text": f"*Duration:*\n{duration}"},
            {"type": "mrkdwn", "text": f"*Risk:*\n{risk_emoji} {risk.title()}"},
        ]
        header_text = "🔐 Access Request - Approval Required"

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": header_text,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{level_text} - {level_label}*",
            },
        },
        {
            "type": "section",
            "fields": detail_fields,
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Reason:*\n{request_data.get('reason', 'N/A')}"},
                {"type": "mrkdwn", "text": f"*Ticket:*\n{request_data.get('ticket') or 'N/A'}"},
            ],
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Rule:*\n{rule.get('name', 'N/A')} (priority {rule.get('priority', 'N/A')})"},
                {"type": "mrkdwn", "text": f"*Approver Group:*\n{group_label}"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Approvers:* {approvers_text}"},
        },
        {
            "type": "actions",
            "block_id": "approval_actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Approve"},
                    "style": "primary",
                    "action_id": "approve_request",
                    "value": json.dumps(
                        {
                            "request_id": request_id,
                            "level": level,
                            "total_levels": total_levels,
                            "approver_group": group_label,
                        }
                    ),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌ Reject"},
                    "style": "danger",
                    "action_id": "reject_request",
                    "value": json.dumps(
                        {
                            "request_id": request_id,
                            "level": level,
                            "total_levels": total_levels,
                            "approver_group": group_label,
                        }
                    ),
                },
            ],
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"⏱ Timeout: 4 hours | Request: {request_id} | <{settings.argo_ui_url}|Argo UI>",
                },
            ],
        },
    ]
    return {"channel": channel or settings.slack_channel, "blocks": blocks, "text": "Access Request - Approval Required"}


# ============================================================
# Approver DM Notifications
# ============================================================


def _send_approver_dms(
    request_id: str,
    approver_group: str,
    blocks: list,
    text: str,
    approval_level: int = 1,
):
    """Send DM to each member of the approver group with the same approval message + buttons."""
    members = db_fetchall(
        """SELECT email, slack_user_id FROM approver_group_members m
           JOIN approver_groups g ON m.group_id = g.id
           WHERE g.group_name = %s""",
        (approver_group,),
    )

    client = get_slack_client()
    for m in members:
        slack_uid = m.get("slack_user_id")
        if not slack_uid:
            slack_uid = resolve_slack_user_id(m["email"])
        if not slack_uid:
            logger.warning(f"Cannot DM approver {m['email']}: no Slack user ID")
            continue

        try:
            # Open DM conversation with the approver
            dm_resp = client.conversations_open(users=[slack_uid])
            dm_channel = dm_resp["channel"]["id"]

            # Post the approval message in the DM
            msg_resp = client.chat_postMessage(
                channel=dm_channel,
                blocks=blocks,
                text=text,
            )

            # Store the DM reference for later updates
            db_execute(
                """INSERT INTO approval_dm_messages
                   (request_id, approver_email, approver_slack_id, dm_channel, dm_ts, approval_level)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (request_id, m["email"], slack_uid, dm_channel, msg_resp["ts"], approval_level),
            )
            logger.info(f"Sent approval DM to {m['email']} for {request_id}")
        except SlackApiError as e:
            logger.error(f"Failed to DM approver {m['email']}: {e}")


def _update_all_approval_messages(
    request_id: str,
    approval_level: int,
    updated_blocks: list,
    updated_text: str,
    exclude_channel: str = "",
    exclude_ts: str = "",
    dm_blocks: list = None,
    dm_text: str = None,
):
    """Update the channel message AND all approver DMs for a request.

    The exclude_channel/exclude_ts params prevent double-updating the message
    that was already updated by the caller's chat_update() call.

    If dm_blocks/dm_text are provided, DMs use those instead of updated_blocks/updated_text.
    This ensures vault secret values are only visible in DMs, never leaked to channels.
    """
    client = get_slack_client()

    # 1. Update channel message (if not the one already updated)
    #    Always uses updated_blocks which should have secrets masked for vault requests
    req_row = db_fetchone(
        "SELECT slack_channel, slack_ts FROM access_requests WHERE request_id=%s",
        (request_id,),
    )
    if req_row and req_row.get("slack_channel") and req_row.get("slack_ts"):
        if not (req_row["slack_channel"] == exclude_channel and req_row["slack_ts"] == exclude_ts):
            try:
                client.chat_update(
                    channel=req_row["slack_channel"],
                    ts=req_row["slack_ts"],
                    blocks=updated_blocks,
                    text=updated_text,
                )
            except SlackApiError as e:
                logger.error(f"Failed to update channel msg for {request_id}: {e}")

    # 2. Update all DM messages for this request and level
    #    Use dm_blocks/dm_text if provided (vault requests keep secrets visible in DMs)
    actual_dm_blocks = dm_blocks if dm_blocks is not None else updated_blocks
    actual_dm_text = dm_text if dm_text is not None else updated_text
    dm_rows = db_fetchall(
        "SELECT dm_channel, dm_ts FROM approval_dm_messages WHERE request_id=%s AND approval_level=%s",
        (request_id, approval_level),
    )
    for dm in dm_rows:
        if dm["dm_channel"] == exclude_channel and dm["dm_ts"] == exclude_ts:
            continue  # already updated by the caller
        try:
            client.chat_update(
                channel=dm["dm_channel"],
                ts=dm["dm_ts"],
                blocks=actual_dm_blocks,
                text=actual_dm_text,
            )
        except SlackApiError as e:
            logger.error(f"Failed to update DM {dm['dm_channel']} for {request_id}: {e}")


# ============================================================
# Argo Workflow Client (via K8s API)
# ============================================================

K8S_API_BASE = "https://kubernetes.default.svc"
K8S_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
K8S_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"


def _get_k8s_token() -> str:
    """Read in-cluster service account token."""
    if os.path.exists(K8S_TOKEN_PATH):
        return Path(K8S_TOKEN_PATH).read_text().strip()
    return ""


def _k8s_headers() -> dict:
    token = _get_k8s_token()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def submit_workflow(parameters: dict, template_name: str | None = None) -> dict:
    """Submit an Argo workflow by creating a Workflow CR via the K8s API."""
    tpl = template_name or settings.argo_workflow_template
    url = f"{K8S_API_BASE}/apis/argoproj.io/v1alpha1/namespaces/{settings.argo_namespace}/workflows"
    params_list = [{"name": k, "value": str(v)} for k, v in parameters.items()]

    workflow = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Workflow",
        "metadata": {
            "generateName": f"{tpl}-",
            "namespace": settings.argo_namespace,
        },
        "spec": {
            "workflowTemplateRef": {
                "name": tpl,
            },
            "arguments": {
                "parameters": params_list,
            },
        },
    }

    async with httpx.AsyncClient(verify=K8S_CA_PATH, timeout=30) as client:
        resp = await client.post(url, json=workflow, headers=_k8s_headers())
        resp.raise_for_status()
        return resp.json()


# ============================================================
# Auth
# ============================================================

_serializer = None


def get_serializer():
    global _serializer
    if _serializer is None:
        _serializer = URLSafeTimedSerializer(settings.secret_key)
    return _serializer


def create_session_token(username: str) -> str:
    return get_serializer().dumps({"user": username})


def verify_session_token(token: str) -> Optional[str]:
    try:
        data = get_serializer().loads(token, max_age=86400)  # 24h
        return data.get("user")
    except (BadSignature, Exception):
        return None


def require_admin(request: Request):
    """Dependency: check session cookie or basic auth."""
    # Check session cookie
    session = request.cookies.get("session")
    if session:
        user = verify_session_token(session)
        if user:
            return user

    # Check basic auth header
    auth = request.headers.get("authorization", "")
    if auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[6:]).decode()
            username, password = decoded.split(":", 1)
            if (
                username == settings.admin_username
                and password == settings.admin_password
            ):
                return username
        except Exception:
            pass

    raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})


# ============================================================
# Pydantic Models
# ============================================================


class RuleCreate(BaseModel):
    name: str
    priority: int = 100
    enabled: bool = True
    requester_email: Optional[str] = None
    who_type: Optional[str] = None
    resource_type: Optional[str] = None
    permission_level: Optional[str] = None
    gcp_project: Optional[str] = None
    action: Optional[str] = None
    access_type: Optional[str] = None
    approvers_required: int = 1
    approval_levels: int = 1
    lead_group: Optional[str] = None
    second_approval_group: Optional[str] = None
    require_reason: bool = True
    require_ticket: bool = False
    ticket_pattern: Optional[str] = "ARC-"
    description: Optional[str] = None


class RuleUpdate(RuleCreate):
    pass


class VaultRequestBody(BaseModel):
    """Request body for vault secret operations submitted from vault-clone web UI."""
    requester_email: str
    vault_path: str
    vault_operation: str = "add-or-update-key"
    vault_keys: list[str]
    vault_values: list[str] = []
    source: str = "vault-clone"
    key_exists: bool = False
    team: str = ""


class GroupCreate(BaseModel):
    group_name: str
    description: Optional[str] = None


class MemberAdd(BaseModel):
    email: str
    display_name: Optional[str] = None


class EvaluateRequest(BaseModel):
    requester_email: str
    action: str = "grant"
    who_type: Optional[str] = None
    resource_type: Optional[str] = None
    permission_level: Optional[str] = None
    gcp_project: Optional[str] = None
    access_type: Optional[str] = None


# ============================================================
# Lifespan (replaces deprecated on_event)
# ============================================================


@asynccontextmanager
async def lifespan(the_app: FastAPI):
    """Application lifespan handler for startup/shutdown."""
    # Startup
    logger.info("Approval Handler starting up")
    try:
        get_pool()
        logger.info("Database connection pool initialized")
    except Exception as e:
        logger.warning(f"Could not connect to database on startup: {e}")
    # Start background Slack user sync (every 4 hours)
    sync_task = asyncio.create_task(_slack_user_sync_loop())
    yield
    # Shutdown
    sync_task.cancel()
    global _db_pool
    if _db_pool:
        _db_pool.closeall()
        _db_pool = None
    logger.info("Approval Handler shut down")


async def _slack_user_sync_loop():
    """Background loop: sync Slack users every 4 hours."""
    while True:
        try:
            await _sync_slack_users()
        except Exception as e:
            logger.error(f"Slack user sync failed: {e}")
        await asyncio.sleep(4 * 3600)  # 4 hours


async def _sync_slack_users():
    """Fetch all active Slack users with @example.com email and upsert into DB."""
    logger.info("Starting Slack user sync...")
    client = get_slack_client()
    cursor = None
    total = 0
    while True:
        resp = client.users_list(cursor=cursor, limit=200)
        members = resp.get("members", [])
        for m in members:
            if m.get("deleted") or m.get("is_bot"):
                continue
            profile = m.get("profile", {})
            email = profile.get("email", "")
            if not email or not email.endswith("@example.com"):
                continue
            db_execute(
                """INSERT INTO slack_users (slack_user_id, email, display_name, real_name, avatar_url, is_active, synced_at)
                   VALUES (%s, %s, %s, %s, %s, TRUE, NOW())
                   ON CONFLICT (slack_user_id) DO UPDATE SET
                     email = EXCLUDED.email,
                     display_name = EXCLUDED.display_name,
                     real_name = EXCLUDED.real_name,
                     avatar_url = EXCLUDED.avatar_url,
                     is_active = TRUE,
                     synced_at = NOW()""",
                (
                    m["id"],
                    email,
                    profile.get("display_name") or profile.get("real_name_normalized", ""),
                    profile.get("real_name", ""),
                    profile.get("image_72", ""),
                ),
            )
            total += 1
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    logger.info(f"Slack user sync complete: {total} users synced")


# ============================================================
# FastAPI App
# ============================================================

app = FastAPI(title="Approval Handler", version="1.0.0", lifespan=lifespan)

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

if (BASE_DIR / "static").exists():
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# ============================================================
# Health
# ============================================================


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/health/ready")
async def readiness():
    try:
        db_fetchone("SELECT 1")
        return {"status": "ready", "db": "connected"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DB not ready: {e}")


# ============================================================
# Slack Endpoints
# ============================================================


@app.post("/slack/commands")
async def slack_commands(request: Request):
    """Handle /access-request slash command."""
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    if settings.slack_signing_secret and not verify_slack_signature(
        timestamp, body, signature
    ):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    form = await request.form()
    command = form.get("command", "")
    trigger_id = form.get("trigger_id", "")

    if command == "/access-request":
        # Resolve requester email and check proxy-requesters group membership
        cmd_user_id = form.get("user_id", "")
        source_channel = form.get("channel_id", "")
        requester_email = ""
        if cmd_user_id:
            try:
                info = get_slack_client().users_info(user=cmd_user_id)
                requester_email = info["user"]["profile"].get("email", "")
            except SlackApiError:
                pass
        is_proxy = _is_proxy_requester(requester_email) if requester_email else False

        try:
            build_access_request_modal(trigger_id, requester_email=requester_email, is_proxy=is_proxy, source_channel=source_channel)
        except SlackApiError as e:
            logger.error(f"Failed to open modal: {e}")
            return JSONResponse(
                {"response_type": "ephemeral", "text": f"Error opening form: {e}"}
            )
        return JSONResponse({"response_type": "ephemeral", "text": ""})

    return JSONResponse(
        {"response_type": "ephemeral", "text": f"Unknown command: {command}"}
    )


@app.post("/slack/interactions")
async def slack_interactions(request: Request):
    """Handle Slack interactive components (buttons, modal submissions)."""
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    if settings.slack_signing_secret and not verify_slack_signature(
        timestamp, body, signature
    ):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    form = await request.form()
    payload = json.loads(form.get("payload", "{}"))
    interaction_type = payload.get("type", "")

    if interaction_type == "view_submission":
        return await _handle_modal_submission(payload)
    elif interaction_type == "block_actions":
        actions = payload.get("actions", [])
        action_id = actions[0].get("action_id", "") if actions else ""

        # Handle who_type, service_category, access_type, team, iam_role, or vault_operation change inside the modal
        if action_id in ("who_type", "service_category", "access_type", "team", "iam_role", "vault_operation") and payload.get("view"):
            view_id = payload["view"]["id"]
            view_hash = payload["view"]["hash"]
            # Read current state values to preserve selections across rebuilds
            state_values = payload.get("view", {}).get("state", {}).get("values", {})
            current_who_type = (
                (state_values.get("who_type_block", {})
                .get("who_type", {})
                .get("selected_option") or {})
                .get("value", "user")
            )
            current_service = (
                (state_values.get("service_category_block", {})
                .get("service_category", {})
                .get("selected_option") or {})
                .get("value", "basic")
            )
            current_access_type = (
                (state_values.get("access_type_block", {})
                .get("access_type", {})
                .get("selected_option") or {})
                .get("value", "temporary")
            )
            current_team = (
                (state_values.get("team_block", {})
                .get("team", {})
                .get("selected_option") or {})
                .get("value", "")
            )
            current_role = (
                (state_values.get("iam_role_block", {})
                .get("iam_role", {})
                .get("selected_option") or {})
                .get("value", "")
            )
            current_vault_op = (
                (state_values.get("vault_operation_block", {})
                .get("vault_operation", {})
                .get("selected_option") or {})
                .get("value", "add-or-update-key")
            )
            # Apply the change from the action that triggered this
            if action_id == "who_type":
                current_who_type = (actions[0].get("selected_option") or {}).get("value", "user")
            elif action_id == "service_category":
                current_service = (actions[0].get("selected_option") or {}).get("value", "basic")
                current_role = ""  # Reset role when category changes
            elif action_id == "access_type":
                current_access_type = (actions[0].get("selected_option") or {}).get("value", "temporary")
            elif action_id == "team":
                current_team = (actions[0].get("selected_option") or {}).get("value", "")
            elif action_id == "iam_role":
                current_role = (actions[0].get("selected_option") or {}).get("value", "")
            elif action_id == "vault_operation":
                current_vault_op = (actions[0].get("selected_option") or {}).get("value", "add-or-update-key")

            # Preserve proxy context from private_metadata through modal rebuilds
            meta_str = payload.get("view", {}).get("private_metadata", "{}")
            meta = json.loads(meta_str) if meta_str else {}
            meta_email = meta.get("requester_email", "")
            meta_proxy = meta.get("is_proxy", False)

            updated_view = {
                "type": "modal",
                "callback_id": "access_request_modal",
                "private_metadata": json.dumps({"requester_email": meta_email, "is_proxy": meta_proxy, "source_channel": meta.get("source_channel", "")}),
                "title": {"type": "plain_text", "text": "Access Request"},
                "submit": {"type": "plain_text", "text": "Submit"},
                "blocks": _build_modal_blocks(current_who_type, current_service, current_access_type, requester_email=meta_email, is_proxy=meta_proxy, selected_team=current_team, selected_role=current_role, vault_operation=current_vault_op),
            }
            try:
                get_slack_client().views_update(
                    view_id=view_id,
                    hash=view_hash,
                    view=updated_view,
                )
            except SlackApiError as e:
                logger.error(f"Failed to update modal view: {e}")
            return JSONResponse({})

        # Handle approve/reject buttons in channel messages
        return await _handle_button_action(payload)

    return JSONResponse({})


async def _handle_modal_submission(payload: dict):
    """Process submitted access request modal form."""
    user = payload.get("user", {})
    requester_email = user.get("name", "") + "@example.com"  # Slack username

    # Try to get actual email from Slack profile
    user_id = user.get("id", "")
    if user_id:
        try:
            info = get_slack_client().users_info(user=user_id)
            requester_email = info["user"]["profile"].get("email", requester_email)
        except SlackApiError:
            pass

    values = payload.get("view", {}).get("state", {}).get("values", {})

    def _get_val(block_id, action_id):
        block = values.get(block_id, {}).get(action_id, {})
        if block.get("type") == "static_select":
            opt = block.get("selected_option")
            return opt["value"] if opt else ""
        if block.get("type") == "users_select":
            selected_uid = block.get("selected_user", "")
            if selected_uid:
                try:
                    info = get_slack_client().users_info(user=selected_uid)
                    resolved_email = info["user"]["profile"].get("email", selected_uid)
                    logger.info(f"users_select resolved: uid={selected_uid} → email={resolved_email}")
                    return resolved_email
                except SlackApiError as e:
                    logger.warning(f"users_select: failed to resolve uid={selected_uid}: {e}")
                    return selected_uid
            logger.info(f"users_select: no user selected in {block_id}/{action_id}")
            return ""
        return block.get("value", "")

    # Read proxy context from modal private_metadata
    meta_str = payload.get("view", {}).get("private_metadata", "{}")
    meta = json.loads(meta_str) if meta_str else {}
    is_proxy = meta.get("is_proxy", False)
    source_channel = meta.get("source_channel", "") or settings.slack_channel
    logger.info(f"Modal submission: requester={requester_email}, is_proxy={is_proxy}, meta={meta_str}")

    who_type = _get_val("who_type_block", "who_type")

    # ── Vault Secret mode: extract vault-specific fields ──
    if who_type == "vault-secret":
        vault_operation = _get_val("vault_operation_block", "vault_operation") or "add-or-update-key"
        # Extract up to 5 key-value pairs
        vault_keys = []
        vault_values = []
        for i in range(1, 6):
            suffix = "" if i == 1 else str(i)
            k = _get_val(f"vault_key{suffix}_block", f"vault_key{suffix}") or ""
            v = _get_val(f"vault_value{suffix}_block", f"vault_value{suffix}") or ""
            if k and v:
                vault_keys.append(k)
                vault_values.append(v)
        request_data = {
            "requester_email": requester_email,
            "request_type": "vault",
            "action": vault_operation,  # maps to rule engine action field for per-operation rules
            "team": _get_val("team_block", "team") or "",
            "who_type": who_type,
            "who_email": requester_email,  # requester is the "who"
            "vault_operation": vault_operation,
            "vault_path": _get_val("vault_path_block", "vault_path"),
            "vault_key": json.dumps(vault_keys) if vault_keys else "",
            "vault_value": json.dumps(vault_values) if vault_values else "",
            "vault_keys": vault_keys,    # list for workflow submission (in-memory)
            "vault_values": vault_values, # list for workflow submission (in-memory)
            # IAM fields set to safe defaults so DB INSERT/rule evaluation won't break
            "resource_type": "vault-secret",
            "resource_name": _get_val("vault_path_block", "vault_path") or "vault",
            "permission_level": "admin",
            "custom_role": "",
            "gcp_project": "your-gcp-project-stage",
            "access_type": "permanent",
            "expiry_hours": "0",
            "reason": _get_val("reason_block", "reason") or "",
            "ticket": _get_val("ticket_block", "ticket") or "",
        }
    else:
        # ── IAM mode: extract Service Category + IAM Role ──
        who_email = _get_val("who_email_block", "who_email")
        logger.info(f"IAM mode: who_type={who_type}, who_email(from form)={who_email}, is_proxy={is_proxy}")

        # For proxy users with users_select: if _get_val returned empty, try direct extraction
        if is_proxy and who_type == "user" and not who_email:
            raw_block = values.get("who_email_block", {}).get("who_email", {})
            selected_uid = raw_block.get("selected_user", "")
            logger.warning(f"Proxy user: who_email empty from _get_val, raw block={raw_block}, selected_uid={selected_uid}")
            if selected_uid:
                try:
                    info = get_slack_client().users_info(user=selected_uid)
                    who_email = info["user"]["profile"].get("email", selected_uid)
                    logger.info(f"Proxy fallback resolved: uid={selected_uid} → email={who_email}")
                except SlackApiError as e:
                    logger.error(f"Proxy fallback: failed to resolve uid={selected_uid}: {e}")
                    who_email = selected_uid  # Use raw UID as last resort

        # Security enforcement: non-proxy users with who_type=user can only request for themselves
        if not is_proxy and who_type == "user":
            who_email = requester_email
            logger.info(f"Non-proxy enforcement: who_email overridden to {requester_email}")
        elif is_proxy and who_type == "user":
            logger.info(f"Proxy user: who_email kept as selected user {who_email}")

        service_category = _get_val("service_category_block", "service_category") or "basic"
        iam_role = _get_val("iam_role_block", "iam_role") or ""
        custom_role_text = _get_val("custom_role_block", "custom_role") or ""

        # Map to workflow parameters:
        # - resource_type = service_category slug (used for resource-level dispatch)
        # - permission_level = "custom" (always, since we pass the exact role)
        # - custom_role = the actual GCP IAM role value
        if service_category == "custom":
            resolved_role = custom_role_text
        else:
            resolved_role = iam_role

        request_data = {
            "requester_email": requester_email,
            "request_type": "iam-permission",
            "action": "grant",  # hardcoded – revoke removed
            "team": _get_val("team_block", "team") or "",
            "who_type": who_type,
            "who_email": who_email,
            "resource_type": service_category,
            "resource_name": _get_val("resource_name_block", "resource_name") or "global",
            "permission_level": "custom",
            "custom_role": resolved_role,
            "gcp_project": _get_val("project_block", "gcp_project"),
            "access_type": _get_val("access_type_block", "access_type"),
            "expiry_hours": _get_val("expiry_block", "expiry_hours") or "4",
            "reason": _get_val("reason_block", "reason") or "",
            "ticket": _get_val("ticket_block", "ticket") or "",
        }

    # Evaluate rules
    rule = evaluate_rules(request_data)
    if not rule:
        return JSONResponse(
            {
                "response_action": "errors",
                "errors": {
                    "reason_block": "No matching approval rule found. Contact admin."
                },
            }
        )

    # Ticket is fully optional — no enforcement of prefix or presence

    risk = determine_risk_level(rule, request_data)
    request_id = f"ar-{int(time.time())}-{user_id[:6] if user_id else 'slack'}"
    request_data["request_id"] = request_id

    approvers_required = rule.get("approvers_required", 1)
    approval_levels = rule.get("approval_levels", 1)

    # Team-based lead group resolution:
    # If rule's lead_group is "__selected_team__" or empty, resolve from team → lead_group
    lead_group = rule.get("lead_group")
    if (not lead_group or lead_group == "__selected_team__") and request_data.get("team"):
        team_row = db_fetchone(
            "SELECT lead_group FROM teams WHERE team_name = %s",
            (request_data["team"],),
        )
        if team_row and team_row.get("lead_group"):
            lead_group = team_row["lead_group"]
        elif lead_group == "__selected_team__":
            # Sentinel was set but team has no lead_group — fallback
            lead_group = None

    # Inject resolved lead_group back into rule dict so build_approval_message() uses it
    if lead_group and lead_group != "__selected_team__":
        rule["lead_group"] = lead_group

    try:
        # Insert request into DB (workflow submitted later on approval)
        db_execute(
            """INSERT INTO access_requests
               (request_id, request_type, requester_email, action, who_type, who_email,
                resource_type, resource_name, permission_level, custom_role, gcp_project,
                access_type, expiry_hours, reason, ticket, matched_rule_id, matched_rule_name,
                risk_level, lead_approval_required, lead_approver_group,
                final_approval_required, status, team,
                vault_operation, vault_path, vault_key, vault_value)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                request_id, request_data.get("request_type", "iam-permission"), requester_email,
                request_data["action"], request_data["who_type"], request_data["who_email"],
                request_data["resource_type"], request_data["resource_name"],
                request_data["permission_level"], request_data.get("custom_role"),
                request_data["gcp_project"], request_data["access_type"],
                int(request_data.get("expiry_hours") or 0), request_data["reason"],
                request_data.get("ticket"), rule["id"], rule["name"],
                risk, approval_levels >= 2, lead_group,
                approvers_required > 0, "auto-approved" if approvers_required == 0 else "pending",
                request_data.get("team"),
                request_data.get("vault_operation"),
                request_data.get("vault_path"),
                request_data.get("vault_key"),
                request_data.get("vault_value"),
            ),
        )

        if approvers_required == 0:
            # Auto-approved - submit workflow immediately
            wf_result = await _submit_workflow_for_request(request_data)
            workflow_name = wf_result.get("metadata", {}).get("name", "unknown")
            # Store workflow name in DB
            db_execute(
                "UPDATE access_requests SET workflow_name=%s, updated_at=NOW() WHERE request_id=%s",
                (workflow_name, request_id),
            )
            wf_link = f"{settings.argo_ui_url}/workflows/{settings.argo_namespace}/{workflow_name}"
            if request_data.get("request_type") == "vault":
                # Parse vault_key JSON array for display
                _vk_raw = request_data.get('vault_key', '')
                try:
                    _vk_list = json.loads(_vk_raw) if _vk_raw else []
                except (json.JSONDecodeError, TypeError):
                    _vk_list = [_vk_raw] if _vk_raw else []
                _vk_display = ", ".join(_vk_list) if _vk_list else "N/A"
                auto_text = (
                    f"🟢 Auto-approved: Vault {request_data.get('vault_operation', 'update')} "
                    f"at `{request_data.get('vault_path', '')}` keys=`{_vk_display}` "
                    f"by {request_data['who_email']} (rule: {rule['name']}) | <{wf_link}|Workflow: {workflow_name}>"
                )
            else:
                # v2.0.0: role-aware auto-approve text
                _auto_role = _get_role_display(request_data.get('custom_role', '')) if request_data.get('custom_role') else request_data.get('permission_level', 'viewer')
                _auto_svc = _get_service_label(request_data.get('resource_type', 'basic'))
                auto_text = (
                    f"🟢 Auto-approved: {request_data['action']} `{_auto_role}` "
                    f"({_auto_svc}) for {request_data['who_email']} "
                    f"(rule: {rule['name']}) | <{wf_link}|Workflow: {workflow_name}>"
                )
            auto_resp = get_slack_client().chat_postMessage(
                channel=source_channel,
                text=auto_text,
            )
            # Spawn background poller for workflow status thread reply
            asyncio.create_task(_poll_workflow_status(
                workflow_name, request_id,
                auto_resp["channel"], auto_resp["ts"],
            ))
        else:
            # Send approval message (workflow will be submitted on approval)
            total_levels = min(approval_levels, 2)
            msg = build_approval_message(
                request_data, rule, risk, request_id, level=1, total_levels=total_levels,
                channel=source_channel,
            )
            resp = get_slack_client().chat_postMessage(**msg)
            db_execute(
                "UPDATE access_requests SET slack_channel=%s, slack_ts=%s WHERE request_id=%s",
                (resp["channel"], resp["ts"], request_id),
            )

            # Send DMs to each approver — with secrets visible for vault requests
            approver_group = rule.get("lead_group") or "final-approvers"
            if request_data.get("request_type") == "vault":
                dm_msg = build_approval_message(
                    request_data, rule, risk, request_id, level=1, total_levels=total_levels,
                    channel=source_channel, show_secrets=True,
                )
                _send_approver_dms(
                    request_id, approver_group,
                    blocks=dm_msg["blocks"], text=dm_msg["text"],
                    approval_level=1,
                )
            else:
                _send_approver_dms(
                    request_id, approver_group,
                    blocks=msg["blocks"], text=msg["text"],
                    approval_level=1,
                )

    except Exception as e:
        logger.error(f"Failed to process request: {e}")
        return JSONResponse(
            {
                "response_action": "errors",
                "errors": {"reason_block": f"Failed to submit: {str(e)[:100]}"},
            }
        )

    return JSONResponse({"response_action": "clear"})


async def _submit_workflow_for_request(request_data: dict) -> dict:
    """Build workflow parameters from request data and submit."""
    if request_data.get("request_type") == "vault":
        # Route to OpenBao manage-secrets workflow template (supports up to 10 KV pairs)
        vault_keys = request_data.get("vault_keys", [])
        vault_values = request_data.get("vault_values", [])
        # Fallback: if vault_keys list not present, parse from DB (JSON array or legacy comma-separated)
        if not vault_keys and request_data.get("vault_key"):
            try:
                vault_keys = json.loads(request_data["vault_key"])
            except (json.JSONDecodeError, TypeError):
                vault_keys = [k.strip() for k in request_data["vault_key"].split(",") if k.strip()]
        if not vault_values and request_data.get("vault_value"):
            try:
                vault_values = json.loads(request_data["vault_value"])
            except (json.JSONDecodeError, TypeError):
                vault_values = [request_data["vault_value"]]
        wf_params = {
            "operation": request_data.get("vault_operation", "add-or-update-key"),
            "path": request_data.get("vault_path", ""),
            "key": vault_keys[0] if len(vault_keys) > 0 else "",
            "value": vault_values[0] if len(vault_values) > 0 else "",
            "key2": vault_keys[1] if len(vault_keys) > 1 else "",
            "value2": vault_values[1] if len(vault_values) > 1 else "",
            "key3": vault_keys[2] if len(vault_keys) > 2 else "",
            "value3": vault_values[2] if len(vault_values) > 2 else "",
            "key4": vault_keys[3] if len(vault_keys) > 3 else "",
            "value4": vault_values[3] if len(vault_values) > 3 else "",
            "key5": vault_keys[4] if len(vault_keys) > 4 else "",
            "value5": vault_values[4] if len(vault_values) > 4 else "",
            "key6": vault_keys[5] if len(vault_keys) > 5 else "",
            "value6": vault_values[5] if len(vault_values) > 5 else "",
            "key7": vault_keys[6] if len(vault_keys) > 6 else "",
            "value7": vault_values[6] if len(vault_values) > 6 else "",
            "key8": vault_keys[7] if len(vault_keys) > 7 else "",
            "value8": vault_values[7] if len(vault_values) > 7 else "",
            "key9": vault_keys[8] if len(vault_keys) > 8 else "",
            "value9": vault_values[8] if len(vault_values) > 8 else "",
            "key10": vault_keys[9] if len(vault_keys) > 9 else "",
            "value10": vault_values[9] if len(vault_values) > 9 else "",
        }
        return await submit_workflow(wf_params, template_name="openbao-manage-secrets")
    elif request_data.get("request_type") in ("rms-db-clone", "postgres-db-manager"):
        # Route to postgres-db-manager WorkflowTemplate in argo-access-control.
        # Supports operations: clone-db, create-db, drop-db, grant-access, rename-db.
        # pg_instance selects a pre-registered postgres instance (dropdown in UI).
        # pg_*_override fields take priority over the instance secret for ad-hoc use.
        wf_params = {
            # Operation (clone-db | create-db | drop-db | grant-access | rename-db)
            "operation": request_data.get("operation", "clone-db"),
            # Instance selection (dropdown — e1-backend, grafana, ...)
            "pg_instance": request_data.get("pg_instance", "e1-backend"),
            # Manual overrides (optional — leave blank to use instance secret)
            "pg_host_override": request_data.get("pg_host_override", ""),
            "pg_port_override": request_data.get("pg_port_override", ""),
            "pg_user_override": request_data.get("pg_user_override", ""),
            "pg_password_override": request_data.get("pg_password_override", ""),
            # DB params
            "source_db": request_data.get("source_db", ""),
            "target_db": request_data.get("target_db", ""),
            "target_user": request_data.get("target_user", ""),
            "db_owner": request_data.get("db_owner", "postgres"),
            "drop_if_exists": str(request_data.get("drop_if_exists", "true")).lower(),
        }
        return await submit_workflow(wf_params, template_name="postgres-db-manager")
    else:
        # Standard IAM workflow
        wf_params = {
            "action": request_data.get("action", "grant"),
            "who-type": request_data.get("who_type", "user"),
            "who-email": request_data.get("who_email", ""),
            "resource-type": request_data.get("resource_type", ""),
            "resource-name": request_data.get("resource_name", "global"),
            "permission-level": request_data.get("permission_level", "viewer"),
            "custom-role": request_data.get("custom_role", ""),
            "gcp-project": request_data.get("gcp_project", ""),
            "access-type": request_data.get("access_type", "temporary"),
            "expiry-hours": str(request_data.get("expiry_hours", "4")),
            "reason": request_data.get("reason", ""),
            "ticket": request_data.get("ticket", ""),
        }
        return await submit_workflow(wf_params)


async def _poll_workflow_status(
    workflow_name: str,
    request_id: str,
    channel: str,
    thread_ts: str,
):
    """Poll Argo Workflow status and post thread reply when complete."""
    max_polls = 15          # 15 * 20s = 5 minutes
    poll_interval = 20      # seconds

    url = (
        f"{K8S_API_BASE}/apis/argoproj.io/v1alpha1"
        f"/namespaces/{settings.argo_namespace}/workflows/{workflow_name}"
    )

    for attempt in range(max_polls):
        await asyncio.sleep(poll_interval)

        try:
            async with httpx.AsyncClient(verify=K8S_CA_PATH, timeout=10) as client:
                resp = await client.get(url, headers=_k8s_headers())
                if resp.status_code != 200:
                    continue
                wf = resp.json()

            phase = wf.get("status", {}).get("phase", "")

            if phase in ("Succeeded", "Failed", "Error"):
                # Extract error message from failed nodes
                message = wf.get("status", {}).get("message", "")
                nodes = wf.get("status", {}).get("nodes", {})
                error_msg = ""
                if phase != "Succeeded":
                    for node in nodes.values():
                        if node.get("phase") in ("Failed", "Error"):
                            error_msg = node.get("message", "")
                            if error_msg:
                                break
                    if not error_msg:
                        error_msg = message or "Unknown error"

                # Post thread reply
                _post_workflow_result_thread(
                    channel, thread_ts, workflow_name,
                    request_id, phase, error_msg,
                )

                # Update DB
                if phase == "Succeeded":
                    db_execute(
                        """UPDATE access_requests
                           SET status='executed', executed_at=NOW(), updated_at=NOW()
                           WHERE request_id=%s""",
                        (request_id,),
                    )
                else:
                    db_execute(
                        """UPDATE access_requests
                           SET status='failed', error_message=%s, updated_at=NOW()
                           WHERE request_id=%s""",
                        (error_msg[:500], request_id),
                    )
                return  # Done

        except Exception as e:
            logger.warning(f"Poll attempt {attempt} for {workflow_name}: {e}")

    # Timeout — post warning
    _post_workflow_result_thread(
        channel, thread_ts, workflow_name,
        request_id, "Timeout",
        "Polling timed out after 5 minutes — check Argo UI",
    )


def _post_workflow_result_thread(
    channel: str,
    thread_ts: str,
    workflow_name: str,
    request_id: str,
    phase: str,
    error_msg: str = "",
):
    """Post a thread reply with the workflow outcome."""
    if not channel or not thread_ts:
        logger.warning(
            f"No channel/thread_ts for {request_id}, skipping thread reply"
        )
        return

    wf_link = (
        f"{settings.argo_ui_url}/workflows/"
        f"{settings.argo_namespace}/{workflow_name}"
    )

    # Fetch request details for the success message
    req = db_fetchone(
        "SELECT * FROM access_requests WHERE request_id=%s", (request_id,)
    )

    if phase == "Succeeded" and req:
        if req.get("request_type") == "vault":
            # Parse vault_key JSON array for display
            _vk_raw = req.get('vault_key', '')
            try:
                _vk_list = json.loads(_vk_raw) if _vk_raw else []
            except (json.JSONDecodeError, TypeError):
                _vk_list = [_vk_raw] if _vk_raw else []
            _vk_display = ", ".join(f"`{k}`" for k in _vk_list) if _vk_list else "`N/A`"
            if req.get('vault_operation') == 'delete-keys':
                text = (
                    f"🗑️ *Vault Keys Deleted*\n\n"
                    f"• *Operation:* delete-keys\n"
                    f"• *Path:* `{req.get('vault_path', 'N/A')}`\n"
                    f"• *Deleted Keys:* {_vk_display}\n"
                    f"• *Workflow:* <{wf_link}|{workflow_name}>"
                )
            else:
                text = (
                    f"✅ *Vault Secret Updated*\n\n"
                    f"• *Operation:* {req.get('vault_operation', 'N/A')}\n"
                    f"• *Path:* `{req.get('vault_path', 'N/A')}`\n"
                    f"• *Keys:* {_vk_display}\n"
                    f"• *Workflow:* <{wf_link}|{workflow_name}>"
                )
        else:
            duration_text = (
                f"Temporary — {req['expiry_hours']}h"
                if req.get("access_type") == "temporary"
                else "Permanent"
            )
            # v2.0.0: role-aware display using catalog
            svc_key = req.get("resource_type", "basic")
            svc_label = _get_service_label(svc_key)
            custom_role = req.get("custom_role", "")
            role_display = _get_role_display(custom_role) if custom_role else req.get("permission_level", "N/A")
            resource_name = req.get("resource_name", "global")
            scope_text = f"{svc_label}: {resource_name}" if resource_name and resource_name != "global" else f"{svc_label} (project-level)"

            text = (
                f"✅ *Access Granted*\n\n"
                f"• *IAM Role:* {role_display}"
                f"{' (`' + custom_role + '`)' if custom_role else ''}\n"
                f"• *Service:* {scope_text}\n"
                f"• *Target:* {req.get('who_type', '')}:{req.get('who_email', '')}\n"
                f"• *Project:* {req.get('gcp_project', '')}\n"
                f"• *Duration:* {duration_text}\n"
                f"• *Workflow:* <{wf_link}|{workflow_name}>"
            )
    elif phase in ("Failed", "Error"):
        text = (
            f"❌ *Workflow Failed*\n\n"
            f"• *Error:* {error_msg}\n"
            f"• *Workflow:* <{wf_link}|{workflow_name}>\n"
            f"• View <{wf_link}|Argo UI> for full logs"
        )
    elif phase == "Timeout":
        text = (
            f"⏱️ *Workflow Status Unknown*\n\n"
            f"• {error_msg}\n"
            f"• *Workflow:* <{wf_link}|{workflow_name}>"
        )
    else:
        text = f"ℹ️ Workflow `{workflow_name}` ended with phase: {phase}"

    try:
        get_slack_client().chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=text,
        )
    except SlackApiError as e:
        logger.error(f"Failed to post thread reply for {request_id}: {e}")


async def _handle_button_action(payload: dict):
    """Handle approve/reject button clicks."""
    actions = payload.get("actions", [])
    if not actions:
        return JSONResponse({})

    action = actions[0]
    action_id = action.get("action_id", "")
    action_value = json.loads(action.get("value", "{}"))
    approver = payload.get("user", {})
    approver_id = approver.get("id", "")
    approver_name = approver.get("username", "unknown")

    request_id = action_value.get("request_id", "")
    level = action_value.get("level", 1)
    total_levels = action_value.get("total_levels", 1)
    required_group = action_value.get("approver_group", "final-approvers")

    # Get the original message for updating
    channel = payload.get("channel", {}).get("id", settings.slack_channel)
    message_ts = payload.get("message", {}).get("ts", "")

    # Verify the clicker is a member of the required approver group
    approver_email = None
    try:
        info = get_slack_client().users_info(user=approver_id)
        approver_email = info["user"]["profile"].get("email", "")
    except SlackApiError:
        logger.warning(f"Could not resolve email for Slack user {approver_id}")

    if approver_email:
        is_authorized = db_fetchone(
            """SELECT 1 FROM approver_group_members m
               JOIN approver_groups g ON m.group_id = g.id
               WHERE g.group_name = %s AND m.email = %s""",
            (required_group, approver_email),
        )
        if not is_authorized:
            logger.warning(
                f"Unauthorized approval attempt by {approver_email} "
                f"(not in group '{required_group}') for request {request_id}"
            )
            # Send ephemeral message to the user
            try:
                get_slack_client().chat_postEphemeral(
                    channel=channel,
                    user=approver_id,
                    text=f"❌ You are not authorized to {action_id.replace('_', ' ')} this request. "
                         f"Only members of '{required_group}' can do this.",
                )
            except SlackApiError:
                pass
            return JSONResponse({})

    if action_id == "approve_request":
        req = db_fetchone(
            "SELECT * FROM access_requests WHERE request_id = %s", (request_id,)
        )
        if not req:
            return JSONResponse({"text": "Request not found"})

        # Self-approval prevention: requester cannot approve their own request
        # Exception: members of the proxy-requesters group may still approve
        if approver_email and approver_email.lower() == (req.get("requester_email") or "").lower():
            if not _is_proxy_requester(approver_email):
                logger.warning(
                    f"Self-approval blocked: {approver_email} tried to approve "
                    f"their own request {request_id}"
                )
                try:
                    get_slack_client().chat_postEphemeral(
                        channel=channel,
                        user=approver_id,
                        text="❌ You cannot approve your own request. "
                             "Please ask another approver to review it.",
                    )
                except SlackApiError:
                    pass
                return JSONResponse({})

        if req["status"] != "pending":
            return JSONResponse({"text": f"Request already {req['status']}"})

        if level == 1 and total_levels == 2:
            # Lead approval - update DB and forward to final approvers
            db_execute(
                """UPDATE access_requests
                   SET lead_approved_by=%s, lead_approved_at=NOW(), updated_at=NOW()
                   WHERE request_id=%s""",
                (approver_name, request_id),
            )

            l1_status_block = {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"✅ *Lead approved* by <@{approver_id}> | Forwarded to final approvers",
                },
            }
            l1_updated_text = f"✅ Lead approved by @{approver_name}. Forwarded to final approvers."

            # For vault requests, rebuild blocks from DB to prevent secret leakage
            # (the button may have been clicked from a DM that shows secrets)
            if req.get("request_type") == "vault":
                rule_for_rebuild = db_fetchone(
                    "SELECT * FROM approval_rules WHERE id = %s", (req["matched_rule_id"],),
                )
                if rule_for_rebuild:
                    # Channel-safe blocks (secrets masked)
                    ch_msg = build_approval_message(
                        dict(req), dict(rule_for_rebuild), req["risk_level"],
                        request_id, level=1, total_levels=total_levels,
                        show_secrets=False,
                    )
                    l1_channel_blocks = ch_msg["blocks"][:-2] + [l1_status_block]
                    # DM blocks (secrets visible)
                    dm_msg = build_approval_message(
                        dict(req), dict(rule_for_rebuild), req["risk_level"],
                        request_id, level=1, total_levels=total_levels,
                        show_secrets=True,
                    )
                    l1_dm_blocks = dm_msg["blocks"][:-2] + [l1_status_block]
                else:
                    l1_channel_blocks = payload.get("message", {}).get("blocks", [])[:-2] + [l1_status_block]
                    l1_dm_blocks = l1_channel_blocks
            else:
                l1_channel_blocks = payload.get("message", {}).get("blocks", [])[:-2] + [l1_status_block]
                l1_dm_blocks = l1_channel_blocks

            # Determine if the button was clicked from a DM or channel
            # to show the right version (secrets visible in DM, masked in channel)
            req_channel = req.get("slack_channel", "")
            is_dm_click = (channel != req_channel) if req_channel else False
            l1_clicked_blocks = l1_dm_blocks if is_dm_click else l1_channel_blocks

            get_slack_client().chat_update(
                channel=channel,
                ts=message_ts,
                text=l1_updated_text,
                blocks=l1_clicked_blocks,
            )

            # Update all other L1 messages (channel + DMs)
            _update_all_approval_messages(
                request_id, approval_level=1,
                updated_blocks=l1_channel_blocks, updated_text=l1_updated_text,
                exclude_channel=channel, exclude_ts=message_ts,
                dm_blocks=l1_dm_blocks, dm_text=l1_updated_text,
            )

            # Send to final approvers
            rule = db_fetchone(
                "SELECT * FROM approval_rules WHERE id = %s",
                (req["matched_rule_id"],),
            )
            if rule:
                request_data = dict(req)
                # Use stored channel for L2 message
                l2_channel = req.get("slack_channel", "") or settings.slack_channel
                msg = build_approval_message(
                    request_data, dict(rule), req["risk_level"],
                    request_id, level=2, total_levels=2,
                    channel=l2_channel,
                )
                resp = get_slack_client().chat_postMessage(**msg)
                db_execute(
                    "UPDATE access_requests SET slack_ts=%s WHERE request_id=%s",
                    (resp["ts"], request_id),
                )

                # Send L2 DMs to final approver group — with secrets visible for vault requests
                l2_group = rule.get("second_approval_group") or rule.get("lead_group") or "final-approvers"
                if req.get("request_type") == "vault":
                    dm_msg = build_approval_message(
                        request_data, dict(rule), req["risk_level"],
                        request_id, level=2, total_levels=2,
                        channel=l2_channel, show_secrets=True,
                    )
                    _send_approver_dms(
                        request_id, l2_group,
                        blocks=dm_msg["blocks"], text=dm_msg["text"],
                        approval_level=2,
                    )
                else:
                    _send_approver_dms(
                        request_id, l2_group,
                        blocks=msg["blocks"], text=msg["text"],
                        approval_level=2,
                    )
        else:
            # Dual-approval dedup: L1 approver cannot also approve at L2
            if total_levels == 2 and req.get("lead_approved_by") and approver_name == req.get("lead_approved_by"):
                logger.warning(
                    f"Dual-approval dedup: {approver_email} already approved L1 "
                    f"for request {request_id}, blocking L2 approval"
                )
                try:
                    get_slack_client().chat_postEphemeral(
                        channel=channel,
                        user=approver_id,
                        text="❌ You already approved this request at Level 1 (Lead). "
                             "A different approver must handle the Level 2 (Final) approval.",
                    )
                except SlackApiError:
                    pass
                return JSONResponse({})

            # Final approval - atomic check-and-update to prevent race conditions
            updated = db_fetchone(
                """UPDATE access_requests
                   SET final_approved_by=%s, final_approved_at=NOW(),
                       status='approved', updated_at=NOW()
                   WHERE request_id=%s AND status='pending'
                   RETURNING *""",
                (approver_name, request_id),
            )
            if not updated:
                return JSONResponse({"text": "Request already processed"})

            workflow_name = "pending"
            try:
                request_data = dict(req)
                wf_result = await _submit_workflow_for_request(request_data)
                workflow_name = wf_result.get("metadata", {}).get("name", "unknown")
                logger.info(f"Workflow {workflow_name} submitted for request {request_id}")
                # Store workflow name in DB
                db_execute(
                    "UPDATE access_requests SET workflow_name=%s, updated_at=NOW() WHERE request_id=%s",
                    (workflow_name, request_id),
                )
            except Exception as e:
                logger.error(f"Failed to submit workflow for {request_id}: {e}")
                workflow_name = f"FAILED: {str(e)[:80]}"

            wf_link = f"{settings.argo_ui_url}/workflows/{settings.argo_namespace}/{workflow_name}"
            approved_status_block = {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"✅ *Approved* by <@{approver_id}> | <{wf_link}|Workflow: {workflow_name}>",
                },
            }
            approved_text = f"✅ Approved by @{approver_name}"

            # For vault requests, rebuild blocks from DB to prevent secret leakage
            if req.get("request_type") == "vault":
                rule_for_rebuild = db_fetchone(
                    "SELECT * FROM approval_rules WHERE id = %s", (req["matched_rule_id"],),
                )
                if rule_for_rebuild:
                    ch_msg = build_approval_message(
                        dict(req), dict(rule_for_rebuild), req["risk_level"],
                        request_id, level=level, total_levels=total_levels,
                        show_secrets=False,
                    )
                    approved_channel_blocks = ch_msg["blocks"][:-2] + [approved_status_block]
                    dm_msg = build_approval_message(
                        dict(req), dict(rule_for_rebuild), req["risk_level"],
                        request_id, level=level, total_levels=total_levels,
                        show_secrets=True,
                    )
                    approved_dm_blocks = dm_msg["blocks"][:-2] + [approved_status_block]
                else:
                    approved_channel_blocks = payload.get("message", {}).get("blocks", [])[:-2] + [approved_status_block]
                    approved_dm_blocks = approved_channel_blocks
            else:
                approved_channel_blocks = payload.get("message", {}).get("blocks", [])[:-2] + [approved_status_block]
                approved_dm_blocks = approved_channel_blocks

            # Determine if the button was clicked from a DM or channel
            req_channel = req.get("slack_channel", "")
            is_dm_click = (channel != req_channel) if req_channel else False
            approved_clicked_blocks = approved_dm_blocks if is_dm_click else approved_channel_blocks

            get_slack_client().chat_update(
                channel=channel,
                ts=message_ts,
                text=approved_text,
                blocks=approved_clicked_blocks,
            )

            # Update all other messages (channel + DMs) for the current approval level
            _update_all_approval_messages(
                request_id, approval_level=level,
                updated_blocks=approved_channel_blocks, updated_text=approved_text,
                exclude_channel=channel, exclude_ts=message_ts,
                dm_blocks=approved_dm_blocks, dm_text=approved_text,
            )

            # Spawn background poller for workflow status thread reply
            # Use the channel message (not DM) for thread replies
            poll_channel = channel
            poll_ts = message_ts
            req_row = db_fetchone(
                "SELECT slack_channel, slack_ts FROM access_requests WHERE request_id=%s",
                (request_id,),
            )
            if req_row and req_row.get("slack_channel"):
                poll_channel = req_row["slack_channel"]
                poll_ts = req_row["slack_ts"]

            if not workflow_name.startswith("FAILED:"):
                asyncio.create_task(_poll_workflow_status(
                    workflow_name, request_id, poll_channel, poll_ts,
                ))

    elif action_id == "reject_request":
        # Atomic check-and-update to prevent race conditions
        rejected = db_fetchone(
            """UPDATE access_requests
               SET status='rejected', rejection_reason=%s, updated_at=NOW()
               WHERE request_id=%s AND status='pending'
               RETURNING *""",
            (f"Rejected by {approver_name}", request_id),
        )
        if not rejected:
            return JSONResponse({"text": "Request already processed"})

        rejected_status_block = {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"❌ *Rejected* by <@{approver_id}>",
            },
        }
        rejected_text = f"❌ Rejected by @{approver_name}"

        # For vault requests, rebuild blocks from DB to prevent secret leakage
        if rejected.get("request_type") == "vault":
            rule_for_rebuild = db_fetchone(
                "SELECT * FROM approval_rules WHERE id = %s", (rejected["matched_rule_id"],),
            )
            if rule_for_rebuild:
                ch_msg = build_approval_message(
                    dict(rejected), dict(rule_for_rebuild), rejected["risk_level"],
                    request_id, level=level, total_levels=total_levels,
                    show_secrets=False,
                )
                rejected_channel_blocks = ch_msg["blocks"][:-2] + [rejected_status_block]
                dm_msg = build_approval_message(
                    dict(rejected), dict(rule_for_rebuild), rejected["risk_level"],
                    request_id, level=level, total_levels=total_levels,
                    show_secrets=True,
                )
                rejected_dm_blocks = dm_msg["blocks"][:-2] + [rejected_status_block]
            else:
                rejected_channel_blocks = payload.get("message", {}).get("blocks", [])[:-2] + [rejected_status_block]
                rejected_dm_blocks = rejected_channel_blocks
        else:
            rejected_channel_blocks = payload.get("message", {}).get("blocks", [])[:-2] + [rejected_status_block]
            rejected_dm_blocks = rejected_channel_blocks

        # Determine if the button was clicked from a DM or channel
        req_row_for_reject = db_fetchone(
            "SELECT slack_channel FROM access_requests WHERE request_id=%s", (request_id,),
        )
        req_reject_channel = req_row_for_reject.get("slack_channel", "") if req_row_for_reject else ""
        is_dm_click = (channel != req_reject_channel) if req_reject_channel else False
        rejected_clicked_blocks = rejected_dm_blocks if is_dm_click else rejected_channel_blocks

        get_slack_client().chat_update(
            channel=channel,
            ts=message_ts,
            text=rejected_text,
            blocks=rejected_clicked_blocks,
        )

        # Update all other messages (channel + DMs) — both levels
        for lvl in (1, 2):
            _update_all_approval_messages(
                request_id, approval_level=lvl,
                updated_blocks=rejected_channel_blocks, updated_text=rejected_text,
                exclude_channel=channel, exclude_ts=message_ts,
                dm_blocks=rejected_dm_blocks, dm_text=rejected_text,
            )

    return JSONResponse({})


# ============================================================
# Admin API — Rules
# ============================================================


@app.get("/api/rules")
async def list_rules(user: str = Depends(require_admin)):
    rules = db_fetchall("SELECT * FROM approval_rules ORDER BY priority ASC")
    return [dict(r) for r in rules]


@app.post("/api/rules")
async def create_rule(rule: RuleCreate, user: str = Depends(require_admin)):
    # Auto-derive approval settings from group selections
    if not rule.lead_group and not rule.second_approval_group:
        approvers_required, approval_levels = 0, 0
    elif rule.lead_group and not rule.second_approval_group:
        approvers_required, approval_levels = 1, 1
    else:
        approvers_required, approval_levels = 1, 2

    row = db_fetchone(
        """INSERT INTO approval_rules
           (name, priority, enabled, requester_email, resource_type, permission_level,
            gcp_project, action, access_type, approvers_required, approval_levels,
            lead_group, second_approval_group, require_reason, require_ticket,
            ticket_pattern, description, created_by)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           RETURNING id""",
        (
            rule.name, rule.priority, rule.enabled, rule.requester_email,
            rule.resource_type, rule.permission_level, rule.gcp_project,
            rule.action, rule.access_type, approvers_required,
            approval_levels, rule.lead_group, rule.second_approval_group,
            rule.require_reason, rule.require_ticket, rule.ticket_pattern,
            rule.description, user,
        ),
    )
    return {"id": row["id"], "message": "Rule created"}


@app.put("/api/rules/{rule_id}")
async def update_rule(rule_id: int, rule: RuleUpdate, user: str = Depends(require_admin)):
    # Auto-derive approval settings from group selections
    if not rule.lead_group and not rule.second_approval_group:
        approvers_required, approval_levels = 0, 0
    elif rule.lead_group and not rule.second_approval_group:
        approvers_required, approval_levels = 1, 1
    else:
        approvers_required, approval_levels = 1, 2

    db_execute(
        """UPDATE approval_rules SET
           name=%s, priority=%s, enabled=%s, requester_email=%s, resource_type=%s,
           permission_level=%s, gcp_project=%s, action=%s, access_type=%s,
           approvers_required=%s, approval_levels=%s, lead_group=%s,
           second_approval_group=%s,
           require_reason=%s, require_ticket=%s, ticket_pattern=%s,
           description=%s, updated_at=NOW()
           WHERE id=%s""",
        (
            rule.name, rule.priority, rule.enabled, rule.requester_email,
            rule.resource_type, rule.permission_level, rule.gcp_project,
            rule.action, rule.access_type, approvers_required,
            approval_levels, rule.lead_group, rule.second_approval_group,
            rule.require_reason, rule.require_ticket, rule.ticket_pattern,
            rule.description, rule_id,
        ),
    )
    return {"message": "Rule updated"}


@app.delete("/api/rules/{rule_id}")
async def delete_rule(rule_id: int, user: str = Depends(require_admin)):
    # Nullify FK references in access_requests before deleting (preserves matched_rule_name for history)
    db_execute("UPDATE access_requests SET matched_rule_id=NULL WHERE matched_rule_id=%s", (rule_id,))
    db_execute("DELETE FROM approval_rules WHERE id=%s", (rule_id,))
    return {"message": "Rule deleted"}


@app.post("/api/rules/evaluate")
async def api_evaluate_rules(req: EvaluateRequest, user: str = Depends(require_admin)):
    rule = evaluate_rules(req.model_dump())
    if rule:
        risk = determine_risk_level(rule, req.model_dump())
        return {"matched": True, "rule": rule, "risk_level": risk}
    return {"matched": False, "rule": None, "risk_level": "unknown"}


# ============================================================
# Admin API — Groups
# ============================================================


@app.get("/api/groups")
async def list_groups(user: str = Depends(require_admin)):
    groups = db_fetchall(
        """SELECT g.*, COALESCE(json_agg(
             json_build_object('email', m.email, 'display_name', m.display_name,
                               'slack_user_id', m.slack_user_id)
           ) FILTER (WHERE m.id IS NOT NULL), '[]') as members
           FROM approver_groups g
           LEFT JOIN approver_group_members m ON g.id = m.group_id
           GROUP BY g.id ORDER BY g.group_name"""
    )
    return [dict(g) for g in groups]


@app.post("/api/groups")
async def create_group(group: GroupCreate, user: str = Depends(require_admin)):
    row = db_fetchone(
        "INSERT INTO approver_groups (group_name, description) VALUES (%s, %s) RETURNING id",
        (group.group_name, group.description),
    )
    return {"id": row["id"], "message": "Group created"}


@app.post("/api/groups/{group_id}/members")
async def add_member(group_id: int, member: MemberAdd, user: str = Depends(require_admin)):
    # Resolve Slack user ID
    slack_id = resolve_slack_user_id(member.email)

    db_execute(
        """INSERT INTO approver_group_members (group_id, email, display_name, slack_user_id)
           VALUES (%s, %s, %s, %s) ON CONFLICT (group_id, email) DO UPDATE
           SET display_name=EXCLUDED.display_name, slack_user_id=EXCLUDED.slack_user_id""",
        (group_id, member.email, member.display_name, slack_id),
    )
    return {"message": "Member added", "slack_user_id": slack_id}


@app.delete("/api/groups/{group_id}/members/{email:path}")
async def remove_member(group_id: int, email: str, user: str = Depends(require_admin)):
    db_execute(
        "DELETE FROM approver_group_members WHERE group_id=%s AND email=%s",
        (group_id, email),
    )
    return {"message": "Member removed"}


@app.delete("/api/groups/{group_id}")
async def delete_group(group_id: int, user: str = Depends(require_admin)):
    """Delete an approver group and its members (CASCADE)."""
    deleted = db_fetchone(
        "DELETE FROM approver_groups WHERE id=%s RETURNING id", (group_id,)
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Group not found")
    return {"message": "Group deleted"}


# ============================================================
# Admin API — Requests
# ============================================================


@app.get("/api/requests")
async def list_requests(
    status: Optional[str] = None,
    limit: int = Query(default=50, le=200),
    user: str = Depends(require_admin),
):
    if status:
        rows = db_fetchall(
            "SELECT * FROM access_requests WHERE status=%s ORDER BY created_at DESC LIMIT %s",
            (status, limit),
        )
    else:
        rows = db_fetchall(
            "SELECT * FROM access_requests ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )
    return [dict(r) for r in rows]


# ============================================================
# Vault Request API (called by vault-clone web UI)
# ============================================================


@app.post("/api/vault-request")
async def api_vault_request(body: VaultRequestBody):
    """Accept vault secret requests from vault-clone web UI.

    Reuses the same approval pipeline as Slack modal submissions:
    evaluate_rules → INSERT access_requests → Slack approval or auto-approve.
    """
    # Validate inputs
    is_delete = body.vault_operation == "delete-keys"
    if not is_delete and len(body.vault_keys) != len(body.vault_values):
        raise HTTPException(status_code=422, detail="vault_keys and vault_values must have the same length")
    if not body.vault_keys or len(body.vault_keys) > 10:
        raise HTTPException(status_code=422, detail="Must provide 1-10 keys")
    if not body.vault_path.strip():
        raise HTTPException(status_code=422, detail="vault_path is required")
    if not body.requester_email.strip():
        raise HTTPException(status_code=422, detail="requester_email is required")

    # Build request_data in the same format as _handle_modal_submission() vault mode
    request_data = {
        "requester_email": body.requester_email,
        "request_type": "vault",
        "action": body.vault_operation,
        "team": body.team,
        "who_type": "vault-secret",
        "who_email": body.requester_email,
        "vault_operation": body.vault_operation,
        "vault_path": body.vault_path,
        "vault_key": json.dumps(body.vault_keys),
        "vault_value": json.dumps(body.vault_values),
        "vault_keys": body.vault_keys,
        "vault_values": body.vault_values,
        "resource_type": "vault-secret",
        "resource_name": body.vault_path,
        "permission_level": "admin",
        "custom_role": "",
        "gcp_project": "your-gcp-project-prod",
        "access_type": "permanent",
        "expiry_hours": "0",
        "reason": f"Vault key request from {body.source}",
        "ticket": "",
    }

    # Evaluate rules
    rule = evaluate_rules(request_data)
    if not rule:
        raise HTTPException(
            status_code=422,
            detail="No matching approval rule found for vault requests. Contact admin to add a rule.",
        )

    risk = determine_risk_level(rule, request_data)
    user_prefix = body.requester_email.split("@")[0][:6] if "@" in body.requester_email else "web"
    request_id = f"ar-{int(time.time())}-{user_prefix}"
    request_data["request_id"] = request_id

    approvers_required = rule.get("approvers_required", 1)
    approval_levels = rule.get("approval_levels", 1)

    # Resolve team-based lead group
    lead_group = rule.get("lead_group")
    if (not lead_group or lead_group == "__selected_team__") and request_data.get("team"):
        team_row = db_fetchone(
            "SELECT lead_group FROM teams WHERE team_name = %s",
            (request_data["team"],),
        )
        if team_row and team_row.get("lead_group"):
            lead_group = team_row["lead_group"]
        elif lead_group == "__selected_team__":
            lead_group = None

    if lead_group and lead_group != "__selected_team__":
        rule["lead_group"] = lead_group

    try:
        # Insert into DB — same schema as _handle_modal_submission()
        db_execute(
            """INSERT INTO access_requests
               (request_id, request_type, requester_email, action, who_type, who_email,
                resource_type, resource_name, permission_level, custom_role, gcp_project,
                access_type, expiry_hours, reason, ticket, matched_rule_id, matched_rule_name,
                risk_level, lead_approval_required, lead_approver_group,
                final_approval_required, status, team,
                vault_operation, vault_path, vault_key, vault_value)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                request_id, "vault", body.requester_email,
                request_data["action"], "vault-secret", body.requester_email,
                "vault-secret", body.vault_path,
                "admin", "",
                "your-gcp-project-prod", "permanent",
                0, request_data["reason"],
                "", rule["id"], rule["name"],
                risk, approval_levels >= 2, lead_group,
                approvers_required > 0, "auto-approved" if approvers_required == 0 else "pending",
                "",
                body.vault_operation, body.vault_path,
                json.dumps(body.vault_keys), json.dumps(body.vault_values),
            ),
        )

        source_channel = settings.slack_channel

        if approvers_required == 0:
            # Auto-approved — submit workflow immediately
            wf_result = await _submit_workflow_for_request(request_data)
            workflow_name = wf_result.get("metadata", {}).get("name", "unknown")
            db_execute(
                "UPDATE access_requests SET workflow_name=%s, updated_at=NOW() WHERE request_id=%s",
                (workflow_name, request_id),
            )
            wf_link = f"{settings.argo_ui_url}/workflows/{settings.argo_namespace}/{workflow_name}"
            _vk_display = ", ".join(body.vault_keys)
            auto_text = (
                f"🟢 Auto-approved (via {body.source}): Vault {body.vault_operation} "
                f"at `{body.vault_path}` keys=`{_vk_display}` "
                f"by {body.requester_email} (rule: {rule['name']}) | <{wf_link}|Workflow: {workflow_name}>"
            )
            try:
                auto_resp = get_slack_client().chat_postMessage(
                    channel=source_channel, text=auto_text,
                )
                asyncio.create_task(_poll_workflow_status(
                    workflow_name, request_id,
                    auto_resp["channel"], auto_resp["ts"],
                ))
            except Exception as slack_err:
                logger.warning(f"Failed to post Slack auto-approve message: {slack_err}")

            return {
                "status": "auto-approved",
                "request_id": request_id,
                "workflow_name": workflow_name,
                "message": "Auto-approved and workflow submitted",
            }
        else:
            # Send approval message to Slack channel
            total_levels = min(approval_levels, 2)
            msg = build_approval_message(
                request_data, rule, risk, request_id, level=1, total_levels=total_levels,
                channel=source_channel,
            )
            try:
                resp = get_slack_client().chat_postMessage(**msg)
                db_execute(
                    "UPDATE access_requests SET slack_channel=%s, slack_ts=%s WHERE request_id=%s",
                    (resp["channel"], resp["ts"], request_id),
                )

                # Send DMs to approvers — with secrets visible for vault requests
                approver_group = rule.get("lead_group") or "final-approvers"
                dm_msg = build_approval_message(
                    request_data, rule, risk, request_id, level=1, total_levels=total_levels,
                    channel=source_channel, show_secrets=True,
                )
                _send_approver_dms(
                    request_id, approver_group,
                    blocks=dm_msg["blocks"], text=dm_msg["text"],
                    approval_level=1,
                )
            except Exception as slack_err:
                logger.warning(f"Failed to post Slack approval message: {slack_err}")

            return {
                "status": "submitted",
                "request_id": request_id,
                "message": "Request submitted for approval in Slack",
            }

    except Exception as e:
        logger.error(f"Failed to process vault request from {body.source}: {e}")
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)[:200]}")


# ============================================================
# Admin UI
# ============================================================


@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/admin/login")
async def admin_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    if username == settings.admin_username and password == settings.admin_password:
        token = create_session_token(username)
        response = RedirectResponse(url="/admin", status_code=303)
        response.set_cookie("session", token, httponly=True, secure=True, samesite="lax", max_age=86400)
        return response
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": "Invalid credentials"}, status_code=401
    )


@app.get("/admin/logout")
async def admin_logout():
    response = RedirectResponse(url="/admin/login", status_code=303)
    response.delete_cookie("session")
    return response


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request, user: str = Depends(require_admin)):
    stats = {
        "total_rules": db_fetchone("SELECT COUNT(*) as c FROM approval_rules")["c"],
        "total_groups": db_fetchone("SELECT COUNT(*) as c FROM approver_groups")["c"],
        "pending_requests": db_fetchone(
            "SELECT COUNT(*) as c FROM access_requests WHERE status='pending'"
        )["c"],
        "total_requests": db_fetchone("SELECT COUNT(*) as c FROM access_requests")["c"],
    }
    recent = db_fetchall(
        "SELECT * FROM access_requests ORDER BY created_at DESC LIMIT 10"
    )
    return templates.TemplateResponse(
        "dashboard.html", {"request": request, "user": user, "stats": stats, "recent": recent}
    )


@app.get("/admin/rules", response_class=HTMLResponse)
async def admin_rules_page(request: Request, user: str = Depends(require_admin)):
    rules = db_fetchall("SELECT * FROM approval_rules ORDER BY priority ASC")
    return templates.TemplateResponse(
        "rules.html", {"request": request, "user": user, "rules": rules}
    )


@app.get("/admin/rules/new", response_class=HTMLResponse)
async def admin_rule_new(request: Request, user: str = Depends(require_admin)):
    groups = db_fetchall("SELECT group_name FROM approver_groups ORDER BY group_name")
    return templates.TemplateResponse(
        "rule_edit.html",
        {"request": request, "user": user, "rule": None, "groups": groups},
    )


@app.get("/admin/rules/{rule_id}/edit", response_class=HTMLResponse)
async def admin_rule_edit(request: Request, rule_id: int, user: str = Depends(require_admin)):
    rule = db_fetchone("SELECT * FROM approval_rules WHERE id=%s", (rule_id,))
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    groups = db_fetchall("SELECT group_name FROM approver_groups ORDER BY group_name")
    return templates.TemplateResponse(
        "rule_edit.html",
        {"request": request, "user": user, "rule": dict(rule), "groups": groups},
    )


@app.post("/admin/rules/save")
async def admin_rule_save(
    request: Request,
    user: str = Depends(require_admin),
    rule_id: Optional[int] = Form(None),
    name: str = Form(...),
    priority: int = Form(100),
    enabled: bool = Form(False),
    requester_email: str = Form(""),
    who_type: str = Form(""),
    resource_type: str = Form(""),
    permission_level: str = Form(""),
    gcp_project: str = Form(""),
    action_field: str = Form(""),
    access_type: str = Form(""),
    lead_group: str = Form(""),
    second_approval_group: str = Form(""),
    require_reason: bool = Form(False),
    require_ticket: bool = Form(False),
    ticket_pattern: str = Form("ARC-"),
    description: str = Form(""),
):
    # Auto-derive approval settings from group selections
    if not lead_group and not second_approval_group:
        approvers_required = 0
        approval_levels = 0
    elif lead_group and not second_approval_group:
        approvers_required = 1
        approval_levels = 1
    else:
        approvers_required = 1
        approval_levels = 2

    params = (
        name, priority, enabled,
        requester_email or None, who_type or None, resource_type or None,
        permission_level or None,
        gcp_project or None, action_field or None, access_type or None,
        approvers_required, approval_levels, lead_group or None,
        second_approval_group or None,
        require_reason, require_ticket, ticket_pattern or None, description or None,
    )

    if rule_id:
        db_execute(
            """UPDATE approval_rules SET
               name=%s, priority=%s, enabled=%s, requester_email=%s, who_type=%s,
               resource_type=%s, permission_level=%s, gcp_project=%s, action=%s,
               access_type=%s, approvers_required=%s, approval_levels=%s, lead_group=%s,
               second_approval_group=%s,
               require_reason=%s, require_ticket=%s, ticket_pattern=%s,
               description=%s, updated_at=NOW()
               WHERE id=%s""",
            params + (rule_id,),
        )
    else:
        db_execute(
            """INSERT INTO approval_rules
               (name, priority, enabled, requester_email, who_type, resource_type,
                permission_level, gcp_project, action, access_type, approvers_required,
                approval_levels, lead_group, second_approval_group, require_reason,
                require_ticket, ticket_pattern, description, created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            params + (user,),
        )

    return RedirectResponse(url="/admin/rules", status_code=303)


@app.post("/admin/rules/{rule_id}/delete")
async def admin_rule_delete(rule_id: int, user: str = Depends(require_admin)):
    # Nullify FK references in access_requests before deleting (preserves matched_rule_name for history)
    db_execute("UPDATE access_requests SET matched_rule_id=NULL WHERE matched_rule_id=%s", (rule_id,))
    db_execute("DELETE FROM approval_rules WHERE id=%s", (rule_id,))
    return RedirectResponse(url="/admin/rules", status_code=303)


# ============================================================
# Admin Actions: Rerun / Force Approve (password-protected)
# ============================================================


@app.post("/admin/requests/{request_id}/rerun")
async def admin_rerun_workflow(
    request_id: str,
    force_password: str = Form(...),
    user: str = Depends(require_admin),
):
    """Re-submit the Argo workflow for a previously approved request."""
    if force_password != settings.force_approve_password:
        raise HTTPException(status_code=403, detail="Invalid force-approve password")

    req = db_fetchone(
        "SELECT * FROM access_requests WHERE request_id = %s", (request_id,)
    )
    if not req:
        raise HTTPException(404, "Request not found")

    if req["status"] not in ("approved", "auto-approved", "executed", "failed"):
        raise HTTPException(400, "Can only rerun approved/executed/failed requests")

    # Build workflow params from the stored request data
    request_data = dict(req)
    wf_result = await _submit_workflow_for_request(request_data)
    workflow_name = wf_result.get("metadata", {}).get("name", "unknown")

    # Update DB: reset to approved status, clear previous error/executed state
    db_execute(
        """UPDATE access_requests
           SET workflow_name=%s, status='approved', updated_at=NOW(),
               error_message=NULL, executed_at=NULL
           WHERE request_id=%s""",
        (workflow_name, request_id),
    )

    logger.info(f"Admin {user} reran workflow for {request_id}: {workflow_name}")

    # Start poller for the new workflow
    asyncio.create_task(_poll_workflow_status(
        workflow_name, request_id,
        req.get("slack_channel", ""), req.get("slack_ts", ""),
    ))

    return RedirectResponse(url="/admin/history", status_code=303)


@app.post("/admin/requests/{request_id}/force-approve")
async def admin_force_approve(
    request_id: str,
    force_password: str = Form(...),
    user: str = Depends(require_admin),
):
    """Force-approve a request, skipping all approval rules, and submit workflow."""
    if force_password != settings.force_approve_password:
        raise HTTPException(status_code=403, detail="Invalid force-approve password")

    req = db_fetchone(
        "SELECT * FROM access_requests WHERE request_id = %s", (request_id,)
    )
    if not req:
        raise HTTPException(404, "Request not found")

    if req["status"] in ("approved", "auto-approved"):
        raise HTTPException(400, "Request already approved — use Rerun instead")

    # Force approve: update status, record who force-approved
    db_execute(
        """UPDATE access_requests
           SET status='approved',
               final_approved_by=%s, final_approved_at=NOW(),
               updated_at=NOW()
           WHERE request_id=%s""",
        (f"FORCE:{user}", request_id),
    )

    # Submit workflow
    request_data = dict(req)
    wf_result = await _submit_workflow_for_request(request_data)
    workflow_name = wf_result.get("metadata", {}).get("name", "unknown")

    db_execute(
        "UPDATE access_requests SET workflow_name=%s, updated_at=NOW() WHERE request_id=%s",
        (workflow_name, request_id),
    )

    logger.info(f"Admin {user} force-approved {request_id}: {workflow_name}")

    # Start poller
    asyncio.create_task(_poll_workflow_status(
        workflow_name, request_id,
        req.get("slack_channel", ""), req.get("slack_ts", ""),
    ))

    # Notify Slack channel (use stored channel from request, fallback to default)
    notify_channel = req.get("slack_channel") or settings.slack_channel
    wf_link = f"{settings.argo_ui_url}/workflows/{settings.argo_namespace}/{workflow_name}"
    try:
        get_slack_client().chat_postMessage(
            channel=notify_channel,
            text=f"⚡ *Force-approved* by admin `{user}`: "
                 f"{request_data.get('action', 'grant')} {request_data.get('permission_level', '')} "
                 f"for {request_data.get('who_email', '')} on {request_data.get('gcp_project', '')} "
                 f"| <{wf_link}|Workflow: {workflow_name}>",
        )
    except SlackApiError as e:
        logger.error(f"Failed to notify Slack for force-approve {request_id}: {e}")

    return RedirectResponse(url="/admin/history", status_code=303)


@app.get("/admin/groups", response_class=HTMLResponse)
async def admin_groups_page(request: Request, user: str = Depends(require_admin)):
    groups = db_fetchall(
        """SELECT g.*, COALESCE(json_agg(
             json_build_object('id', m.id, 'email', m.email, 'display_name', m.display_name,
                               'slack_user_id', m.slack_user_id)
           ) FILTER (WHERE m.id IS NOT NULL), '[]') as members
           FROM approver_groups g
           LEFT JOIN approver_group_members m ON g.id = m.group_id
           GROUP BY g.id ORDER BY g.group_name"""
    )
    return templates.TemplateResponse(
        "groups.html", {"request": request, "user": user, "groups": groups}
    )


@app.post("/admin/groups/add")
async def admin_group_add(
    request: Request,
    user: str = Depends(require_admin),
    group_name: str = Form(...),
    description: str = Form(""),
):
    db_execute(
        "INSERT INTO approver_groups (group_name, description) VALUES (%s, %s)",
        (group_name, description or None),
    )
    return RedirectResponse(url="/admin/groups", status_code=303)


@app.post("/admin/groups/{group_id}/members/add")
async def admin_member_add(
    group_id: int,
    user: str = Depends(require_admin),
    email: str = Form(...),
    display_name: str = Form(""),
):
    slack_id = resolve_slack_user_id(email)
    db_execute(
        """INSERT INTO approver_group_members (group_id, email, display_name, slack_user_id)
           VALUES (%s, %s, %s, %s) ON CONFLICT (group_id, email) DO UPDATE
           SET display_name=EXCLUDED.display_name, slack_user_id=EXCLUDED.slack_user_id""",
        (group_id, email, display_name or None, slack_id),
    )
    return RedirectResponse(url="/admin/groups", status_code=303)


@app.post("/admin/groups/{group_id}/members/{member_id}/delete")
async def admin_member_delete(
    group_id: int, member_id: int, user: str = Depends(require_admin)
):
    db_execute("DELETE FROM approver_group_members WHERE id=%s AND group_id=%s", (member_id, group_id))
    return RedirectResponse(url="/admin/groups", status_code=303)


@app.post("/admin/groups/{group_id}/delete")
async def admin_group_delete(group_id: int, user: str = Depends(require_admin)):
    """Delete an approver group via the admin UI."""
    db_execute("DELETE FROM approver_groups WHERE id=%s", (group_id,))
    return RedirectResponse(url="/admin/groups", status_code=303)


@app.get("/admin/history", response_class=HTMLResponse)
async def admin_history_page(
    request: Request,
    status: Optional[str] = None,
    user: str = Depends(require_admin),
):
    if status:
        requests_list = db_fetchall(
            "SELECT * FROM access_requests WHERE status=%s ORDER BY created_at DESC LIMIT 100",
            (status,),
        )
    else:
        requests_list = db_fetchall(
            "SELECT * FROM access_requests ORDER BY created_at DESC LIMIT 100"
        )
    return templates.TemplateResponse(
        "history.html",
        {"request": request, "user": user, "requests": requests_list, "filter_status": status},
    )


@app.get("/admin/requests/{request_id}/detail", response_class=HTMLResponse)
async def admin_request_detail_fragment(
    request: Request, request_id: str, user: str = Depends(require_admin)
):
    """Return an HTML fragment with full request details (horizontal pipeline)."""
    req = db_fetchone(
        "SELECT * FROM access_requests WHERE request_id = %s", (request_id,)
    )
    if not req:
        return HTMLResponse(
            '<td colspan="99"><div class="px-6 py-4 text-red-500 text-sm">Request not found</div></td>'
        )
    req = dict(req)

    return templates.TemplateResponse(
        "_detail_fragment.html",
        {
            "request": request,
            "req": req,
            "argo_ui_url": settings.argo_ui_url,
            "argo_namespace": settings.argo_namespace,
        },
    )


# ============================================================
# Teams Management (v1.8.0)
# ============================================================


@app.get("/api/teams")
async def list_teams(user: str = Depends(require_admin)):
    rows = db_fetchall("SELECT * FROM teams ORDER BY team_name")
    return [dict(r) for r in rows]


@app.get("/api/teams/list")
async def list_teams_public():
    """Public endpoint for vault-clone — returns team names only (no admin auth)."""
    rows = db_fetchall("SELECT team_name FROM teams ORDER BY team_name")
    return {"teams": [r["team_name"] for r in rows]}


@app.get("/api/slack-users")
async def list_slack_users(q: str = "", user: str = Depends(require_admin)):
    """Return cached Slack users, optionally filtered by search query."""
    if q:
        rows = db_fetchall(
            """SELECT slack_user_id, email, display_name, real_name, avatar_url
               FROM slack_users
               WHERE is_active = TRUE
                 AND (email ILIKE %s OR display_name ILIKE %s OR real_name ILIKE %s)
               ORDER BY display_name
               LIMIT 50""",
            (f"%{q}%", f"%{q}%", f"%{q}%"),
        )
    else:
        rows = db_fetchall(
            """SELECT slack_user_id, email, display_name, real_name, avatar_url
               FROM slack_users
               WHERE is_active = TRUE
               ORDER BY display_name
               LIMIT 100""",
        )
    return [dict(r) for r in rows]


@app.post("/api/slack-users/sync")
async def trigger_slack_sync(user: str = Depends(require_admin)):
    """Manually trigger Slack user sync."""
    asyncio.create_task(_sync_slack_users())
    return {"status": "sync_started"}


@app.get("/admin/teams", response_class=HTMLResponse)
async def admin_teams_page(request: Request, user: str = Depends(require_admin)):
    teams = db_fetchall(
        """SELECT t.*, g.group_name as lead_group_name
           FROM teams t
           LEFT JOIN approver_groups g ON t.lead_group = g.group_name
           ORDER BY t.team_name"""
    )
    groups = db_fetchall("SELECT group_name FROM approver_groups ORDER BY group_name")
    return templates.TemplateResponse(
        "teams.html",
        {"request": request, "user": user, "teams": teams, "groups": groups},
    )


@app.post("/admin/teams/add")
async def admin_team_add(
    team_name: str = Form(...),
    lead_group: str = Form(""),
    description: str = Form(""),
    user: str = Depends(require_admin),
):
    db_execute(
        "INSERT INTO teams (team_name, lead_group, description) VALUES (%s, %s, %s)",
        (team_name.strip(), lead_group.strip() or None, description.strip()),
    )
    return RedirectResponse("/admin/teams", status_code=303)


@app.post("/admin/teams/{team_id}/edit")
async def admin_team_edit(
    team_id: int,
    team_name: str = Form(...),
    lead_group: str = Form(""),
    description: str = Form(""),
    user: str = Depends(require_admin),
):
    db_execute(
        "UPDATE teams SET team_name=%s, lead_group=%s, description=%s, updated_at=NOW() WHERE id=%s",
        (team_name.strip(), lead_group.strip() or None, description.strip(), team_id),
    )
    return RedirectResponse("/admin/teams", status_code=303)

@app.post("/admin/teams/{team_id}/delete")
async def admin_team_delete(team_id: int, user: str = Depends(require_admin)):
    db_execute("DELETE FROM teams WHERE id = %s", (team_id,))
    return RedirectResponse("/admin/teams", status_code=303)


# ============================================================
# Admin: IAM Role Catalog (v2.1.0)
# ============================================================

@app.get("/admin/catalog", response_class=HTMLResponse)
async def admin_catalog_page(request: Request, category: str = "", user: str = Depends(require_admin)):
    """Admin page for managing IAM Role Catalog."""
    categories = db_fetchall(
        "SELECT * FROM service_categories ORDER BY sort_order, label"
    )
    selected_key = category or (categories[0]["category_key"] if categories else "")
    roles = []
    selected_cat = None
    if selected_key:
        selected_cat = db_fetchone(
            "SELECT * FROM service_categories WHERE category_key = %s", (selected_key,)
        )
        roles = db_fetchall(
            "SELECT * FROM iam_roles WHERE category_key = %s ORDER BY sort_order, display_name",
            (selected_key,),
        )
    return templates.TemplateResponse("catalog.html", {
        "request": request, "user": user,
        "categories": categories, "selected_key": selected_key,
        "selected_cat": selected_cat, "roles": roles,
    })


@app.post("/admin/catalog/category/add")
async def admin_catalog_add_category(
    request: Request,
    category_key: str = Form(...),
    label: str = Form(...),
    resource_level: bool = Form(False),
    resource_placeholder: str = Form(""),
    sort_order: int = Form(100),
    user: str = Depends(require_admin),
):
    db_execute(
        """INSERT INTO service_categories (category_key, label, resource_level, resource_placeholder, enabled, sort_order)
           VALUES (%s, %s, %s, %s, TRUE, %s) ON CONFLICT (category_key) DO NOTHING""",
        (category_key.strip().lower().replace(" ", "-"), label.strip(), resource_level,
         resource_placeholder.strip() or None, sort_order),
    )
    return RedirectResponse(f"/admin/catalog?category={category_key.strip().lower().replace(' ', '-')}", status_code=303)


@app.post("/admin/catalog/category/{cat_key}/toggle")
async def admin_catalog_toggle_category(cat_key: str, user: str = Depends(require_admin)):
    db_execute(
        "UPDATE service_categories SET enabled = NOT enabled, updated_at = NOW() WHERE category_key = %s",
        (cat_key,),
    )
    return RedirectResponse(f"/admin/catalog?category={cat_key}", status_code=303)


@app.post("/admin/catalog/category/{cat_key}/update")
async def admin_catalog_update_category(
    cat_key: str,
    label: str = Form(...),
    resource_level: bool = Form(False),
    resource_placeholder: str = Form(""),
    sort_order: int = Form(100),
    user: str = Depends(require_admin),
):
    db_execute(
        """UPDATE service_categories SET label=%s, resource_level=%s, resource_placeholder=%s,
           sort_order=%s, updated_at=NOW() WHERE category_key = %s""",
        (label.strip(), resource_level, resource_placeholder.strip() or None, sort_order, cat_key),
    )
    return RedirectResponse(f"/admin/catalog?category={cat_key}", status_code=303)


@app.post("/admin/catalog/category/{cat_key}/delete")
async def admin_catalog_delete_category(cat_key: str, user: str = Depends(require_admin)):
    db_execute("DELETE FROM service_categories WHERE category_key = %s", (cat_key,))
    return RedirectResponse("/admin/catalog", status_code=303)


@app.post("/admin/catalog/role/add")
async def admin_catalog_add_role(
    category_key: str = Form(...),
    display_name: str = Form(...),
    role_value: str = Form(...),
    description: str = Form(""),
    sort_order: int = Form(100),
    user: str = Depends(require_admin),
):
    db_execute(
        """INSERT INTO iam_roles (category_key, display_name, role_value, description, enabled, sort_order)
           VALUES (%s, %s, %s, %s, TRUE, %s) ON CONFLICT (category_key, role_value) DO NOTHING""",
        (category_key, display_name.strip(), role_value.strip(), description.strip() or None, sort_order),
    )
    return RedirectResponse(f"/admin/catalog?category={category_key}", status_code=303)


@app.post("/admin/catalog/role/{role_id}/toggle")
async def admin_catalog_toggle_role(role_id: int, user: str = Depends(require_admin)):
    role = db_fetchone("SELECT category_key FROM iam_roles WHERE id = %s", (role_id,))
    db_execute("UPDATE iam_roles SET enabled = NOT enabled WHERE id = %s", (role_id,))
    cat_key = role["category_key"] if role else ""
    return RedirectResponse(f"/admin/catalog?category={cat_key}", status_code=303)


@app.post("/admin/catalog/role/{role_id}/update")
async def admin_catalog_update_role(
    role_id: int,
    display_name: str = Form(...),
    role_value: str = Form(...),
    description: str = Form(""),
    sort_order: int = Form(100),
    user: str = Depends(require_admin),
):
    role = db_fetchone("SELECT category_key FROM iam_roles WHERE id = %s", (role_id,))
    db_execute(
        "UPDATE iam_roles SET display_name=%s, role_value=%s, description=%s, sort_order=%s WHERE id=%s",
        (display_name.strip(), role_value.strip(), description.strip() or None, sort_order, role_id),
    )
    cat_key = role["category_key"] if role else ""
    return RedirectResponse(f"/admin/catalog?category={cat_key}", status_code=303)


@app.post("/admin/catalog/role/{role_id}/delete")
async def admin_catalog_delete_role(role_id: int, user: str = Depends(require_admin)):
    role = db_fetchone("SELECT category_key FROM iam_roles WHERE id = %s", (role_id,))
    db_execute("DELETE FROM iam_roles WHERE id = %s", (role_id,))
    cat_key = role["category_key"] if role else ""
    return RedirectResponse(f"/admin/catalog?category={cat_key}", status_code=303)



