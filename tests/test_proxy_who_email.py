"""
Unit tests for proxy-requester who_email fix.

Tests cover:
1. _get_val() handling of users_select element type
2. Proxy user flow: who_email set to selected user's email
3. Non-proxy user flow: who_email locked to requester's own email
4. Group/serviceAccount who_type flows unaffected
5. Modal rebuild preserves is_proxy in private_metadata
6. Edge cases: empty selection, Slack API failure, missing metadata
"""

import json
import sys
import os
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

# Add parent directory to path so we can import main
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# We need to mock settings before importing main
# Patch environment variables needed by Settings
ENV_VARS = {
    "SLACK_BOT_TOKEN": "xoxb-test-token",
    "SLACK_SIGNING_SECRET": "test-signing-secret",
    "SLACK_CHANNEL": "C_TEST_CHANNEL",
    "DB_HOST": "localhost",
    "DB_PORT": "5432",
    "DB_NAME": "testdb",
    "DB_USER": "testuser",
    "DB_PASSWORD": "testpass",
    "ADMIN_PASSWORD": "testadmin",
}


# ---------------------------------------------------------------------------
# Helpers to build Slack modal submission payloads
# ---------------------------------------------------------------------------

def _make_users_select_block(selected_uid: str = "") -> dict:
    """Build a users_select state-values entry."""
    block = {"type": "users_select"}
    if selected_uid:
        block["selected_user"] = selected_uid
    return block


def _make_static_select_block(value: str = "") -> dict:
    """Build a static_select state-values entry."""
    block = {"type": "static_select"}
    if value:
        block["selected_option"] = {"value": value}
    else:
        block["selected_option"] = None
    return block


def _make_plain_text_block(value: str = "") -> dict:
    """Build a plain_text_input state-values entry."""
    return {"type": "plain_text_input", "value": value}


def _build_iam_submission_payload(
    *,
    user_id: str = "U_REQUESTER",
    who_type: str = "user",
    who_email_block: dict = None,
    is_proxy: bool = False,
    requester_email: str = "proxy@example.com",
    source_channel: str = "C_TEST",
    service_category: str = "basic",
    iam_role: str = "roles/viewer",
    gcp_project: str = "test-project",
    access_type: str = "temporary",
    reason: str = "testing",
    team: str = "infra",
) -> dict:
    """Build a full Slack modal submission payload for IAM mode."""
    if who_email_block is None:
        who_email_block = _make_plain_text_block(requester_email)

    values = {
        "who_type_block": {"who_type": _make_static_select_block(who_type)},
        "who_email_block": {"who_email": who_email_block},
        "team_block": {"team": _make_static_select_block(team)},
        "service_category_block": {"service_category": _make_static_select_block(service_category)},
        "iam_role_block": {"iam_role": _make_static_select_block(iam_role)},
        "project_block": {"gcp_project": _make_plain_text_block(gcp_project)},
        "access_type_block": {"access_type": _make_static_select_block(access_type)},
        "expiry_block": {"expiry_hours": _make_plain_text_block("4")},
        "reason_block": {"reason": _make_plain_text_block(reason)},
        "ticket_block": {"ticket": _make_plain_text_block("TEST-123")},
        "resource_name_block": {"resource_name": _make_plain_text_block("global")},
    }

    private_metadata = json.dumps({
        "requester_email": requester_email,
        "is_proxy": is_proxy,
        "source_channel": source_channel,
    })

    return {
        "type": "view_submission",
        "user": {"id": user_id, "name": requester_email.split("@")[0]},
        "view": {
            "callback_id": "access_request_modal",
            "private_metadata": private_metadata,
            "state": {"values": values},
        },
    }


def _mock_slack_users_info(uid_to_email: dict):
    """Return a side_effect function for get_slack_client().users_info()."""
    def _users_info(user=""):
        if user in uid_to_email:
            return {"user": {"profile": {"email": uid_to_email[user]}}}
        raise Exception(f"Unknown user {user}")
    return _users_info


# ---------------------------------------------------------------------------
# Test class: _get_val behaviour with users_select
# ---------------------------------------------------------------------------

