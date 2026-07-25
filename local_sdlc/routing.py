"""Supervisor routing and approval rules."""

from __future__ import annotations

import re
import textwrap

from .models import RunnerError, SDLC_PHASES, SUPERVISOR_STEPS, SupervisorRoute
from .utils import unique_ordered


def parse_supervisor_steps(raw: str) -> list[str]:
    if raw.strip().lower() == "all":
        return ["pm", "coder", "judge"]

    steps = [part.strip().lower() for part in raw.split(",") if part.strip()]
    if not steps:
        raise RunnerError("at least one supervisor step is required")

    unknown = [step for step in steps if step not in SUPERVISOR_STEPS]
    if unknown:
        raise RunnerError(f"unknown supervisor step(s): {', '.join(unknown)}")
    return steps

def has_section(markdown: str, title: str) -> bool:
    pattern = rf"^##\s+{re.escape(title)}(?:\s|$)"
    return re.search(pattern, markdown, flags=re.MULTILINE) is not None

def detect_danger_signals(brief: str) -> list[str]:
    text = brief.lower()
    checks = [
        ("irreversible", [r"削除", r"消す", r"\brm\s+-rf\b", r"\bdrop\b", r"\btruncate\b"]),
        ("production", [r"本番", r"\bproduction\b", r"\bprod\b", r"ライブ"]),
        ("database", [r"\bdb\b", r"database", r"データベース", r"マイグレーション", r"migration", r"schema", r"スキーマ", r"カラム"]),
        ("security", [r"認証", r"パスワード", r"password", r"token", r"secret", r"権限", r"permission", r"セキュリティ"]),
        ("ambiguous", [r"とりあえず", r"ざっくり", r"適当", r"おまかせ"]),
    ]
    signals: list[str] = []
    for name, patterns in checks:
        if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns):
            signals.append(name)
    return signals

def classify_task_type(brief: str) -> str:
    text = brief.lower()
    if re.search(r"画面|ui|ux|デザイン|html|css|コンポーネント|フロント", text):
        return "ui_ux"
    if re.search(r"直して|修正|動かない|エラー|bug|バグ|失敗|壊れ", text):
        return "bugfix"
    if re.search(r"リファクタ|整理|構造改善|きれいに|負債", text):
        return "refactor"
    if re.search(r"デプロイ|deploy|docker|compose|ポート|環境|設定|移行|サーバ", text):
        return "infrastructure"
    if re.search(r"落ちた|止まった|障害|alert|アラート|復旧", text):
        return "incident"
    if re.search(r"追加|実装|作って|生成|開発|新規|機能", text):
        return "new_feature"
    if re.search(r"どう|なぜ|何|教えて|状況|確認", text):
        return "question"
    return "development"

DOMAIN_MODELING_RE = re.compile(
    "|".join(
        [
            r"仕様",
            r"受け入れ",
            r"検証",
            r"命題",
            r"ドメイン",
            r"不変条件",
            r"状態",
            r"状態遷移",
            r"操作",
            r"振る舞い",
            r"ルール",
            r"ゲーム",
            r"テトリス",
            r"ブラウザ",
            r"インタラクティブ",
            r"\binteractive\b",
            r"\bworkflow\b",
            r"\bstate\b",
            r"\bbehavior\b",
            r"\brule\b",
            r"\binvariant\b",
            r"\bacceptance\b",
            r"\bverification\b",
            r"\bproposition\b",
            r"\bparser\b",
            r"\blexer\b",
            r"\bprotocol\b",
            r"\bengine\b",
            r"\bsql\b",
            r"\bsqlite\b",
            r"\bredis\b",
            r"\bdatabase\b",
            r"\bdb\b",
            r"\bschema\b",
        ]
    ),
    flags=re.IGNORECASE,
)

def needs_domain_modeling(
    brief: str,
    spec: str,
    task_type: str,
    danger_signals: list[str],
) -> bool:
    text = f"{brief}\n{spec}"
    if task_type in {"new_feature", "development"}:
        return True
    if task_type == "ui_ux":
        return bool(DOMAIN_MODELING_RE.search(text))
    if task_type in {"bugfix", "refactor"}:
        return bool(DOMAIN_MODELING_RE.search(text))
    if "database" in danger_signals and task_type not in {"question", "incident"}:
        return True
    return False

def insert_after_spec(phases: list[str], phase: str) -> None:
    if phase in phases or not phases:
        return
    insert_at = 1 if phases[0] == "spec" else 0
    phases.insert(insert_at, phase)

