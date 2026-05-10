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

# Baseline-comparison defaults: target the same demo-bench release the
# main harness's reuse-mode sample.json was produced against, so the
# numbers in eval/results/comparison.md line up apples-to-apples.
EVAL_BASELINE_K3D_CLUSTER ?= pacopepe
EVAL_BASELINE_RELEASE     ?= demo-bench
EVAL_BASELINE_NAMESPACE   ?= demo
EVAL_BASELINE_DB_ROOT_PW  ?= changeit

.DEFAULT_GOAL := help
.PHONY: help fmt lint fix lint-check eval eval-clean eval-baseline

help:
	@echo "Containerised lint targets (ghcr.io/astral-sh/ruff:0.14.10):"
	@echo "  make fmt           Format Python code in place"
	@echo "  make lint          Report ruff lint findings"
	@echo "  make fix           Auto-fix ruff findings, then format"
	@echo "  make lint-check    CI-equivalent dry run (format --check + check)"
	@echo ""
	@echo "Evaluation harness (eval/README.md):"
	@echo "  make eval          Run the golden-path harness against the local k3d cluster"
	@echo "  make eval-baseline Run the same workflow with raw kubectl + helm (eval/baseline/README.md)"
	@echo "  make eval-clean    Remove all eval/results/*.json reports"

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

eval-baseline:
	python3 eval/baseline/host_driver.py \
		--k3d-cluster $(EVAL_BASELINE_K3D_CLUSTER) \
		--release-name $(EVAL_BASELINE_RELEASE) \
		--namespace $(EVAL_BASELINE_NAMESPACE) \
		--db-root-password $(EVAL_BASELINE_DB_ROOT_PW)

eval-clean:
	@find eval/results -type f -name '*.json' -print -delete
