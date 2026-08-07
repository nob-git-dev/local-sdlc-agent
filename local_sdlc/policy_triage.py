"""Deterministic interpretation of project-policy triage records."""

from __future__ import annotations

import ast
import itertools
import json
import re
from pathlib import Path
from typing import Sequence

from .models import RepairAdvice
from .utils import truncate_text, unique_ordered
from .workspace import normalize_new_files, resolve_project_path


PROJECT_POLICY_TRIAGE_TRIGGERS = frozenset(
    {
        "test_harness_ownership",
        "test_edit_attempt",
        "artifact_policy_boundary",
        "generated_test_oracle_conflict",
    }
)


JUDGE_OWNERSHIP_VALUES = frozenset(
    {"test_harness", "product_bug", "spec_conflict", "insufficient_context", "not_applicable"}
)


def patch_plan_requests_generated_test_oracle_triage(document: str) -> bool:
    """Accept only an explicit, unambiguous planner escalation pair."""
    patch_types = re.findall(
        r"(?mi)^\s*-\s*patch_type\s*:\s*([a-z_]+)\s*$",
        document,
    )
    escalations = re.findall(
        r"(?mi)^\s*-\s*escalation\s*:\s*([a-z_]+)\s*$",
        document,
    )
    return patch_types == ["missing_context"] and escalations == [
        "generated_test_oracle_triage"
    ]


def judge_ownership_classification(document: str) -> str:
    """Extract the judge's explicit ownership vote without interpreting prose."""
    match = re.search(
        r"(?mi)^\s*(?:[-*]\s*)?OWNERSHIP\s*:\s*([a-z_]+)\s*$",
        document,
    )
    if not match:
        return "not_applicable"
    value = match.group(1).lower()
    return value if value in JUDGE_OWNERSHIP_VALUES else "not_applicable"


def generated_test_receiver_identity_facts(source: str, path: str) -> list[str]:
    """Extract only mechanically certain fresh-receiver facts from Python tests."""

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    facts: list[str] = []
    for function in (
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test")
    ):
        fresh_calls: dict[tuple[str, str], list[ast.Call]] = {}
        for node in ast.walk(function):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            receiver = node.func.value
            if not isinstance(receiver, ast.Call):
                continue
            constructor = receiver.func
            if isinstance(constructor, ast.Name):
                constructor_name = constructor.id
            elif isinstance(constructor, ast.Attribute):
                constructor_name = constructor.attr
            else:
                continue
            if not constructor_name[:1].isupper():
                continue
            fresh_calls.setdefault((constructor_name, node.func.attr), []).append(receiver)
        for (constructor_name, method_name), calls in sorted(fresh_calls.items()):
            if len(calls) < 2:
                continue
            lines = [call.lineno for call in calls]
            argument_sets = []
            for call in calls:
                expressions = {ast.unparse(argument) for argument in call.args}
                expressions.update(
                    ast.unparse(keyword.value)
                    for keyword in call.keywords
                    if keyword.arg is not None
                )
                argument_sets.append(expressions)
            shared_arguments = sorted(set.intersection(*argument_sets))
            facts.append(
                f"{path}:{function.name} calls {constructor_name}(...).{method_name}(...) "
                f"on {len(lines)} distinct fresh constructor expressions at lines "
                f"{', '.join(str(line) for line in lines)}; these receivers are not the same instance; "
                "shared constructor arguments JSON: "
                f"{json.dumps(shared_arguments, ensure_ascii=True)}."
            )
    return facts