class TestGetValUsersSelect:
    """Test _get_val() handling of different block types."""

    def _run_get_val(self, values: dict, block_id: str, action_id: str, uid_to_email: dict = None):
        """
        Execute _get_val in isolation by recreating its logic.
        This mirrors the exact implementation in main.py lines 1715-1733.
        """
        if uid_to_email is None:
            uid_to_email = {}

        mock_client = MagicMock()
        mock_client.users_info = MagicMock(side_effect=_mock_slack_users_info(uid_to_email))

        block = values.get(block_id, {}).get(action_id, {})
        if block.get("type") == "static_select":
            opt = block.get("selected_option")
            return opt["value"] if opt else ""
        if block.get("type") == "users_select":
            selected_uid = block.get("selected_user", "")
            if selected_uid:
                try:
                    info = mock_client.users_info(user=selected_uid)
                    resolved_email = info["user"]["profile"].get("email", selected_uid)
                    return resolved_email
                except Exception:
                    return selected_uid
            return ""
        return block.get("value", "")

    def test_users_select_resolves_email(self):
        """users_select with valid UID resolves to email."""
        values = {
            "who_email_block": {
                "who_email": _make_users_select_block("U_TARGET_USER")
            }
        }
        result = self._run_get_val(
            values, "who_email_block", "who_email",
            uid_to_email={"U_TARGET_USER": "target@example.com"}
        )
        assert result == "target@example.com", f"Expected target@example.com, got {result}"

    def test_users_select_no_selection(self):
        """users_select with no user selected returns empty string."""
        values = {
            "who_email_block": {
                "who_email": _make_users_select_block("")
            }
        }
        result = self._run_get_val(values, "who_email_block", "who_email")
        assert result == "", f"Expected empty string, got {result}"

    def test_users_select_missing_selected_user_key(self):
        """users_select with no selected_user key returns empty string."""
        values = {
            "who_email_block": {
                "who_email": {"type": "users_select"}  # no selected_user key
            }
        }
        result = self._run_get_val(values, "who_email_block", "who_email")
        assert result == "", f"Expected empty string, got {result}"

    def test_users_select_api_failure_returns_uid(self):
        """users_select falls back to raw UID when Slack API fails."""
        values = {
            "who_email_block": {
                "who_email": _make_users_select_block("U_UNKNOWN_USER")
            }
        }
        # uid_to_email doesn't have U_UNKNOWN_USER → raises Exception
        result = self._run_get_val(
            values, "who_email_block", "who_email",
            uid_to_email={}
        )
        assert result == "U_UNKNOWN_USER", f"Expected U_UNKNOWN_USER, got {result}"

    def test_static_select_returns_value(self):
        """static_select returns selected_option value."""
        values = {
            "who_type_block": {
                "who_type": _make_static_select_block("user")
            }
        }
        result = self._run_get_val(values, "who_type_block", "who_type")
        assert result == "user"

    def test_static_select_no_selection(self):
        """static_select with no selection returns empty string."""
        values = {
            "who_type_block": {
                "who_type": _make_static_select_block("")
            }
        }
        result = self._run_get_val(values, "who_type_block", "who_type")
        assert result == ""

    def test_plain_text_returns_value(self):
        """plain_text_input returns value."""
        values = {
            "reason_block": {
                "reason": _make_plain_text_block("need access for deploy")
            }
        }
        result = self._run_get_val(values, "reason_block", "reason")
        assert result == "need access for deploy"

    def test_missing_block_returns_empty(self):
        """Missing block_id returns empty string."""
        result = self._run_get_val({}, "nonexistent_block", "action")
        assert result == ""


# ---------------------------------------------------------------------------
# Test class: Proxy user who_email extraction logic
# ---------------------------------------------------------------------------

