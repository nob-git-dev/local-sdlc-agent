"""Event contract completeness audit for a run."""

from __future__ import annotations

import json
from pathlib import Path

from sdlc_events import RuntimeEventLedger


AUDIT_REPORT_FILENAME = "event-contract-audit.json"


def audit_run(
    run_dir: Path,
    *,
    import_legacy: bool = True,
    persist_violation: bool = True,
) -> dict[str, object]:
    legacy_report: dict[str, object] = {}
    if import_legacy:
        from .legacy import import_legacy_run

        legacy_report = import_legacy_run(run_dir)
    ledger = RuntimeEventLedger(run_dir)
    findings = list(ledger.integrity_findings())
    legacy_findings = legacy_report.get("findings")
    if isinstance(legacy_findings, list):
        findings.extend(item for item in legacy_findings if isinstance(item, dict))
    report: dict[str, object] = {
        "schema_version": 1,
        "status": "pass" if not findings else "fail",
        "run_id": ledger.run_id,
        "transition_count": ledger.transition_count(),
        "event_count": ledger.event_count(),
        "outbox": ledger.outbox_status(),
        "findings": findings,
        "legacy_import": legacy_report,
    }
    if findings and persist_violation:
        violation = ledger.record_contract_violation(findings)
        report["violation_event_id"] = violation.event_id
    report["audit_id"] = ledger.record_audit(report)
    path = run_dir / AUDIT_REPORT_FILENAME
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report["report_path"] = str(path)
    return report
