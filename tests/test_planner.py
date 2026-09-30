import unittest

from app.models import (
    ConditionAtom,
    ConditionGroup,
    LogicRule,
    QuestionSpec,
    SourceLocation,
    SurveySpec,
)
from app.planner import build_scenarios


class PlannerTests(unittest.TestCase):
    def test_creates_positive_and_negative_scenarios(self):
        rule = LogicRule(
            action="show",
            target_question_id="Q2",
            condition=ConditionGroup(
                atoms=[ConditionAtom(question_id="Q1", operator="in", values=["3", "4", "5"])]
            ),
            raw_text="Показать Q2, если Q1=3-5",
        )
        question = QuestionSpec(
            id="Q2",
            text="Почему?",
            display_rules=[rule],
            source=SourceLocation(block_index=2, kind="paragraph"),
        )
        scenarios = build_scenarios(SurveySpec(title="Тест", questions=[question]))

        self.assertEqual(len(scenarios), 2)
        self.assertEqual(scenarios[0].assignments["Q1"], ["3"])
        self.assertIn("Q2", scenarios[0].expected_visible)
        self.assertNotIn(scenarios[1].assignments["Q1"][0], {"3", "4", "5"})
        self.assertIn("Q2", scenarios[1].expected_hidden)

    def test_terminate_rule_does_not_override_safe_continue_value(self):
        continue_rule = LogicRule(
            action="continue",
            condition=ConditionGroup(atoms=[ConditionAtom(question_id="S3", values=["5"])]),
            raw_text="Продолжить, если S3=5",
        )
        terminate_rule = LogicRule(
            action="terminate",
            condition=ConditionGroup(atoms=[ConditionAtom(question_id="S3", values=["98"])]),
            raw_text="Завершить, если S3=98",
        )
        show_rule = LogicRule(
            action="show",
            target_question_id="Q2",
            condition=ConditionGroup(atoms=[ConditionAtom(question_id="Q1", values=["1"])]),
            raw_text="Показать Q2, если Q1=1",
        )
        question = QuestionSpec(
            id="Q2",
            text="Почему?",
            display_rules=[show_rule],
            source=SourceLocation(block_index=2, kind="paragraph"),
        )
        scenarios = build_scenarios(
            SurveySpec(title="Тест", questions=[question], rules=[continue_rule, terminate_rule])
        )

        self.assertEqual(scenarios[0].assignments["S3"], ["5"])

    def test_checks_every_atom_of_and_condition(self):
        rule = LogicRule(
            action="show",
            target_question_id="Q3",
            condition=ConditionGroup(
                combinator="and",
                atoms=[
                    ConditionAtom(question_id="Q1", values=["1"]),
                    ConditionAtom(question_id="Q2", operator="not_in", values=["9"]),
                ],
            ),
            raw_text="Показать Q3, если Q1=1 и Q2≠9",
        )
        question = QuestionSpec(
            id="Q3",
            text="Почему?",
            display_rules=[rule],
            source=SourceLocation(block_index=2, kind="paragraph"),
        )

        scenarios = build_scenarios(SurveySpec(title="Тест", questions=[question]))

        self.assertEqual(len(scenarios), 3)
        self.assertEqual(scenarios[0].expected_visible, ["Q3"])
        self.assertEqual(scenarios[1].expected_hidden, ["Q3"])
        self.assertEqual(scenarios[2].expected_hidden, ["Q3"])
        self.assertNotEqual(scenarios[0].assignments["Q1"], scenarios[1].assignments["Q1"])
        self.assertNotEqual(scenarios[0].assignments["Q2"], scenarios[2].assignments["Q2"])

    def test_hide_rule_reverses_visibility_expectation(self):
        rule = LogicRule(
            action="hide",
            target_question_id="Q2",
            condition=ConditionGroup(atoms=[ConditionAtom(question_id="Q1", values=["1"])]),
            raw_text="Не показывать Q2, если Q1=1",
        )
        question = QuestionSpec(
            id="Q2",
            text="Почему?",
            display_rules=[rule],
            source=SourceLocation(block_index=2, kind="paragraph"),
        )

        scenarios = build_scenarios(SurveySpec(title="Тест", questions=[question]))

        self.assertEqual(scenarios[0].expected_hidden, ["Q2"])
        self.assertEqual(scenarios[1].expected_visible, ["Q2"])

    def test_consolidates_checks_that_follow_the_same_path(self):
        rule_q2 = LogicRule(
            action="show",
            target_question_id="Q2",
            condition=ConditionGroup(atoms=[ConditionAtom(question_id="Q1", values=["1"])]),
            raw_text="Показать Q2, если Q1=1",
        )
        rule_q3 = LogicRule(
            action="show",
            target_question_id="Q3",
            condition=ConditionGroup(atoms=[ConditionAtom(question_id="Q1", values=["1"])]),
            raw_text="Показать Q3, если Q1=1",
        )
        questions = [
            QuestionSpec(
                id="Q2", text="Почему?", display_rules=[rule_q2],
                source=SourceLocation(block_index=2, kind="paragraph"),
            ),
            QuestionSpec(
                id="Q3", text="Расскажите подробнее", display_rules=[rule_q3],
                source=SourceLocation(block_index=3, kind="paragraph"),
            ),
        ]

        scenarios = build_scenarios(SurveySpec(title="Тест", questions=questions))

        self.assertEqual(len(scenarios), 2)
        self.assertEqual(set(scenarios[0].expected_visible), {"Q2", "Q3"})
        self.assertEqual(set(scenarios[1].expected_hidden), {"Q2", "Q3"})


if __name__ == "__main__":
    unittest.main()
