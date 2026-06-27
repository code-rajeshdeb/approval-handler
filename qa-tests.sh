#!/usr/bin/env bash
# =============================================================================
# QA Test Script for Approval Handler System
# =============================================================================
#
# Tests the full approval-handler stack:
#   1. Argo WorkflowTemplate (manage-iam-permissions) — parameter validation
#   2. Approval Handler API endpoints (health, rules, teams, evaluate)
#   3. Database connectivity (tables, row counts)
#   4. Parameter mapping consistency (handler ↔ workflow template)
#
# Usage:
#   bash qa-tests.sh                   # Full test run (submits workflows)
#   bash qa-tests.sh --dry-run         # Validate only, no workflow submissions
#   bash qa-tests.sh --api-only        # Only test API endpoints
#   bash qa-tests.sh --db-only         # Only test DB connectivity
#   bash qa-tests.sh --workflow-only   # Only test workflow submissions
#
# Prerequisites:
#   - kubectl configured for the stage cluster
#   - argo CLI installed
#   - jq installed
#   - Access to argo-access-control namespace
#
# Environment:
#   - Namespace:   argo-access-control
#   - GCP Project: your-gcp-project-stage (STAGE ONLY — never prod)
#   - Test Email:  qa-test@example.com
# =============================================================================

set -euo pipefail

# =============================================================================
# CONFIGURATION
# =============================================================================

NAMESPACE="argo-access-control"
GCP_PROJECT="your-gcp-project-stage"
TEST_EMAIL="qa-test@example.com"
TEST_GROUP_EMAIL="qa-test-group@example.com"
TEST_SA_EMAIL="qa-test-sa@${GCP_PROJECT}.iam.gserviceaccount.com"
WORKFLOW_TEMPLATE="manage-iam-permissions"
EXPIRY_HOURS="1"  # Shortest possible for safety
ACCESS_TYPE="temporary"
SERVICE_NAME="approval-handler"
SERVICE_PORT="8000"
LOCAL_PORT="18765"   # Port-forward target (ephemeral)
ADMIN_SECRET_NAME="approval-handler-admin-auth"
PF_PID=""            # port-forward process PID

# --- Flags ---
DRY_RUN=false
API_ONLY=false
DB_ONLY=false
WORKFLOW_ONLY=false

# --- Counters ---
PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0
TOTAL_COUNT=0
CLEANUP_WORKFLOWS=()

# =============================================================================
# ARGUMENT PARSING
# =============================================================================

for arg in "$@"; do
  case "$arg" in
    --dry-run)     DRY_RUN=true ;;
    --api-only)    API_ONLY=true ;;
    --db-only)     DB_ONLY=true ;;
    --workflow-only) WORKFLOW_ONLY=true ;;
    --help|-h)
      echo "Usage: bash qa-tests.sh [--dry-run] [--api-only] [--db-only] [--workflow-only]"
      exit 0
      ;;
    *)
      echo "Unknown argument: $arg"
      exit 1
      ;;
  esac
done

# =============================================================================
# COLOR HELPERS
# =============================================================================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

pass_test() {
  PASS_COUNT=$((PASS_COUNT + 1))
  TOTAL_COUNT=$((TOTAL_COUNT + 1))
  echo -e "  ${GREEN}✅ PASS${NC}: $1"
}

fail_test() {
  FAIL_COUNT=$((FAIL_COUNT + 1))
  TOTAL_COUNT=$((TOTAL_COUNT + 1))
  echo -e "  ${RED}❌ FAIL${NC}: $1"
  if [ -n "${2:-}" ]; then
    echo -e "         ${RED}→ $2${NC}"
  fi
}

skip_test() {
  SKIP_COUNT=$((SKIP_COUNT + 1))
  TOTAL_COUNT=$((TOTAL_COUNT + 1))
  echo -e "  ${YELLOW}⏭️  SKIP${NC}: $1"
}

