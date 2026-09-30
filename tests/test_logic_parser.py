import unittest

from app.logic_parser import parse_logic_rule, parse_logic_rules, parse_rotation


class LogicParserTests(unittest.TestCase):
    def test_parses_show_rule_with_code_range(self):
        rules = parse_logic_rules("Показать Q2, если в Q1 выбраны коды 3-5")

        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].action, "show")
        self.assertEqual(rules[0].target_question_id, "Q2")
        self.assertEqual(rules[0].condition.atoms[0].question_id, "Q1")
        self.assertEqual(rules[0].condition.atoms[0].values, ["3", "4", "5"])

    def test_splits_continue_and_terminate_instructions(self):
        rules = parse_logic_rules(
            "Продолжить, если в S1 выбраны коды 1-5, иначе завершить интервью"
        )

        self.assertEqual([rule.action for rule in rules], ["continue", "terminate"])
        self.assertEqual(rules[0].condition.atoms[0].values, ["1", "2", "3", "4", "5"])

    def test_parses_rotation_and_fixed_codes(self):
        rotation = parse_rotation("Ротация кодов 1-6, кроме кода 6")

        self.assertTrue(rotation.enabled)
        self.assertEqual(rotation.axis, "options")
        self.assertIn("6", rotation.fixed_codes)

    def test_allows_dot_before_condition_operator(self):
        rule = parse_logic_rule("Если Q6.2.=1-3")

        self.assertEqual(rule.condition.atoms[0].question_id, "Q6.2")
        self.assertEqual(rule.condition.atoms[0].values, ["1", "2", "3"])

    def test_does_not_treat_natural_if_wording_as_logic(self):
        rule = parse_logic_rule("Если продукт появится в меню, как изменится ваше посещение?")

        self.assertIsNone(rule)


if __name__ == "__main__":
    unittest.main()
