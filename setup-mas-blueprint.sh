#!/bin/bash
# MAS A2A Blueprint Setup Script
#
# Correct Architecture:
# 1. Register LLM resource → get LLM_RID
# 2. Register Orchestrator Node (referencing LLM_RID) → get ORCHESTRATOR_RID
# 3. Register A2A Agent Node(s) → get their RIDs
# 4. Create blueprint where node.rid = inventory resource RID (no config needed)
#
# Usage:
#   export LITELLM_API_KEY="your-key"
#   export LITELLM_API_BASE="https://maas-rhdp.apps.maas.redhatworkshops.io/v1"
#   export LITELLM_MODEL="gpt-oss-20b"
#   ./setup-mas-blueprint.sh [simple|multi-cluster|multi-cluster-hitl]
#
# multi-cluster-hitl is multi-cluster plus an approval gate before the
# orchestrator, to test human approval (TASK_STATE_INPUT_REQUIRED) and token
# expiry deterministically: a custom agent with hitl_mode=ask runs one
# read-only `oc whoami` through the oc_exec tool, which always needs approval.
# It needs a cluster token for that tool:
#   HITL_OC_TOKEN   token for HITL_OC_SERVER; or set HITL_CREATE_SA=true to
#                   create a view-only ServiceAccount (mas-hitl-test in mao)
#                   and a 30-day token with `oc` (needs cluster-admin).
#   HITL_OC_SERVER  defaults to https://api.${CLUSTER_DOMAIN}:6443

set -e

# Configuration
export KUBECONFIG="${KUBECONFIG:-$HOME/gitops.kubeconfig}"
export MAS_API_URL="${MAS_API_URL:-https://$(oc get route mas-api -n mao -o jsonpath='{.spec.host}' 2>/dev/null)}"
export MAS_TOKEN="${MAS_TOKEN:-dev-token}"
MAS_API_CA_BUNDLE="${MAS_API_CA_BUNDLE:-}"
MAS_API_INSECURE_TLS="${MAS_API_INSECURE_TLS:-false}"

# Keep TLS verification enabled unless an operator explicitly opts out for a
# temporary non-production test. Prefer MAS_API_CA_BUNDLE for private ingress
# CAs so the API token remains protected by certificate verification.
MAS_API_CURL_TLS_ARGS=()
if [ -n "$MAS_API_CA_BUNDLE" ]; then
  if [ ! -r "$MAS_API_CA_BUNDLE" ]; then
    echo "❌ MAS_API_CA_BUNDLE is not a readable file: $MAS_API_CA_BUNDLE"
    exit 1
  fi
  MAS_API_CURL_TLS_ARGS+=(--cacert "$MAS_API_CA_BUNDLE")
elif [ "$MAS_API_INSECURE_TLS" = "true" ]; then
  MAS_API_CURL_TLS_ARGS+=(--insecure)
elif [ "$MAS_API_INSECURE_TLS" != "false" ]; then
  echo "❌ MAS_API_INSECURE_TLS must be 'true' or 'false'"
  exit 1
fi

mas_api_curl() {
  curl "${MAS_API_CURL_TLS_ARGS[@]}" "$@"
}

# Create a MAS inventory resource, or update the already-registered
# same-named one. resource.save rejects a duplicate (category, type, name)
# with HTTP 400 "exists for user"; without this fallback a re-run of this
# script silently kept the STALE config of the existing resource (the rid
# lookup returned the old doc unchanged -- discovered live when a
# temperature change refused to apply). Prints the save/update response
# JSON (both carry .rid) on stdout; non-zero exit if neither is possible.
mas_api_upsert_resource() {
  local payload="$1" response rid category name
  response=$(mas_api_curl -sS -X POST "${MAS_API_URL}/api/resources/resource.save" \
    -H "Authorization: Bearer ${MAS_TOKEN}" \
    -H "Content-Type: application/json" \
    --data-binary "$payload")
  rid=$(printf '%s' "$response" | jq -r '.rid // empty' 2>/dev/null || true)
  if [ -n "$rid" ]; then
    printf '%s\n' "$response"
    return 0
  fi
  category=$(printf '%s' "$payload" | jq -r '.category')
  name=$(printf '%s' "$payload" | jq -r '.name')
  rid=$(mas_api_curl -sS "${MAS_API_URL}/api/resources/resources.list?category=${category}" \
    -H "Authorization: Bearer ${MAS_TOKEN}" \
    | jq -r ".resources[]? | select(.name == \"${name}\") | .rid" | head -1)
  if [ -z "$rid" ]; then
    echo "❌ resource.save failed and no existing ${category} resource named '${name}' found to update:" >&2
    printf '%s\n' "$response" >&2
    return 1
  fi
  printf '%s' "$payload" | jq --arg rid "$rid" '{resourceId: $rid, config: .config}' \
    | mas_api_curl -sS -X PUT "${MAS_API_URL}/api/resources/resource.update" \
      -H "Authorization: Bearer ${MAS_TOKEN}" \
      -H "Content-Type: application/json" \
      --data-binary @-
}

