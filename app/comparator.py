from __future__ import annotations

import re
from datetime import UTC, datetime

from .models import (
    ComparisonReport,
    Issue,
    ObservedSurvey,
    QuestionMatch,
    QuestionSpec,
    QuestionType,
    SurveySpec,
)
from .normalization import normalize_id, normalize_space, normalize_text, text_similarity


TYPE_COMPATIBILITY = {
    (QuestionType.SINGLE, QuestionType.SCALE),
    (QuestionType.SCALE, QuestionType.SINGLE),
    (QuestionType.SINGLE, QuestionType.MATRIX_SINGLE),
    (QuestionType.MATRIX_SINGLE, QuestionType.SINGLE),
}


def _types_match(expected: QuestionType, observed: QuestionType) -> bool:
    if QuestionType.UNKNOWN in {expected, observed}:
        return True
    return expected == observed or (expected, observed) in TYPE_COMPATIBILITY


TECHNICAL_TEXT_PATTERNS = (
    r"\bПОКАЖИТЕ?\s+ЭКРАН\b\.?",
    r"\bВОЗМОЖНО\s+НЕСКОЛЬКО\s+ОТВЕТОВ\b\.?",
    r"\bОДИН\s+ОТВЕТ\b\.?",
    r"\bОТМЕТЬТЕ\s+НЕ\s+СПРАШИВАЯ\b\.?",
    r"\bЗАПИШИТЕ(?:\s*,?\s*ПОЖАЛУЙСТА\s*,?)?\s+ПОДРОБНО\b\.?",
    r"\([^)]*\bВЫВЕСТИ\b[^)]*\)",
    r"\bВЫВЕСТИ\s+КОД\s+ПРОДУКТА\s*,?\s*ВЫБРАННОГО\s+В\s+[A-ZА-ЯЁ]+\d+(?:[._]\d+)*",
)


def _clean_question_text(value: str) -> str:
    result = value
    for pattern in TECHNICAL_TEXT_PATTERNS:
        result = re.sub(pattern, " ", result, flags=re.I)
    return normalize_space(result).strip(" .")


def _question_text_score(expected: str, actual: str) -> float:
    cleaned_expected = _clean_question_text(expected)
    cleaned_actual = _clean_question_text(actual)
    left = normalize_text(cleaned_expected)
    right = normalize_text(cleaned_actual)
    if left and right:
        shorter, longer = sorted((left, right), key=len)
        if len(shorter) >= 24 and shorter in longer:
            return 1.0
        if actual.rstrip().endswith(("...", "…")):
            if left.startswith(right):
                return 1.0
            expected_prefix = " ".join(left.split()[:4])
            if expected_prefix and expected_prefix in right:
                return 1.0
    return text_similarity(cleaned_expected, cleaned_actual)


def _is_technical_question(text: str) -> bool:
    value = normalize_text(_clean_question_text(text))
    return value in {
        "закодировать возраст",
        "группировка возраста",
        "порядок концепций",
        "продукты",
    }


def _is_age_question(text: str) -> bool:
    value = normalize_text(text)
    return "возраст" in value or "полных лет" in value or "сколько вам лет" in value


def _reference_base(value: str | None) -> str:
    """Remove SurveyStudio's final concept/product suffix (for example .195)."""
    normalized = normalize_id(value or "")
    return re.sub(r"\.\d{3,}$", "", normalized)


def _reference_family_matches(expected_id: str, actual_reference: str | None) -> bool:
    expected = normalize_id(expected_id)
    actual = _reference_base(actual_reference)
    return actual == expected or bool(re.fullmatch(rf"{re.escape(expected)}\.\d{{1,2}}", actual))


def _match_questions(spec: SurveySpec, observed: ObservedSurvey) -> tuple[list[QuestionMatch], set[int], set[int]]:
    candidates: list[tuple[float, int, int, float]] = []
    expected_count = max(1, len(spec.questions) - 1)
    observed_count = max(1, len(observed.questions) - 1)
    for expected_index, expected in enumerate(spec.questions):
        if _is_technical_question(expected.text):
            continue
        for observed_index, actual in enumerate(observed.questions):
            if _is_technical_question(actual.text):
                continue
            actual_reference = _reference_base(actual.reference_code)
            reference_match = _reference_family_matches(expected.id, actual.reference_code)
            # SurveyStudio exposes the questionnaire code in its inventory.
            # A second concept copy of B18 must not hide a missing B20.
            if observed.platform.lower() == "surveystudio" and actual_reference and not reference_match:
                continue
            semantic = _question_text_score(expected.text, actual.text)
            order_distance = abs(expected_index / expected_count - observed_index / observed_count)
            adjusted = (
                1.5 + semantic
                if reference_match
                else semantic - min(0.12, 0.12 * order_distance)
            )
            candidates.append((adjusted, expected_index, observed_index, semantic))

    used_expected: set[int] = set()
    used_observed: set[int] = set()
    matches: list[QuestionMatch] = []
    for adjusted, expected_index, observed_index, semantic in sorted(candidates, reverse=True):
        if adjusted < 0.32 or expected_index in used_expected or observed_index in used_observed:
            continue
        expected = spec.questions[expected_index]
        actual = observed.questions[observed_index]
        used_expected.add(expected_index)
        used_observed.add(observed_index)
        matches.append(
            QuestionMatch(
                expected_id=expected.id,
                observed_id=actual.platform_id,
                score=semantic,
                expected_position=expected_index,
                observed_position=observed_index,
            )
        )
    return sorted(matches, key=lambda item: item.expected_position), used_expected, used_observed


