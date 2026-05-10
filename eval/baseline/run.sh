#!/usr/bin/env bash
# In-container half of the Kubeport baseline-comparison harness.
#
# Drives the same 10-phase golden path as eval/_inproc.py but using only
# raw `kubectl`, `helm`, and `kubectl exec ... -- bench` (no Kubeport).
# Per phase records: wall-clock duration, distinct shell commands issued
# (`commands_issued`), and operator decisions/actions that Kubeport
# elides (`manual_steps`).  Manual-step tallies are documented inline
# next to the call site so the count is auditable.
#
# Output: one JSON document on stdout between the markers
# `RESULT_BEGIN` / `RESULT_END`, with the same top-level shape as
# eval/_inproc.py plus per-phase `commands_issued` and `manual_steps`.
#
# Invoked from `eval/baseline/host_driver.sh` via `docker exec`; not
# meant to be run directly.

set -uo pipefail

# ---------- args ----------

KUBECONFIG_FILE=""
KUBECONFIG_CONTEXT=""
HELM_REPO_NAME="frappe"
HELM_REPO_URL="https://helm.erpnext.com"
CHART_NAME="erpnext"
RELEASE_NAME=""
NAMESPACE=""
SITE_NAME=""
DB_ROOT_USER="root"
DB_ROOT_PASSWORD=""
ADMIN_PASSWORD=""
INSTALL_APPS="erpnext"
INSECURE_SKIP_TLS_VERIFY="true"
GUNICORN_CONTAINER="gunicorn"
WORKDIR="/tmp/kubeport-baseline"

while [ $# -gt 0 ]; do
	case "$1" in
		--kubeconfig-file) KUBECONFIG_FILE="$2"; shift 2 ;;
		--kubeconfig-context) KUBECONFIG_CONTEXT="$2"; shift 2 ;;
		--helm-repo-name) HELM_REPO_NAME="$2"; shift 2 ;;
		--helm-repo-url) HELM_REPO_URL="$2"; shift 2 ;;
		--chart-name) CHART_NAME="$2"; shift 2 ;;
		--release-name) RELEASE_NAME="$2"; shift 2 ;;
		--namespace) NAMESPACE="$2"; shift 2 ;;
		--site-name) SITE_NAME="$2"; shift 2 ;;
		--db-root-user) DB_ROOT_USER="$2"; shift 2 ;;
		--db-root-password) DB_ROOT_PASSWORD="$2"; shift 2 ;;
		--admin-password) ADMIN_PASSWORD="$2"; shift 2 ;;
		--install-apps) INSTALL_APPS="$2"; shift 2 ;;
		--insecure-skip-tls-verify) INSECURE_SKIP_TLS_VERIFY="$2"; shift 2 ;;
		--gunicorn-container) GUNICORN_CONTAINER="$2"; shift 2 ;;
		--workdir) WORKDIR="$2"; shift 2 ;;
		*) echo "unknown arg: $1" >&2; exit 2 ;;
	esac
done

if [ -z "$KUBECONFIG_FILE" ] || [ -z "$RELEASE_NAME" ] || [ -z "$NAMESPACE" ] || [ -z "$SITE_NAME" ]; then
	echo "missing required arg(s); see eval/baseline/README.md" >&2
	exit 2
fi

if [ -z "$ADMIN_PASSWORD" ]; then
	ADMIN_PASSWORD="$(openssl rand -hex 8 2>/dev/null || head -c 16 /dev/urandom | xxd -p)"
fi

export KUBECONFIG="$KUBECONFIG_FILE"
mkdir -p "$WORKDIR/counters" "$WORKDIR/backups"
PHASES_JSONL="$WORKDIR/phases.jsonl"
: > "$PHASES_JSONL"

# ---------- counter helpers ----------
#
# Counters are file-backed and keyed by $CURRENT_PHASE so that wrappers
# and helper functions called via $(...) (a subshell) still update the
# right counter.  bump_cmd / bump_manual append a single byte; the
# parent shell counts file size after the phase function returns.

CURRENT_PHASE=""

bump_cmd()    { printf '.' >> "$WORKDIR/counters/cmds-$CURRENT_PHASE"; }
bump_manual() { printf '.' >> "$WORKDIR/counters/manual-$CURRENT_PHASE"; }

read_count() {
	local f="$1"
	[ -f "$f" ] && wc -c < "$f" | tr -d ' \n' || echo 0
}

# ---------- kubectl/helm wrappers ----------
#
# Every wrapper increments the per-phase command counter so the tally
# counts the distinct shell invocations an operator would type.

