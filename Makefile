.PHONY: help install rbac kubeconfig verify-rbac test lint doctor dashboard

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-16s %s\n", $$1, $$2}'

install:  ## Create venv and install the project (editable) with dev deps
	python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"

rbac:  ## Apply the read-only ServiceAccount + ClusterRole
	kubectl apply -f infra/rbac/sre-agent-rbac.yaml

kubeconfig:  ## Generate the read-only kubeconfig for the agent
	bash infra/rbac/gen-kubeconfig.sh

verify-rbac:  ## Show exactly what the agent identity can (and cannot) do
	@echo "== can the agent READ pods? (expect yes) =="
	kubectl auth can-i get pods --as=system:serviceaccount:sre-agent:sre-agent -A
	@echo "== can the agent DELETE pods? (expect no) =="
	kubectl auth can-i delete pods --as=system:serviceaccount:sre-agent:sre-agent -A
	@echo "== can the agent READ secrets? (expect no) =="
	kubectl auth can-i get secrets --as=system:serviceaccount:sre-agent:sre-agent -A

test:  ## Run unit tests
	. .venv/bin/activate && pytest -q

lint:  ## Ruff lint
	. .venv/bin/activate && ruff check src tests

doctor:  ## Verify the agent can read the cluster read-only
	. .venv/bin/activate && sre-agent doctor

dashboard:  ## Launch the Streamlit run-history dashboard
	. .venv/bin/activate && streamlit run src/sre_agent/dashboard.py
