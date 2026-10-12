import unittest

from app.models import (
    AnswerOption,
    ConditionAtom,
    ConditionGroup,
    LogicRule,
    ObservedQuestion,
    ObservedSurvey,
    QuestionMatch,
    QuestionSpec,
    QuestionType,
    ScenarioResult,
    SourceLocation,
    SurveySpec,
)
from app.pipeline import _build_answer_code_map, _merge_scenario_observations, adapter_for
from app.platforms.enjoysurvey import EnjoySurveyAdapter
from app.platforms.surveystudio import SurveyStudioAdapter


class PipelineTests(unittest.TestCase):
    def test_maps_word_codes_to_platform_codes_by_text(self):
        spec = SurveySpec(
            title="Тест",
            questions=[
                QuestionSpec(
                    id="Q1",
                    text="Выберите",
                    options=[
                        AnswerOption(code="1", text="Первый вариант"),
                        AnswerOption(code="2", text="Второй вариант"),
                    ],
                    source=SourceLocation(block_index=1, kind="paragraph"),
                )
            ],
        )
        observed = ObservedSurvey(
            platform="test",
            source_url="https://example.test",
            questions=[
                ObservedQuestion(
                    platform_id="900",
                    text="Выберите",
                    options=[
                        AnswerOption(code="20", text="Второй вариант"),
                        AnswerOption(code="10", text="Первый вариант"),
                    ],
                )
            ],
        )
        matches = [
            QuestionMatch(
                expected_id="Q1",
                observed_id="900",
                score=1,
                expected_position=0,
                observed_position=0,
            )
        ]

        self.assertEqual(_build_answer_code_map(spec, observed, matches), {"Q1": {"1": "10", "2": "20"}})

    def test_selects_surveystudio_adapter(self):
        adapter = adapter_for("https://go.surveystudio.ru/test/token/start")
        self.assertIsInstance(adapter, SurveyStudioAdapter)

    def test_enjoy_info_wrapper_with_open_field_is_converted_to_text(self):
        adapter = EnjoySurveyAdapter()
        question = adapter._convert(
            {
                "platform_id": "102",
                "text": "Что Вам понравилось?",
                "raw_type": "type_text",
                "dom_raw_type": "type_info",
                "has_text_field": True,
                "options": [],
                "rows": 0,
                "matrix_rows": [],
            },
            0,
        )

        self.assertEqual(question.type, QuestionType.TEXT)

    def test_maps_screening_code_from_label_in_logic_instruction(self):
        question = QuestionSpec(
            id="S3",
            text="Какие блюда Вы покупали?",
            options=[],
            source=SourceLocation(block_index=1, kind="paragraph"),
        )
        spec = SurveySpec(
            title="Тест",
            questions=[question],
            rules=[
                LogicRule(
                    action="continue",
                    raw_text="Продолжить, если S3 = 5 (бургеры с говядиной)",
                    condition=ConditionGroup(
                        atoms=[ConditionAtom(question_id="S3", values=["5"])]
                    ),
                )
            ],
        )
        observed = ObservedSurvey(
            platform="EnjoySurvey",
            source_url="https://example.test",
            questions=[
                ObservedQuestion(
                    platform_id="900",
                    text=question.text,
                    options=[
                        AnswerOption(code="internal-17", text="Бургеры с курицей"),
                        AnswerOption(code="internal-42", text="Бургеры с говядиной"),
                    ],
                )
            ],
        )
        matches = [
            QuestionMatch(
                expected_id="S3",
                observed_id="900",
                score=1,
                expected_position=0,
                observed_position=0,
            )
        ]

        self.assertEqual(
            _build_answer_code_map(spec, observed, matches),
            {"S3": {"5": "internal-42"}},
        )

    def test_filtered_runtime_questions_are_inserted_between_real_anchors(self):
        observed = ObservedSurvey(
            platform="EnjoySurvey",
            source_url="https://example.test",
            questions=[
                ObservedQuestion(platform_id="1", text="Q1", position=0),
                ObservedQuestion(platform_id="4", text="Q4", position=1),
            ],
        )
        runtime = ScenarioResult(
            scenario_id="route-1",
            completed=True,
            observed_questions=[
                ObservedQuestion(platform_id="1", text="Q1"),
                ObservedQuestion(platform_id="2", text="Q2", type=QuestionType.TEXT),
                ObservedQuestion(platform_id="3", text="Q3", type=QuestionType.TEXT),
                ObservedQuestion(platform_id="4", text="Q4"),
            ],
        )

        self.assertTrue(_merge_scenario_observations(observed, [runtime]))
        self.assertEqual(
            [question.platform_id for question in observed.questions],
            ["1", "2", "3", "4"],
        )
        self.assertEqual([question.position for question in observed.questions], [0, 1, 2, 3])


if __name__ == "__main__":
    unittest.main()
