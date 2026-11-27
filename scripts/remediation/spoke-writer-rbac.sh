#!/usr/bin/env bash
# Print the remediation writer RBAC for ONE target spoke, to apply ON that
# spoke (the pattern does not manage spoke clusters):
#
#   scripts/remediation/spoke-writer-rbac.sh <spoke-key> <namespace> [<namespace>...] \
#     | oc --kubeconfig <spoke.kubeconfig> apply -f -
#
# Binds the audience-scoped group carried by U_exec
# (<prefix>praxis-spoke-<spoke-key>:writer) to the reviewed writer rules of
# charts/all/lightspeed-hub spokeExecutorRBAC.rules, with namespaced
# RoleBindings only -- never a ClusterRoleBinding. GROUP_PREFIX (default
# "keycloak:") must match the spoke Authentication groups prefix.
# tests/chart/test_remediation_execution.py keeps the rules identical to the
# chart's. Writers can run Pods (any ServiceAccount, mount Secrets) in these
# namespaces: list only reviewed, disposable workload namespaces.
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "usage: $0 <spoke-key> <namespace> [<namespace>...]" >&2
  exit 2
fi
spoke="$1"; shift
group="${GROUP_PREFIX-keycloak:}praxis-spoke-${spoke}:writer"
name="lightspeed-hub-spoke-executor"

for ns in "$@"; do
  case "$ns" in
    ""|default|openshift*|kube-*|lightspeed-hub)
      echo "refusing forbidden namespace '$ns'" >&2; exit 2 ;;
  esac
done

cat <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: ${name}
  labels:
    app.kubernetes.io/name: lightspeed-hub
    app.kubernetes.io/component: remediation-writer
rules:
- apiGroups: ['']
  resources: [pods, services, configmaps, persistentvolumeclaims, endpoints, replicationcontrollers]
  verbs: [get, list, watch, create, update, patch, delete]
- apiGroups: ['']
  resources: [pods/eviction]
  verbs: [create]
- apiGroups: [apps]
  resources: [deployments, deployments/scale, replicasets, replicasets/scale, statefulsets, statefulsets/scale, daemonsets]
  verbs: [get, list, watch, create, update, patch, delete]
- apiGroups: [batch]
  resources: [jobs, cronjobs]
  verbs: [get, list, watch, create, update, patch, delete]
- apiGroups: [networking.k8s.io]
  resources: [networkpolicies, ingresses]
  verbs: [get, list, watch, create, update, patch, delete]
EOF
for ns in "$@"; do
  cat <<EOF
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: ${name}
  namespace: ${ns}
  labels:
    app.kubernetes.io/name: lightspeed-hub
    app.kubernetes.io/component: remediation-writer
subjects:
- kind: Group
  apiGroup: rbac.authorization.k8s.io
  name: ${group}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: ${name}
EOF
done
