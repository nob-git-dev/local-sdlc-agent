"""Mechanical replay, metamorphic, negative, and holdout validation."""

from __future__ import annotations

import hashlib
import math
from typing import Sequence

from sdlc_events import canonical_json, stable_identifier

from .applicability import evaluate_applicability
from .evaluation_store import EvaluationStore
from .knowledge_schema import KnowledgeItem
from .metamorphic import renamed_domain_map
from .storage import ExperienceStore
from .validation_models import ValidationCase, ValidationPolicy
from .work_control import LearningLimits, LearningWorkControl, LearningWorkStopped


def _wilson_lower_bound(successes: int, total: int, z: float) -> float:
    if total <= 0:
        return 0.0
    proportion = successes / total
    denominator = 1.0 + (z * z / total)
    center = proportion + (z * z / (2.0 * total))
    margin = z * math.sqrt(
        (proportion * (1.0 - proportion) / total)
        + (z * z / (4.0 * total * total))
    )
    return max(0.0, (center - margin) / denominator)


def _metamorphic_cases(
    cases: Sequence[ValidationCase],
    control: LearningWorkControl | None = None,
) -> tuple[ValidationCase, ...]:
    generated: list[ValidationCase] = []
    for case in cases:
        if control is not None:
            control.checkpoint("before_metamorphic_transform")
        if case.suite not in {"replay", "holdout"} or not case.expected_applies:
            continue
        case_id = stable_identifier("VCM", case.case_id, "rename")
        generated.append(
            ValidationCase(
                case_id=case_id,
                suite="metamorphic",
                domain_map=renamed_domain_map(case.domain_map),
                expected_applies=True,
                verified=case.verified,
                critical=case.critical,
                authored_from_candidate=case.authored_from_candidate,
                episode_id=case.episode_id,
                evidence_refs=case.evidence_refs,
            )
        )
    return tuple(generated)


def _case_result(item: KnowledgeItem, case: ValidationCase) -> dict[str, object]:
    decision = evaluate_applicability(
        item,
        case.domain_map,
        episode_id=case.episode_id,
    )
    passed = case.verified and decision.applies == case.expected_applies
    return {
        **case.to_evaluation_dict(),
        "actual_applies": decision.applies,
        "passed": passed,
        "matched_predicates": list(decision.matched_predicates),
        "failed_predicates": list(decision.failed_predicates),
    }


def _evidence_quality(item: KnowledgeItem, experience: ExperienceStore) -> float:
    source_ids = {
        anchor.episode_id for anchor in item.evidence_refs if anchor.episode_id
    }
    if not source_ids:
        return 0.0
    episodes = {str(episode.get("episode_id")): episode for episode in experience.episodes()}
    eligible = sum(
        1
        for episode_id in source_ids
        if episodes.get(episode_id, {}).get("eligibility") == "eligible"
    )
    return eligible / len(source_ids)


