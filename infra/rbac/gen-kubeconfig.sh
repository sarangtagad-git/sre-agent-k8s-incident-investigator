#!/usr/bin/env bash
# Generate a read-only kubeconfig for the sre-agent ServiceAccount.
# The agent uses THIS kubeconfig — never your admin one — so it is physically
# restricted to read-only access by RBAC.
#
# Usage:  bash infra/rbac/gen-kubeconfig.sh [output-path]
# Default output: infra/rbac/sre-agent.kubeconfig  (git-ignored — contains a token)
set -euo pipefail

NS=sre-agent
SECRET=sre-agent-token
OUT="${1:-infra/rbac/sre-agent.kubeconfig}"

echo "Reading token + CA from secret '${SECRET}' in namespace '${NS}'..."
TOKEN=$(kubectl -n "$NS" get secret "$SECRET" -o jsonpath='{.data.token}' | base64 -d)
CA=$(kubectl -n "$NS" get secret "$SECRET" -o jsonpath='{.data.ca\.crt}')
SERVER=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')

if [ -z "$TOKEN" ] || [ -z "$CA" ]; then
  echo "ERROR: token/CA empty. Did you 'kubectl apply -f infra/rbac/sre-agent-rbac.yaml' first?" >&2
  exit 1
fi

cat > "$OUT" <<EOF
apiVersion: v1
kind: Config
clusters:
  - name: sre-lab
    cluster:
      server: ${SERVER}
      certificate-authority-data: ${CA}
users:
  - name: sre-agent
    user:
      token: ${TOKEN}
contexts:
  - name: sre-agent@sre-lab
    context:
      cluster: sre-lab
      user: sre-agent
      namespace: default
current-context: sre-agent@sre-lab
EOF

chmod 600 "$OUT"
echo "Wrote read-only kubeconfig -> $OUT"
echo "Test it:  KUBECONFIG=$OUT kubectl get pods -A | head"