def _dynamic_option(text: str) -> bool:
    value = normalize_text(text)
    return (
        "код" in value and ("первый" in value or "второй" in value or "продукт" in value)
    )


def _matches_test_product(text: str, products: list[dict[str, str]]) -> bool:
    normalized = normalize_text(text)
    if not products:
        return True
    return any(
        normalize_text(product.get("name", "")) in normalized
        or any(code in normalized.split() for code in re.findall(r"\d+", product.get("code", "")))
        for product in products
    )


def _compare_options(
    expected: QuestionSpec,
    actual,
    issues: list[Issue],
    *,
    allow_expected_subset: bool = False,
    test_products: list[dict[str, str]] | None = None,
) -> None:
    if not expected.options:
        return
    if not actual.options:
        issues.append(
            Issue(
                code="answer_options_missing",
                severity="major",
                title="В ссылке не найдены варианты ответа",
                expected=f"Вариантов в Word: {len(expected.options)}",
                actual="Варианты ответа отсутствуют или не прочитаны",
                question_id=expected.id,
            )
        )
        return
    unmatched_actual = set(range(len(actual.options)))
    for expected_option in expected.options:
        if _dynamic_option(expected_option.text) and unmatched_actual:
            matching_products = [
                index
                for index in sorted(unmatched_actual)
                if _matches_test_product(actual.options[index].text, test_products or [])
            ]
            if matching_products:
                unmatched_actual.remove(matching_products[0])
                continue
        def option_similarity(option) -> float:
            actual_text = re.sub(r"^\s*\d+\s*[-–—]\s*", "", option.text)
            expected_text = re.sub(r"^\s*\d+\s*[-–—]\s*", "", expected_option.text)
            return text_similarity(expected_text, actual_text)

        ranked = sorted(
            (
                (option_similarity(option), index, option)
                for index, option in enumerate(actual.options)
                if index in unmatched_actual
            ),
            reverse=True,
        )
        if not ranked or ranked[0][0] < 0.55:
            if not allow_expected_subset:
                issues.append(
                    Issue(
                        code="missing_option",
                        severity="major",
                        title=f"Не найден вариант ответа {expected_option.code}",
                        expected=expected_option.text,
                        question_id=expected.id,
                    )
                )
            continue
        score, index, actual_option = ranked[0]
        unmatched_actual.remove(index)
        if score < 0.86:
            issues.append(
                Issue(
                    code="option_text_mismatch",
                    severity="minor",
                    title=f"Отличается формулировка варианта {expected_option.code}",
                    expected=expected_option.text,
                    actual=actual_option.text,
                    question_id=expected.id,
                    evidence={"similarity": score},
                )
            )
        if (
            expected_option.exclusive
            and not actual_option.exclusive
            and actual.type not in {QuestionType.SINGLE, QuestionType.SCALE}
        ):
            issues.append(
                Issue(
                    code="exclusive_option_missing",
                    severity="major",
                    title="Вариант должен быть исключающим",
                    expected=expected_option.text,
                    actual="На платформе нет признака исключающего варианта",
                    question_id=expected.id,
                )
            )
    for index in sorted(unmatched_actual):
        option = actual.options[index]
        issues.append(
            Issue(
                code="extra_option",
                severity="major",
                title="В ссылке найден лишний вариант ответа",
                actual=f"{option.code}: {option.text}",
                question_id=expected.id,
            )
        )


