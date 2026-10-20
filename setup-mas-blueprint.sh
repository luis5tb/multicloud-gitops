#!/bin/bash
# MAS Multi-Cluster Blueprint Setup Script
#
# Correct Architecture:
# 1. Register LLM resource → get LLM_RID
# 2. Register Orchestrator Node (referencing LLM_RID) → get ORCHESTRATOR_RID
# 3. Register A2A Agent Node → get A2A_RID
# 4. Create blueprint where node.rid = inventory resource RID (no config needed)
#
# Usage:
#   export LITELLM_API_KEY="your-key"
#   export LITELLM_API_BASE="https://maas-rhdp.apps.maas.redhatworkshops.io/v1"
#   export LITELLM_MODEL="gpt-oss-20b"
#   ./create-mas-blueprint-final.sh [simple|multi-cluster]

set -e

# Configuration
export KUBECONFIG="${KUBECONFIG:-/home/ltomasbo/git_repos/multicloud-gitops/admin-break-glass.kubeconfig}"
export MAS_API_URL="${MAS_API_URL:-https://$(oc get route mas-api -n mao -o jsonpath='{.spec.host}' 2>/dev/null)}"
export MAS_TOKEN="${MAS_TOKEN:-dev-token}"
CLUSTER_DOMAIN="${CLUSTER_DOMAIN:-ocp.phkl2.sandbox315.opentlc.com}"
CLUSTER_ID="api-$(echo $CLUSTER_DOMAIN | sed 's/\./-/g')-6443"

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

echo "========================================="
echo "MAS Multi-Cluster Blueprint Setup"
echo "========================================="
echo "MAS API: $MAS_API_URL"
echo "Cluster: $CLUSTER_DOMAIN"
echo "Blueprint Type: $BLUEPRINT_TYPE"
echo ""

# Check connectivity
echo "Testing MAS API connectivity..."
HEALTH=$(curl -s "${MAS_API_URL}/api/health/" | jq -r '.status' 2>/dev/null || echo "error")
if [ "$HEALTH" != "ok" ]; then
  echo "❌ Cannot reach MAS API"
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