CLUSTER_DOMAIN="${CLUSTER_DOMAIN:-bm-cluster.e2e.bos.redhat.com}"
CLUSTER_ID="api-$(echo $CLUSTER_DOMAIN | sed 's/\./-/g')-6443"
ASK_REDHAT_A2A_URL="${ASK_REDHAT_A2A_URL:-https://access.stage.redhat.com/api/ask/v1/a2a}"

# Shared by the registered orchestrator resource and the blueprint node. Ask
# Red Hat's A2A node has an empty bearer_token so MAS can use the inbound
# execution-context credential; do not place a token in this blueprint.
#
# LIVE FINDING (2026-10-22, bm-cluster): with only the "mandatory header"
# bullet below and the planning LLM at temperature 0.7, Gemini emitted the
# routing header name as X_OLS_Cluster (underscores) in 3 of 5 delegations.
# MAS forwards work-item headers verbatim on the outbound A2A POST, Praxis
# matches the X-OLS-Cluster header name exactly (fail-closed, no default
# route), so every misspelling became "no route matched path=/a2a" -> HTTP
# 404 and the delegation was silently lost. Models habitually normalize
# hyphenated keys to underscores in dict/JSON-valued tool arguments, so the
# prompt now states the spelling rule explicitly (with the wrong form named)
# and the LLM registers at temperature 0. If this still proves unreliable,
# the durable fix is in the MAS image: canonicalize the header name when
# building the outbound request (see UPSTREAM_CONTRIBUTIONS.md), or set it
# statically on the a2a_agent_node instead of via model tool args.
ORCHESTRATOR_SYSTEM_MESSAGE=$(cat <<EOF
You are an OpenShift investigation and Red Hat knowledge router. For every request, decide whether to use the OLS Investigator, Ask Red Hat, or both before finalizing.

AVAILABLE CLUSTER:
- ${CLUSTER_DOMAIN} → ${CLUSTER_ID}

AVAILABLE AGENTS:
- OLS Investigator: authoritative live inspection of the configured cluster, including current state, resources, events, errors, and troubleshooting.
- Ask Red Hat: Red Hat documentation, product information, and guidance. Use it when the request matches a capability advertised in the Ask Red Hat Agent Card. Treat that card's skill names and descriptions as the supported scope; do not invent skills or assume Ask Red Hat can inspect the live cluster.

ROUTING:
- For questions that require current cluster facts, deployed configuration, or live troubleshooting, delegate to the OLS Investigator.
- For documentation, product questions, how-to guidance, or other topics covered by an advertised Ask Red Hat Agent Card skill, delegate to Ask Red Hat.
- If the user needs both live cluster findings and documentation or product guidance to interpret them, delegate to both agents in separate tasks. After each response, decide whether the other agent is still needed; do not finalize until every selected agent has responded.
- For OLS calls, always use delegate_task and set data to {"headers": {"X-OLS-Cluster": "${CLUSTER_ID}"}}. This header is mandatory for every OLS delegation.
- Copy the header name character for character: X-OLS-Cluster (uppercase, single hyphens). The name X_OLS_Cluster (underscores) or any other spelling is invalid: the gateway matches the header name literally, ignores a misspelled one, and rejects the request with HTTP 404, losing the entire delegation.
- For Ask Red Hat calls, use the Ask Red Hat node and do not add the X-OLS-Cluster header.

Synthesize the selected agents' results into one answer. Distinguish observed cluster facts from documentation-based guidance, and do not present documentation as evidence of the cluster's current state.
EOF
)

