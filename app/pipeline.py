from __future__ import annotations

import re
from pathlib import Path

from .comparator import compare_surveys
from .docx_parser import DocxSurveyParser
from .models import (
    ComparisonReport,
    Issue,
    ObservedQuestion,
    ObservedSurvey,
    QuestionMatch,
    ScenarioResult,
    SurveySpec,
)
from .normalization import text_similarity
from .planner import build_scenarios
from .platforms import EnjoySurveyAdapter, SurveyStudioAdapter
from .platforms.base import PlatformAdapter
from .url_security import validate_public_url


def adapter_for(url: str, headless: bool = True) -> PlatformAdapter:
    if EnjoySurveyAdapter.supports(url):
        return EnjoySurveyAdapter(headless=headless)
    if SurveyStudioAdapter.supports(url):
        return SurveyStudioAdapter(headless=headless)
    raise ValueError(
        "Платформа пока не поддерживается. Сейчас работают EnjoySurvey и SurveyStudio."
    )


def _build_answer_code_map(
    spec: SurveySpec,
    observed: ObservedSurvey,
    matches: list[QuestionMatch],
) -> dict[str, dict[str, str]]:
    """Translate Word answer codes to platform codes using answer wording."""
    observed_by_id = {question.platform_id: question for question in observed.questions}
    result: dict[str, dict[str, str]] = {}
    observed_by_expected: dict[str, ObservedQuestion] = {}
    for match in matches:
        if match.expected_position >= len(spec.questions):
            continue
        expected = spec.questions[match.expected_position]
        actual = observed_by_id.get(match.observed_id)
        if not actual:
            continue
        observed_by_expected[expected.id] = actual
        available = set(range(len(actual.options)))
        code_map: dict[str, str] = {}
        for expected_option in expected.options:
            ranked = sorted(
                (
                    (text_similarity(expected_option.text, actual.options[index].text), index)
                    for index in available
                ),
                reverse=True,
            )
            if not ranked or ranked[0][0] < 0.55:
                continue
            _, index = ranked[0]
            available.remove(index)
            code_map[expected_option.code] = actual.options[index].code
        if code_map:
            result[expected.id] = code_map

    # A routing instruction can still provide the answer wording when its
    # shared Word answer table was intentionally struck out, for example:
    # "продолжить, если S3=5 (бургеры с говядиной)".  Enjoy's internal
    # data-value-code is not guaranteed to equal Word code 5, so map it by
    # that wording instead of clicking the raw number and screening out.
    for rule in spec.rules:
        if not rule.condition:
            continue
        for atom in rule.condition.atoms:
            actual = observed_by_expected.get(atom.question_id)
            if not actual or not actual.options:
                continue
            for word_code in atom.values:
                if word_code in result.get(atom.question_id, {}):
                    continue
                label_match = re.search(
                    rf"\b{re.escape(atom.question_id)}\s*(?:=|==|:)?\s*"
                    rf"{re.escape(word_code)}\s*\(([^)]+)\)",
                    rule.raw_text,
                    flags=re.I,
                )
                if not label_match:
                    continue
                label = label_match.group(1).strip()
                ranked = sorted(
                    (
                        (text_similarity(label, option.text), option)
                        for option in actual.options
                    ),
                    key=lambda item: item[0],
                    reverse=True,
                )
                if ranked and ranked[0][0] >= 0.45:
                    result.setdefault(atom.question_id, {})[word_code] = ranked[0][1].code
    return result


def _same_platform_question(existing_id: str, runtime_id: str) -> bool:
    return existing_id.split("@", 1)[0] == runtime_id.split("@", 1)[0]


def _merge_scenario_observations(
    observed: ObservedSurvey,
    scenario_results: list[ScenarioResult],
) -> bool:
    """Merge filtered questions at their runtime position between known anchors."""
    changed = False
    for result in scenario_results:
        runtime_questions = result.observed_questions
        for runtime_index, detail in enumerate(runtime_questions):
            candidates = [
                (index, question)
                for index, question in enumerate(observed.questions)
                if _same_platform_question(question.platform_id, detail.platform_id)
            ]
            if candidates:
                index, current = max(
                    candidates,
                    key=lambda item: text_similarity(item[1].text, detail.text),
                )
                detail.position = current.position
                observed.questions[index] = detail
                changed = True
                continue

            previous_index = None
            for previous in reversed(runtime_questions[:runtime_index]):
                previous_index = next(
                    (
                        index
                        for index, question in enumerate(observed.questions)
                        if _same_platform_question(question.platform_id, previous.platform_id)
                    ),
                    None,
                )
                if previous_index is not None:
                    break
            next_index = None
            for following in runtime_questions[runtime_index + 1:]:
                next_index = next(
                    (
                        index
                        for index, question in enumerate(observed.questions)
                        if _same_platform_question(question.platform_id, following.platform_id)
                    ),
                    None,
                )
                if next_index is not None:
                    break

            if next_index is not None:
                insert_at = next_index
            elif previous_index is not None:
                insert_at = previous_index + 1
            else:
                insert_at = len(observed.questions)
            observed.questions.insert(insert_at, detail)
            changed = True

    if changed:
        for position, question in enumerate(observed.questions):
            question.position = position
    return changed