def generated_test_receiver_lineage_facts(source: str, path: str) -> list[str]:
    """Extract method calls made on aliases of a prior method return value."""

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    facts: list[str] = []
    for function in (
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test")
    ):
        lineage: dict[str, str] = {}
        processed_calls: set[int] = set()

        def call_lineage(call: ast.Call) -> str | None:
            if not isinstance(call.func, ast.Attribute):
                return None
            receiver = call.func.value
            if isinstance(receiver, ast.Call):
                constructor = receiver.func
                if isinstance(constructor, ast.Name) and constructor.id[:1].isupper():
                    return f"syntactic return value of {constructor.id}.{call.func.attr}()"
            if isinstance(receiver, ast.Name) and receiver.id in lineage:
                origin = lineage[receiver.id]
                if origin.startswith("syntactic return value"):
                    facts.append(
                        f"{path}:{function.name} calls {receiver.id}.{call.func.attr}() at line "
                        f"{call.lineno} on an alias of the {origin}; source syntax does not "
                        "establish that this value is the original constructor receiver."
                    )
                return f"syntactic return value of {origin}.{call.func.attr}()"
            return None

        nodes = sorted(
            ast.walk(function),
            key=lambda node: (
                getattr(node, "lineno", 0),
                0 if isinstance(node, ast.Assign) else 1,
                getattr(node, "col_offset", 0),
            ),
        )
        for node in nodes:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if not isinstance(target, ast.Name):
                    continue
                if isinstance(node.value, ast.Name) and node.value.id in lineage:
                    lineage[target.id] = lineage[node.value.id]
                elif isinstance(node.value, ast.Call):
                    derived = call_lineage(node.value)
                    processed_calls.add(id(node.value))
                    if derived:
                        lineage[target.id] = derived
            elif isinstance(node, ast.Call) and id(node) not in processed_calls:
                call_lineage(node)
    return list(dict.fromkeys(facts))


def _literal_loop_values(node: ast.AST) -> list[object]:
    if not isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return []
    values: list[object] = []
    for element in node.elts:
        try:
            values.append(ast.literal_eval(element))
        except (ValueError, TypeError):
            return []
    return values


def _concrete_call_signatures(
    node: ast.Call,
    bindings: dict[str, list[object]],
) -> list[str]:
    """Expand a call only when every argument has a concrete literal value."""

    try:
        callable_name = ast.unparse(node.func)
    except (ValueError, TypeError):
        return []
    argument_options: list[list[str]] = []
    for argument in node.args:
        if isinstance(argument, ast.Name) and argument.id in bindings:
            argument_options.append([repr(value) for value in bindings[argument.id]])
            continue
        try:
            argument_options.append([repr(ast.literal_eval(argument))])
        except (ValueError, TypeError):
            return []
    keyword_options: list[tuple[str, list[str]]] = []
    for keyword in node.keywords:
        if keyword.arg is None:
            return []
        value = keyword.value
        if isinstance(value, ast.Name) and value.id in bindings:
            keyword_options.append((keyword.arg, [repr(item) for item in bindings[value.id]]))
            continue
        try:
            keyword_options.append((keyword.arg, [repr(ast.literal_eval(value))]))
        except (ValueError, TypeError):
            return []
    options = argument_options + [values for _name, values in keyword_options]
    combinations = itertools.product(*options) if options else [()]
    signatures: list[str] = []
    for combination in combinations:
        positional_count = len(argument_options)
        rendered = list(combination[:positional_count])
        rendered.extend(
            f"{name}={value}"
            for (name, _values), value in zip(
                keyword_options,
                combination[positional_count:],
            )
        )
        signatures.append(f"{callable_name}({', '.join(rendered)})")
    return signatures