k() {
	bump_cmd
	local args=()
	if [ -n "$KUBECONFIG_CONTEXT" ]; then args+=(--context "$KUBECONFIG_CONTEXT"); fi
	if [ "$INSECURE_SKIP_TLS_VERIFY" = "true" ]; then args+=(--insecure-skip-tls-verify=true); fi
	kubectl "${args[@]}" "$@"
}

h() {
	bump_cmd
	local args=()
	if [ -n "$KUBECONFIG_CONTEXT" ]; then args+=(--kube-context "$KUBECONFIG_CONTEXT"); fi
	if [ "$INSECURE_SKIP_TLS_VERIFY" = "true" ]; then args+=(--kube-insecure-skip-tls-verify); fi
	helm "${args[@]}" "$@"
}

# ---------- phase helpers ----------

now_iso() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
mono_s()  { python3 -c 'import time; print(f"{time.monotonic():.6f}")'; }

ABORT=0

run_phase() {
	local name="$1"
	local timeout_s="$2"
	local fn="$3"

	CURRENT_PHASE="$name"
	: > "$WORKDIR/counters/cmds-$name"
	: > "$WORKDIR/counters/manual-$name"

	local started status="pending" detail="" finished duration t0 t1
	started="$(now_iso)"
	t0="$(mono_s)"

	if [ "$ABORT" = "1" ]; then
		status="skipped"
		detail="Earlier phase failed"
		finished="$started"
		duration="0.000"
	else
		local tmp
		tmp="$(mktemp)"
		set +e
		"$fn" > "$tmp" 2>&1
		local rc=$?
		set -e
		detail="$(cat "$tmp")"
		rm -f "$tmp"
		finished="$(now_iso)"
		t1="$(mono_s)"
		duration="$(awk -v a="$t0" -v b="$t1" 'BEGIN{printf "%.3f", b-a}')"
		if [ $rc -eq 0 ]; then
			status="passed"
			detail=""
		else
			status="failed"
			ABORT=1
		fi
	fi

	local cmds manual
	cmds="$(read_count "$WORKDIR/counters/cmds-$name")"
	manual="$(read_count "$WORKDIR/counters/manual-$name")"

	NAME="$name" \
	STARTED="$started" FINISHED="$finished" DURATION="$duration" \
	TIMEOUT="$timeout_s" STATUS="$status" DETAIL="$detail" \
	CMDS="$cmds" MANUAL="$manual" \
	python3 - <<-'PY' >> "$PHASES_JSONL"
	import json, os
	print(json.dumps({
	    "phase":            os.environ["NAME"],
	    "started_at":       os.environ["STARTED"],
	    "finished_at":      os.environ["FINISHED"],
	    "duration_seconds": float(os.environ["DURATION"]),
	    "timeout_seconds":  int(os.environ["TIMEOUT"]),
	    "status":           os.environ["STATUS"],
	    "detail":           os.environ["DETAIL"],
	    "commands_issued":  int(os.environ["CMDS"]),
	    "manual_steps":     int(os.environ["MANUAL"]),
	}))
	PY
}

resolve_pod() {
	# Locate a Frappe workload pod — gunicorn is the canonical bench
	# pod ERPNext charts ship.  An operator without Kubeport reads the
	# chart docs to learn the label scheme; that counts as one manual
	# step.
	bump_manual
	k get pods -n "$NAMESPACE" \
		-l "app.kubernetes.io/name=${CHART_NAME}-gunicorn" \
		--field-selector=status.phase=Running \
		-o jsonpath='{.items[0].metadata.name}' 2>/dev/null
}

# ---------- phases ----------

phase_setup_cluster_doc() {
	# Operator picks the right context out of their kubeconfig — Kubeport
	# stores this on the Kubernetes Cluster row.
	bump_manual
	if [ -n "$KUBECONFIG_CONTEXT" ]; then
		k config use-context "$KUBECONFIG_CONTEXT" >/dev/null
	else
		k config current-context >/dev/null
	fi
}

phase_setup_helm_repo() {
	h repo add "$HELM_REPO_NAME" "$HELM_REPO_URL" --force-update >/dev/null
	h repo update "$HELM_REPO_NAME" >/dev/null
}

phase_verify_chart() {
	h search repo "${HELM_REPO_NAME}/${CHART_NAME}" --output json >/dev/null
}

phase_create_release() {
	# Operator hand-writes a values.yaml — no DocType to validate it.
	# Kubeport persists this through the Helm Release row + chart-default
	# merge; baseline writes a minimal stub so phase 5 has something to
	# verify against.
	bump_manual
	cat > "$WORKDIR/values.yaml" <<-EOF
		nameOverride: ""
		fullnameOverride: "${RELEASE_NAME}"
	EOF
}

