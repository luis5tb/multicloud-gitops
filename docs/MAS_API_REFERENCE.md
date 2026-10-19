# MAS API Reference

Quick reference for the actual MAS API endpoints discovered through testing.

## Resource API

### Save Resource

**Endpoint**: `POST /api/resources/resource.save`

**Parameters** (all required):
- `category`: Resource category (see valid values below)
- `type`: Resource type
- `name`: Resource name
- `config`: Resource configuration (object)

**Valid Categories**:
- `nodes` - Use for A2A agents, orchestrator nodes
- `llms` - LLM configurations
- `tools` - Custom tools
- `retrievers` - Vector stores, retrievers
- `conditions` - Conditional logic
- `providers` - MCP providers, data providers
- `sandboxes` - Execution sandboxes

**Response Field**: `rid` (not `resource_id` or `id`)

**Example** - Register OLS A2A Agent:
```bash
curl -s -X POST "${MAS_API_URL}/api/resources/resource.save" \
  -H "Authorization: Bearer ${MAS_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "category": "nodes",
    "type": "a2a_agent_node",
    "name": "OpenShift Lightspeed Agent",
    "config": {
      "base_url": "https://openshift-lightspeed.apps.example.com/a2a",
      "bearer_token": "",
      "verify_ssl": true
    }
  }' | jq -r '.rid'
```

**Example** - Register LLM with LiteLLM:
```bash
curl -s -X POST "${MAS_API_URL}/api/resources/resource.save" \
  -H "Authorization: Bearer ${MAS_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "category": "llms",
    "type": "openai",
    "name": "LiteLLM Proxy",
    "config": {
      "type": "openai",
      "model_name": "gpt-oss-20b",
      "api_key": "'"${LITELLM_API_KEY}"'",
      "base_url": "'"${LITELLM_API_BASE}"'",
      "temperature": 0.7
    }
  }' | jq -r '.rid'
```

**Note**: Model name format depends on LiteLLM configuration - use `gpt-oss-20b` not `openai/gpt-oss-20b`.

### List Resources

**Endpoint**: `GET /api/resources/resources.list`

**Query Parameters**:
- `category`: Filter by category (optional)
- `type`: Filter by type (optional)
- `limit`: Max results (default: 1000)
- `offset`: Pagination offset (default: 0)

**Response**: Resource ID is in `rid` field

**Example**:
```bash
curl -s "${MAS_API_URL}/api/resources/resources.list?category=nodes" \
  -H "Authorization: Bearer ${MAS_TOKEN}" \
  | jq '.resources[] | {rid, name, type}'
```

### Get Resource

**Endpoint**: `GET /api/resources/resource.get?resource_id=<rid>`

**Example**:
```bash
curl -s "${MAS_API_URL}/api/resources/resource.get?resource_id=0b7db98046e64b3691391be9dfb005b6" \
  -H "Authorization: Bearer ${MAS_TOKEN}" | jq .
```

## Blueprint API

### Save Blueprint

**Endpoint**: `POST /api/blueprints/blueprint.save`

**Parameters**:
- `blueprintRaw`: YAML string of the blueprint

**Response Field**: `blueprint_id`

**Example**:
```bash
BLUEPRINT_YAML=$(cat blueprint.yaml)
curl -s -X POST "${MAS_API_URL}/api/blueprints/blueprint.save" \
  -H "Authorization: Bearer ${MAS_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"blueprintRaw\": $(echo "$BLUEPRINT_YAML" | jq -Rs .)}" \
  | jq -r '.blueprint_id'
```

### Get Blueprint

**Endpoint**: `GET /api/blueprints/available.blueprints.resolved.get?blueprintId=<id>`

**Example**:
```bash
curl -s "${MAS_API_URL}/api/blueprints/available.blueprints.resolved.get?blueprintId=c9fc1c38-bcca-42bd-b368-86ce5f8dae09" \
  -H "Authorization: Bearer ${MAS_TOKEN}" | jq .
```

## Common Resource Types

### Nodes (category: "nodes")
- `a2a_agent_node` - A2A agent
- `user_question_node` - User input
- `final_answer_node` - Final output
- `orchestrator_node` - Orchestrator with LLM

### LLMs (category: "llms")
- `openai` - OpenAI or LiteLLM (OpenAI-compatible)
- `google_vertex` - Google Vertex AI

### Config Examples

**A2A Agent Node**:
```json
{
  "base_url": "https://agent.example.com/a2a",
  "bearer_token": "",
  "verify_ssl": true
}
```

**OpenAI/LiteLLM**:
```json
{
  "type": "openai",
  "model_name": "gpt-oss-20b",
  "api_key": "sk-...",
  "base_url": "https://litellm-proxy.example.com/v1",
  "temperature": 0.7
}
```