# Orchestrator LLM provider: "openai" (LiteLLM/MaaS, needs LITELLM_API_KEY) or
# "vertex" (Gemini on Vertex via the google_genai element). Vertex is
# recommended: gpt-oss served over the MaaS returns "harmony" tool calls whose
# arguments do not survive the endpoint's OpenAI translation, so the
# orchestrator can never build a plan (empty plan / unterminated-string errors).
# Gemini returns proper OpenAI-style tool_calls. Vertex uses the ADC creds
# mounted into the worker (charts/all/mao/mas-worker vertexCreds -> the same
# Vault key OLS uses), so no api_key is needed here.
LLM_PROVIDER="${LLM_PROVIDER:-openai}"
VERTEX_PROJECT="${VERTEX_PROJECT:-redhat-marketplace-dev}"
VERTEX_LOCATION="${VERTEX_LOCATION:-global}"
VERTEX_MODEL="${VERTEX_MODEL:-gemini-3.8-flash}"

BLUEPRINT_TYPE="${1:-simple}"
case "$BLUEPRINT_TYPE" in
  simple) IS_MULTI_CLUSTER=false ;;
  multi-cluster|multi-cluster-hitl) IS_MULTI_CLUSTER=true ;;
  *)
    echo "❌ Unknown blueprint type '$BLUEPRINT_TYPE' (simple, multi-cluster, multi-cluster-hitl)"
    exit 1
    ;;
esac

echo "========================================="
echo "MAS A2A Blueprint Setup"
echo "========================================="
echo "MAS API: $MAS_API_URL"
echo "Cluster: $CLUSTER_DOMAIN"
echo "Blueprint Type: $BLUEPRINT_TYPE"
echo "Ask Red Hat A2A: $ASK_REDHAT_A2A_URL"
if [ "$MAS_API_INSECURE_TLS" = "true" ] && [ -z "$MAS_API_CA_BUNDLE" ]; then
  echo "⚠️  WARNING: MAS API TLS certificate verification is disabled."
fi
echo ""

# Check connectivity
echo "Testing MAS API connectivity..."
if ! HEALTH_RESPONSE=$(mas_api_curl -sS --connect-timeout 10 --max-time 30 "${MAS_API_URL}/api/health/"); then
  echo "❌ MAS API health request failed; see the curl error above."
  echo "   For a private ingress CA, set MAS_API_CA_BUNDLE=/path/to/ca-bundle.crt."
  echo "   MAS_API_INSECURE_TLS=true is available for temporary non-production testing only."
  exit 1
fi
HEALTH=$(printf '%s' "$HEALTH_RESPONSE" | jq -r '.status // empty' 2>/dev/null || true)
if [ "$HEALTH" != "ok" ]; then
  echo "❌ MAS API health check did not return status=ok"
  echo "   Response: $HEALTH_RESPONSE"
  exit 1
fi
echo "✅ MAS API is healthy"
echo ""

# ============================================================================
# STEP 1: Register Inventory Resources
# ============================================================================

echo "========================================="
echo "STEP 1: Registering Inventory Resources"
echo "========================================="
echo ""

