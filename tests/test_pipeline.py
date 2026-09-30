import unittest

from app.models import (
    AnswerOption,
    ObservedQuestion,
    ObservedSurvey,
    QuestionMatch,
    QuestionSpec,
    SourceLocation,
    SurveySpec,
)
from app.pipeline import _build_answer_code_map, adapter_for
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


if __name__ == "__main__":
    unittest.main()