async def run_audit(
    document_path: str | Path,
    survey_url: str,
    run_logic: bool = True,
    allow_standard_demographics: bool = True,
    scenario_limit: int = 80,
    headless: bool = True,
) -> ComparisonReport:
    survey_url = validate_public_url(survey_url)
    spec = DocxSurveyParser().parse(document_path)
    adapter = adapter_for(survey_url, headless=headless)
    if isinstance(adapter, EnjoySurveyAdapter):
        adapter.configure_spec(spec)
    observed = await adapter.discover(survey_url)
    report = compare_surveys(
        spec,
        observed,
        allow_standard_demographics=allow_standard_demographics,
    )
    answer_code_map = _build_answer_code_map(spec, observed, report.matches)

    if run_logic:
        runtime_issue_codes = {
            "scenario_limit_reached",
            "scenario_inconclusive",
            "logic_question_missing",
            "logic_question_extra",
        }
        all_scenarios = build_scenarios(spec, limit=None)
        report.scenarios = all_scenarios[:scenario_limit]
        if len(all_scenarios) > scenario_limit:
            report.issues.append(
                Issue(
                    code="scenario_limit_reached",
                    severity="warning",
                    title="Не все логические сценарии были запущены",
                    expected=f"Сценариев сформировано: {len(all_scenarios)}",
                    actual=f"Лимит запуска: {scenario_limit}",
                )
            )
        for scenario in report.scenarios:
            result = await adapter.run_scenario(
                survey_url,
                scenario,
                report.matches,
                answer_code_map=answer_code_map,
            )
            report.scenario_results.append(result)
            if not result.completed and result.warnings:
                report.issues.append(
                    Issue(
                        code="scenario_inconclusive",
                        severity="warning",
                        title=f"Сценарий «{scenario.label}» не завершён",
                        actual="; ".join(result.warnings),
                        evidence={"scenario_id": scenario.id},
                    )
                )
                continue
            for expected_id in scenario.expected_visible:
                if expected_id not in result.visited_expected_ids:
                    report.issues.append(
                        Issue(
                            code="logic_question_missing",
                            severity="critical",
                            title="Вопрос не появился в ветке, где должен задаваться",
                            expected=f"Вопрос {expected_id} должен появиться",
                            actual="Вопрос не встретился при прохождении",
                            question_id=expected_id,
                            evidence={
                                "scenario": scenario.label,
                                "assignments": scenario.assignments,
                                "source_rule": scenario.source_rule,
                            },
                        )
                    )
            for expected_id in scenario.expected_hidden:
                if expected_id in result.visited_expected_ids:
                    report.issues.append(
                        Issue(
                            code="logic_question_extra",
                            severity="critical",
                            title="Вопрос появился в ветке, где не должен задаваться",
                            expected=f"Вопрос {expected_id} не должен появляться",
                            actual="Вопрос появился при прохождении",
                            question_id=expected_id,
                            evidence={
                                "scenario": scenario.label,
                                "assignments": scenario.assignments,
                                "source_rule": scenario.source_rule,
                            },
                        )
                    )
        if _merge_scenario_observations(observed, report.scenario_results):
            runtime_issues = [issue for issue in report.issues if issue.code in runtime_issue_codes]
            refreshed = compare_surveys(
                spec,
                observed,
                allow_standard_demographics=allow_standard_demographics,
            )
            report.matches = refreshed.matches
            report.issues = refreshed.issues + runtime_issues
            for result in report.scenario_results:
                # These details have already been merged into the structural
                # comparison; keep the downloadable report compact.
                result.observed_questions = []

        visited_in_scenarios = {
            expected_id
            for result in report.scenario_results
            for expected_id in result.visited_expected_ids
        }
        if visited_in_scenarios:
            report.issues = [
                issue
                for issue in report.issues
                if not (
                    issue.code == "missing_question"
                    and issue.question_id in visited_in_scenarios
                )
            ]
    return report