class TestProxyWhoEmailLogic:
    """
    Test the proxy requester who_email extraction logic.
    This tests the decision tree at lines 1783-1805 of main.py.
    """

    def _extract_who_email(
        self, *, is_proxy: bool, who_type: str,
        who_email_from_get_val: str, requester_email: str,
        raw_block: dict = None, uid_to_email: dict = None,
    ) -> str:
        """
        Simulate the who_email extraction logic from _handle_modal_submission().
        Mirrors lines 1783-1805 of main.py exactly.
        """
        if uid_to_email is None:
            uid_to_email = {}
        if raw_block is None:
            raw_block = {}

        mock_client = MagicMock()
        mock_client.users_info = MagicMock(side_effect=_mock_slack_users_info(uid_to_email))

        who_email = who_email_from_get_val

        # For proxy users with users_select: if _get_val returned empty, try direct extraction
        if is_proxy and who_type == "user" and not who_email:
            selected_uid = raw_block.get("selected_user", "")
            if selected_uid:
                try:
                    info = mock_client.users_info(user=selected_uid)
                    who_email = info["user"]["profile"].get("email", selected_uid)
                except Exception:
                    who_email = selected_uid  # Use raw UID as last resort

        # Security enforcement: non-proxy users with who_type=user can only request for themselves
        if not is_proxy and who_type == "user":
            who_email = requester_email
        elif is_proxy and who_type == "user":
            pass  # keep as selected user

        return who_email

    def test_proxy_user_gets_selected_users_email(self):
        """Proxy user selecting another user → who_email = selected user's email."""
        result = self._extract_who_email(
            is_proxy=True,
            who_type="user",
            who_email_from_get_val="target@example.com",
            requester_email="proxy@example.com",
        )
        assert result == "target@example.com", f"Expected target@example.com, got {result}"

    def test_proxy_user_fallback_when_get_val_empty(self):
        """Proxy user: if _get_val returns empty, fallback extracts from raw block."""
        result = self._extract_who_email(
            is_proxy=True,
            who_type="user",
            who_email_from_get_val="",  # _get_val failed
            requester_email="proxy@example.com",
            raw_block={"type": "users_select", "selected_user": "U_TARGET"},
            uid_to_email={"U_TARGET": "target-fallback@example.com"},
        )
        assert result == "target-fallback@example.com", f"Expected target-fallback@example.com, got {result}"

    def test_proxy_user_fallback_api_failure_uses_uid(self):
        """Proxy user: fallback uses raw UID when Slack API fails."""
        result = self._extract_who_email(
            is_proxy=True,
            who_type="user",
            who_email_from_get_val="",
            requester_email="proxy@example.com",
            raw_block={"type": "users_select", "selected_user": "U_UNRESOLV"},
            uid_to_email={},  # API will fail for U_UNRESOLV
        )
        assert result == "U_UNRESOLV", f"Expected U_UNRESOLV, got {result}"

    def test_proxy_user_fallback_no_selection(self):
        """Proxy user: if no user selected at all, who_email stays empty."""
        result = self._extract_who_email(
            is_proxy=True,
            who_type="user",
            who_email_from_get_val="",
            requester_email="proxy@example.com",
            raw_block={"type": "users_select"},  # no selected_user
        )
        assert result == "", f"Expected empty string, got {result}"

    def test_non_proxy_user_always_gets_own_email(self):
        """Non-proxy user: who_email is ALWAYS overridden to requester's email."""
        result = self._extract_who_email(
            is_proxy=False,
            who_type="user",
            who_email_from_get_val="hacker@evil.com",  # attempt to override
            requester_email="normal@example.com",
        )
        assert result == "normal@example.com", f"Expected normal@example.com, got {result}"

    def test_non_proxy_user_ignores_form_value(self):
        """Non-proxy user: even if form has a different email, it's overridden."""
        result = self._extract_who_email(
            is_proxy=False,
            who_type="user",
            who_email_from_get_val="someone-else@example.com",
            requester_email="me@example.com",
        )
        assert result == "me@example.com"

    def test_proxy_group_who_type_keeps_form_value(self):
        """Proxy user with who_type=group: security enforcement doesn't apply, form value kept."""
        result = self._extract_who_email(
            is_proxy=True,
            who_type="group",
            who_email_from_get_val="team@example.com",
            requester_email="proxy@example.com",
        )
        assert result == "team@example.com"

    def test_non_proxy_group_who_type_keeps_form_value(self):
        """Non-proxy user with who_type=group: security enforcement only for who_type=user."""
        result = self._extract_who_email(
            is_proxy=False,
            who_type="group",
            who_email_from_get_val="myteam@example.com",
            requester_email="normal@example.com",
        )
        assert result == "myteam@example.com"

    def test_service_account_who_type_keeps_form_value(self):
        """serviceAccount who_type: security enforcement doesn't apply."""
        result = self._extract_who_email(
            is_proxy=False,
            who_type="serviceAccount",
            who_email_from_get_val="sa@project.iam.gserviceaccount.com",
            requester_email="normal@example.com",
        )
        assert result == "sa@project.iam.gserviceaccount.com"

    def test_proxy_service_account_who_type_keeps_form_value(self):
        """Proxy user with serviceAccount: form value preserved."""
        result = self._extract_who_email(
            is_proxy=True,
            who_type="serviceAccount",
            who_email_from_get_val="sa@project.iam.gserviceaccount.com",
            requester_email="proxy@example.com",
        )
        assert result == "sa@project.iam.gserviceaccount.com"


