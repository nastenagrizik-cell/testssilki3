import unittest

from app.comparator import compare_surveys
from app.models import (
    AnswerOption,
    ConditionAtom,
    ConditionGroup,
    LogicRule,
    ObservedQuestion,
    ObservedSurvey,
    QuestionSpec,
    QuestionType,
    RotationSpec,
    SourceLocation,
    SurveySpec,
)


def expected_question(qid: str, text: str, options: list[AnswerOption]) -> QuestionSpec:
    return QuestionSpec(
        id=qid,
        text=text,
        type=QuestionType.SINGLE,
        options=options,
        source=SourceLocation(block_index=1, kind="paragraph"),
    )


class ComparatorTests(unittest.TestCase):
    def test_identical_surveys_have_no_issues(self):
        options = [AnswerOption(code="1", text="Да"), AnswerOption(code="2", text="Нет")]
        spec = SurveySpec(title="Тест", questions=[expected_question("Q1", "Вы согласны?", options)])
        observed = ObservedSurvey(
            platform="enjoysurvey",
            source_url="https://example.test/survey",
            questions=[
                ObservedQuestion(
                    platform_id="11",
                    text="Вы согласны?",
                    type=QuestionType.SINGLE,
                    options=options,
                )
            ],
        )

        report = compare_surveys(spec, observed)

        self.assertEqual(report.issues, [])

    def test_reports_missing_option_and_extra_question(self):
        spec = SurveySpec(
            title="Тест",
            questions=[
                expected_question(
                    "Q1",
                    "Выберите напиток",
                    [AnswerOption(code="1", text="Чай"), AnswerOption(code="2", text="Кофе")],
                )
            ],
        )
        observed = ObservedSurvey(
            platform="enjoysurvey",
            source_url="https://example.test/survey",
            questions=[
                ObservedQuestion(
                    platform_id="11",
                    text="Выберите напиток",
                    type=QuestionType.SINGLE,
                    options=[AnswerOption(code="1", text="Чай")],
                ),
                ObservedQuestion(
                    platform_id="12",
                    text="Лишний вопрос",
                    type=QuestionType.SINGLE,
                    options=[AnswerOption(code="1", text="Да")],
                ),
            ],
        )

        report = compare_surveys(spec, observed)
        codes = {issue.code for issue in report.issues}

        self.assertIn("missing_option", codes)
        self.assertIn("extra_question", codes)

    def test_reports_missing_and_unexpected_rotation(self):
        options = [
            AnswerOption(code="1", text="Первый"),
            AnswerOption(code="2", text="Второй"),
            AnswerOption(code="3", text="Третий"),
        ]
        rotating = expected_question("Q1", "Вопрос с ротацией", options)
        rotating.rotation = RotationSpec(enabled=True, axis="options")
        stable = expected_question("Q2", "Вопрос без ротации", options)
        spec = SurveySpec(title="Тест", questions=[rotating, stable])
        observed = ObservedSurvey(
            platform="enjoysurvey",
            source_url="https://example.test/survey",
            questions=[
                ObservedQuestion(
                    platform_id="11",
                    text="Вопрос с ротацией",
                    type=QuestionType.SINGLE,
                    options=options,
                    option_order_samples=[["1", "2", "3"], ["1", "2", "3"], ["1", "2", "3"]],
                ),
                ObservedQuestion(
                    platform_id="12",
                    text="Вопрос без ротации",
                    type=QuestionType.SINGLE,
                    options=options,
                    option_order_samples=[["1", "2", "3"], ["2", "1", "3"]],
                ),
            ],
        )

        codes = {issue.code for issue in compare_surveys(spec, observed).issues}

        self.assertIn("rotation_not_observed", codes)
        self.assertIn("unexpected_rotation", codes)

    def test_compares_matrix_rows(self):
        question = expected_question("Q1", "Оцените характеристики", [])
        question.type = QuestionType.MATRIX_SINGLE
        question.matrix_rows = [
            AnswerOption(code="1", text="Вкус"),
            AnswerOption(code="2", text="Внешний вид"),
        ]
        observed = ObservedSurvey(
            platform="enjoysurvey",
            source_url="https://example.test/survey",
            questions=[
                ObservedQuestion(
                    platform_id="11",
                    text="Оцените характеристики",
                    type=QuestionType.MATRIX_SINGLE,
                    matrix_rows=[AnswerOption(code="1", text="Вкус")],
                )
            ],
        )

        codes = {issue.code for issue in compare_surveys(SurveySpec(title="Тест", questions=[question]), observed).issues}

        self.assertIn("missing_matrix_row", codes)

    def test_reports_when_all_answer_options_are_absent(self):
        options = [AnswerOption(code="1", text="Да"), AnswerOption(code="2", text="Нет")]
        spec = SurveySpec(title="Тест", questions=[expected_question("Q1", "Вы согласны?", options)])
        observed = ObservedSurvey(
            platform="enjoysurvey",
            source_url="https://example.test/survey",
            questions=[ObservedQuestion(platform_id="11", text="Вы согласны?", type=QuestionType.SINGLE)],
        )

        codes = {issue.code for issue in compare_surveys(spec, observed).issues}

        self.assertIn("answer_options_missing", codes)

    def test_answer_codes_may_differ_when_wording_matches(self):
        expected_options = [AnswerOption(code="1", text="Да"), AnswerOption(code="2", text="Нет")]
        actual_options = [AnswerOption(code="10", text="Да"), AnswerOption(code="20", text="Нет")]
        spec = SurveySpec(title="Тест", questions=[expected_question("Q1", "Вы согласны?", expected_options)])
        observed = ObservedSurvey(
            platform="enjoysurvey",
            source_url="https://example.test/survey",
            questions=[
                ObservedQuestion(
                    platform_id="11",
                    text="Вы согласны?",
                    type=QuestionType.SINGLE,
                    options=actual_options,
                )
            ],
        )

        codes = {issue.code for issue in compare_surveys(spec, observed).issues}

        self.assertNotIn("option_code_mismatch", codes)

    def test_scale_number_prefix_is_not_a_wording_error_but_wrong_label_is(self):
        expected = expected_question(
            "Q1",
            "Оцените важность",
            [AnswerOption(code="5", text="Очень важно")],
        )
        observed = ObservedSurvey(
            platform="EnjoySurvey",
            source_url="https://example.test/survey",
            questions=[
                ObservedQuestion(
                    platform_id="1", text=expected.text, type=QuestionType.SINGLE,
                    options=[AnswerOption(code="5", text="5 - Очень важно")],
                )
            ],
        )
        self.assertEqual(compare_surveys(SurveySpec(title="Тест", questions=[expected]), observed).issues, [])

        observed.questions[0].options[0].text = "5 - Совсем не важно"
        codes = {issue.code for issue in compare_surveys(SurveySpec(title="Тест", questions=[expected]), observed).issues}
        self.assertTrue({"option_text_mismatch", "missing_option"} & codes)

    def test_filtered_question_is_checked_by_scenarios_not_marked_structurally_missing(self):
        rule = LogicRule(
            action="show",
            target_question_id="Q2",
            condition=ConditionGroup(atoms=[ConditionAtom(question_id="Q1", values=["5"])]),
            raw_text="Если Q1=5",
        )
        question = expected_question("Q2", "Почему Вам понравилось?", [])
        question.display_rules = [rule]
        report = compare_surveys(
            SurveySpec(title="Тест", questions=[question]),
            ObservedSurvey(platform="EnjoySurvey", source_url="https://example.test/survey"),
        )
        self.assertNotIn("missing_question", {issue.code for issue in report.issues})

        question.display_rules = [
            LogicRule(action="show", target_question_id="Q2", raw_text="Показать вопрос")
        ]
        report = compare_surveys(
            SurveySpec(title="Тест", questions=[question]),
            ObservedSurvey(platform="EnjoySurvey", source_url="https://example.test/survey"),
        )
        self.assertIn("missing_question", {issue.code for issue in report.issues})

    def test_standard_demographics_can_be_allowed_or_checked(self):
        spec = SurveySpec(title="Тест")
        observed = ObservedSurvey(
            platform="enjoysurvey",
            source_url="https://example.test/survey",
            questions=[
                ObservedQuestion(
                    platform_id="sex",
                    text="Укажите Ваш пол",
                    type=QuestionType.SINGLE,
                    options=[AnswerOption(code="1", text="Мужской")],
                ),
                ObservedQuestion(
                    platform_id="age",
                    text="Укажите Ваш возраст",
                    type=QuestionType.NUMBER,
                ),
            ],
        )

        allowed = compare_surveys(spec, observed, allow_standard_demographics=True)
        checked = compare_surveys(spec, observed, allow_standard_demographics=False)

        self.assertEqual(allowed.issues, [])
        self.assertEqual([issue.code for issue in checked.issues], ["extra_question", "extra_question"])

    def test_does_not_claim_options_are_missing_for_inventory_only_question(self):
        options = [AnswerOption(code="1", text="Да"), AnswerOption(code="2", text="Нет")]
        spec = SurveySpec(title="Тест", questions=[expected_question("Q1", "Вы согласны?", options)])
        observed = ObservedSurvey(
            platform="SurveyStudio",
            source_url="https://go.surveystudio.ru/test/example/start",
            questions=[
                ObservedQuestion(
                    platform_id="101",
                    text="Вы согласны?",
                    type=QuestionType.INFO,
                    raw_type="inventory-only",
                )
            ],
        )

        self.assertEqual(compare_surveys(spec, observed).issues, [])

    def test_ignores_technical_question_wording_and_truncated_platform_text(self):
        expected = expected_question(
            "S8",
            "Какие блюда Вы покупали? ПОКАЖИТЕ ЭКРАН. ВОЗМОЖНО НЕСКОЛЬКО ОТВЕТОВ.",
            [AnswerOption(code="1", text="Бургеры")],
        )
        observed = ObservedSurvey(
            platform="SurveyStudio",
            source_url="https://example.test/survey",
            questions=[
                ObservedQuestion(
                    platform_id="8",
                    reference_code="S8",
                    text="Какие блюда Вы покупали?",
                    type=QuestionType.SINGLE,
                    options=[AnswerOption(code="10", text="Бургеры")],
                )
            ],
        )
        self.assertEqual(compare_surveys(SurveySpec(title="Тест", questions=[expected]), observed).issues, [])

        expected.text = "Что Вам понравилось больше всего? Пожалуйста, опишите как можно более подробно."
        observed.questions[0].text = "Что Вам понравилось больше всег..."
        observed.questions[0].type = QuestionType.TEXT
        expected.type = QuestionType.TEXT
        expected.options = []
        self.assertNotIn(
            "question_text_mismatch",
            {issue.code for issue in compare_surveys(SurveySpec(title="Тест", questions=[expected]), observed).issues},
        )

    def test_reference_code_prevents_similar_questions_from_being_swapped(self):
        first = expected_question("S8", "Какие блюда Вы покупали?", [AnswerOption(code="1", text="Бургеры")])
        second = expected_question("S9", "Какие блюда Вы не будете покупать?", [AnswerOption(code="1", text="Бургеры")])
        observed = ObservedSurvey(
            platform="SurveyStudio",
            source_url="https://example.test/survey",
            questions=[
                ObservedQuestion(
                    platform_id="9", reference_code="S9", text=second.text,
                    type=QuestionType.SINGLE, options=second.options,
                ),
                ObservedQuestion(
                    platform_id="8", reference_code="S8", text=first.text,
                    type=QuestionType.SINGLE, options=first.options,
                ),
            ],
        )
        report = compare_surveys(SurveySpec(title="Тест", questions=[first, second]), observed)
        matched = {match.expected_id: match.observed_id for match in report.matches}
        self.assertEqual(matched, {"S8": "8", "S9": "9"})

    def test_surveystudio_reference_uses_product_suffix_and_does_not_hide_missing_question(self):
        b18 = expected_question("B18", "Насколько напиток отличается от других?", [])
        b20 = expected_question("B20", "Насколько напиток подходит ресторану?", [])
        observed = ObservedSurvey(
            platform="SurveyStudio",
            source_url="https://example.test/survey",
            questions=[
                ObservedQuestion(
                    platform_id="18195", reference_code="B18.195",
                    text="Насколько напиток отличается от других?", type=QuestionType.SINGLE,
                ),
                ObservedQuestion(
                    platform_id="18349", reference_code="B18.349",
                    text="Насколько напиток отличается от других?", type=QuestionType.SINGLE,
                ),
            ],
        )

        report = compare_surveys(SurveySpec(title="Тест", questions=[b18, b20]), observed)

        self.assertEqual(
            [issue.question_id for issue in report.issues if issue.code == "missing_question"],
            ["B20"],
        )

    def test_reports_unrelated_duplicate_inside_each_product_block_once(self):
        expected = expected_question("B16.3", "Насколько выражена кислинка?", [])
        observed = ObservedSurvey(
            platform="SurveyStudio",
            source_url="https://example.test/survey",
            questions=[
                ObservedQuestion(
                    platform_id="wrong195", reference_code="B16.3.195",
                    text="Насколько Вам удобна трубочка?", type=QuestionType.INFO,
                    raw_type="inventory-only",
                ),
                ObservedQuestion(
                    platform_id="right195", reference_code="B16.3.195",
                    text=expected.text, type=QuestionType.INFO, raw_type="inventory-only",
                ),
                ObservedQuestion(
                    platform_id="wrong349", reference_code="B16.3.349",
                    text="Насколько Вам удобна трубочка?", type=QuestionType.INFO,
                    raw_type="inventory-only",
                ),
                ObservedQuestion(
                    platform_id="right349", reference_code="B16.3.349",
                    text=expected.text, type=QuestionType.INFO, raw_type="inventory-only",
                ),
            ],
        )

        report = compare_surveys(SurveySpec(title="Тест", questions=[expected]), observed)

        extras = [issue for issue in report.issues if issue.code == "extra_question"]
        self.assertEqual(len(extras), 1)
        self.assertIn("трубочка", extras[0].actual)

    def test_inventory_truncation_with_matching_reference_is_not_a_wording_error(self):
        expected = expected_question(
            "F10",
            "Вы отметили, что чаще всего посещаете ресторан Вкусно и Точка / Rostic’s (ВЫВЕСТИ В ЗАВИСИМОСТИ ОТ КОДА, ВЫБРАННОГО В S6).",
            [],
        )
        observed = ObservedSurvey(
            platform="SurveyStudio",
            source_url="https://example.test/survey",
            questions=[
                ObservedQuestion(
                    platform_id="10195", reference_code="F10.195",
                    text="Вы отметили, что чаще всего посещаете ресторан Вкусно и Точка. Если в меню р...",
                    type=QuestionType.INFO, raw_type="inventory-only",
                )
            ],
        )
        codes = {
            issue.code
            for issue in compare_surveys(SurveySpec(title="Тест", questions=[expected]), observed).issues
        }
        self.assertNotIn("question_text_mismatch", codes)

    def test_allows_age_text_number_single_exclusivity_and_dynamic_product_labels(self):
        age = expected_question("S2", "Укажите Ваш возраст", [])
        age.type = QuestionType.TEXT
        product = expected_question(
            "M1",
            "Какой напиток вкуснее?",
            [
                AnswerOption(code="1", text="Первый молочный коктейль – код ___"),
                AnswerOption(code="2", text="Второй молочный коктейль – код ___"),
            ],
        )
        exclusive = expected_question(
            "S9",
            "Что Вы не купите?",
            [AnswerOption(code="99", text="Ничего из перечисленного", exclusive=True)],
        )
        spec = SurveySpec(
            title="Тест",
            questions=[age, product, exclusive],
            metadata={
                "test_products": [
                    {"name": "Шоколадный милкшейк БК", "code": "Напиток 195"},
                    {"name": "Шоколадный милкшейк БК (2.0)", "code": "Напиток 349"},
                ]
            },
        )
        observed = ObservedSurvey(
            platform="SurveyStudio",
            source_url="https://example.test/survey",
            questions=[
                ObservedQuestion(platform_id="2", reference_code="S2", text=age.text, type=QuestionType.NUMBER),
                ObservedQuestion(
                    platform_id="3", reference_code="M1", text=product.text, type=QuestionType.SINGLE,
                    options=[
                        AnswerOption(code="10", text="Шоколадный милкшейк БК 349"),
                        AnswerOption(code="20", text="Шоколадный милкшейк БК 195"),
                    ],
                ),
                ObservedQuestion(
                    platform_id="4", reference_code="S9", text=exclusive.text, type=QuestionType.SINGLE,
                    options=[AnswerOption(code="99", text="Ничего из перечисленного")],
                ),
            ],
        )
        self.assertEqual(compare_surveys(spec, observed).issues, [])

    def test_ignores_known_technical_screens(self):
        spec = SurveySpec(title="Тест")
        observed = ObservedSurvey(
            platform="SurveyStudio",
            source_url="https://example.test/survey",
            questions=[
                ObservedQuestion(
                    platform_id="tech", text="Порядок концепций", type=QuestionType.SINGLE,
                    options=[AnswerOption(code="1", text="195–349")],
                )
            ],
        )
        self.assertEqual(compare_surveys(spec, observed).issues, [])


if __name__ == "__main__":
    unittest.main()