if [ "$BLUEPRINT_TYPE" == "multi-cluster" ]; then

  # 1a. Register LLM (Vertex/Gemini or OpenAI/LiteLLM)
  if [ "$LLM_PROVIDER" == "vertex" ]; then
    LLM_NAME="Multi-Cluster Orchestrator LLM (Vertex)"
    echo "1a. Registering Vertex google_genai LLM: ${VERTEX_MODEL} (project=${VERTEX_PROJECT}, location=${VERTEX_LOCATION})..."
    # api_key is ignored in Vertex mode (google-genai Client(vertexai=true)
    # authenticates via ADC/GOOGLE_APPLICATION_CREDENTIALS mounted in the
    # worker). vertexai/project/location go through the passthrough "extra".
    LLM_RESPONSE=$(curl -s -X POST "${MAS_API_URL}/api/resources/resource.save" \
      -H "Authorization: Bearer ${MAS_TOKEN}" \
      -H "Content-Type: application/json" \
      -d '{
        "category": "llms",
        "type": "google_genai",
        "name": "'"${LLM_NAME}"'",
        "config": {
          "type": "google_genai",
          "model_name": "'"${VERTEX_MODEL}"'",
          "api_key": "unused-vertex-adc",
          "temperature": 0.7,
          "extra": {"vertexai": true, "project": "'"${VERTEX_PROJECT}"'", "location": "'"${VERTEX_LOCATION}"'"}
        }
      }')
  else
    if [ -z "$LITELLM_API_KEY" ]; then
      echo "❌ Error: LITELLM_API_KEY required for LLM_PROVIDER=openai"
      exit 1
    fi
    LLM_NAME="Multi-Cluster Orchestrator LLM"
    LLM_MODEL="${LITELLM_MODEL:-gpt-oss-20b}"
    LLM_BASE_URL="${LITELLM_API_BASE:-https://maas-rhdp.apps.maas.redhatworkshops.io/v1}"
    echo "1a. Registering OpenAI/LiteLLM LLM: ${LLM_MODEL}..."
    LLM_RESPONSE=$(curl -s -X POST "${MAS_API_URL}/api/resources/resource.save" \
      -H "Authorization: Bearer ${MAS_TOKEN}" \
      -H "Content-Type: application/json" \
      -d '{
        "category": "llms",
        "type": "openai",
        "name": "'"${LLM_NAME}"'",
        "config": {
          "type": "openai",
          "model_name": "'"${LLM_MODEL}"'",
          "api_key": "'"${LITELLM_API_KEY}"'",
          "base_url": "'"${LLM_BASE_URL}"'",
          "temperature": 0.7
        }
      }')
  fi

  LLM_RID=$(echo "$LLM_RESPONSE" | jq -r '.rid // empty')
  if [ -z "$LLM_RID" ]; then
    LLM_RID=$(curl -s "${MAS_API_URL}/api/resources/resources.list?category=llms" \
      -H "Authorization: Bearer ${MAS_TOKEN}" \
      | jq -r '.resources[]? | select(.name == "'"${LLM_NAME}"'") | .rid' | head -1)
  fi
  echo "   ✅ LLM RID: $LLM_RID"

  # 1b. Register Orchestrator Node
  echo ""
  echo "1b. Registering Orchestrator Node..."
  ORCH_RESPONSE=$(curl -s -X POST "${MAS_API_URL}/api/resources/resource.save" \
    -H "Authorization: Bearer ${MAS_TOKEN}" \
    -H "Content-Type: application/json" \
    -d '{
      "category": "nodes",
      "type": "orchestrator_node",
      "name": "Multi-Cluster Router",
      "config": {
        "type": "orchestrator_node",
        "llm": "'"${LLM_RID}"'",
        "system_message": "You are a multi-cluster OpenShift investigation router.\n\nAVAILABLE CLUSTERS:\n- '"${CLUSTER_DOMAIN}"' → '"${CLUSTER_ID}"'\n\nWhen delegating to the investigation agent:\n1. Use the delegate_task tool\n2. Set data parameter: {\"headers\": {\"X-OLS-Cluster\": \"'"${CLUSTER_ID}"'\"}}\n\nIMPORTANT: Always include the X-OLS-Cluster header!"
      }
    }')

  ORCH_RID=$(echo "$ORCH_RESPONSE" | jq -r '.rid // empty')
  if [ -z "$ORCH_RID" ]; then
    ORCH_RID=$(curl -s "${MAS_API_URL}/api/resources/resources.list?category=nodes" \
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
  COND_RESPONSE=$(curl -s -X POST "${MAS_API_URL}/api/resources/resource.save" \
    -H "Authorization: Bearer ${MAS_TOKEN}" \
    -H "Content-Type: application/json" \
    -d '{
      "category": "conditions",
      "type": "router_direct",
      "name": "Multi-Cluster Orchestrator Router",
      "config": {
        "type": "router_direct"
      }
    }')
  COND_RID=$(echo "$COND_RESPONSE" | jq -r '.rid // empty')
  if [ -z "$COND_RID" ]; then
    COND_RID=$(curl -s "${MAS_API_URL}/api/resources/resources.list?category=conditions" \
      -H "Authorization: Bearer ${MAS_TOKEN}" \
      | jq -r '.resources[]? | select(.name == "Multi-Cluster Orchestrator Router") | .rid' | head -1)
  fi
  echo "   ✅ Router Condition RID: $COND_RID"
fi

# 1c. Register A2A Agent Node
echo ""
echo "1c. Registering A2A Agent Node..."
A2A_RESPONSE=$(curl -s -X POST "${MAS_API_URL}/api/resources/resource.save" \
  -H "Authorization: Bearer ${MAS_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "category": "nodes",
    "type": "a2a_agent_node",
    "name": "OpenShift Lightspeed Agent",
    "config": {
      "base_url": "https://openshift-lightspeed.apps.'"${CLUSTER_DOMAIN}"'/a2a",
      "bearer_token": "",
      "verify_ssl": true
    }
  }')

A2A_RID=$(echo "$A2A_RESPONSE" | jq -r '.rid // empty')
if [ -z "$A2A_RID" ]; then
  A2A_RID=$(curl -s "${MAS_API_URL}/api/resources/resources.list?category=nodes" \
    -H "Authorization: Bearer ${MAS_TOKEN}" \
    | jq -r '.resources[]? | select(.name == "OpenShift Lightspeed Agent") | .rid' | head -1)
fi
echo "   ✅ A2A Agent RID: $A2A_RID"

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
description: Simple OpenShift investigation via OLS through Praxis proxy

name: OLS Investigation Blueprint (Simple)

nodes:
  - rid: user_question_start
    name: User Question
    type: user_question_node
    config:
      type: user_question_node

  - rid: ${A2A_RID}
    name: OLS Investigator
    type: a2a_agent_node
    config:
      type: a2a_agent_node
      base_url: "https://openshift-lightspeed.apps.${CLUSTER_DOMAIN}/a2a"
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

  - uid: investigate
    after: start
    node: ${A2A_RID}

  - uid: end
    after: investigate
    node: final_answer_end
EOF

elif [ "$BLUEPRINT_TYPE" == "multi-cluster" ]; then

  echo "Creating multi-cluster blueprint..."

  cat > /tmp/mas-blueprint.yaml <<EOF
description: Multi-cluster OpenShift investigation with intelligent routing and feedback loop

name: Multi-Cluster OLS Investigation Blueprint

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
      # Strong, mandatory-delegation wording: the orchestrator's built-in
      # behavior prompt tells it to NOT delegate on assumptions and to just ask
      # clarifying questions when a request looks vague -- which made the small
      # model finish in one planning cycle with an empty answer, never calling
      # the investigation agent. This message overrides that for the cluster
      # use case: it MUST delegate to the OLS investigator before finalizing.
      system_message: "You are a multi-cluster OpenShift investigation router. Your ONLY job is to delegate investigation work to the OLS investigation agent and then relay its findings. For ANY request about an OpenShift cluster (status, health, pods, events, errors, resources, troubleshooting, or general OpenShift questions), you MUST delegate to the investigation agent. Never answer from your own knowledge, and never finalize before the investigation agent has responded at least once. When you delegate, always include headers {\"X-OLS-Cluster\": \"${CLUSTER_ID}\"} so the investigation targets the correct cluster. After the investigation agent responds, review its findings and either delegate a focused follow-up (investigate) if more detail is needed, or finalize with a complete answer based strictly on what the investigation agent returned. Do NOT treat the request as vague and do NOT stop to ask the user clarifying questions unless the message names no cluster topic at all; otherwise proceed directly to delegation."

  - rid: ${A2A_RID}
    name: OLS Investigator
    type: a2a_agent_node
    config:
      type: a2a_agent_node
      base_url: "https://openshift-lightspeed.apps.${CLUSTER_DOMAIN}/a2a"
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

  - uid: orchestrate
    after:
      - start
      - investigate
    node: ${ORCH_RID}
    exit_condition: ${COND_RID}
    branches:
      investigate: investigate
      finalize: finalize

  - uid: investigate
    node: ${A2A_RID}

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

SAVE_RESPONSE=$(curl -s -X POST "${MAS_API_URL}/api/blueprints/blueprint.save" \
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
echo "  - A2A Agent RID: $A2A_RID"
if [ "$BLUEPRINT_TYPE" == "multi-cluster" ]; then
  echo "  - LLM RID: $LLM_RID"
  echo "  - Orchestrator RID: $ORCH_RID"
fi
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