def python_exception_call_obligations(
    source: str,
    path: str,
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Return concrete calls required to raise and required not to raise.

    A call under ``assertRaises``/``pytest.raises`` is a positive exception
    obligation. A call elsewhere in a test must return normally for the test to
    reach its later assertions, so it is a negative exception obligation.
    Unknown argument expressions are deliberately ignored.
    """

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}, {}

    guarded: dict[str, list[str]] = {}
    unguarded: dict[str, list[str]] = {}

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.exception_depth = 0
            self.exception_roots: set[int] = set()
            self.bindings: dict[str, list[object]] = {}
            self.test_depth = 0

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            if not node.name.startswith("test"):
                return
            previous = self.test_depth
            self.test_depth += 1
            for statement in node.body:
                self.visit(statement)
            self.test_depth = previous

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_For(self, node: ast.For) -> None:
            previous = dict(self.bindings)
            if isinstance(node.target, ast.Name):
                values = _literal_loop_values(node.iter)
                if values:
                    self.bindings[node.target.id] = values
            for statement in node.body:
                self.visit(statement)
            self.bindings = previous
            for statement in node.orelse:
                self.visit(statement)

        def visit_With(self, node: ast.With) -> None:
            is_exception_scope = any(
                isinstance(item.context_expr, ast.Call)
                and (
                    isinstance(item.context_expr.func, ast.Attribute)
                    and item.context_expr.func.attr in {"assertRaises", "raises"}
                    or isinstance(item.context_expr.func, ast.Name)
                    and item.context_expr.func.id == "raises"
                )
                for item in node.items
            )
            for item in node.items:
                if not is_exception_scope:
                    self.visit(item.context_expr)
            if is_exception_scope:
                self.exception_depth += 1
                self.exception_roots.update(
                    id(statement.value)
                    for statement in node.body
                    if isinstance(statement, ast.Expr)
                    and isinstance(statement.value, ast.Call)
                )
            for statement in node.body:
                self.visit(statement)
            if is_exception_scope:
                self.exception_depth -= 1

        visit_AsyncWith = visit_With

        def visit_Call(self, node: ast.Call) -> None:
            if self.test_depth:
                if not self.exception_depth or id(node) in self.exception_roots:
                    target = guarded if self.exception_depth else unguarded
                    for signature in _concrete_call_signatures(node, self.bindings):
                        target.setdefault(signature, []).append(f"{path}:{node.lineno}")
            self.generic_visit(node)

    Visitor().visit(tree)
    return guarded, unguarded


def generated_fixed_exception_conflict_facts(
    project: Path,
    generated_test_paths: Sequence[str],
    fixed_test_paths: Sequence[str],
) -> list[str]:
    """Prove R(c) and not-R(c) conflicts across generated and fixed tests."""

    generated_guarded: dict[str, list[str]] = {}
    fixed_unguarded: dict[str, list[str]] = {}
    for path, target, guarded_side in [
        *((path, generated_guarded, True) for path in generated_test_paths),
        *((path, fixed_unguarded, False) for path in fixed_test_paths),
    ]:
        try:
            source = resolve_project_path(project, path).read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            continue
        guarded, unguarded = python_exception_call_obligations(source, path)
        selected = guarded if guarded_side else unguarded
        for signature, locations in selected.items():
            target.setdefault(signature, []).extend(locations)
    facts: list[str] = []
    for signature in sorted(set(generated_guarded) & set(fixed_unguarded)):
        facts.append(
            "MECHANICAL_ORACLE_CONFLICT: generated test requires "
            f"{signature} to raise at {', '.join(generated_guarded[signature])}; "
            "fixed acceptance executes the same concrete call outside an exception "
            f"scope at {', '.join(fixed_unguarded[signature])}, requiring it not to raise."
        )
    return facts


def _spec_api_parameters_and_regexes(
    spec: str,
) -> tuple[dict[str, list[str]], dict[str, str]]:
    signatures: dict[str, list[str]] = {}
    for line in spec.splitlines():
        match = re.fullmatch(r"\s*([A-Za-z_]\w*)\((.*)\)(?:\s*->.*)?\s*", line)
        if not match:
            continue
        parameters: list[str] = []
        for raw in match.group(2).split(","):
            name = raw.strip().split(":", 1)[0].split("=", 1)[0].strip()
            if re.fullmatch(r"[A-Za-z_]\w*", name):
                parameters.append(name)
        signatures.setdefault(match.group(1), parameters)
    regexes = {
        name: pattern
        for name, pattern in re.findall(
            r"`([A-Za-z_]\w*)`\s+must match\s+`([^`]+)`",
            spec,
        )
    }
    return signatures, regexes


def _call_literal_argument(signature: str, parameter: str, parameters: Sequence[str]) -> object:
    try:
        expression = ast.parse(signature, mode="eval").body
    except SyntaxError:
        return None
    if not isinstance(expression, ast.Call):
        return None
    for keyword in expression.keywords:
        if keyword.arg == parameter:
            try:
                return ast.literal_eval(keyword.value)
            except (ValueError, TypeError):
                return None
    try:
        index = list(parameters).index(parameter)
    except ValueError:
        return None
    if index >= len(expression.args):
        return None
    try:
        return ast.literal_eval(expression.args[index])
    except (ValueError, TypeError):
        return None


def generated_fixed_spec_partition_conflict_facts(
    project: Path,
    spec: str,
    generated_test_paths: Sequence[str],
    fixed_test_paths: Sequence[str],
) -> list[str]:
    """Detect unlicensed behavioral distinctions inside one SPEC partition.

    If SPEC maps two concrete inputs to the same explicit invalid class, a
    generated test cannot require a different exception boundary from fixed
    acceptance without an additional SPEC predicate that distinguishes them.
    """

    api_parameters, regexes = _spec_api_parameters_and_regexes(spec)
    if not api_parameters or not regexes:
        return []
    generated_guarded: dict[str, list[str]] = {}
    fixed_unguarded: dict[str, list[str]] = {}
    for path, target, guarded_side in [
        *((path, generated_guarded, True) for path in generated_test_paths),
        *((path, fixed_unguarded, False) for path in fixed_test_paths),
    ]:
        try:
            source = resolve_project_path(project, path).read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            continue
        guarded, unguarded = python_exception_call_obligations(source, path)
        for signature, locations in (guarded if guarded_side else unguarded).items():
            target.setdefault(signature, []).extend(locations)
    facts: list[str] = []
    for generated_signature, generated_locations in sorted(generated_guarded.items()):
        try:
            generated_call = ast.parse(generated_signature, mode="eval").body
            generated_name = ast.unparse(generated_call.func).rsplit(".", 1)[-1]
        except (SyntaxError, AttributeError, ValueError, TypeError):
            continue
        parameters = api_parameters.get(generated_name, [])
        for parameter, pattern in regexes.items():
            if parameter not in parameters:
                continue
            generated_value = _call_literal_argument(
                generated_signature, parameter, parameters
            )
            if not isinstance(generated_value, str) or re.fullmatch(pattern, generated_value):
                continue
            for fixed_signature, fixed_locations in sorted(fixed_unguarded.items()):
                try:
                    fixed_call = ast.parse(fixed_signature, mode="eval").body
                    fixed_name = ast.unparse(fixed_call.func).rsplit(".", 1)[-1]
                except (SyntaxError, AttributeError, ValueError, TypeError):
                    continue
                if fixed_name != generated_name:
                    continue
                fixed_value = _call_literal_argument(fixed_signature, parameter, parameters)
                if not isinstance(fixed_value, str) or re.fullmatch(pattern, fixed_value):
                    continue
                facts.append(
                    "SPEC_PARTITION_ORACLE_CONFLICT: SPEC predicate "
                    f"{parameter} matches /{pattern}/ places {generated_value!r} and "
                    f"{fixed_value!r} in the same invalid partition for {generated_name}; "
                    f"generated test requires {generated_signature} to raise at "
                    f"{', '.join(generated_locations)}, while fixed acceptance requires "
                    f"{fixed_signature} not to raise at {', '.join(fixed_locations)}; "
                    "SPEC provides no predicate licensing different behavior within this partition."
                )
    return list(dict.fromkeys(facts))


def generated_test_oracle_evidence_document(
    project: Path,
    spec: str,
    test_paths: Sequence[str],
    command_docs: Sequence[tuple[str, str]],
    prior_judge_document: str = "",
    *,
    max_test_chars: int = 16000,
    max_spec_chars: int = 24000,
) -> str:
    """Build neutral primary evidence for generated-test ownership triage.

    Deliberately exclude repair advice and prior failure-analysis conclusions.
    Those documents are hypotheses under review and caused circular ownership
    decisions when they were presented as evidence.
    """
    normalized_tests = [
        path for path in normalize_new_files(test_paths) if path.startswith("tests/")
    ]
    fixed_tests = [
        path.relative_to(project).as_posix()
        for path in sorted((project / "acceptance_tests").rglob("*.py"))
        if path.is_file()
    ]
    oracle_conflict_facts = generated_fixed_exception_conflict_facts(
        project,
        normalized_tests,
        fixed_tests,
    )
    partition_conflict_facts = generated_fixed_spec_partition_conflict_facts(
        project,
        spec,
        normalized_tests,
        fixed_tests,
    )
    sections = [
        "## Ownership Facts",
        "",
        "- SPEC.md is fixed external policy.",
        "- acceptance_tests/ is fixed read-only evidence.",
        "- The paths below are machine-verified stage-owned generated tests.",
        "- A generated assertion is provisional until its setup, action, and expected proposition agree with SPEC.md.",
        "- Repair advice and prior failure-analysis conclusions are intentionally excluded from this evidence packet.",
        "- stage_owned_generated_tests: " + (", ".join(normalized_tests) or "(none)"),
        "",
        "## Mechanical Oracle Conflict Facts",
        "",
        *(f"- {fact}" for fact in [*oracle_conflict_facts, *partition_conflict_facts]),
        *(["- (none)"] if not oracle_conflict_facts and not partition_conflict_facts else []),
        "",
        "## Fixed Specification",
        "",
        truncate_text(spec, max_spec_chars),
    ]
    for path in normalized_tests:
        source_path = resolve_project_path(project, path)
        try:
            source = source_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            source = "(unavailable)"
        identity_facts = generated_test_receiver_identity_facts(source, path)
        lineage_facts = generated_test_receiver_lineage_facts(source, path)
        sections.extend(
            [
                "",
                f"## Mechanical Receiver Identity Facts: {path}",
                "",
                *(f"- {fact}" for fact in identity_facts),
                *(["- (none)"] if not identity_facts else []),
                "",
                f"## Mechanical Receiver Lineage Facts: {path}",
                "",
                *(f"- {fact}" for fact in lineage_facts),
                *(["- (none)"] if not lineage_facts else []),
                "",
                f"## Generated Test Source: {path}",
                "",
                truncate_text(source, max_test_chars),
            ]
        )
    for path in fixed_tests:
        try:
            source = resolve_project_path(project, path).read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            source = "(unavailable)"
        sections.extend(
            [
                "",
                f"## Fixed Acceptance Source (readonly): {path}",
                "",
                truncate_text(source, max_test_chars),
            ]
        )
    sections.extend(["", "## Executable Command Evidence", ""])
    sections.extend(truncate_text(document, 12000) for _name, document in command_docs)
    if prior_judge_document:
        sections.extend(
            [
                "",
                "## Independent Prior Judge Vote (advisory)",
                "",
                truncate_text(prior_judge_document, 12000),
            ]
        )
    return "\n".join(sections)


def receiver_identity_facts_from_evidence(evidence_doc: str) -> list[str]:
    """Recover only the machine-authored receiver facts from an evidence document."""

    return [
        line[2:].strip()
        for line in evidence_doc.splitlines()
        if line.startswith("- ")
        and "distinct fresh constructor expressions" in line
        and "receivers are not the same instance" in line
    ]


def receiver_lineage_facts_from_evidence(evidence_doc: str) -> list[str]:
    """Recover only machine-authored return-value lineage facts."""

    return [
        line[2:].strip()
        for line in evidence_doc.splitlines()
        if line.startswith("- ")
        and "on an alias of the syntactic return value" in line
        and "source syntax does not establish" in line
    ]


def oracle_conflict_facts_from_evidence(evidence_doc: str) -> list[str]:
    """Recover only machine-authored exception-timing contradiction facts."""

    return [
        line[2:].strip()
        for line in evidence_doc.splitlines()
        if line.startswith(("- MECHANICAL_ORACLE_CONFLICT:", "- SPEC_PARTITION_ORACLE_CONFLICT:"))
    ]


def shared_constructor_arguments_from_facts(facts: Sequence[str]) -> list[set[str]]:
    """Recover machine-authored shared constructor expressions from identity facts."""

    recovered: list[set[str]] = []
    for fact in facts:
        match = re.search(r"shared constructor arguments JSON:\s*(\[[^\n]*\])\.", fact)
        if not match:
            recovered.append(set())
            continue
        try:
            values = json.loads(match.group(1))
        except json.JSONDecodeError:
            recovered.append(set())
            continue
        recovered.append(
            {value for value in values if isinstance(value, str) and value.strip()}
            if isinstance(values, list)
            else set()
        )
    return recovered


def validate_project_policy_triage_proposition(
    record: dict[str, object],
    *,
    receiver_identity_facts: Sequence[str] = (),
    receiver_lineage_facts: Sequence[str] = (),
    oracle_conflict_facts: Sequence[str] = (),
    generated_test_paths: Sequence[str] = (),
) -> dict[str, object]:
    """Fail closed when a generated-oracle verdict lacks positive evidence."""
    normalized = dict(record)
    if str(normalized.get("trigger", "")) != "generated_test_oracle_conflict":
        return normalized
    case_type = str(normalized.get("case_type", ""))
    selected = str(normalized.get("selected_hypothesis", ""))
    product_evidence = normalized.get("product_violation_evidence", [])
    test_evidence = normalized.get("test_contradiction_evidence", [])
    product_items = [item for item in product_evidence if isinstance(item, str) and item.strip()] if isinstance(product_evidence, list) else []
    test_items = [item for item in test_evidence if isinstance(item, str) and item.strip()] if isinstance(test_evidence, list) else []
    valid = True
    reason = ""
    distinct_fresh_receivers = bool(receiver_identity_facts)
    receiver_scope = normalized.get("receiver_scope_analysis", {})
    receiver_scope = receiver_scope if isinstance(receiver_scope, dict) else {}
    mechanical_identity = str(receiver_scope.get("mechanical_identity", ""))
    requires_cross_instance = receiver_scope.get("requires_cross_instance_continuity")
    continuity_witness = str(receiver_scope.get("continuity_witness", ""))
    witness_expression = str(receiver_scope.get("continuity_witness_expression", "")).strip()
    witness_evidence = receiver_scope.get("witness_evidence", [])
    witness_items = [
        item for item in witness_evidence if isinstance(item, str) and item.strip()
    ] if isinstance(witness_evidence, list) else []
    shared_argument_sets = shared_constructor_arguments_from_facts(receiver_identity_facts)
    lineage_scope = normalized.get("receiver_lineage_analysis", {})
    lineage_scope = lineage_scope if isinstance(lineage_scope, dict) else {}
    return_value_contract = lineage_scope.get("method_defined_on_return_value")
    return_value_evidence = lineage_scope.get("contract_evidence", [])
    return_value_evidence_items = [
        item for item in return_value_evidence if isinstance(item, str) and item.strip()
    ] if isinstance(return_value_evidence, list) else []
    oracle_scope = normalized.get("oracle_conflict_analysis", {})
    oracle_scope = oracle_scope if isinstance(oracle_scope, dict) else {}
    jointly_satisfiable = oracle_scope.get("fixed_and_generated_jointly_satisfiable")
    fixed_contradicts_spec = oracle_scope.get("fixed_acceptance_explicitly_contradicts_spec")
    conflict_evidence = oracle_scope.get("conflict_evidence", [])
    conflict_items = [
        item for item in conflict_evidence if isinstance(item, str) and item.strip()
    ] if isinstance(conflict_evidence, list) else []
    fixed_contradiction_evidence = oracle_scope.get("fixed_spec_contradiction_evidence", [])
    fixed_contradiction_items = [
        item for item in fixed_contradiction_evidence if isinstance(item, str) and item.strip()
    ] if isinstance(fixed_contradiction_evidence, list) else []
    exact_conflict = any(
        fact.startswith("MECHANICAL_ORACLE_CONFLICT:") for fact in oracle_conflict_facts
    )
    partition_conflict = any(
        fact.startswith("SPEC_PARTITION_ORACLE_CONFLICT:") for fact in oracle_conflict_facts
    )
    mechanically_conflicting = exact_conflict or partition_conflict
    if (
        (jointly_satisfiable is False and conflict_items or mechanically_conflicting)
        and fixed_contradicts_spec is False
        and not fixed_contradiction_items
    ):
        test_paths = unique_ordered(
            path for path in generated_test_paths if path.startswith("tests/")
        )
        if test_paths:
            contradiction = (
                "Fixed acceptance and generated test propositions are mechanically reported "
                "as jointly unsatisfiable, while no explicit SPEC proposition refutes the "
                "fixed acceptance contract. The generated oracle therefore owns the conflict."
            )
            normalized.update(
                {
                    "case_type": "test_harness",
                    "confidence": "high",
                    "selected_hypothesis": "H_test",
                    "test_contradiction_evidence": [
                        contradiction,
                        *oracle_conflict_facts,
                        *conflict_items,
                    ],
                    "safe_next_action": "edit_test_harness",
                    "editable_paths": test_paths,
                    "proposition_gate": {
                        "status": "corrected",
                        "reason": (
                            "mechanical_exception_timing_conflict"
                            if exact_conflict
                            else "undiscriminated_spec_partition_conflict"
                            if partition_conflict
                            else "unsatisfiable_generated_oracle_precedence"
                        ),
                    },
                    "rationale": contradiction,
                }
            )
            return normalized
    if case_type == "product_bug" and (selected != "H_product" or not product_items):
        valid = False
        reason = "product_bug requires selected_hypothesis=H_product and positive product_violation_evidence"
    elif case_type == "test_harness" and (selected != "H_test" or not test_items):
        valid = False
        reason = "test_harness requires selected_hypothesis=H_test and positive test_contradiction_evidence"
    elif distinct_fresh_receivers and mechanical_identity != "distinct_fresh":
        valid = False
        reason = "receiver_scope_analysis must acknowledge mechanically distinct fresh receivers"
    elif (
        case_type == "product_bug"
        and receiver_lineage_facts
        and not (return_value_contract is True and return_value_evidence_items)
    ):
        test_paths = unique_ordered(
            path for path in generated_test_paths if path.startswith("tests/")
        )
        contradiction = (
            "The generated test invokes a method on a syntactic method return value, but "
            "no explicit SPEC contract defines that method on the return value or proves "
            "that the return value is the original receiver."
        )
        normalized.update(
            {
                "case_type": "test_harness",
                "confidence": "high",
                "selected_hypothesis": "H_test",
                "test_contradiction_evidence": [contradiction, *receiver_lineage_facts],
                "safe_next_action": "edit_test_harness",
                "editable_paths": test_paths,
                "proposition_gate": {
                    "status": "corrected",
                    "reason": "unsupported_return_value_receiver",
                },
                "rationale": contradiction,
            }
        )
        return normalized
    elif (
        case_type == "product_bug"
        and distinct_fresh_receivers
        and requires_cross_instance is True
        and (
            continuity_witness
            not in {"explicit_shared_persistence", "unconditional_spec_contract"}
            or not witness_items
            or (
                continuity_witness == "explicit_shared_persistence"
                and (
                    not witness_expression
                    or not shared_argument_sets
                    or not all(witness_expression in values for values in shared_argument_sets)
                )
            )
            or (
                continuity_witness == "unconditional_spec_contract"
                and witness_expression != "none_required"
            )
        )
    ):
        test_paths = unique_ordered(
            path for path in generated_test_paths if path.startswith("tests/")
        )
        contradiction = (
            "The generated test requires state continuity across mechanically distinct fresh "
            "receivers without a mechanically present shared-persistence expression or an "
            "explicitly unconditional cross-instance SPEC contract."
        )
        normalized.update(
            {
                "case_type": "test_harness",
                "confidence": "high",
                "selected_hypothesis": "H_test",
                "test_contradiction_evidence": [contradiction, *receiver_identity_facts],
                "safe_next_action": "edit_test_harness",
                "editable_paths": test_paths,
                "proposition_gate": {
                    "status": "corrected",
                    "reason": "unsupported_cross_instance_continuity",
                },
                "rationale": contradiction,
            }
        )
        return normalized
    if valid:
        normalized["proposition_gate"] = {"status": "pass"}
        return normalized
    normalized.update(
        {
            "case_type": "insufficient_context",
            "confidence": "low",
            "safe_next_action": "reject",
            "editable_paths": [],
            "proposition_gate": {"status": "reject", "reason": reason},
            "rationale": reason,
        }
    )
    return normalized


def project_policy_triage_enabled(mode: str, trigger: str) -> bool:
    if mode == "never":
        return False
    if mode == "always":
        return True
    return trigger in PROJECT_POLICY_TRIAGE_TRIGGERS


def triage_string_list(record: dict[str, object] | None, key: str) -> list[str]:
    if not record:
        return []
    raw = record.get(key, [])
    if not isinstance(raw, list):
        return []
    return normalize_new_files(str(item) for item in raw if isinstance(item, str))


def triage_allows_test_harness_edit(record: dict[str, object] | None) -> bool:
    if not record:
        return False
    return (
        str(record.get("case_type", "")).strip() == "test_harness"
        and str(record.get("safe_next_action", "")).strip() == "edit_test_harness"
        and str(record.get("confidence", "")).strip() != "low"
    )


def authorized_test_edit_paths(records: Sequence[dict[str, object]]) -> list[str]:
    paths: list[str] = []
    for record in records:
        if triage_allows_test_harness_edit(record):
            paths.extend(triage_string_list(record, "editable_paths"))
    return unique_ordered(path for path in paths if path.startswith("tests/"))


def generated_test_oracle_triage_needed(
    records: Sequence[dict[str, object]],
    failure_signature: str | None,
) -> bool:
    """Re-triage only when the concrete executable counterexample changed."""
    if not failure_signature:
        return not any(
            str(record.get("trigger", "")) == "generated_test_oracle_conflict"
            for record in records
        )
    return not any(
        str(record.get("trigger", "")) == "generated_test_oracle_conflict"
        and str(record.get("failure_signature", "")) == failure_signature
        for record in records
    )


def enforce_test_harness_triage_gate(
    record: dict[str, object],
    stage_owned_test_paths: Sequence[str],
) -> dict[str, object]:
    """Limit advisory LLM decisions to machine-owned generated test paths."""
    normalized = dict(record)
    if not triage_allows_test_harness_edit(normalized):
        return normalized
    owned = {
        path
        for path in normalize_new_files(stage_owned_test_paths)
        if path.startswith("tests/")
    }
    requested = triage_string_list(normalized, "editable_paths")
    approved = [path for path in requested if path in owned]
    rejected = [path for path in requested if path not in owned]
    normalized["editable_paths"] = approved
    normalized["action_gate"] = {
        "stage_owned_test_paths": sorted(owned),
        "approved_editable_paths": approved,
        "rejected_editable_paths": rejected,
    }
    if approved:
        return normalized
    normalized["safe_next_action"] = "reject"
    normalized["confidence"] = "low"
    normalized["rationale"] = (
        "Test edit denied because no requested path is a machine-verified stage-owned test harness."
    )
    return normalized


def apply_project_policy_triage_to_advice(
    advice: RepairAdvice,
    triage: dict[str, object] | None,
    existing_project_paths: Sequence[str],
    test_harness_write_strategies: Sequence[str],
) -> RepairAdvice:
    if advice.strategy not in test_harness_write_strategies or not triage:
        return advice
    editable_paths = triage_string_list(triage, "editable_paths")
    readonly_paths = triage_string_list(triage, "readonly_paths")
    forbidden_actions = [
        str(item)
        for item in triage.get("forbidden_actions", [])
        if isinstance(item, str)
    ] if isinstance(triage.get("forbidden_actions", []), list) else []
    if triage_allows_test_harness_edit(triage):
        return RepairAdvice(
            strategy=advice.strategy,
            focus_files=tuple(unique_ordered([*editable_paths, *advice.focus_files, *readonly_paths])),
            instructions=tuple(
                unique_ordered(
                    [
                        *advice.instructions,
                        "Project-policy triage classified the relevant generated test harness as writable for this repair.",
                        *[f"Forbidden by project-policy triage: {item}" for item in forbidden_actions],
                    ]
                )
            ),
            evidence=tuple(
                unique_ordered(
                    [
                        *advice.evidence,
                        f"project_policy_triage={triage.get('case_type')}:{triage.get('safe_next_action')}",
                    ]
                )
            ),
        )
    product_focus = [
        path
        for path in [*advice.focus_files, *readonly_paths]
        if path in existing_project_paths and not path.startswith("tests/")
    ]
    return RepairAdvice(
        strategy="root_cause_patch",
        focus_files=tuple(unique_ordered(product_focus)),
        instructions=tuple(
            unique_ordered(
                [
                    "Project-policy triage did not authorize editing tests; treat tests as read-only evidence.",
                    "Repair product code or request missing context instead of changing the test harness.",
                    *[f"Forbidden by project-policy triage: {item}" for item in forbidden_actions],
                ]
            )
        ),
        evidence=tuple(
            unique_ordered(
                [
                    *advice.evidence,
                    f"project_policy_triage={triage.get('case_type')}:{triage.get('safe_next_action')}",
                ]
            )
        ),
    )