**Orchestrator Node** (references LLM RID):
```json
{
  "type": "orchestrator_node",
  "llm": "abc123-llm-rid-here",
  "system_message": "You are a multi-cluster router..."
}
```

## Environment Setup

```bash
export KUBECONFIG=/path/to/admin-break-glass.kubeconfig
export MAS_API_URL="https://$(oc get route mas-api -n mao -o jsonpath='{.spec.host}')"
export MAS_TOKEN="dev-token"  # In dev mode

# For LiteLLM (multi-cluster orchestration)
export LITELLM_API_BASE="https://maas-rhdp.apps.maas.redhatworkshops.io/v1"
export LITELLM_API_KEY="sk-litellm-..."
export LITELLM_MODEL="gpt-oss-20b"  # Note: NOT openai/gpt-oss-20b
```

## Error Handling

**Resource exists**: `"error": "ResourceCategory.NODE:a2a_agent_node:Name exists for user"`

**Solution**: List existing resources and get the `rid`:
```bash
curl -s "${MAS_API_URL}/api/resources/resources.list?category=nodes" \
  -H "Authorization: Bearer ${MAS_TOKEN}" \
  | jq -r '.resources[] | select(.name == "Your Resource Name") | .rid'
```

## Blueprint Architecture

**Key Learnings:**

1. **Inventory resources** are registered for management/tracking
2. **Blueprint nodes** reference inventory by using the resource RID as the node's `rid`
3. **Nodes still need config** with inline values, NOT `resource_id` references
4. **Orchestrator nodes** reference LLM RID in their config's `llm` field
5. **Conditions** (like `router_direct`) are needed for orchestrator branching

## Complete Multi-Cluster Example

```bash
#!/bin/bash
set -e

export KUBECONFIG=admin-break-glass.kubeconfig
export MAS_API_URL="https://$(oc get route mas-api -n mao -o jsonpath='{.spec.host}')"
export MAS_TOKEN="dev-token"
CLUSTER_DOMAIN="ocp.example.com"

# 1. Register LLM
LLM_RID=$(curl -s -X POST "${MAS_API_URL}/api/resources/resource.save" \
  -H "Authorization: Bearer ${MAS_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "category": "llms",
    "type": "openai",
    "name": "Orchestrator LLM",
    "config": {
      "type": "openai",
      "model_name": "gpt-oss-20b",
      "api_key": "'"${LITELLM_API_KEY}"'",
      "base_url": "'"${LITELLM_API_BASE}"'",
      "temperature": 0.7
    }
  }' | jq -r '.rid')

echo "LLM RID: $LLM_RID"

# 2. Register Orchestrator Node (references LLM)
ORCH_RID=$(curl -s -X POST "${MAS_API_URL}/api/resources/resource.save" \
  -H "Authorization: Bearer ${MAS_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "category": "nodes",
    "type": "orchestrator_node",
    "name": "Multi-Cluster Router",
    "config": {
      "type": "orchestrator_node",
      "llm": "'"${LLM_RID}"'",
      "system_message": "You are a multi-cluster router. Delegate to OLS agent."
    }
  }' | jq -r '.rid')

echo "Orchestrator RID: $ORCH_RID"

# 3. Register A2A Agent
A2A_RID=$(curl -s -X POST "${MAS_API_URL}/api/resources/resource.save" \
  -H "Authorization: Bearer ${MAS_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "category": "nodes",
    "type": "a2a_agent_node",
    "name": "OLS Agent",
    "config": {
      "base_url": "https://openshift-lightspeed.apps.'"${CLUSTER_DOMAIN}"'/a2a",
      "bearer_token": "",
      "verify_ssl": true
    }
  }' | jq -r '.rid')

echo "A2A Agent RID: $A2A_RID"

# 4. Create blueprint - nodes use inventory RIDs + inline config
cat > /tmp/blueprint.yaml <<YAML
name: Multi-Cluster OLS Blueprint

conditions:
  - rid: router_condition
    name: Orchestrator Router
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
    name: Router
    type: orchestrator_node
    config:
      type: orchestrator_node
      llm: "${LLM_RID}"
      system_message: "Multi-cluster router"

  - rid: ${A2A_RID}
    name: Investigator
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
    exit_condition: router_condition
    branches:
      investigate: investigate
      finalize: finalize

  - uid: investigate
    node: ${A2A_RID}

  - uid: finalize
    node: final_answer_end
YAML

# 5. Save blueprint
BLUEPRINT_ID=$(curl -s -X POST "${MAS_API_URL}/api/blueprints/blueprint.save" \
  -H "Authorization: Bearer ${MAS_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"blueprintRaw\": $(cat /tmp/blueprint.yaml | jq -Rs .)}" \
  | jq -r '.blueprint_id')

echo "Blueprint ID: $BLUEPRINT_ID"
```

**Note**: Use `setup-mas-blueprint.sh` script instead of manually running these commands.
