"""Idempotent outbox collector for one or more completed or active runs."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from sdlc_events import RuntimeEventLedger

from .storage import ExperienceStore


def collect_run(
    run_dir: Path,
    *,
    data_dir: Path | None = None,
    import_legacy: bool = True,
) -> dict[str, object]:
    legacy_report: Mapping[str, object] = {}
    if import_legacy:
        from .legacy import import_legacy_run

        legacy_report = import_legacy_run(run_dir)

    ledger = RuntimeEventLedger(run_dir)
    store = ExperienceStore(data_dir)
    pending = ledger.pending_outbox()
    inserted_ids: list[str] = []
    delivered_ids: list[str] = []
    duplicate_count = 0
    try:
        for event in pending:
            inserted = store.put_event(event)
            delivered_ids.append(event.event_id)
            if inserted:
                inserted_ids.append(event.event_id)
            else:
                duplicate_count += 1
    except Exception as exc:
        remaining = [event.event_id for event in pending if event.event_id not in delivered_ids]
        ledger.mark_delivery_error(remaining, str(exc))
        raise
    ledger.mark_delivered(delivered_ids)
    integrity = ledger.integrity_findings()
    return {
        "status": "pass" if not integrity else "integrity_failed",
        "run_id": ledger.run_id,
        "pending_before": len(pending),
        "inserted_count": len(inserted_ids),
        "duplicate_count": duplicate_count,
        "delivered_count": len(delivered_ids),
        "outbox": ledger.outbox_status(),
        "experience_event_count": store.event_count(),
        "integrity_findings": integrity,
        "legacy_import": dict(legacy_report),
    }