phase_deploy_release() {
	# In reuse mode the release already exists; an operator would run
	# `helm upgrade --install`. We verify with `helm status` so we don't
	# disturb the shared demo-bench. README documents this — the
	# `commands_issued` count would be 1 either way.
	h status "$RELEASE_NAME" -n "$NAMESPACE" -o json >/dev/null
	# Ground-truth: confirm the gunicorn pod is Running.  Kubeport hides
	# this readiness check behind the reconciliation loop.
	bump_manual
	k wait -n "$NAMESPACE" \
		-l "app.kubernetes.io/name=${CHART_NAME}-gunicorn" \
		--for=condition=Ready pod --timeout=120s >/dev/null
}

phase_create_site() {
	local pod
	pod="$(resolve_pod)"
	if [ -z "$pod" ]; then
		echo "no Running gunicorn pod found in namespace $NAMESPACE"
		return 1
	fi
	# Operator chooses an admin password and supplies the DB root creds
	# (Kubeport injects both via per-Job Secrets).
	bump_manual; bump_manual
	# `bench new-site` blocks; no async wrapper.  Long timeout because
	# fixtures + install-apps dominate the wall-clock.
	k exec -n "$NAMESPACE" "$pod" -c "$GUNICORN_CONTAINER" -- bash -lc "
		set -euo pipefail
		cd /home/frappe/frappe-bench
		bench new-site '$SITE_NAME' \
			--mariadb-user-host-login-scope='%' \
			--db-type=mariadb \
			--mariadb-root-username='$DB_ROOT_USER' \
			--mariadb-root-password='$DB_ROOT_PASSWORD' \
			--admin-password='$ADMIN_PASSWORD' \
			--install-app='$INSTALL_APPS'
	" >/dev/null
}

phase_migrate_site() {
	local pod
	pod="$(resolve_pod)"
	[ -n "$pod" ] || { echo "no pod"; return 1; }
	k exec -n "$NAMESPACE" "$pod" -c "$GUNICORN_CONTAINER" -- bash -lc "
		set -euo pipefail
		cd /home/frappe/frappe-bench
		bench --site '$SITE_NAME' migrate
	" >/dev/null
}

phase_backup_site() {
	local pod
	pod="$(resolve_pod)"
	[ -n "$pod" ] || { echo "no pod"; return 1; }
	# Operator chooses an out-of-pod destination directory to copy the
	# archive to.  Kubeport pipes this onto the kubeport-backups PVC
	# automatically.
	bump_manual
	mkdir -p "$WORKDIR/backups/$SITE_NAME"
	k exec -n "$NAMESPACE" "$pod" -c "$GUNICORN_CONTAINER" -- bash -lc "
		set -euo pipefail
		cd /home/frappe/frappe-bench
		bench --site '$SITE_NAME' backup --with-files
	" >/dev/null
	# Operator inspects the bench backups dir to learn the new file
	# names.  Kubeport diff-tracks before/after via a wrapper script.
	bump_manual
	local files
	files="$(k exec -n "$NAMESPACE" "$pod" -c "$GUNICORN_CONTAINER" -- bash -lc "
		ls -1t /home/frappe/frappe-bench/sites/'$SITE_NAME'/private/backups/ | head -n 4
	")"
	while IFS= read -r f; do
		[ -z "$f" ] && continue
		k cp -n "$NAMESPACE" -c "$GUNICORN_CONTAINER" \
			"$pod:/home/frappe/frappe-bench/sites/$SITE_NAME/private/backups/$f" \
			"$WORKDIR/backups/$SITE_NAME/$f" >/dev/null
	done <<< "$files"
}

phase_restore_site() {
	local pod
	pod="$(resolve_pod)"
	[ -n "$pod" ] || { echo "no pod"; return 1; }
	# Operator copies the archive set back into the pod and figures out
	# the correct flags for restore.
	bump_manual
	local db public private
	db="$(ls "$WORKDIR/backups/$SITE_NAME/" | grep -E '\.sql\.gz$' | head -n 1)"
	public="$(ls "$WORKDIR/backups/$SITE_NAME/" | grep -E '\-public\-files\.tar$' | head -n 1)"
	private="$(ls "$WORKDIR/backups/$SITE_NAME/" | grep -E '\-private\-files\.tar$' | head -n 1)"
	if [ -z "$db" ]; then
		echo "no database file in $WORKDIR/backups/$SITE_NAME"
		return 1
	fi
	k exec -n "$NAMESPACE" "$pod" -c "$GUNICORN_CONTAINER" -- bash -lc "
		mkdir -p /tmp/baseline-restore
	" >/dev/null
	for f in "$db" "$public" "$private"; do
		[ -z "$f" ] && continue
		k cp -n "$NAMESPACE" -c "$GUNICORN_CONTAINER" \
			"$WORKDIR/backups/$SITE_NAME/$f" "$pod:/tmp/baseline-restore/$f" >/dev/null
	done
	local extra=""
	[ -n "$public" ] && extra="$extra --with-public-files /tmp/baseline-restore/$public"
	[ -n "$private" ] && extra="$extra --with-private-files /tmp/baseline-restore/$private"
	k exec -n "$NAMESPACE" "$pod" -c "$GUNICORN_CONTAINER" -- bash -lc "
		set -euo pipefail
		cd /home/frappe/frappe-bench
		bench --site '$SITE_NAME' restore /tmp/baseline-restore/'$db' --force \
			--mariadb-root-username='$DB_ROOT_USER' \
			--mariadb-root-password='$DB_ROOT_PASSWORD' \
			$extra
		rm -rf /tmp/baseline-restore
	" >/dev/null
}