if [ "$IS_MULTI_CLUSTER" == "true" ]; then

  # 1a. Register LLM (Vertex/Gemini or OpenAI/LiteLLM)
  if [ "$LLM_PROVIDER" == "vertex" ]; then
    LLM_NAME="Multi-Cluster Orchestrator LLM (Vertex)"
    echo "1a. Registering Vertex google_genai LLM: ${VERTEX_MODEL} (project=${VERTEX_PROJECT}, location=${VERTEX_LOCATION})..."
    # api_key is ignored in Vertex mode (google-genai Client(vertexai=true)
    # authenticates via ADC/GOOGLE_APPLICATION_CREDENTIALS mounted in the
    # worker). vertexai/project/location go through the passthrough "extra".
    # temperature 0 (was 0.7): routing-header spelling in tool args must be
    # deterministic -- see the LIVE FINDING comment above
    # ORCHESTRATOR_SYSTEM_MESSAGE. max_tokens 64000: MAS defaults it to
    # 8192 when unset, which is tight for multi-agent plan + synthesis
    # turns; 64000 fits gemini-3.8-flash's 65536-token output cap.
    LLM_PAYLOAD='{
        "category": "llms",
        "type": "google_genai",
        "name": "'"${LLM_NAME}"'",
        "config": {
          "type": "google_genai",
          "model_name": "'"${VERTEX_MODEL}"'",
          "api_key": "unused-vertex-adc",
          "temperature": 0.0,
          "max_tokens": 64000,
          "extra": {"vertexai": true, "project": "'"${VERTEX_PROJECT}"'", "location": "'"${VERTEX_LOCATION}"'"}
        }
      }'
    LLM_RESPONSE=$(mas_api_upsert_resource "$LLM_PAYLOAD")
  else
    if [ -z "$LITELLM_API_KEY" ]; then
      echo "❌ Error: LITELLM_API_KEY required for LLM_PROVIDER=openai"
      exit 1
    fi
    LLM_NAME="Multi-Cluster Orchestrator LLM"
    LLM_MODEL="${LITELLM_MODEL:-gpt-oss-20b}"
    LLM_BASE_URL="${LITELLM_API_BASE:-https://maas-rhdp.apps.maas.redhatworkshops.io/v1}"
    echo "1a. Registering OpenAI/LiteLLM LLM: ${LLM_MODEL}..."
    # temperature 0 (was 0.7): routing-header spelling in tool args must be
    # deterministic -- see the LIVE FINDING comment above
    # ORCHESTRATOR_SYSTEM_MESSAGE. max_tokens 64000: MAS defaults it to
    # 8192 when unset, which is tight for multi-agent plan + synthesis
    # turns; 64000 fits gemini-3.8-flash's 65536-token output cap.
    LLM_PAYLOAD='{
        "category": "llms",
        "type": "openai",
        "name": "'"${LLM_NAME}"'",
        "config": {
          "type": "openai",
          "model_name": "'"${LLM_MODEL}"'",
          "api_key": "'"${LITELLM_API_KEY}"'",
          "base_url": "'"${LLM_BASE_URL}"'",
          "temperature": 0.0,
          "max_tokens": 64000
        }
      }'
    LLM_RESPONSE=$(mas_api_upsert_resource "$LLM_PAYLOAD")
  fi

  LLM_RID=$(echo "$LLM_RESPONSE" | jq -r '.rid // empty')
  if [ -z "$LLM_RID" ]; then
    LLM_RID=$(mas_api_curl -sS "${MAS_API_URL}/api/resources/resources.list?category=llms" \
      -H "Authorization: Bearer ${MAS_TOKEN}" \
      | jq -r '.resources[]? | select(.name == "'"${LLM_NAME}"'") | .rid' | head -1)
  fi
  echo "   ✅ LLM RID: $LLM_RID"

  # 1b. Register Orchestrator Node
  echo ""
  echo "1b. Registering Orchestrator Node..."
  ORCH_PAYLOAD=$(jq -n \
    --arg llm "$LLM_RID" \
    --arg system_message "$ORCHESTRATOR_SYSTEM_MESSAGE" \
    '{
      "category": "nodes",
      "type": "orchestrator_node",
      "name": "Multi-Cluster Router",
      "config": {
        "type": "orchestrator_node",
        "llm": $llm,
        "system_message": $system_message
      }
      }')
  ORCH_RESPONSE=$(mas_api_upsert_resource "$ORCH_PAYLOAD")

  ORCH_RID=$(echo "$ORCH_RESPONSE" | jq -r '.rid // empty')
  if [ -z "$ORCH_RID" ]; then
    ORCH_RID=$(mas_api_curl -sS "${MAS_API_URL}/api/resources/resources.list?category=nodes" \
      -H "Authorization: Bearer ${MAS_TOKEN}" \
      | jq -r '.resources[]? | select(.name == "Multi-Cluster Router") | .rid' | head -1)
  fi
  echo "   ✅ Orchestrator RID: $ORCH_RID"

  # 1b2. Register Router Condition (router_direct) as an inventory resource so
  # the blueprint can reference it via $ref -- exactly like the LLM and nodes.
  # An inline condition declared only in the blueprint (a bare local rid) is not
  # resolved into the runtime graph the orchestrator routes through; it must be
  # an external ref to a saved conditions resource.
  echo ""
  echo "1b2. Registering Router Condition..."
  COND_PAYLOAD='{
      "category": "conditions",
      "type": "router_direct",
      "name": "Multi-Cluster Orchestrator Router",
      "config": {
        "type": "router_direct"
      }
    }'
  COND_RESPONSE=$(mas_api_upsert_resource "$COND_PAYLOAD")
  COND_RID=$(echo "$COND_RESPONSE" | jq -r '.rid // empty')
  if [ -z "$COND_RID" ]; then
    COND_RID=$(mas_api_curl -sS "${MAS_API_URL}/api/resources/resources.list?category=conditions" \
      -H "Authorization: Bearer ${MAS_TOKEN}" \
      | jq -r '.resources[]? | select(.name == "Multi-Cluster Orchestrator Router") | .rid' | head -1)
  fi
  echo "   ✅ Router Condition RID: $COND_RID"