def _compare_matrix_rows(expected: QuestionSpec, actual, issues: list[Issue]) -> None:
    if not expected.matrix_rows:
        return
    if not actual.matrix_rows:
        issues.append(
            Issue(
                code="matrix_rows_unreadable",
                severity="warning",
                title="Не удалось прочитать строки матрицы на платформе",
                expected=f"Строк в Word: {len(expected.matrix_rows)}",
                question_id=expected.id,
            )
        )
        return
    unmatched_actual = set(range(len(actual.matrix_rows)))
    for expected_row in expected.matrix_rows:
        ranked = sorted(
            (
                (text_similarity(expected_row.text, row.text), index, row)
                for index, row in enumerate(actual.matrix_rows)
                if index in unmatched_actual
            ),
            reverse=True,
        )
        if not ranked or ranked[0][0] < 0.55:
            issues.append(
                Issue(
                    code="missing_matrix_row",
                    severity="major",
                    title="В матрице не найдена строка из Word",
                    expected=expected_row.text,
                    question_id=expected.id,
                )
            )
            continue
        score, index, actual_row = ranked[0]
        unmatched_actual.remove(index)
        if score < 0.86:
            issues.append(
                Issue(
                    code="matrix_row_text_mismatch",
                    severity="minor",
                    title="Формулировка строки матрицы отличается",
                    expected=expected_row.text,
                    actual=actual_row.text,
                    question_id=expected.id,
                    evidence={"similarity": score},
                )
            )
    for index in sorted(unmatched_actual):
        issues.append(
            Issue(
                code="extra_matrix_row",
                severity="major",
                title="В ссылке найдена лишняя строка матрицы",
                actual=actual.matrix_rows[index].text,
                question_id=expected.id,
            )
        )


def _unique_orders(samples: list[list[str]]) -> list[tuple[str, ...]]:
    return list(dict.fromkeys(tuple(value for value in sample if value) for sample in samples if sample))


def _compare_rotation(expected: QuestionSpec, actual, issues: list[Issue]) -> None:
    rotation = expected.rotation
    option_orders = _unique_orders(actual.option_order_samples)
    row_orders = _unique_orders(actual.row_order_samples)

    if rotation and rotation.enabled:
        samples = row_orders if rotation.axis == "rows" else option_orders
        if rotation.axis in {"options", "rows", "columns"} and len(samples) == 1 and len(samples[0]) >= 3:
            issues.append(
                Issue(
                    code="rotation_not_observed",
                    severity="major",
                    title="Ротация не обнаружена в независимых запусках",
                    expected=f"Ротация: {rotation.axis}",
                    actual="Порядок не изменился",
                    question_id=expected.id,
                    evidence={"samples": [list(item) for item in samples]},
                )
            )
        if rotation.fixed_codes and option_orders:
            for code in rotation.fixed_codes:
                positions = [order.index(code) for order in option_orders if code in order]
                if len(set(positions)) > 1:
                    issues.append(
                        Issue(
                            code="fixed_option_moved",
                            severity="major",
                            title="Закреплённый вариант меняет позицию",
                            expected=f"Код {code} должен быть закреплён",
                            actual=f"Позиции в запусках: {positions}",
                            question_id=expected.id,
                        )
                    )
    elif len(option_orders) > 1 or len(row_orders) > 1:
        issues.append(
            Issue(
                code="unexpected_rotation",
                severity="major",
                title="Обнаружена ротация, которой нет в Word",
                expected="Стабильный порядок",
                actual="Порядок менялся между независимыми запусками",
                question_id=expected.id,
            )
        )


def _is_standard_platform_demographic(text: str) -> bool:
    value = normalize_text(text)
    patterns = (
        "в каком населенном пункте вы проживаете",
        "населенный пункт проживания",
        "ваша страна",
        "страна проживания",
        "в каком городе вы проживаете",
        "город проживания",
        "укажите ваш пол",
        "ваш пол",
        "пол",
        "укажите ваш возраст",
        "ваш возраст",
        "сколько вам полных лет",
    )
    return any(value == pattern or value.startswith(f"{pattern} ") for pattern in patterns)


