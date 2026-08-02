"""Independent experience collection and event-audit control plane."""

from .audit import audit_run
from .applicability import ApplicabilityDecision, evaluate_applicability
from .collector import collect_run
from .domain_map import (
    ComponentObservation,
    DomainMap,
    DomainRelation,
    TechnologyObservation,
    validate_domain_map,
)
from .episodes import (
    build_and_store_recovery_episodes,
    build_recovery_episode_documents,
)
from .knowledge_schema import (
    Applicability,
    ApplicabilityPredicate,
    EvidenceAnchor,
    KnowledgeItem,
    KnowledgeValidationError,
)
from .legacy import import_legacy_run
from .operations import doctor_report, explain_knowledge, inspect_knowledge, snapshot_view
from .promotion import PromotionService
from .promotion_policy import is_high_impact
from .registry_store import RegistryStore
from .snapshots import SnapshotStore
from .storage import ExperienceStore, learning_data_dir
from .validation import validate_and_store, validate_candidate
from .validation_models import ValidationCase, ValidationPolicy
from .work_control import (
    LearningLimits,
    LearningWorkControl,
    LearningWorkStopped,
    request_learning_cancel,
)
from .work_control_store import learning_work_status

__all__ = [
    "Applicability",
    "ApplicabilityDecision",
    "ApplicabilityPredicate",
    "ComponentObservation",
    "DomainMap",
    "DomainRelation",
    "EvidenceAnchor",
    "ExperienceStore",
    "KnowledgeItem",
    "KnowledgeValidationError",
    "LearningLimits",
    "LearningWorkControl",
    "LearningWorkStopped",
    "PromotionService",
    "RegistryStore",
    "SnapshotStore",
    "TechnologyObservation",
    "ValidationCase",
    "ValidationPolicy",
    "audit_run",
    "build_and_store_recovery_episodes",
    "build_recovery_episode_documents",
    "collect_run",
    "doctor_report",
    "evaluate_applicability",
    "explain_knowledge",
    "import_legacy_run",
    "inspect_knowledge",
    "is_high_impact",
    "learning_data_dir",
    "learning_work_status",
    "request_learning_cancel",
    "snapshot_view",
    "validate_domain_map",
    "validate_and_store",
    "validate_candidate",
]