fi

# 1c. Register the live-cluster investigator for multi-cluster blueprints.
if [ "$IS_MULTI_CLUSTER" == "true" ]; then
  echo ""
  echo "1c. Registering OpenShift Lightspeed A2A Agent Node..."
  A2A_PAYLOAD='{
      "category": "nodes",
      "type": "a2a_agent_node",
      "name": "OpenShift Lightspeed Agent",
      "config": {
        "base_url": "https://openshift-lightspeed.apps.'"${CLUSTER_DOMAIN}"'/a2a",
        "bearer_token": "",
        "verify_ssl": true
      }
    }'
  A2A_RESPONSE=$(mas_api_upsert_resource "$A2A_PAYLOAD")

  A2A_RID=$(echo "$A2A_RESPONSE" | jq -r '.rid // empty')
  if [ -z "$A2A_RID" ]; then
    A2A_RID=$(mas_api_curl -sS "${MAS_API_URL}/api/resources/resources.list?category=nodes" \
      -H "Authorization: Bearer ${MAS_TOKEN}" \
      | jq -r '.resources[]? | select(.name == "OpenShift Lightspeed Agent") | .rid' | head -1)
  fi
  echo "   ✅ OLS A2A Agent RID: $A2A_RID"
fi

# Register Ask Red Hat for both blueprint types. The empty bearer_token leaves
# authentication to the MAS execution context instead of storing a static token.
echo ""
echo "1d. Registering Ask Red Hat A2A Agent Node..."
ASK_REDHAT_PAYLOAD=$(jq -n \
  --arg base_url "$ASK_REDHAT_A2A_URL" \
  '{
    "category": "nodes",
    "type": "a2a_agent_node",
    "name": "Ask Red Hat",
    "config": {
      "type": "a2a_agent_node",
      "base_url": $base_url,
      "bearer_token": "",
      "verify_ssl": true
    }
  }')
ASK_REDHAT_RESPONSE=$(mas_api_upsert_resource "$ASK_REDHAT_PAYLOAD")

ASK_REDHAT_RID=$(echo "$ASK_REDHAT_RESPONSE" | jq -r '.rid // empty')
if [ -z "$ASK_REDHAT_RID" ]; then
  ASK_REDHAT_RID=$(mas_api_curl -sS "${MAS_API_URL}/api/resources/resources.list?category=nodes" \
    -H "Authorization: Bearer ${MAS_TOKEN}" \
    | jq -r '.resources[]? | select(.name == "Ask Red Hat") | .rid' | head -1)
fi
echo "   ✅ Ask Red Hat A2A Agent RID: $ASK_REDHAT_RID"