def compare_surveys(
    spec: SurveySpec,
    observed: ObservedSurvey,
    allow_standard_demographics: bool = True,
) -> ComparisonReport:
    matches, used_expected, used_observed = _match_questions(spec, observed)
    issues: list[Issue] = []
    by_expected_position = {match.expected_position: match for match in matches}
    observed_by_id = {question.platform_id: question for question in observed.questions}

    for index, question in enumerate(spec.questions):
        has_filter_rule = any(
            rule.condition and rule.condition.atoms for rule in question.display_rules
        )
        if (
            index not in used_expected
            and not has_filter_rule
            and not _is_technical_question(question.text)
        ):
            issues.append(
                Issue(
                    code="missing_question",
                    severity="critical",
                    title="Вопрос из Word не найден в ссылке",
                    expected=question.text,
                    question_id=question.id,
                )
            )

    for index, question in enumerate(observed.questions):
        if index in used_observed:
            continue
        if _is_technical_question(question.text):
            continue
        is_standard_demographic = _is_standard_platform_demographic(question.text)
        if allow_standard_demographics and is_standard_demographic:
            continue
        if question.type != QuestionType.INFO and (question.options or is_standard_demographic):
            previous_matches = [match for match in matches if match.observed_position < index]
            next_matches = [match for match in matches if match.observed_position > index]
            previous = max(previous_matches, key=lambda item: item.observed_position, default=None)
            following = min(next_matches, key=lambda item: item.observed_position, default=None)
            location_parts: list[str] = []
            if previous:
                location_parts.append(f"после {spec.questions[previous.expected_position].id}")
            if following:
                location_parts.append(f"перед {spec.questions[following.expected_position].id}")
            issues.append(
                Issue(
                    code="extra_question",
                    severity="major",
                    title="В ссылке найден лишний вопрос",
                    expected=(
                        f"Расположение относительно Word: {', '.join(location_parts)}"
                        if location_parts
                        else None
                    ),
                    actual=question.text,
                    evidence={"platform_id": question.platform_id},
                )
            )

    # SurveyStudio repeats product blocks for every tested concept.  Those
    # repetitions are legitimate, but a second, unrelated question carrying
    # the same questionnaire code is not.  Detect it by wording and report it
    # once even when it appears in both concept loops.
    if observed.platform.lower() == "surveystudio":
        reported_unexpected_texts: set[str] = set()
        for index, actual in enumerate(observed.questions):
            if index in used_observed or actual.raw_type != "inventory-only" or not actual.reference_code:
                continue
            family = [
                expected for expected in spec.questions
                if _reference_family_matches(expected.id, actual.reference_code)
            ]
            if not family:
                continue
            best_score = max(_question_text_score(expected.text, actual.text) for expected in family)
            if best_score >= 0.55:
                continue
            normalized_actual = normalize_text(actual.text)
            if not normalized_actual or normalized_actual in reported_unexpected_texts:
                continue
            reported_unexpected_texts.add(normalized_actual)
            previous_matches = [match for match in matches if match.observed_position < index]
            next_matches = [match for match in matches if match.observed_position > index]
            previous = max(previous_matches, key=lambda item: item.observed_position, default=None)
            following = min(next_matches, key=lambda item: item.observed_position, default=None)
            location_parts: list[str] = []
            if previous:
                location_parts.append(f"после {spec.questions[previous.expected_position].id}")
            if following:
                location_parts.append(f"перед {spec.questions[following.expected_position].id}")
            issues.append(
                Issue(
                    code="extra_question",
                    severity="major",
                    title="В ссылке найден лишний вопрос",
                    expected=(
                        f"Расположение относительно Word: {', '.join(location_parts)}"
                        if location_parts else None
                    ),
                    actual=actual.text,
                    evidence={
                        "platform_id": actual.platform_id,
                        "reference_code": actual.reference_code,
                    },
                )
            )

    piped_options = spec.metadata.get("piped_options", {})
    test_products = spec.metadata.get("test_products", [])
    for expected_position, expected in enumerate(spec.questions):
        match = by_expected_position.get(expected_position)
        if not match:
            continue
        actual = observed_by_id[match.observed_id]
        details_unavailable = actual.raw_type == "inventory-only"
        inventory_text_is_usable = actual.raw_type != "inventory-only" or match.score < 0.55
        if match.score < 0.86 and inventory_text_is_usable:
            issues.append(
                Issue(
                    code="question_text_mismatch",
                    severity="major" if match.score < 0.65 else "minor",
                    title="Формулировка вопроса отличается",
                    expected=expected.text,
                    actual=actual.text,
                    question_id=expected.id,
                    evidence={"similarity": match.score},
                )
            )
        age_text_number_pair = (
            {expected.type, actual.type} == {QuestionType.TEXT, QuestionType.NUMBER}
            and _is_age_question(f"{expected.text} {actual.text}")
        )
        if not details_unavailable and not age_text_number_pair and not _types_match(expected.type, actual.type):
            issues.append(
                Issue(
                    code="question_type_mismatch",
                    severity="major",
                    title="Тип вопроса не совпадает",
                    expected=expected.type.value,
                    actual=actual.type.value,
                    question_id=expected.id,
                )
            )
        if not details_unavailable:
            _compare_options(
                expected,
                actual,
                issues,
                allow_expected_subset=expected.id in piped_options,
                test_products=test_products,
            )
            _compare_matrix_rows(expected, actual, issues)
            _compare_rotation(expected, actual, issues)

    observed_positions = [match.observed_position for match in matches]
    if observed_positions != sorted(observed_positions):
        issues.append(
            Issue(
                code="question_order_mismatch",
                severity="major",
                title="Последовательность вопросов отличается от Word",
                expected="Порядок вопросов из Word",
                actual="Порядок вопросов в тестовой ссылке",
            )
        )

    return ComparisonReport(
        created_at=datetime.now(UTC).isoformat(),
        document_title=spec.title,
        platform=observed.platform,
        source_url=observed.source_url,
        expected_question_count=len(spec.questions),
        observed_question_count=len(observed.questions),
        matches=matches,
        issues=issues,
        parser_warnings=spec.warnings,
        platform_warnings=observed.warnings,
    )
