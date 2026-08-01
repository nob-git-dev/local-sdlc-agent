"""Public compatibility facade for stalled-run recovery."""

from .recovery_analysis import failure_family_plateau
from .recovery_core import (
    ANALYTIC_RECOVERY_STRATEGIES,
    DEFAULT_FAILURE_FAMILY_THRESHOLD,
    ORDINARY_RECOVERY_STRATEGIES,
    RECOVERY_ORIGIN_FILENAME,
    RECOVERY_PLAN_FILENAME,
    RECOVERY_SCHEMA_VERSION,
    RECOVERY_STATE_FILENAME,
    VALID_RECOVERY_STRATEGIES,
    InvalidRecoveryPlan,
    RecoveryPlanRequired,
    recovery_origin_file_path,
    recovery_plan_file_path,
    recovery_state_file_path,
    recovery_timestamp,
)
from .recovery_plan import (
    plan_stalled_recovery,
    read_recovery_plan,
    recovery_authorization,
    recovery_authorization_is_valid,
    require_recovery_plan_for_resume,
    validate_recovery_plan,
)
from .recovery_runtime import begin_stalled_recovery, complete_stalled_recovery


__all__ = [
    "ANALYTIC_RECOVERY_STRATEGIES",
    "DEFAULT_FAILURE_FAMILY_THRESHOLD",
    "ORDINARY_RECOVERY_STRATEGIES",
    "RECOVERY_ORIGIN_FILENAME",
    "RECOVERY_PLAN_FILENAME",
    "RECOVERY_SCHEMA_VERSION",
    "RECOVERY_STATE_FILENAME",
    "VALID_RECOVERY_STRATEGIES",
    "InvalidRecoveryPlan",
    "RecoveryPlanRequired",
    "begin_stalled_recovery",
    "complete_stalled_recovery",
    "failure_family_plateau",
    "plan_stalled_recovery",
    "read_recovery_plan",
    "recovery_authorization",
    "recovery_authorization_is_valid",
    "recovery_origin_file_path",
    "recovery_plan_file_path",
    "recovery_state_file_path",
    "recovery_timestamp",
    "require_recovery_plan_for_resume",
    "validate_recovery_plan",
]