# 1e. Approval-gate tool for the multi-cluster-hitl test blueprint.
HITL_GATE_NODE=""
HITL_GATE_PLAN=""
ORCHESTRATE_FIRST_AFTER="start"
if [ "$BLUEPRINT_TYPE" == "multi-cluster-hitl" ]; then
  echo ""
  echo "1e. Registering the HITL test oc_exec tool..."
  HITL_OC_SERVER="${HITL_OC_SERVER:-https://api.${CLUSTER_DOMAIN}:6443}"
  if [ -z "${HITL_OC_TOKEN:-}" ] && [ "${HITL_CREATE_SA:-false}" == "true" ]; then
    oc create serviceaccount mas-hitl-test -n mao --dry-run=client -o yaml | oc apply -f -
    oc adm policy add-cluster-role-to-user view -z mas-hitl-test -n mao >/dev/null
    HITL_OC_TOKEN=$(oc create token mas-hitl-test -n mao --duration=720h)
  fi
  if [ -z "${HITL_OC_TOKEN:-}" ]; then
    echo "❌ multi-cluster-hitl needs HITL_OC_TOKEN, or HITL_CREATE_SA=true"
    exit 1
  fi
  HITL_TOOL_PAYLOAD=$(jq -n \
    --arg server "$HITL_OC_SERVER" \
    --arg token "$HITL_OC_TOKEN" \
    '{
      "category": "tools",
      "type": "oc_exec",
      "name": "HITL Test oc (read-only)",
      "config": {"type": "oc_exec", "server": $server, "token": $token, "skip_tls_verify": false}
    }')
  HITL_TOOL_RESPONSE=$(mas_api_upsert_resource "$HITL_TOOL_PAYLOAD")
  HITL_TOOL_RID=$(echo "$HITL_TOOL_RESPONSE" | jq -r '.rid // empty')
  if [ -z "$HITL_TOOL_RID" ]; then
    HITL_TOOL_RID=$(mas_api_curl -sS "${MAS_API_URL}/api/resources/resources.list?category=tools" \
      -H "Authorization: Bearer ${MAS_TOKEN}" \
      | jq -r '.resources[]? | select(.name == "HITL Test oc (read-only)") | .rid' | head -1)
  fi
  echo "   ✅ HITL test tool RID: $HITL_TOOL_RID"
  HITL_GATE_NODE=$(cat <<NODE
  - rid: hitl_gate
    name: Approval Gate (HITL test)
    type: custom_agent_node
    config:
      type: custom_agent_node
      llm: "\$ref:${LLM_RID}"
      tools:
        - "\$ref:${HITL_TOOL_RID}"
      # oc_exec is a WRITE tool, so every call waits for human approval.
      hitl_mode: ask
      max_rounds: 3
      system_message: |
        You are a test approval gate. For every request, call the oc tool
        exactly once with the command "whoami". Then reply with one short
        sentence saying the pre-flight check ran (or was rejected). Do not
        answer the user's question; the orchestrator does that next.
NODE
)
  HITL_GATE_PLAN=$(cat <<PLAN
  - uid: hitl_gate
    after: start
    node: hitl_gate
PLAN
)
  ORCHESTRATE_FIRST_AFTER="hitl_gate"
fi

echo ""

# ============================================================================
# STEP 2: Create Blueprint Using Inventory Resource RIDs
# ============================================================================

echo "========================================="
echo "STEP 2: Creating Blueprint"
echo "========================================="
echo ""

if [ "$BLUEPRINT_TYPE" == "simple" ]; then

  echo "Creating simple blueprint..."

  cat > /tmp/mas-blueprint.yaml <<EOF
description: Ask Red Hat documentation and product guidance

name: Ask Red Hat Blueprint (Simple)

nodes:
  - rid: user_question_start
    name: User Question
    type: user_question_node
    config:
      type: user_question_node

  - rid: ${ASK_REDHAT_RID}
    name: Ask Red Hat
    type: a2a_agent_node
    config:
      type: a2a_agent_node
      base_url: "${ASK_REDHAT_A2A_URL}"
      # Empty uses the token from the MAS execution context; no static token is stored.
      bearer_token: ""
      verify_ssl: true

  - rid: final_answer_end
    name: Final Answer
    type: final_answer_node
    config:
      type: final_answer_node

plan:
  - uid: start
    node: user_question_start

  - uid: ask_red_hat
    after: start
    node: ${ASK_REDHAT_RID}

  - uid: end
    after: ask_red_hat
    node: final_answer_end
EOF

elif [ "$IS_MULTI_CLUSTER" == "true" ]; then

  echo "Creating multi-cluster blueprint..."

  cat > /tmp/mas-blueprint.yaml <<EOF
description: Multi-cluster OpenShift investigation and Red Hat knowledge with intelligent routing and feedback loop

name: Multi-Cluster Investigation and Ask Red Hat Blueprint

conditions:
  # External ref to the saved router_direct condition (registered in step 1b2),
  # so the resolver pulls it from inventory into the runtime graph. The plan's
  # exit_condition below references the same rid.
  - rid: "\$ref:${COND_RID}"
    name: Multi-Cluster Orchestrator Router
    type: router_direct
    config:
      type: router_direct

nodes:
  - rid: user_question_start
    name: User Question
    type: user_question_node
    config:
      type: user_question_node

${HITL_GATE_NODE}

  - rid: ${ORCH_RID}
    name: Multi-Cluster Router
    type: orchestrator_node
    config:
      type: orchestrator_node
      # Must be an EXTERNAL ref ("\$ref:<rid>"), not a bare RID. MAS's
      # BlueprintResolver only walks refs prefixed with "\$ref:" (see
      # mas/core/ref/models.py is_external_ref + walker.external_rids); a bare
      # RID is treated as an inline/local ref, so the LLM is never pulled from
      # inventory, the resolved blueprint's llms catalog stays empty, and the
      # session build fails with "unknown llms ref '<rid>'".
      llm: "\$ref:${LLM_RID}"
      system_message: |
