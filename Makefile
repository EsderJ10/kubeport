# Lint targets backed by docker-compose.lint.yml.
#
# Nothing is installed on the host: every target shells out to the
# pinned ruff image declared in docker-compose.lint.yml. The container
# runs as the host UID:GID so any files written back (format, --fix)
# are owned by the user, not root.

COMPOSE     := docker compose -f docker-compose.lint.yml
HOST_UID    := $(shell id -u)
HOST_GID    := $(shell id -g)
RUFF        := $(COMPOSE) run --rm --user $(HOST_UID):$(HOST_GID) ruff

EVAL_K3D_CLUSTER ?= frappe-cluster
EVAL_FAULT_RELEASE ?= demo-k3d/demo/demo-bench
EVAL_FAULT_SITE ?= demo-k3d/demo/demo-bench/erp.cluster.local
EVAL_FAULT_SCENARIOS ?= worker_kill_mid_helm_upgrade,job_ttl_expired_before_reconcile,pod_exec_timeout_during_site_probe

.DEFAULT_GOAL := help
.PHONY: help fmt lint fix lint-check eval eval-clean eval-faults eval-faults-real

help:
	@echo "Containerised lint targets (ghcr.io/astral-sh/ruff:0.14.10):"
	@echo "  make fmt              Format Python code in place"
	@echo "  make lint             Report ruff lint findings"
	@echo "  make fix              Auto-fix ruff findings, then format"
	@echo "  make lint-check       CI-equivalent dry run (format --check + check)"
	@echo ""
	@echo "Evaluation harness (eval/README.md):"
	@echo "  make eval             Run the golden-path harness against the local k3d cluster"
	@echo "  make eval-clean       Remove all eval/results/*.json reports"
	@echo "  make eval-faults      Run fault-injection scenarios with fast-forward (seconds)"
	@echo "  make eval-faults-real Run fault-injection scenarios at real-time (~30 min)"

fmt:
	$(RUFF) format .

lint:
	$(RUFF) check .

fix:
	$(RUFF) check --fix .
	$(RUFF) format .

lint-check:
	$(RUFF) format --check .
	$(RUFF) check .

eval:
	python3 eval/harness.py --k3d-cluster $(EVAL_K3D_CLUSTER)

eval-clean:
	@find eval/results -type f -name '*.json' -print -delete

eval-faults:
	python3 eval/faults/run.py \
	    --release-doc-name $(EVAL_FAULT_RELEASE) \
	    --site-doc-name $(EVAL_FAULT_SITE) \
	    --scenarios $(EVAL_FAULT_SCENARIOS) \
	    --fast-forward

eval-faults-real:
	python3 eval/faults/run.py \
	    --release-doc-name $(EVAL_FAULT_RELEASE) \
	    --site-doc-name $(EVAL_FAULT_SITE) \
	    --scenarios $(EVAL_FAULT_SCENARIOS)
