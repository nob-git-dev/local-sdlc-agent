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
from .storage import ExperienceStore, learning_data_dir
from .validation import validate_and_store, validate_candidate
from .validation_models import ValidationCase, ValidationPolicy

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
    "TechnologyObservation",
    "ValidationCase",
    "ValidationPolicy",
    "audit_run",
    "build_and_store_recovery_episodes",
    "build_recovery_episode_documents",
    "collect_run",
    "evaluate_applicability",
    "import_legacy_run",
    "learning_data_dir",
    "validate_domain_map",
    "validate_and_store",
    "validate_candidate",
]