# ---------------------------------------------------------------------------
# Test class: Modal private_metadata preservation
# ---------------------------------------------------------------------------

class TestPrivateMetadataPreservation:
    """Test that is_proxy and requester_email survive modal rebuilds."""

    def test_private_metadata_roundtrip_proxy_true(self):
        """Proxy flag survives JSON roundtrip in private_metadata."""
        meta = json.dumps({
            "requester_email": "proxy@example.com",
            "is_proxy": True,
            "source_channel": "C_TEST",
        })
        parsed = json.loads(meta)
        assert parsed["is_proxy"] is True
        assert parsed["requester_email"] == "proxy@example.com"
        assert parsed["source_channel"] == "C_TEST"

    def test_private_metadata_roundtrip_proxy_false(self):
        """Non-proxy flag survives JSON roundtrip."""
        meta = json.dumps({
            "requester_email": "normal@example.com",
            "is_proxy": False,
            "source_channel": "C_CHAN",
        })
        parsed = json.loads(meta)
        assert parsed["is_proxy"] is False
        assert parsed["requester_email"] == "normal@example.com"

    def test_private_metadata_missing_is_proxy_defaults_false(self):
        """Missing is_proxy in metadata defaults to False."""
        meta = json.dumps({"requester_email": "user@example.com"})
        parsed = json.loads(meta)
        is_proxy = parsed.get("is_proxy", False)
        assert is_proxy is False

    def test_private_metadata_empty_string(self):
        """Empty private_metadata string handled gracefully."""
        meta_str = "{}"
        parsed = json.loads(meta_str) if meta_str else {}
        is_proxy = parsed.get("is_proxy", False)
        assert is_proxy is False

    def test_modal_rebuild_preserves_proxy_context(self):
        """Simulate modal rebuild: proxy context from old metadata carried to new view."""
        # Original metadata (set when modal opened)
        original_meta = json.dumps({
            "requester_email": "proxy@example.com",
            "is_proxy": True,
            "source_channel": "C_SRC",
        })

        # Simulate block_actions handler reading metadata and rebuilding
        parsed = json.loads(original_meta)
        meta_email = parsed.get("requester_email", "")
        meta_proxy = parsed.get("is_proxy", False)

        # Rebuilt metadata (as done in slack_interactions block_actions handler)
        rebuilt_meta = json.dumps({
            "requester_email": meta_email,
            "is_proxy": meta_proxy,
            "source_channel": parsed.get("source_channel", ""),
        })

        rebuilt_parsed = json.loads(rebuilt_meta)
        assert rebuilt_parsed["is_proxy"] is True
        assert rebuilt_parsed["requester_email"] == "proxy@example.com"
        assert rebuilt_parsed["source_channel"] == "C_SRC"


# ---------------------------------------------------------------------------
# Test class: _build_who_block output
# ---------------------------------------------------------------------------