def recommended_sdlc_phases(brief: str, spec: str) -> SupervisorRoute:
    task_type = classify_task_type(brief)
    danger_signals = detect_danger_signals(brief)
    spec_missing = not bool(spec.strip())
    domain_modeling = needs_domain_modeling(brief, spec, task_type, danger_signals)
    phases: list[str]

    if task_type == "question":
        phases = []
    elif task_type == "ui_ux":
        phases = ["ui", "review"]
        if re.search(r"ゲーム|操作|動作|interactive|canvas|html", brief, flags=re.IGNORECASE):
            phases.insert(0, "tdd")
    elif task_type == "bugfix":
        phases = ["tdd", "review"]
    elif task_type == "refactor":
        phases = ["tdd", "refactor", "review"]
    elif task_type == "infrastructure":
        phases = ["architect", "security", "deploy", "observe"]
    elif task_type == "incident":
        phases = ["sre", "observe", "review"]
    else:
        phases = ["architect", "tdd", "review"]

    if spec_missing and phases:
        phases.insert(0, "spec")

    if "database" in danger_signals and "architect" not in phases and phases:
        phases.insert(0 if not spec_missing else 1, "architect")
    if any(signal in danger_signals for signal in ("production", "database", "security", "irreversible")) and phases:
        if "security" not in phases:
            insert_at = max(1, len(phases) - 1) if "review" in phases else len(phases)
            phases.insert(insert_at, "security")
    if "production" in danger_signals and "deploy" not in phases and phases:
        phases.append("deploy")
    if "ambiguous" in danger_signals and "spec" not in phases:
        phases.insert(0, "spec")
    if domain_modeling:
        insert_after_spec(phases, "ddd")

    phases = [phase for phase in unique_ordered(phases) if phase in SDLC_PHASES]
    reason = (
        f"task_type={task_type}; "
        f"danger_signals={', '.join(danger_signals) or 'none'}; "
        f"spec={'missing' if spec_missing else 'present'}; "
        f"domain_modeling={'yes' if domain_modeling else 'no'}"
    )
    return SupervisorRoute(
        task_type=task_type,
        danger_signals=tuple(danger_signals),
        phases=tuple(phases),
        reason=reason,
    )

def route_document(route: SupervisorRoute) -> str:
    dangers = ", ".join(route.danger_signals) if route.danger_signals else "none"
    phases = " -> ".join(route.phases) if route.phases else "(no skill phase; direct answer or clarification)"
    return textwrap.dedent(
        f"""
        ## Supervisor Route

        - task_type: {route.task_type}
        - danger_signals: {dangers}
        - recommended_phases: {phases}
        - reason: {route.reason}

        ## Gate Policy

        - Each phase is executed as an independent API call.
        - Each phase receives its own SKILL.md in the system prompt.
        - Handoff between phases is document-based only: SPEC.md, prior phase documents, project manifest, and supplied file contents.
        - DDD is used when the task needs shared domain language, invariants, state rules, or verification propositions.
        - Coder-level implementation is not accepted without review or executable evidence.
        - Production, database, irreversible, and security-sensitive signals force security/deploy-related gates.
        """
    ).strip()

def phase_instruction(phase: str, brief: str, route: SupervisorRoute) -> str:
    phase_contract = ""
    if phase == "ddd":
        phase_contract = """
        DDD phase contract:
        - Do not implement product code.
        - Define the ubiquitous language that later phases must use.
        - Define bounded contexts only when they clarify ownership or model boundaries.
        - Define domain invariants as short propositions.
        - Build a Verification Proposition Contract table with columns:
          R_id | Domain term | Truth condition | Check_id | relation | observation | fail_owner
        - relation must be one of: equivalent, sufficient, necessary, proxy.
        - observation must say whether the check is runtime, command, static, structural, or heuristic.
        - fail_owner must be product, spec, harness, supervisor, or unknown.
        - If a static/proxy check can disagree with a runtime/equivalent check, state the precedence rule.
        - End with "Handoff Requirements" for architect, TDD, coder, and judge.
        """

    return textwrap.dedent(
        f"""
        Run the /{phase} phase for this request, following the bundled SDLC contract.

        Request:
        {brief}

        Supervisor classification:
        - task_type: {route.task_type}
        - danger_signals: {', '.join(route.danger_signals) or 'none'}
        - full phase plan: {' -> '.join(route.phases)}

        Requirements:
        - Treat SPEC.md as the single source of truth.
        - Read and preserve fixed requirements.
        - Do not rely on hidden context from any previous API call.
        - Return the section or document this phase should contribute.
        - If required context is missing, say exactly what is missing instead of guessing.

        {phase_contract}
        """
    ).strip()

def judge_approved(text: str) -> bool:
    normalized = text.lower()
    lines = [line.strip() for line in text.splitlines()]

    def classify_verdict_line(line: str) -> bool | None:
        lowered = re.sub(r"^[#>\-\*\s]+", "", line).lower()
        if any(marker in lowered for marker in ["修正依頼", "要修正", "不承認", "rejected", "needs changes", "fail", "failed"]):
            return False
        if any(marker in lowered for marker in ["承認", "approved", "pass", "passed"]):
            return True
        return None

    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line:
            continue
        verdict_line = re.sub(r"^[#>\-\*\s]+", "", line).lower()
        if "判定" in verdict_line or "verdict" in verdict_line:
            direct = classify_verdict_line(line)
            if direct is not None:
                return direct
            for following in lines[index + 1 : index + 4]:
                if not following:
                    continue
                indirect = classify_verdict_line(following)
                if indirect is not None:
                    return indirect
            break

    rejection_markers = [
        "修正依頼",
        "要修正",
        "不承認",
        "rejected",
        "needs changes",
        "must fix",
        "fail",
        "failed",
    ]
    if any(marker in normalized for marker in rejection_markers):
        return False

    approval_markers = [
        "判定: 承認",
        "判定：承認",
        "承認",
        "verdict: approved",
        "approved",
        "pass",
        "passed",
    ]
    return any(marker in normalized for marker in approval_markers)
