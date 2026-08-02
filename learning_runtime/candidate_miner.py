"""LLM-assisted candidate mining with deterministic authority boundaries."""

from __future__ import annotations

import hashlib
from typing import Mapping, Protocol

from sdlc_events import canonical_json, stable_identifier

from local_sdlc.models import RunnerError

from .candidate_batches import ScopeBoundaryError, build_candidate_batches
from .candidate_contracts import serialization_output_contract
from .candidate_prompts import PROMPTS
from .candidate_protocol import (
    CandidateAbstraction,
    CandidateProtocolError,
    CandidateScope,
    CandidateSerialization,
    parse_candidate_json,
)
from .candidate_store import CandidateStore
from .domain_map import DomainMap
from .knowledge_schema import EvidenceAnchor, KnowledgeItem
from .storage import ExperienceStore


class CandidateLLM(Protocol):
    def complete(
        self,
        function_name: str,
        system_prompt: str,
        document: dict[str, object],
    ) -> str:
        """Run one isolated function call and return content only."""


def _response_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _episode_evidence(episodes: tuple[dict[str, object], ...]) -> tuple[EvidenceAnchor, ...]:
    return tuple(
        EvidenceAnchor(
            sha256=hashlib.sha256(canonical_json(episode).encode("utf-8")).hexdigest(),
            media_type="application/vnd.local-sdlc.episode+json",
            role="causal_episode",
            episode_id=str(episode["episode_id"]),
        )
        for episode in episodes
    )


def _assemble_candidate(
    serialized: CandidateSerialization,
    episodes: tuple[dict[str, object], ...],
) -> KnowledgeItem:
    evidence = _episode_evidence(episodes)
    projects = tuple(sorted({str(item["project_fingerprint"]) for item in episodes}))
    identity_payload = {
        "serialization": serialized.to_dict(),
        "evidence": [item.to_dict() for item in evidence],
        "projects": projects,
    }
    payload = {
        "knowledge_id": stable_identifier("K", canonical_json(identity_payload)),
        "version": 1,
        "kind": serialized.abstraction.kind,
        "scope": serialized.scope.scope,
        "applicability": serialized.scope.applicability.to_dict(),
        "antecedents": [dict(item) for item in serialized.abstraction.antecedents],
        "conclusion": dict(serialized.abstraction.conclusion),
        "effect": serialized.abstraction.effect,
        "evidence_refs": [item.to_dict() for item in evidence],
        "supporting_projects": list(projects),
        "counterexamples": list(serialized.abstraction.counterexamples),
        "generalization_rationale": serialized.abstraction.generalization_rationale,
        "regression_tests": list(serialized.abstraction.regression_tests),
        "authority": "llm_hypothesis",
        "confidence": serialized.abstraction.confidence,
        "state": "candidate",
        "supersedes": [],
        "created_by": "llm-assisted",
    }
    return KnowledgeItem.from_dict(payload)


def _call(
    llm: CandidateLLM,
    function_name: str,
    document: dict[str, object],
    response_hashes: list[str],
) -> dict[str, object]:
    raw = llm.complete(function_name, PROMPTS[function_name], document)
    response_hashes.append(_response_hash(raw))
    return parse_candidate_json(raw, function_name)


def mine_candidates(
    experience: ExperienceStore,
    candidates: CandidateStore,
    llm: CandidateLLM,
    *,
    domain_maps: Mapping[str, DomainMap] | None = None,
    max_batches: int = 10,
) -> dict[str, object]:
    if isinstance(max_batches, bool) or not 1 <= int(max_batches) <= 100:
        raise ValueError("max_batches must be between 1 and 100")
    batches = build_candidate_batches(experience.episodes(), domain_maps)[: int(max_batches)]
    accepted = 0
    duplicates = 0
    rejected = 0

    for batch in batches:
        hashes: list[str] = []
        reason_code = "candidate_unknown_failure"
        stage = "candidate_abstraction"
        try:
            abstraction_payload = _call(
                llm,
                "candidate_abstraction",
                batch.abstraction_document(),
                hashes,
            )
            abstraction = CandidateAbstraction.from_dict(abstraction_payload)
            if abstraction.source_episode_ids != batch.source_episode_ids:
                raise CandidateProtocolError("abstraction source episodes do not match the batch")

            stage = "scope_classification"
            scope_payload = _call(
                llm,
                "scope_classification",
                batch.scope_document(abstraction.to_dict()),
                hashes,
            )
            scope = CandidateScope.from_dict(scope_payload)
            batch.validate_scope(scope)

            stage = "candidate_serialization"
            abstraction_document = abstraction.to_dict()
            scope_document = scope.to_dict()
            serialization_payload = _call(
                llm,
                "candidate_serialization",
                {
                    "schema_version": 1,
                    "batch_id": batch.batch_id,
                    "abstraction": abstraction_document,
                    "scope_classification": scope_document,
                    "output_contract": serialization_output_contract(
                        abstraction_document,
                        scope_document,
                    ),
                },
                hashes,
            )
            serialized = CandidateSerialization.from_dict(serialization_payload)
            serialized.assert_matches(abstraction, scope)
            stage = "candidate_assembly"
            item = _assemble_candidate(serialized, batch.episodes)
            stage = "candidate_storage"
            inserted = candidates.put_candidate(item, batch.source_episode_ids, hashes)
            status = "accepted" if inserted else "duplicate"
            reason_code = "candidate_accepted" if inserted else "candidate_duplicate"
            accepted += int(inserted)
            duplicates += int(not inserted)
        except ScopeBoundaryError:
            status = "rejected"
            reason_code = "scope_not_allowed"
            rejected += 1
        except CandidateProtocolError:
            status = "rejected"
            reason_code = f"{stage}_invalid"
            rejected += 1
        except RunnerError:
            status = "rejected"
            reason_code = f"{stage}_call_failure"
            rejected += 1
        except Exception:
            candidates.record_attempt(
                batch_id=batch.batch_id,
                status="rejected",
                reason_code=f"{stage}_internal_failure",
                source_episode_ids=batch.source_episode_ids,
                response_hashes=hashes,
            )
            raise
        candidates.record_attempt(
            batch_id=batch.batch_id,
            status=status,
            reason_code=reason_code,
            source_episode_ids=batch.source_episode_ids,
            response_hashes=hashes,
        )

    return {
        "status": "pass" if rejected == 0 else "fail",
        "batch_count": len(batches),
        "accepted_count": accepted,
        "duplicate_count": duplicates,
        "rejected_count": rejected,
        "candidate_count": candidates.candidate_count(),
        "attempt_count": len(candidates.attempts()),
    }