$(printf '%s\n' "$ORCHESTRATOR_SYSTEM_MESSAGE" | sed 's/^/        /')

  - rid: ${A2A_RID}
    name: OLS Investigator
    type: a2a_agent_node
    config:
      type: a2a_agent_node
      base_url: "https://openshift-lightspeed.apps.${CLUSTER_DOMAIN}/a2a"
      bearer_token: ""
      verify_ssl: true

  - rid: ${ASK_REDHAT_RID}
    name: Ask Red Hat
    type: a2a_agent_node
    config:
      type: a2a_agent_node
      base_url: "${ASK_REDHAT_A2A_URL}"
      # Empty uses the token from the MAS execution context; no static token is stored.
      bearer_token: ""
      verify_ssl: true

  - rid: final_answer_end
    name: Final Answer
    type: final_answer_node
    config:
      type: final_answer_node

plan:
  - uid: start
    node: user_question_start

${HITL_GATE_PLAN}

  - uid: orchestrate
    after:
      - ${ORCHESTRATE_FIRST_AFTER}
      - investigate
      - ask_red_hat
    node: ${ORCH_RID}
    exit_condition: ${COND_RID}
    branches:
      investigate: investigate
      ask_red_hat: ask_red_hat
      finalize: finalize

  - uid: investigate
    node: ${A2A_RID}

  - uid: ask_red_hat
    node: ${ASK_REDHAT_RID}

  - uid: finalize
    node: final_answer_end
EOF

fi

echo "Blueprint YAML:"
cat /tmp/mas-blueprint.yaml
echo ""

# ============================================================================
# STEP 3: Save Blueprint
# ============================================================================

echo "========================================="
echo "STEP 3: Saving Blueprint"
echo "========================================="
echo ""

SAVE_RESPONSE=$(mas_api_curl -sS -X POST "${MAS_API_URL}/api/blueprints/blueprint.save" \
  -H "Authorization: Bearer ${MAS_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"blueprintRaw\": $(cat /tmp/mas-blueprint.yaml | jq -Rs .)}")

BLUEPRINT_ID=$(echo "$SAVE_RESPONSE" | jq -r '.blueprint_id // empty')

if [ -n "$BLUEPRINT_ID" ]; then
  echo "✅ Blueprint saved: $BLUEPRINT_ID"
else
  echo "❌ Failed to save blueprint"
  echo "$SAVE_RESPONSE" | jq '.'
  exit 1
fi

echo ""

# ============================================================================
# STEP 4: Summary
# ============================================================================

echo "========================================="
echo "✅ SUCCESS!"
echo "========================================="
echo ""
echo "Blueprint ID: $BLUEPRINT_ID"
echo ""
echo "Inventory Resources:"
if [ "$IS_MULTI_CLUSTER" == "true" ]; then
  echo "  - OLS A2A Agent RID: $A2A_RID"
  echo "  - LLM RID: $LLM_RID"
  echo "  - Orchestrator RID: $ORCH_RID"
fi
echo "  - Ask Red Hat A2A Agent RID: $ASK_REDHAT_RID"
echo ""
echo "Next: Update variants/standalone/values-standalone.yaml:"
echo ""
echo "    mao-a2a:"
echo "      overrides:"
echo "        - name: a2a.enabled"
echo "          value: \"true\""
echo "        - name: a2a.blueprintId"
echo "          value: \"${BLUEPRINT_ID}\""
echo "        - name: a2a.publicBaseUrl"
echo "          value: \"https://mas-a2a-mao.apps.${CLUSTER_DOMAIN}\""
echo "        - name: a2a.allowedPrincipals"
echo "          value: '[\"<your-principal-sub>\"]'  # e.g., Red Hat SSO 'sub' claim or 'dev' for testing"
echo "        - name: route.enabled"
echo "          value: \"true\""
echo ""
echo "Note: route.host and identity.providerMode are optional."
echo "  - route.host auto-derives from global.clusterBaseDomain"
echo "  - identity.oidcIssuers in values-standalone.yaml configures multi-issuer OIDC"
echo "  - For dev/testing, you can use: identity.providerMode=dev and allowedPrincipals=[\"dev\"]"
echo ""