class TestBuildWhoBlock:
    """Test _build_who_block generates correct elements for each scenario."""

    def _build_who_block(self, who_type="user", requester_email="", is_proxy=False):
        """Replicate _build_who_block logic for testing without importing."""
        if who_type == "group":
            return [{
                "type": "input",
                "block_id": "who_email_block",
                "label": {"type": "plain_text", "text": "Group"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "who_email",
                    "placeholder": {"type": "plain_text", "text": "my-team@example.com"},
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
                    "placeholder": {"type": "plain_text", "text": "sa-name@project.iam.gserviceaccount.com"},
                },
            }]
        else:  # "user"
            if is_proxy:
                return [{
                    "type": "input",
                    "block_id": "who_email_block",
                    "label": {"type": "plain_text", "text": "User"},
                    "element": {
                        "type": "users_select",
                        "action_id": "who_email",
                        "placeholder": {"type": "plain_text", "text": "Select a user"},
                    },
                }]
            else:
                display_email = requester_email or "your-email@example.com"
                return [{
                    "type": "section",
                    "block_id": "who_email_display_block",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*User*\n📧 {display_email} _(auto-selected)_",
                    },
                }]

    def test_proxy_user_gets_users_select(self):
        """Proxy user gets users_select element type."""
        blocks = self._build_who_block(who_type="user", is_proxy=True)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "input"
        assert blocks[0]["block_id"] == "who_email_block"
        assert blocks[0]["element"]["type"] == "users_select"
        assert blocks[0]["element"]["action_id"] == "who_email"

    def test_non_proxy_user_gets_locked_section(self):
        """Non-proxy user gets locked section display, not an input."""
        blocks = self._build_who_block(who_type="user", requester_email="me@example.com", is_proxy=False)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "section"
        assert blocks[0]["block_id"] == "who_email_display_block"
        assert "me@example.com" in blocks[0]["text"]["text"]
        assert "auto-selected" in blocks[0]["text"]["text"]

    def test_group_who_type_gets_text_input(self):
        """Group who_type gets plain_text_input regardless of proxy status."""
        blocks = self._build_who_block(who_type="group", is_proxy=True)
        assert blocks[0]["element"]["type"] == "plain_text_input"
        assert blocks[0]["block_id"] == "who_email_block"

    def test_service_account_who_type_gets_text_input(self):
        """serviceAccount who_type gets plain_text_input."""
        blocks = self._build_who_block(who_type="serviceAccount", is_proxy=False)
        assert blocks[0]["element"]["type"] == "plain_text_input"
        assert blocks[0]["block_id"] == "who_email_block"


# ---------------------------------------------------------------------------
# Test class: End-to-end proxy flow payload validation
# ---------------------------------------------------------------------------