def validate_candidate(
    item: KnowledgeItem,
    experience: ExperienceStore,
    cases: Sequence[ValidationCase],
    *,
    policy: ValidationPolicy | None = None,
    control: LearningWorkControl | None = None,
) -> dict[str, object]:
    if item.state != "candidate":
        raise ValueError("validation accepts candidate state only")
    active_policy = policy or ValidationPolicy()
    supplied = tuple(cases)
    if not supplied:
        raise ValueError("validation requires at least one case")
    case_ids = [case.case_id for case in supplied]
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("validation case IDs must be unique")
    all_cases = supplied + _metamorphic_cases(supplied, control)
    result_list: list[dict[str, object]] = []
    for case in all_cases:
        if control is not None:
            control.checkpoint("before_validation_case", cases=1)
        result_list.append(_case_result(item, case))
    results = tuple(sorted(result_list, key=lambda result: str(result["case_id"])))
    reasons: set[str] = set()
    source_episode_ids = {
        anchor.episode_id for anchor in item.evidence_refs if anchor.episode_id
    }

    valid_replays = [
        case
        for case in supplied
        if case.suite == "replay"
        and case.expected_applies
        and case.verified
        and case.episode_id in source_episode_ids
    ]
    if not valid_replays:
        reasons.add("source_replay_required")
    if not any(case.suite == "negative" and not case.expected_applies for case in supplied):
        reasons.add("negative_case_required")
    if not any(case.suite == "metamorphic" for case in all_cases):
        reasons.add("metamorphic_case_required")

    if item.scope in {"structural", "technology"}:
        holdout = [
            case
            for case in supplied
            if case.suite == "holdout"
            and case.expected_applies
            and case.verified
            and not case.authored_from_candidate
            and case.domain_map.project_fingerprint not in item.supporting_projects
        ]
        if not holdout:
            reasons.add("positive_holdout_required")

    failed = [result for result in results if not result["passed"]]
    critical_regressions = sum(
        1 for result in failed if bool(result["critical"])
    )
    if failed:
        reasons.add("case_expectation_failed")
    if critical_regressions:
        reasons.add("critical_regression")

    passed_counterexamples = sum(
        1
        for result in results
        if result["suite"] == "counterexample"
        and not result["expected_applies"]
        and result["passed"]
    )
    unresolved_counterexamples = max(
        0,
        len(item.counterexamples) - passed_counterexamples,
    )
    if unresolved_counterexamples:
        reasons.add("counterexamples_unresolved")

    evidence_quality = _evidence_quality(item, experience)
    if evidence_quality < active_policy.minimum_evidence_quality:
        reasons.add("evidence_quality_below_threshold")
    independent_support = len(set(item.supporting_projects))
    if independent_support < active_policy.minimum_support(item.scope):
        reasons.add("independent_support_below_threshold")

    verified_results = [result for result in results if result["verified"]]
    precision_lower_bound = _wilson_lower_bound(
        sum(1 for result in verified_results if result["passed"]),
        len(verified_results),
        active_policy.confidence_z,
    )
    if precision_lower_bound < active_policy.minimum_precision_lower_bound:
        reasons.add("precision_below_threshold")

    rejection_reasons = {
        "case_expectation_failed",
        "critical_regression",
        "precision_below_threshold",
    }
    if reasons.intersection(rejection_reasons):
        verdict = "rejected"
    elif reasons:
        verdict = "incomplete"
    else:
        verdict = "shadow_pass"

    return {
        "knowledge_id": item.knowledge_id,
        "knowledge_version": item.version,
        "candidate_hash": hashlib.sha256(
            canonical_json(item.to_dict()).encode("utf-8")
        ).hexdigest(),
        "verdict": verdict,
        "suite_coverage": sorted({str(result["suite"]) for result in results}),
        "case_count": len(results),
        "passed_case_count": sum(1 for result in results if result["passed"]),
        "critical_regressions": critical_regressions,
        "unresolved_counterexamples": unresolved_counterexamples,
        "evidence_quality": evidence_quality,
        "independent_support": independent_support,
        "precision_lower_bound": precision_lower_bound,
        "reason_codes": sorted(reasons),
        "policy": active_policy.to_dict(),
        "case_results": list(results),
    }


def validate_and_store(
    item: KnowledgeItem,
    experience: ExperienceStore,
    evaluations: EvaluationStore,
    cases: Sequence[ValidationCase],
    *,
    policy: ValidationPolicy | None = None,
    control: LearningWorkControl | None = None,
    limits: LearningLimits | None = None,
) -> dict[str, object]:
    active_control = control or LearningWorkControl(
        experience.data_dir,
        "candidate_validation",
        limits=limits,
    )
    try:
        core = validate_candidate(
            item,
            experience,
            cases,
            policy=policy,
            control=active_control,
        )
        active_control.checkpoint("before_evaluation_storage")
        report = evaluations.put_report(core)
        active_control.complete()
        return report
    except LearningWorkStopped:
        raise
    except Exception:
        active_control.stop("internal_failure")
        raise
