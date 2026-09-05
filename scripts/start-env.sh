#!/usr/bin/env bash
# Bring up the Boutique app (port-forward), Opik, and the Streamlit dashboard —
# everything except Docker Desktop itself, which is Windows-side (see
# scripts/start-env.ps1, the entrypoint you actually want to run).
#
# Run from WSL, from the repo root: bash scripts/start-env.sh
#
# The point of this script vs. doing it by hand: every slow piece (k3d cluster
# start, the Opik compose healthy-chain, the Streamlit cold start) is launched
# in PARALLEL and polled together, not started-then-waited-for one at a time.
# Opik's own dependency chain (redis/mysql/zookeeper/minio -> clickhouse ->
# backend -> frontend) genuinely takes a few minutes; this script just stops
# making you (or an agent) wait for it before starting anything else.

set -uo pipefail
cd "$(dirname "$0")/.."

OPIK_DIR="/mnt/d/Claude Code/opik/deployment/docker-compose"
LOG_DIR="/tmp/sre-agent-env"
mkdir -p "$LOG_DIR"

echo "== 1. k3d cluster =="
if ! ~/.local/bin/k3d cluster list 2>/dev/null | grep -q "^sre-lab .*1/1.*[1-9]/2\|^sre-lab .*1/1.*2/2"; then
  echo "  starting sre-lab..."
  ~/.local/bin/k3d cluster start sre-lab
else
  echo "  already up."
fi

echo "== 2. kubeconfig (cheap, always refresh) =="
SERVER_PORT=$(docker port k3d-sre-lab-serverlb 2>/dev/null | grep '6443/tcp' | head -1 | grep -oE '[0-9]+$')
if [ -n "${SERVER_PORT:-}" ]; then
  docker exec k3d-sre-lab-server-0 cat /etc/rancher/k3s/k3s.yaml \
    | sed "s#server: https://127.0.0.1:6443#server: https://127.0.0.1:${SERVER_PORT}#" > ~/.kube/config
  echo "  wrote kubeconfig (server port ${SERVER_PORT})"
else
  echo "  WARNING: couldn't detect the k3d server's host port, kubeconfig not refreshed"
fi

echo "== 3. launching Opik, port-forward, and dashboard IN PARALLEL =="

# --- Opik ---
(
  cd "$OPIK_DIR" || exit 1
  export MYSQL_PORT=3307 SERVER_ADMIN_PORT=8082 MINIO_CONSOLE_PORT=9091
  docker compose --profile opik up -d
) > "$LOG_DIR/opik.log" 2>&1 &
OPIK_PID=$!

# --- Boutique frontend port-forward (skip if already healthy, else kill+restart) ---
if ! curl -s -o /dev/null --max-time 2 http://localhost:8089/ 2>/dev/null; then
  pkill -f "port-forward -n boutique svc/frontend" 2>/dev/null
  sleep 1
  nohup kubectl port-forward -n boutique svc/frontend 8089:80 > "$LOG_DIR/port-forward.log" 2>&1 &
fi

# --- Streamlit dashboard (skip if already running and healthy) ---
if ! curl -s -o /dev/null --max-time 2 http://localhost:8501/ 2>/dev/null; then
  pkill -f "streamlit run src/sre_agent/dashboard.py" 2>/dev/null
  nohup .venv/bin/streamlit run src/sre_agent/dashboard.py --server.headless true \
    > "$LOG_DIR/dashboard.log" 2>&1 &
fi

echo "  all three launched, waiting on Opik's compose call to finish issuing (not to be healthy)..."
wait "$OPIK_PID"

echo "== 4. polling all three until ready (up to ~5 min) =="
check() { curl -s -o /dev/null -w "%{http_code}" --max-time 2 "$1" 2>/dev/null; }

for i in $(seq 1 60); do
  B=$(check http://localhost:8089/)
  D=$(check http://localhost:8501/)
  O=$(check http://localhost:5173/)
  printf "\r  [%2ds] boutique=%s dashboard=%s opik=%s   " "$((i*5))" "${B:-...}" "${D:-...}" "${O:-...}"
  if [ "$B" = "200" ] && [ "$D" = "200" ] && [ "$O" = "200" ]; then
    echo ""
    echo "== ALL READY =="
    echo "  Boutique app : http://localhost:8089"
    echo "  Dashboard    : http://localhost:8501"
    echo "  Opik         : http://localhost:5173"
    exit 0
  fi
  sleep 5
done

echo ""
echo "== TIMED OUT after 5 min — check logs in $LOG_DIR =="
echo "  boutique=$B dashboard=$D opik=$O"
exit 1