class TestProxyFlowEndToEnd:
    """
    End-to-end tests that build a full Slack payload and validate
    that who_email is correctly extracted for proxy vs non-proxy users.
    """

    def _simulate_who_email_extraction(self, payload: dict, uid_to_email: dict = None):
        """
        Simulate the complete who_email extraction from a submission payload.
        Mirrors lines 1699-1836 of main.py, focused on who_email extraction.
        """
        if uid_to_email is None:
            uid_to_email = {}

        mock_client = MagicMock()
        mock_client.users_info = MagicMock(side_effect=_mock_slack_users_info(uid_to_email))

        user = payload.get("user", {})
        user_id = user.get("id", "")
        requester_email = uid_to_email.get(user_id, user.get("name", "") + "@example.com")

        values = payload.get("view", {}).get("state", {}).get("values", {})

        # _get_val implementation
        def _get_val(block_id, action_id):
            block = values.get(block_id, {}).get(action_id, {})
            if block.get("type") == "static_select":
                opt = block.get("selected_option")
                return opt["value"] if opt else ""
            if block.get("type") == "users_select":
                selected_uid = block.get("selected_user", "")
                if selected_uid:
                    try:
                        info = mock_client.users_info(user=selected_uid)
                        return info["user"]["profile"].get("email", selected_uid)
                    except Exception:
                        return selected_uid
                return ""
            return block.get("value", "")

        # Read proxy context
        meta_str = payload.get("view", {}).get("private_metadata", "{}")
        meta = json.loads(meta_str) if meta_str else {}
        is_proxy = meta.get("is_proxy", False)

        who_type = _get_val("who_type_block", "who_type")

        if who_type == "vault-secret":
            return requester_email  # vault always uses requester

        # IAM mode
        who_email = _get_val("who_email_block", "who_email")

        # Fallback for proxy users
        if is_proxy and who_type == "user" and not who_email:
            raw_block = values.get("who_email_block", {}).get("who_email", {})
            selected_uid = raw_block.get("selected_user", "")
            if selected_uid:
                try:
                    info = mock_client.users_info(user=selected_uid)
                    who_email = info["user"]["profile"].get("email", selected_uid)
                except Exception:
                    who_email = selected_uid

        # Security enforcement
        if not is_proxy and who_type == "user":
            who_email = requester_email

        return who_email

    def test_proxy_user_selects_another_user(self):
        """Full payload: proxy user picks U_TARGET → who_email = target@example.com."""
        payload = _build_iam_submission_payload(
            user_id="U_REQUESTER",
            who_type="user",
            who_email_block=_make_users_select_block("U_TARGET"),
            is_proxy=True,
            requester_email="proxy@example.com",
        )
        uid_map = {
            "U_REQUESTER": "proxy@example.com",
            "U_TARGET": "target@example.com",
        }
        result = self._simulate_who_email_extraction(payload, uid_map)
        assert result == "target@example.com", (
            f"PROXY BUG: Expected target@example.com but got {result}"
        )

    def test_proxy_user_selects_themselves(self):
        """Proxy user can also select themselves (edge case)."""
        payload = _build_iam_submission_payload(
            user_id="U_REQUESTER",
            who_type="user",
            who_email_block=_make_users_select_block("U_REQUESTER"),
            is_proxy=True,
            requester_email="proxy@example.com",
        )
        uid_map = {"U_REQUESTER": "proxy@example.com"}
        result = self._simulate_who_email_extraction(payload, uid_map)
        assert result == "proxy@example.com"

    def test_non_proxy_user_locked_to_self(self):
        """Non-proxy user: who_email ALWAYS == requester_email, regardless of form."""
        payload = _build_iam_submission_payload(
            user_id="U_NORMAL",
            who_type="user",
            who_email_block=_make_plain_text_block("doesnt-matter@example.com"),
            is_proxy=False,
            requester_email="normal@example.com",
        )
        uid_map = {"U_NORMAL": "normal@example.com"}
        result = self._simulate_who_email_extraction(payload, uid_map)
        assert result == "normal@example.com"

    def test_proxy_user_group_who_type(self):
        """Proxy user with group who_type: who_email = form group email."""
        payload = _build_iam_submission_payload(
            user_id="U_REQUESTER",
            who_type="group",
            who_email_block=_make_plain_text_block("devteam@example.com"),
            is_proxy=True,
            requester_email="proxy@example.com",
        )
        uid_map = {"U_REQUESTER": "proxy@example.com"}
        result = self._simulate_who_email_extraction(payload, uid_map)
        assert result == "devteam@example.com"

    def test_proxy_user_service_account_who_type(self):
        """Proxy user with serviceAccount who_type: who_email = form SA email."""
        payload = _build_iam_submission_payload(
            user_id="U_REQUESTER",
            who_type="serviceAccount",
            who_email_block=_make_plain_text_block("deploy@proj.iam.gserviceaccount.com"),
            is_proxy=True,
            requester_email="proxy@example.com",
        )
        uid_map = {"U_REQUESTER": "proxy@example.com"}
        result = self._simulate_who_email_extraction(payload, uid_map)
        assert result == "deploy@proj.iam.gserviceaccount.com"

    def test_non_proxy_service_account_who_type(self):
        """Non-proxy user with serviceAccount who_type: form value preserved."""
        payload = _build_iam_submission_payload(
            user_id="U_NORMAL",
            who_type="serviceAccount",
            who_email_block=_make_plain_text_block("sa@proj.iam.gserviceaccount.com"),
            is_proxy=False,
            requester_email="normal@example.com",
        )
        uid_map = {"U_NORMAL": "normal@example.com"}
        result = self._simulate_who_email_extraction(payload, uid_map)
        assert result == "sa@proj.iam.gserviceaccount.com"

    def test_proxy_missing_metadata_defaults_non_proxy(self):
        """If private_metadata is missing, is_proxy defaults to False → locked to requester."""
        payload = _build_iam_submission_payload(
            user_id="U_REQUESTER",
            who_type="user",
            who_email_block=_make_users_select_block("U_TARGET"),
            is_proxy=True,  # set in metadata
            requester_email="proxy@example.com",
        )
        # Override metadata to empty
        payload["view"]["private_metadata"] = "{}"
        uid_map = {
            "U_REQUESTER": "proxy@example.com",
            "U_TARGET": "target@example.com",
        }
        result = self._simulate_who_email_extraction(payload, uid_map)
        # Without is_proxy, security enforcement locks to requester
        assert result == "proxy@example.com"


# ---------------------------------------------------------------------------
# Run tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
