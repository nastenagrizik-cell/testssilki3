from __future__ import annotations

from copy import deepcopy

from .models import ConditionAtom, LogicRule, SurveySpec, TestScenario


def _first_value(atom: ConditionAtom, truth: bool) -> str:
    values = atom.values or ["1"]
    if truth:
        if atom.operator in {"in", "eq"}:
            return values[0]
        candidate = "1"
        while candidate in values:
            candidate = str(int(candidate) + 1)
        return candidate
    if atom.operator in {"not_in", "ne"}:
        return values[0]
    candidate = "1"
    while candidate in values:
        candidate = str(int(candidate) + 1)
    return candidate


def _apply_condition(assignments: dict[str, list[str]], rule: LogicRule, truth: bool) -> None:
    if not rule.condition or not rule.condition.atoms:
        return
    atoms = rule.condition.atoms
    if truth:
        selected = atoms if rule.condition.combinator == "and" else atoms[:1]
        for atom in selected:
            assignments[atom.question_id] = [_first_value(atom, True)]
    else:
        atom = atoms[0]
        assignments[atom.question_id] = [_first_value(atom, False)]


def _set_atom(assignments: dict[str, list[str]], atom: ConditionAtom, truth: bool) -> None:
    assignments[atom.question_id] = [_first_value(atom, truth)]


def _scenario(
    *,
    scenario_id: str,
    label: str,
    assignments: dict[str, list[str]],
    question_id: str,
    question_text: str,
    should_show: bool,
    rule: LogicRule,
) -> TestScenario:
    return TestScenario(
        id=scenario_id,
        label=label,
        assignments=assignments,
        expected_visible=[question_id] if should_show else [],
        expected_hidden=[] if should_show else [question_id],
        expected_texts={question_id: question_text},
        source_rule=rule.raw_text,
    )


def _baseline_assignments(spec: SurveySpec) -> dict[str, list[str]]:
    baseline: dict[str, list[str]] = {}
    # First select every explicitly qualifying "continue" answer.  Screening
    # terminate rules are evaluated afterwards and must not overwrite an
    # already safe value for the same question.
    for rule in spec.rules:
        if rule.action == "continue" and rule.condition:
            _apply_condition(baseline, rule, True)
    for rule in spec.rules:
        if rule.action == "terminate" and rule.condition:
            assigned_questions = set(baseline)
            rule_questions = {atom.question_id for atom in rule.condition.atoms}
            if assigned_questions & rule_questions:
                continue
            _apply_condition(baseline, rule, False)
    return baseline


def _consolidate_scenarios(scenarios: list[TestScenario]) -> list[TestScenario]:
    """Combine compatible checks that use the same response path."""
    groups: list[TestScenario] = []
    for scenario in scenarios:
        assignment_key = tuple(
            sorted((question_id, tuple(values)) for question_id, values in scenario.assignments.items())
        )
        target = None
        for candidate in groups:
            candidate_key = tuple(
                sorted((question_id, tuple(values)) for question_id, values in candidate.assignments.items())
            )
            conflicts = bool(
                set(candidate.expected_visible) & set(scenario.expected_hidden)
                or set(candidate.expected_hidden) & set(scenario.expected_visible)
            )
            if candidate_key == assignment_key and not conflicts:
                target = candidate
                break
        if target is None:
            groups.append(scenario.model_copy(deep=True))
            continue
        target.expected_visible = list(dict.fromkeys(target.expected_visible + scenario.expected_visible))
        target.expected_hidden = list(dict.fromkeys(target.expected_hidden + scenario.expected_hidden))
        target.expected_texts.update(scenario.expected_texts)
        rules = [value for value in (target.source_rule, scenario.source_rule) if value]
        target.source_rule = " | ".join(dict.fromkeys(rules))
        checked = target.expected_visible + target.expected_hidden
        target.label = f"Объединённая проверка: {', '.join(checked)}"
    for index, scenario in enumerate(groups, start=1):
        scenario.id = f"route-{index}"
    return groups


def build_scenarios(spec: SurveySpec, limit: int | None = 80) -> list[TestScenario]:
    baseline = _baseline_assignments(spec)
    scenarios: list[TestScenario] = []
    counter = 1
    for question in spec.questions:
        for rule in question.display_rules:
            if not rule.condition or not rule.condition.atoms:
                continue
            atoms = rule.condition.atoms
            truth_means_show = rule.action != "hide"
            candidates: list[tuple[str, dict[str, list[str]], bool]] = []

            if rule.condition.combinator == "and":
                positive = deepcopy(baseline)
                for atom in atoms:
                    _set_atom(positive, atom, True)
                candidates.append(("все части условия истинны", positive, truth_means_show))
                for atom_index, atom in enumerate(atoms, start=1):
                    negative = deepcopy(positive)
                    _set_atom(negative, atom, False)
                    candidates.append(
                        (
                            f"часть {atom_index} условия ложна",
                            negative,
                            not truth_means_show,
                        )
                    )
            else:
                negative = deepcopy(baseline)
                for atom in atoms:
                    _set_atom(negative, atom, False)
                candidates.append(("все части условия ложны", negative, not truth_means_show))
                for atom_index, atom in enumerate(atoms, start=1):
                    positive = deepcopy(negative)
                    _set_atom(positive, atom, True)
                    candidates.append(
                        (
                            f"часть {atom_index} условия истинна",
                            positive,
                            truth_means_show,
                        )
                    )

            for variant_index, (detail, assignments, should_show) in enumerate(candidates, start=1):
                state = "должен появиться" if should_show else "не должен появиться"
                scenarios.append(
                    _scenario(
                        scenario_id=f"logic-{counter}-{variant_index}",
                        label=f"{question.id} {state}: {detail}",
                        assignments=assignments,
                        question_id=question.id,
                        question_text=question.text,
                        should_show=should_show,
                        rule=rule,
                    )
                )
            counter += 1
    consolidated = _consolidate_scenarios(scenarios)
    return consolidated if limit is None else consolidated[:limit]