phase_drop_site() {
	local pod
	pod="$(resolve_pod)"
	[ -n "$pod" ] || { echo "no pod"; return 1; }
	k exec -n "$NAMESPACE" "$pod" -c "$GUNICORN_CONTAINER" -- bash -lc "
		set -euo pipefail
		cd /home/frappe/frappe-bench
		bench drop-site '$SITE_NAME' \
			--root-login='$DB_ROOT_USER' \
			--root-password='$DB_ROOT_PASSWORD' \
			--no-backup --force
	" >/dev/null
}

# ---------- driver ----------

OVERALL_START="$(now_iso)"
OVERALL_T0="$(mono_s)"

run_phase setup_cluster_doc 60   phase_setup_cluster_doc
run_phase setup_helm_repo   240  phase_setup_helm_repo
run_phase verify_chart      30   phase_verify_chart
run_phase create_release    30   phase_create_release
run_phase deploy_release    900  phase_deploy_release
run_phase create_site       900  phase_create_site
run_phase migrate_site      600  phase_migrate_site
run_phase backup_site       600  phase_backup_site
run_phase restore_site      600  phase_restore_site
run_phase drop_site         600  phase_drop_site

OVERALL_FINISH="$(now_iso)"
OVERALL_DURATION="$(awk -v a="$OVERALL_T0" -v b="$(mono_s)" 'BEGIN{printf "%.3f", b-a}')"

# ---------- emit JSON ----------

echo "RESULT_BEGIN"
PHASES_JSONL="$PHASES_JSONL" \
OVERALL_START="$OVERALL_START" OVERALL_FINISH="$OVERALL_FINISH" \
OVERALL_DURATION="$OVERALL_DURATION" \
RELEASE_NAME="$RELEASE_NAME" NAMESPACE="$NAMESPACE" SITE_NAME="$SITE_NAME" \
CHART_NAME="$CHART_NAME" HELM_REPO_NAME="$HELM_REPO_NAME" \
HELM_REPO_URL="$HELM_REPO_URL" INSTALL_APPS="$INSTALL_APPS" \
KUBECONFIG_CONTEXT="$KUBECONFIG_CONTEXT" \
python3 - <<-'PY'
import json, os
phases = []
with open(os.environ["PHASES_JSONL"], encoding="utf-8") as fh:
    for line in fh:
        line = line.strip()
        if line:
            phases.append(json.loads(line))
report = {
    "schema_version": 1,
    "generated_at":     os.environ["OVERALL_FINISH"],
    "started_at":       os.environ["OVERALL_START"],
    "finished_at":      os.environ["OVERALL_FINISH"],
    "duration_seconds": float(os.environ["OVERALL_DURATION"]),
    "context": {
        "release_name":       os.environ["RELEASE_NAME"],
        "namespace":          os.environ["NAMESPACE"],
        "site_name":          os.environ["SITE_NAME"],
        "chart_name":         os.environ["CHART_NAME"],
        "helm_repo_name":     os.environ["HELM_REPO_NAME"],
        "helm_repo_url":      os.environ["HELM_REPO_URL"],
        "install_apps":       os.environ["INSTALL_APPS"],
        "kubeconfig_context": os.environ["KUBECONFIG_CONTEXT"],
    },
    "phases": phases,
    "summary": {
        "passed":  sum(1 for p in phases if p["status"] == "passed"),
        "failed":  sum(1 for p in phases if p["status"] == "failed"),
        "skipped": sum(1 for p in phases if p["status"] == "skipped"),
        "total":   len(phases),
        "commands_issued_total": sum(p["commands_issued"] for p in phases),
        "manual_steps_total":    sum(p["manual_steps"]    for p in phases),
    },
}
print(json.dumps(report, indent=2, sort_keys=True))
PY
echo "RESULT_END"

[ "$ABORT" = "1" ] && exit 1 || exit 0