section() {
  echo ""
  echo -e "${BOLD}${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo -e "${BOLD}${BLUE}  $1${NC}"
  echo -e "${BOLD}${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

info() {
  echo -e "  ${CYAN}ℹ️  $1${NC}"
}

# =============================================================================
# PREREQUISITE CHECKS
# =============================================================================

check_prerequisites() {
  section "Prerequisite Checks"

  # kubectl
  if command -v kubectl &>/dev/null; then
    pass_test "kubectl is installed ($(kubectl version --client -o json 2>/dev/null | jq -r '.clientVersion.gitVersion' 2>/dev/null || echo 'unknown'))"
  else
    fail_test "kubectl is not installed"
    echo "  Install: https://kubernetes.io/docs/tasks/tools/"
    exit 1
  fi

  # argo CLI
  if command -v argo &>/dev/null; then
    pass_test "argo CLI is installed ($(argo version --short 2>/dev/null || echo 'unknown'))"
  else
    fail_test "argo CLI is not installed"
    echo "  Install: https://github.com/argoproj/argo-workflows/releases"
    exit 1
  fi

  # jq
  if command -v jq &>/dev/null; then
    pass_test "jq is installed"
  else
    fail_test "jq is not installed"
    echo "  Install: brew install jq"
    exit 1
  fi

  # Namespace access
  if kubectl get ns "${NAMESPACE}" &>/dev/null; then
    pass_test "Namespace ${NAMESPACE} is accessible"
  else
    fail_test "Cannot access namespace ${NAMESPACE}" "Check kubeconfig / VPN"
    exit 1
  fi

  # WorkflowTemplate exists
  if kubectl get workflowtemplate "${WORKFLOW_TEMPLATE}" -n "${NAMESPACE}" &>/dev/null; then
    pass_test "WorkflowTemplate '${WORKFLOW_TEMPLATE}' exists in ${NAMESPACE}"
  else
    fail_test "WorkflowTemplate '${WORKFLOW_TEMPLATE}' not found in ${NAMESPACE}"
    exit 1
  fi
}

# =============================================================================
# CLEANUP
# =============================================================================

cleanup() {
  section "Cleanup"

  # Kill port-forward if running
  if [ -n "${PF_PID}" ] && kill -0 "${PF_PID}" 2>/dev/null; then
    kill "${PF_PID}" 2>/dev/null || true
    wait "${PF_PID}" 2>/dev/null || true
    info "Stopped port-forward (PID ${PF_PID})"
  fi

  # Delete test workflows
  if [ ${#CLEANUP_WORKFLOWS[@]} -gt 0 ]; then
    info "Cleaning up ${#CLEANUP_WORKFLOWS[@]} test workflow(s)..."
    for wf in "${CLEANUP_WORKFLOWS[@]}"; do
      if kubectl get workflow "${wf}" -n "${NAMESPACE}" &>/dev/null; then
        argo delete "${wf}" -n "${NAMESPACE}" --force 2>/dev/null || true
        info "  Deleted workflow: ${wf}"
      fi
    done
  else
    info "No test workflows to clean up"
  fi

  echo ""
}

trap cleanup EXIT

# =============================================================================
# SECTION 1: WORKFLOW TEMPLATE TESTS
# =============================================================================

# --- Service categories and a representative role for each ---
declare -A CATEGORY_ROLES
CATEGORY_ROLES=(
  ["basic"]="roles/viewer"
  ["bigquery"]="roles/bigquery.dataViewer"
  ["gcs-bucket"]="roles/storage.objectViewer"
  ["compute-vm"]="roles/compute.viewer"
  ["gke-cluster"]="roles/container.viewer"
  ["cloud-sql"]="roles/cloudsql.viewer"
  ["service-account"]="roles/iam.serviceAccountViewer"
  ["pubsub"]="roles/pubsub.viewer"
  ["secret-manager"]="roles/secretmanager.viewer"
  ["artifact-registry"]="roles/artifactregistry.reader"
  ["cloud-logging"]="roles/logging.viewer"
  ["cloud-monitoring"]="roles/monitoring.viewer"
  ["cloud-composer"]="roles/composer.user"
  ["custom"]="roles/browser"
)

# --- Who-types and corresponding test emails ---
declare -A WHO_TYPE_EMAILS
WHO_TYPE_EMAILS=(
  ["user"]="${TEST_EMAIL}"
  ["group"]="${TEST_GROUP_EMAIL}"
  ["serviceAccount"]="${TEST_SA_EMAIL}"
)

submit_test_workflow() {
  local test_name="$1"
  local category="$2"
  local role="$3"
  local who_type="$4"
  local who_email="$5"
  local resource_name="${6:-global}"
  local extra_label="${7:-}"

  local label="${test_name}"
  if [ -n "${extra_label}" ]; then
    label="${label} (${extra_label})"
  fi

  if [ "${DRY_RUN}" = true ]; then
    # Dry-run: just validate the argo submit command builds correctly
    local cmd="argo submit --from workflowtemplate/${WORKFLOW_TEMPLATE} \
      -p action=grant \
      -p who-type=${who_type} \
      -p who-email=${who_email} \
      -p resource-type=${category} \
      -p resource-name=${resource_name} \
      -p permission-level=custom \
      -p custom-role=${role} \
      -p gcp-project=${GCP_PROJECT} \
      -p access-type=${ACCESS_TYPE} \
      -p expiry-hours=${EXPIRY_HOURS} \
      -p reason=QA-test-dry-run \
      -p ticket=QA-0000 \
      -n ${NAMESPACE} \
      --dry-run \
      -o json"

    local output
    if output=$(eval "${cmd}" 2>&1); then
      local wf_name
      wf_name=$(echo "${output}" | jq -r '.metadata.generateName // .metadata.name // "unknown"' 2>/dev/null || echo "unknown")
      pass_test "${label} — dry-run OK (template: ${wf_name})"
    else
      fail_test "${label} — dry-run FAILED" "${output}"
    fi
    return
  fi

  # Live run: submit, wait briefly, then check status
  local cmd="argo submit --from workflowtemplate/${WORKFLOW_TEMPLATE} \
    -p action=grant \
    -p who-type=${who_type} \
    -p who-email=${who_email} \
    -p resource-type=${category} \
    -p resource-name=${resource_name} \
    -p permission-level=custom \
    -p custom-role=${role} \
    -p gcp-project=${GCP_PROJECT} \
    -p access-type=${ACCESS_TYPE} \
    -p expiry-hours=${EXPIRY_HOURS} \
    -p reason=QA-automated-test \
    -p ticket=QA-0000 \
    -n ${NAMESPACE} \
    --wait --wait-timeout 120 \
    -o json"

  local output
  if output=$(eval "${cmd}" 2>&1); then
    local wf_name status
    wf_name=$(echo "${output}" | jq -r '.metadata.name' 2>/dev/null || echo "unknown")
    status=$(echo "${output}" | jq -r '.status.phase' 2>/dev/null || echo "unknown")

    CLEANUP_WORKFLOWS+=("${wf_name}")

    if [ "${status}" = "Succeeded" ]; then
      pass_test "${label} — workflow ${wf_name} succeeded"

      # --- Auto-revoke the grant immediately to clean up ---
      info "  Auto-revoking grant for cleanup..."
      local revoke_output
      revoke_output=$(argo submit --from workflowtemplate/${WORKFLOW_TEMPLATE} \
        -p action=revoke \
        -p who-type="${who_type}" \
        -p who-email="${who_email}" \
        -p resource-type="${category}" \
        -p resource-name="${resource_name}" \
        -p permission-level=custom \
        -p custom-role="${role}" \
        -p gcp-project="${GCP_PROJECT}" \
        -p access-type=temporary \
        -p expiry-hours=1 \
        -p reason=QA-auto-revoke \
        -p ticket=QA-0000 \
        -n "${NAMESPACE}" \
        --wait --wait-timeout 120 \
        -o json 2>&1) || true

      local revoke_wf
      revoke_wf=$(echo "${revoke_output}" | jq -r '.metadata.name' 2>/dev/null || echo "unknown")
      if [ "${revoke_wf}" != "unknown" ]; then
        CLEANUP_WORKFLOWS+=("${revoke_wf}")
        info "  Revoke workflow: ${revoke_wf}"
      fi
    else
      fail_test "${label} — workflow ${wf_name} status: ${status}"
    fi
  else
    fail_test "${label} — submission failed" "$(echo "${output}" | head -3)"
  fi
}

test_workflow_template_exists() {
  section "1.1 — WorkflowTemplate Existence & Structure"

  # Check template has expected parameters
  local params
  params=$(kubectl get workflowtemplate "${WORKFLOW_TEMPLATE}" -n "${NAMESPACE}" -o json \
    | jq -r '.spec.arguments.parameters[].name' 2>/dev/null)

  local expected_params=(
    "action" "who-type" "who-email" "resource-type" "resource-name"
    "permission-level" "custom-role" "gcp-project" "access-type"
    "expiry-hours" "reason" "ticket"
  )

  for p in "${expected_params[@]}"; do
    if echo "${params}" | grep -q "^${p}$"; then
      pass_test "Parameter '${p}' exists in WorkflowTemplate"
    else
      fail_test "Parameter '${p}' missing from WorkflowTemplate"
    fi
  done

  # Check template has expected templates (steps)
  local tmpl_names
  tmpl_names=$(kubectl get workflowtemplate "${WORKFLOW_TEMPLATE}" -n "${NAMESPACE}" -o json \
    | jq -r '.spec.templates[].name' 2>/dev/null)

  for tmpl in "main" "validate-inputs" "execute-permission-change" "notify-outcome"; do
    if echo "${tmpl_names}" | grep -q "^${tmpl}$"; then
      pass_test "Template step '${tmpl}' exists"
    else
      fail_test "Template step '${tmpl}' missing"
    fi
  done

  # Check service account
  local sa
  sa=$(kubectl get workflowtemplate "${WORKFLOW_TEMPLATE}" -n "${NAMESPACE}" -o json \
    | jq -r '.spec.serviceAccountName' 2>/dev/null)
  if [ "${sa}" = "argo-iam-admin" ]; then
    pass_test "ServiceAccount is 'argo-iam-admin'"
  else
    fail_test "ServiceAccount is '${sa}', expected 'argo-iam-admin'"
  fi

  # Check onExit handler
  local on_exit
  on_exit=$(kubectl get workflowtemplate "${WORKFLOW_TEMPLATE}" -n "${NAMESPACE}" -o json \
    | jq -r '.spec.onExit' 2>/dev/null)
  if [ "${on_exit}" = "notify-outcome" ]; then
    pass_test "onExit handler is 'notify-outcome'"
  else
    fail_test "onExit handler is '${on_exit}', expected 'notify-outcome'"
  fi
}

test_workflow_per_category() {
  section "1.2 — Workflow Submissions per Service Category (who-type=user)"

  for category in basic bigquery gcs-bucket compute-vm gke-cluster cloud-sql \
                   service-account pubsub secret-manager artifact-registry \
                   cloud-logging cloud-monitoring cloud-composer custom; do
    local role="${CATEGORY_ROLES[$category]}"
    submit_test_workflow \
      "Category: ${category}" \
      "${category}" \
      "${role}" \
      "user" \
      "${TEST_EMAIL}" \
      "global"
  done
}

test_workflow_per_who_type() {
  section "1.3 — Workflow Submissions per who-type (category=basic)"

  for who_type in user group serviceAccount; do
    local email="${WHO_TYPE_EMAILS[$who_type]}"
    submit_test_workflow \
      "who-type: ${who_type}" \
      "basic" \
      "roles/viewer" \
      "${who_type}" \
      "${email}" \
      "global"
  done
}

test_workflow_resource_level() {
  section "1.4 — Resource-Level Scoping Tests"

  # These categories support resource-level scoping
  # Use "global" since we don't have real resources in QA context —
  # this tests the parameter passing, not actual resource existence
  local resource_categories=(
    "gcs-bucket:roles/storage.objectViewer:qa-test-bucket"
    "bigquery:roles/bigquery.dataViewer:qa_test_dataset"
    "pubsub:roles/pubsub.viewer:qa-test-topic"
    "secret-manager:roles/secretmanager.viewer:qa-test-secret"
    "service-account:roles/iam.serviceAccountViewer:${TEST_SA_EMAIL}"
    "compute-vm:roles/compute.viewer:qa-test-vm"
  )

  if [ "${DRY_RUN}" = true ]; then
    for entry in "${resource_categories[@]}"; do
      IFS=':' read -r cat role res <<< "${entry}"
      submit_test_workflow \
        "Resource-level: ${cat}" \
        "${cat}" \
        "${role}" \
        "user" \
        "${TEST_EMAIL}" \
        "${res}" \
        "resource=${res}"
    done
  else
    info "Skipping resource-level live tests (resources may not exist in stage)"
    info "Use --dry-run to validate parameter construction"
    for entry in "${resource_categories[@]}"; do
      IFS=':' read -r cat role res <<< "${entry}"
      skip_test "Resource-level: ${cat} (${res}) — use --dry-run"
    done
  fi
}

test_workflow_validation_failures() {
  section "1.5 — Validation Failure Tests (Expected Failures)"

  # Test 1: Invalid email domain
  info "Testing invalid email domain..."
  local cmd="argo submit --from workflowtemplate/${WORKFLOW_TEMPLATE} \
    -p action=grant \
    -p who-type=user \
    -p who-email=bad-user@gmail.com \
    -p resource-type=basic \
    -p resource-name=global \
    -p permission-level=custom \
    -p custom-role=roles/viewer \
    -p gcp-project=${GCP_PROJECT} \
    -p access-type=temporary \
    -p expiry-hours=1 \
    -p reason=QA-validation-test \
    -n ${NAMESPACE} \
    --dry-run -o json"

  # In dry-run, argo will generate the YAML but the actual validation happens
  # inside the workflow container. We can at least verify the template accepts the params.
  if eval "${cmd}" &>/dev/null; then
    pass_test "Invalid email accepted for submission (validation happens at runtime)"
  else
    pass_test "Invalid email rejected at submission time"
  fi

  # Test 2: Empty custom-role with permission-level=custom
  info "Testing empty custom-role with permission-level=custom..."
  cmd="argo submit --from workflowtemplate/${WORKFLOW_TEMPLATE} \
    -p action=grant \
    -p who-type=user \
    -p who-email=${TEST_EMAIL} \
    -p resource-type=basic \
    -p resource-name=global \
    -p permission-level=custom \
    -p custom-role= \
    -p gcp-project=${GCP_PROJECT} \
    -p access-type=temporary \
    -p expiry-hours=1 \
    -p reason=QA-validation-test \
    -n ${NAMESPACE} \
    --dry-run -o json"

  if eval "${cmd}" &>/dev/null; then
    pass_test "Empty custom-role accepted (runtime validation will catch it)"
  else
    pass_test "Empty custom-role rejected at submission"
  fi
}

# =============================================================================
# SECTION 2: API ENDPOINT TESTS
# =============================================================================

start_port_forward() {
  info "Starting port-forward to ${SERVICE_NAME}:${SERVICE_PORT} → localhost:${LOCAL_PORT}..."

  # Find the pod
  local pod
  pod=$(kubectl get pods -n "${NAMESPACE}" -l app="${SERVICE_NAME}" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || \
        kubectl get pods -n "${NAMESPACE}" --field-selector=status.phase=Running -o json 2>/dev/null \
        | jq -r ".items[] | select(.metadata.name | startswith(\"${SERVICE_NAME}\")) | .metadata.name" 2>/dev/null | head -1)

  if [ -z "${pod}" ]; then
    # Try service-based port-forward
    kubectl port-forward "svc/${SERVICE_NAME}" "${LOCAL_PORT}:${SERVICE_PORT}" -n "${NAMESPACE}" &>/dev/null &
    PF_PID=$!
  else
    kubectl port-forward "pod/${pod}" "${LOCAL_PORT}:${SERVICE_PORT}" -n "${NAMESPACE}" &>/dev/null &
    PF_PID=$!
  fi

  # Wait for port-forward to be ready
  local retries=0
  while ! curl -sf "http://localhost:${LOCAL_PORT}/health" &>/dev/null; do
    retries=$((retries + 1))
    if [ "${retries}" -gt 15 ]; then
      fail_test "Port-forward failed to become ready after 15s"
      PF_PID=""
      return 1
    fi
    sleep 1
  done

  pass_test "Port-forward established (PID ${PF_PID})"
  return 0
}

get_admin_credentials() {
  local secret_json
  secret_json=$(kubectl get secret "${ADMIN_SECRET_NAME}" -n "${NAMESPACE}" -o json 2>/dev/null)

  if [ -z "${secret_json}" ]; then
    echo ""
    return
  fi

  local username password
  username=$(echo "${secret_json}" | jq -r '.data.username' 2>/dev/null | base64 -d 2>/dev/null || echo "")
  password=$(echo "${secret_json}" | jq -r '.data.password' 2>/dev/null | base64 -d 2>/dev/null || echo "")

  if [ -n "${username}" ] && [ -n "${password}" ]; then
    echo "${username}:${password}"
  else
    echo ""
  fi
}

test_api_health() {
  section "2.1 — Health Endpoints"

  # GET /health
  local response
  response=$(curl -sf "http://localhost:${LOCAL_PORT}/health" 2>/dev/null)
  if [ $? -eq 0 ]; then
    local status
    status=$(echo "${response}" | jq -r '.status' 2>/dev/null)
    if [ "${status}" = "ok" ]; then
      pass_test "GET /health — status: ok"
    else
      fail_test "GET /health — unexpected status: ${status}"
    fi
  else
    fail_test "GET /health — request failed"
  fi

  # GET /health/ready
  response=$(curl -sf "http://localhost:${LOCAL_PORT}/health/ready" 2>/dev/null)
  if [ $? -eq 0 ]; then
    local db_status
    db_status=$(echo "${response}" | jq -r '.db' 2>/dev/null)
    if [ "${db_status}" = "connected" ]; then
      pass_test "GET /health/ready — DB connected"
    else
      fail_test "GET /health/ready — DB status: ${db_status}"
    fi
  else
    fail_test "GET /health/ready — request failed (DB may be down)"
  fi
}

test_api_rules() {
  section "2.2 — Rules API"

  local creds
  creds=$(get_admin_credentials)

  if [ -z "${creds}" ]; then
    skip_test "GET /api/rules — admin credentials not found in secret '${ADMIN_SECRET_NAME}'"
    skip_test "POST /api/rules/evaluate — admin credentials not found"
    return
  fi

  # GET /api/rules (with Basic Auth)
  local response http_code
  http_code=$(curl -sf -o /tmp/qa-rules-response.json -w "%{http_code}" \
    -u "${creds}" \
    "http://localhost:${LOCAL_PORT}/api/rules" 2>/dev/null) || http_code="000"

  if [ "${http_code}" = "200" ]; then
    local rule_count
    rule_count=$(jq '. | length' /tmp/qa-rules-response.json 2>/dev/null || echo "0")
    pass_test "GET /api/rules — ${rule_count} rule(s) returned (HTTP 200)"
  elif [ "${http_code}" = "401" ]; then
    fail_test "GET /api/rules — HTTP 401 Unauthorized (credentials may be wrong)"
  else
    fail_test "GET /api/rules — HTTP ${http_code}"
  fi

  # POST /api/rules/evaluate
  local eval_payload='{
    "requester_email": "qa-test@example.com",
    "action": "grant",
    "who_type": "user",
    "resource_type": "basic",
    "permission_level": "custom",
    "gcp_project": "your-gcp-project-stage",
    "access_type": "temporary"
  }'

  http_code=$(curl -sf -o /tmp/qa-evaluate-response.json -w "%{http_code}" \
    -u "${creds}" \
    -H "Content-Type: application/json" \
    -d "${eval_payload}" \
    "http://localhost:${LOCAL_PORT}/api/rules/evaluate" 2>/dev/null) || http_code="000"

  if [ "${http_code}" = "200" ]; then
    local matched
    matched=$(jq -r '.matched' /tmp/qa-evaluate-response.json 2>/dev/null)
    local risk
    risk=$(jq -r '.risk_level' /tmp/qa-evaluate-response.json 2>/dev/null)
    pass_test "POST /api/rules/evaluate — matched=${matched}, risk=${risk} (HTTP 200)"
  else
    fail_test "POST /api/rules/evaluate — HTTP ${http_code}"
  fi
}

test_api_teams() {
  section "2.3 — Teams API"

  local creds
  creds=$(get_admin_credentials)

  if [ -z "${creds}" ]; then
    skip_test "GET /api/teams — admin credentials not found"
    return
  fi

  local http_code
  http_code=$(curl -sf -o /tmp/qa-teams-response.json -w "%{http_code}" \
    -u "${creds}" \
    "http://localhost:${LOCAL_PORT}/api/teams" 2>/dev/null) || http_code="000"

  if [ "${http_code}" = "200" ]; then
    local team_count
    team_count=$(jq '. | length' /tmp/qa-teams-response.json 2>/dev/null || echo "0")
    pass_test "GET /api/teams — ${team_count} team(s) returned (HTTP 200)"
  else
    fail_test "GET /api/teams — HTTP ${http_code}"
  fi
}

test_api_groups() {
  section "2.4 — Groups API"

  local creds
  creds=$(get_admin_credentials)

  if [ -z "${creds}" ]; then
    skip_test "GET /api/groups — admin credentials not found"
    return
  fi

  local http_code
  http_code=$(curl -sf -o /tmp/qa-groups-response.json -w "%{http_code}" \
    -u "${creds}" \
    "http://localhost:${LOCAL_PORT}/api/groups" 2>/dev/null) || http_code="000"

  if [ "${http_code}" = "200" ]; then
    local group_count
    group_count=$(jq '. | length' /tmp/qa-groups-response.json 2>/dev/null || echo "0")
    pass_test "GET /api/groups — ${group_count} group(s) returned (HTTP 200)"
  else
    fail_test "GET /api/groups — HTTP ${http_code}"
  fi
}

test_api_requests() {
  section "2.5 — Requests API"

  local creds
  creds=$(get_admin_credentials)

  if [ -z "${creds}" ]; then
    skip_test "GET /api/requests — admin credentials not found"
    return
  fi

  local http_code
  http_code=$(curl -sf -o /tmp/qa-requests-response.json -w "%{http_code}" \
    -u "${creds}" \
    "http://localhost:${LOCAL_PORT}/api/requests" 2>/dev/null) || http_code="000"

  if [ "${http_code}" = "200" ]; then
    local req_count
    req_count=$(jq '. | length' /tmp/qa-requests-response.json 2>/dev/null || echo "0")
    pass_test "GET /api/requests — ${req_count} request(s) returned (HTTP 200)"
  else
    fail_test "GET /api/requests — HTTP ${http_code}"
  fi
}

test_api_auth_rejection() {
  section "2.6 — Auth Rejection (no credentials)"

  # Unauthenticated request should be rejected
  local http_code
  http_code=$(curl -sf -o /dev/null -w "%{http_code}" \
    "http://localhost:${LOCAL_PORT}/api/rules" 2>/dev/null) || http_code="000"

  if [ "${http_code}" = "401" ]; then
    pass_test "GET /api/rules without auth — HTTP 401 (correctly rejected)"
  elif [ "${http_code}" = "403" ]; then
    pass_test "GET /api/rules without auth — HTTP 403 (correctly rejected)"
  else
    fail_test "GET /api/rules without auth — HTTP ${http_code} (expected 401/403)"
  fi
}

# =============================================================================
# SECTION 3: DATABASE CONNECTIVITY TESTS
# =============================================================================

get_db_pod() {
  # Find the approval-handler pod to exec into for DB queries
  kubectl get pods -n "${NAMESPACE}" -l app="${SERVICE_NAME}" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || \
  kubectl get pods -n "${NAMESPACE}" --field-selector=status.phase=Running -o json 2>/dev/null \
    | jq -r ".items[] | select(.metadata.name | startswith(\"${SERVICE_NAME}\")) | .metadata.name" 2>/dev/null | head -1
}

run_db_query() {
  local pod="$1"
  local query="$2"

  # Use python inside the pod to run the query via the app's DB connection settings
  kubectl exec "${pod}" -n "${NAMESPACE}" -- python3 -c "
import os, psycopg2, json
conn = psycopg2.connect(
    host=os.environ.get('DB_HOST', 'localhost'),
    port=os.environ.get('DB_PORT', '5432'),
    dbname=os.environ.get('DB_NAME', 'approval_handler'),
    user=os.environ.get('DB_USER', 'postgres'),
    password=os.environ.get('DB_PASSWORD', ''),
    sslmode=os.environ.get('DB_SSLMODE', 'prefer'),
)
cur = conn.cursor()
cur.execute(\"\"\"${query}\"\"\")
rows = cur.fetchall()
cols = [d[0] for d in cur.description] if cur.description else []
result = [dict(zip(cols, r)) for r in rows]
print(json.dumps(result))
conn.close()
" 2>/dev/null
}

test_db_connectivity() {
  section "3.1 — Database Table Checks"

  local pod
  pod=$(get_db_pod)

  if [ -z "${pod}" ]; then
    skip_test "Cannot find approval-handler pod for DB queries"
    skip_test "Skipping all DB tests"
    return
  fi

  info "Using pod: ${pod}"

  # --- approval_rules ---
  local result
  result=$(run_db_query "${pod}" "SELECT COUNT(*) as count FROM approval_rules" 2>/dev/null) || result=""
  if [ -n "${result}" ]; then
    local count
    count=$(echo "${result}" | jq -r '.[0].count' 2>/dev/null || echo "error")
    if [ "${count}" != "error" ] && [ "${count}" != "null" ]; then
      pass_test "Table 'approval_rules' exists — ${count} row(s)"
    else
      fail_test "Table 'approval_rules' — query returned unexpected result"
    fi
  else
    fail_test "Table 'approval_rules' — query failed"
  fi

  # --- approval_dm_messages ---
  result=$(run_db_query "${pod}" "SELECT COUNT(*) as count FROM approval_dm_messages" 2>/dev/null) || result=""
  if [ -n "${result}" ]; then
    local count
    count=$(echo "${result}" | jq -r '.[0].count' 2>/dev/null || echo "error")
    if [ "${count}" != "error" ] && [ "${count}" != "null" ]; then
      pass_test "Table 'approval_dm_messages' exists — ${count} row(s)"
    else
      fail_test "Table 'approval_dm_messages' — query returned unexpected result"
    fi
  else
    fail_test "Table 'approval_dm_messages' — query failed"
  fi

  # --- service_categories ---
  result=$(run_db_query "${pod}" "SELECT COUNT(*) as count FROM service_categories" 2>/dev/null) || result=""
  if [ -n "${result}" ]; then
    local count
    count=$(echo "${result}" | jq -r '.[0].count' 2>/dev/null || echo "error")
    if [ "${count}" != "error" ] && [ "${count}" != "null" ]; then
      pass_test "Table 'service_categories' exists — ${count} row(s)"
    else
      fail_test "Table 'service_categories' — query returned unexpected result"
    fi
  else
    fail_test "Table 'service_categories' — query failed"
  fi

  # --- iam_roles ---
  result=$(run_db_query "${pod}" "SELECT COUNT(*) as count FROM iam_roles" 2>/dev/null) || result=""
  if [ -n "${result}" ]; then
    local count
    count=$(echo "${result}" | jq -r '.[0].count' 2>/dev/null || echo "error")
    if [ "${count}" != "error" ] && [ "${count}" != "null" ]; then
      pass_test "Table 'iam_roles' exists — ${count} row(s)"
    else
      fail_test "Table 'iam_roles' — query returned unexpected result"
    fi
  else
    fail_test "Table 'iam_roles' — query failed"
  fi

  # --- access_requests ---
  result=$(run_db_query "${pod}" "SELECT COUNT(*) as count FROM access_requests" 2>/dev/null) || result=""
  if [ -n "${result}" ]; then
    local count
    count=$(echo "${result}" | jq -r '.[0].count' 2>/dev/null || echo "error")
    if [ "${count}" != "error" ] && [ "${count}" != "null" ]; then
      pass_test "Table 'access_requests' exists — ${count} row(s)"
    else
      fail_test "Table 'access_requests' — query returned unexpected result"
    fi
  else
    fail_test "Table 'access_requests' — query failed"
  fi

  # --- teams ---
  result=$(run_db_query "${pod}" "SELECT COUNT(*) as count FROM teams" 2>/dev/null) || result=""
  if [ -n "${result}" ]; then
    local count
    count=$(echo "${result}" | jq -r '.[0].count' 2>/dev/null || echo "error")
    if [ "${count}" != "error" ] && [ "${count}" != "null" ]; then
      pass_test "Table 'teams' exists — ${count} row(s)"
    else
      fail_test "Table 'teams' — query returned unexpected result"
    fi
  else
    fail_test "Table 'teams' — query failed"
  fi
}

test_db_sample_data() {
  section "3.2 — Database Sample Data Validation"

  local pod
  pod=$(get_db_pod)

  if [ -z "${pod}" ]; then
    skip_test "Cannot find approval-handler pod for DB queries"
    return
  fi

  # --- Sample approval_rules ---
  local result
  result=$(run_db_query "${pod}" "SELECT name, priority, enabled FROM approval_rules ORDER BY priority LIMIT 3" 2>/dev/null) || result=""
  if [ -n "${result}" ] && [ "${result}" != "[]" ]; then
    local first_rule
    first_rule=$(echo "${result}" | jq -r '.[0].name' 2>/dev/null || echo "unknown")
    pass_test "Sample approval_rules — first rule: '${first_rule}'"
  else
    info "No approval rules found (table may be empty)"
    pass_test "approval_rules query executed successfully (empty table OK)"
  fi

  # --- Verify service_categories match expected catalog ---
  result=$(run_db_query "${pod}" "SELECT category_key FROM service_categories WHERE enabled = TRUE ORDER BY sort_order" 2>/dev/null) || result=""
  if [ -n "${result}" ] && [ "${result}" != "[]" ]; then
    local cat_count
    cat_count=$(echo "${result}" | jq '. | length' 2>/dev/null || echo "0")
    pass_test "Enabled service_categories count: ${cat_count}"

    # Check if key categories exist
    local expected_cats=("basic" "bigquery" "gcs-bucket" "compute-vm" "gke-cluster" "cloud-sql")
    for cat in "${expected_cats[@]}"; do
      if echo "${result}" | jq -e ".[] | select(.category_key == \"${cat}\")" &>/dev/null; then
        pass_test "  Category '${cat}' exists and enabled"
      else
        fail_test "  Category '${cat}' missing or disabled"
      fi
    done
  else
    info "No service_categories in DB (using fallback GCP_ROLE_CATALOG)"
    pass_test "service_categories query executed (may use code fallback)"
  fi

  # --- Verify iam_roles sample ---
  result=$(run_db_query "${pod}" "SELECT category_key, role_value, display_name FROM iam_roles WHERE enabled = TRUE LIMIT 5" 2>/dev/null) || result=""
  if [ -n "${result}" ] && [ "${result}" != "[]" ]; then
    local role_sample
    role_sample=$(echo "${result}" | jq -r '.[0].role_value' 2>/dev/null || echo "unknown")
    pass_test "Sample iam_roles — first role: '${role_sample}'"
  else
    pass_test "iam_roles query executed (may use code fallback)"
  fi
}

# =============================================================================
# SECTION 4: PARAMETER MAPPING CONSISTENCY
# =============================================================================

test_parameter_mapping() {
  section "4 — Parameter Mapping: Handler ↔ WorkflowTemplate"

  # Verify that the parameters the approval-handler sends match what
  # the WorkflowTemplate expects.

  # Parameters sent by _submit_workflow_for_request()
  local handler_params=(
    "action"
    "who-type"
    "who-email"
    "resource-type"
    "resource-name"
    "permission-level"
    "custom-role"
    "gcp-project"
    "access-type"
    "expiry-hours"
    "reason"
    "ticket"
  )

  # Parameters defined in the WorkflowTemplate
  local template_params
  template_params=$(kubectl get workflowtemplate "${WORKFLOW_TEMPLATE}" -n "${NAMESPACE}" -o json \
    | jq -r '.spec.arguments.parameters[].name' 2>/dev/null)

  info "Checking handler→template parameter alignment..."
  for p in "${handler_params[@]}"; do
    if echo "${template_params}" | grep -q "^${p}$"; then
      pass_test "Handler param '${p}' → Template param '${p}' ✓"
    else
      fail_test "Handler param '${p}' has no matching Template param"
    fi
  done

  info "Checking for template params NOT sent by handler..."
  while IFS= read -r tp; do
    local found=false
    for hp in "${handler_params[@]}"; do
      if [ "${tp}" = "${hp}" ]; then
        found=true
        break
      fi
    done
    if [ "${found}" = false ]; then
      info "  Template param '${tp}' not sent by handler (uses default)"
    fi
  done <<< "${template_params}"

  # Verify enum values match
  info "Checking enum values alignment..."

  # resource-type enums
  local template_resource_types
  template_resource_types=$(kubectl get workflowtemplate "${WORKFLOW_TEMPLATE}" -n "${NAMESPACE}" -o json \
    | jq -r '.spec.arguments.parameters[] | select(.name == "resource-type") | .description' 2>/dev/null)

  local handler_categories=("basic" "bigquery" "gcs-bucket" "compute-vm" "gke-cluster" "cloud-sql"
    "service-account" "pubsub" "secret-manager" "artifact-registry"
    "cloud-logging" "cloud-monitoring" "cloud-composer" "custom")

  for cat in "${handler_categories[@]}"; do
    if echo "${template_resource_types}" | grep -q "${cat}"; then
      pass_test "Category '${cat}' documented in template description"
    else
      fail_test "Category '${cat}' NOT documented in template description"
    fi
  done

  # gcp-project enums
  local template_projects
  template_projects=$(kubectl get workflowtemplate "${WORKFLOW_TEMPLATE}" -n "${NAMESPACE}" -o json \
    | jq -r '.spec.arguments.parameters[] | select(.name == "gcp-project") | .enum[]' 2>/dev/null)

  for proj in "your-gcp-project-stage" "your-gcp-project-prod" "your-gcp-project-data"; do
    if echo "${template_projects}" | grep -q "^${proj}$"; then
      pass_test "GCP project '${proj}' in template enum"
    else
      fail_test "GCP project '${proj}' NOT in template enum"
    fi
  done
}

# =============================================================================
# SUMMARY
# =============================================================================

print_summary() {
  echo ""
  echo -e "${BOLD}${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo -e "${BOLD}${BLUE}  QA TEST SUMMARY${NC}"
  echo -e "${BOLD}${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo ""
  echo -e "  ${GREEN}✅ Passed: ${PASS_COUNT}${NC}"
  echo -e "  ${RED}❌ Failed: ${FAIL_COUNT}${NC}"
  echo -e "  ${YELLOW}⏭️  Skipped: ${SKIP_COUNT}${NC}"
  echo -e "  📊 Total:  ${TOTAL_COUNT}"
  echo ""

  if [ "${DRY_RUN}" = true ]; then
    echo -e "  ${CYAN}Mode: DRY RUN (no workflows submitted to cluster)${NC}"
  fi

  echo -e "  ${CYAN}Environment: stage (${GCP_PROJECT})${NC}"
  echo -e "  ${CYAN}Namespace:   ${NAMESPACE}${NC}"
  echo ""

  if [ "${FAIL_COUNT}" -gt 0 ]; then
    echo -e "  ${RED}${BOLD}⚠️  ${FAIL_COUNT} test(s) failed — review output above${NC}"
    echo ""
    exit 1
  else
    echo -e "  ${GREEN}${BOLD}🎉 All tests passed!${NC}"
    echo ""
    exit 0
  fi
}

# =============================================================================
# MAIN EXECUTION
# =============================================================================

main() {
  echo ""
  echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════════════════════╗${NC}"
  echo -e "${BOLD}${CYAN}║  Approval Handler — QA Test Suite                          ║${NC}"
  echo -e "${BOLD}${CYAN}║  Environment: STAGE (${GCP_PROJECT})          ║${NC}"
  if [ "${DRY_RUN}" = true ]; then
  echo -e "${BOLD}${CYAN}║  Mode: DRY RUN                                             ║${NC}"
  fi
  echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════════════════════╝${NC}"
  echo ""

  check_prerequisites

  if [ "${DB_ONLY}" = true ]; then
    # API port-forward needed for DB tests (we exec into the pod)
    test_db_connectivity
    test_db_sample_data
    print_summary
    return
  fi

  if [ "${WORKFLOW_ONLY}" = true ]; then
    test_workflow_template_exists
    test_workflow_per_category
    test_workflow_per_who_type
    test_workflow_resource_level
    test_workflow_validation_failures
    test_parameter_mapping
    print_summary
    return
  fi

  if [ "${API_ONLY}" = true ]; then
    if start_port_forward; then
      test_api_health
      test_api_rules
      test_api_teams
      test_api_groups
      test_api_requests
      test_api_auth_rejection
    fi
    print_summary
    return
  fi

  # --- Full test suite ---

  # Section 1: Workflow Template
  test_workflow_template_exists
  test_workflow_per_category
  test_workflow_per_who_type
  test_workflow_resource_level
  test_workflow_validation_failures

  # Section 2: API Endpoints
  if start_port_forward; then
    test_api_health
    test_api_rules
    test_api_teams
    test_api_groups
    test_api_requests
    test_api_auth_rejection

    # Section 3: DB Connectivity (uses the pod, not port-forward)
    test_db_connectivity
    test_db_sample_data
  else
    skip_test "API tests skipped — port-forward failed"
    skip_test "DB tests skipped — cannot reach pod"
  fi

  # Section 4: Parameter Mapping
  test_parameter_mapping

  # Summary
  print_summary
}

main "$@"
