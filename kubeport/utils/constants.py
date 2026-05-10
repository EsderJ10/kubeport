"""Shared constants used across reconciliation, dashboard, and tasks.

Defining cross-cutting numeric thresholds in one place avoids the silent
drift that occurs when the same value is duplicated in two modules and
one is later tuned without the other.
"""

# Operations whose ``operation_token`` is set but whose ``operation_started_at``
# (or ``modified`` for DocTypes without that field) is older than this threshold
# are considered stale: a worker likely crashed mid-flight, and reconciliation's
# stale-recovery branch will re-check live cluster state on its next tick.
#
# Imported by ``kubeport.tasks.reconciliation`` (drives stale-recovery logic)
# and ``kubeport.api.dashboard`` (drives the operator-facing Stale Operations
# Number Card).  Both must use the same value or the dashboard will lie about
# what reconciliation considers stale.
STALE_OPERATION_THRESHOLD_MINUTES = 30
