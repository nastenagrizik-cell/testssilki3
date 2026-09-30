import tempfile
import unittest
from pathlib import Path

from docx import Document

from app.docx_parser import DocxSurveyParser
from app.models import QuestionType


class DocxParserTests(unittest.TestCase):
    def _build_sample(self, path: Path) -> None:
        document = Document()
        document.add_heading("Тестовая анкета", level=1)
        document.add_paragraph("S1. Какие продукты вы покупали?")
        document.add_paragraph("Несколько ответов")
        table = document.add_table(rows=1, cols=2)
        table.rows[0].cells[0].text = "1"
        table.rows[0].cells[1].text = "Бургеры"
        row = table.add_row()
        row.cells[0].text = "99"
        row.cells[1].text = "Ничего из перечисленного"
        document.add_paragraph("Код 99 — исключающий")
        document.add_paragraph("Q1. Оцените идею")
        document.add_paragraph("Один ответ")
        table = document.add_table(rows=1, cols=2)
        table.rows[0].cells[0].text = "1"
        table.rows[0].cells[1].text = "Не нравится"
        row = table.add_row()
        row.cells[0].text = "5"
        row.cells[1].text = "Очень нравится"
        document.add_paragraph("Q2. Почему идея вам нравится?")
        document.add_paragraph("Открытый ответ")
        document.add_paragraph("Показать Q2, если в Q1 выбран код 5")
        document.save(path)

    def test_reads_questions_options_types_and_logic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.docx"
            self._build_sample(path)
            spec = DocxSurveyParser().parse(path)

        self.assertEqual([question.id for question in spec.questions], ["S1", "Q1", "Q2"])
        self.assertEqual(spec.questions[0].type, QuestionType.MULTI)
        self.assertTrue(spec.questions[0].options[-1].exclusive)
        self.assertEqual(spec.questions[2].type, QuestionType.TEXT)
        self.assertEqual(len(spec.questions[2].display_rules), 1)
        self.assertEqual(spec.questions[2].display_rules[0].condition.atoms[0].values, ["5"])

    def test_reads_uncoded_paragraph_options_and_ignores_formula(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plain-options.docx"
            document = Document()
            document.add_paragraph("S1. Выберите ресторан")
            document.add_paragraph("Один ответ")
            document.add_paragraph("Бургер Кинг")
            document.add_paragraph("Другой ресторан")
            document.add_paragraph("E5. При какой цене блюдо покажется дорогим?")
            document.add_paragraph("E4 < E5 < E2")
            document.save(path)

            spec = DocxSurveyParser().parse(path)

        self.assertEqual([option.code for option in spec.questions[0].options], ["1", "2"])
        self.assertEqual(spec.questions[0].type, QuestionType.SINGLE)
        self.assertEqual(spec.questions[1].type, QuestionType.NUMBER)
        self.assertEqual(spec.questions[1].options, [])

    def test_infers_previous_question_for_code_only_screening_rule(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "screening.docx"
            document = Document()
            document.add_paragraph("S1. Выберите ресторан")
            document.add_paragraph("Один ответ")
            document.add_paragraph("Бургер Кинг")
            document.add_paragraph("Другой ресторан")
            document.add_paragraph("Закончить интервью, если не выбран код 1")
            document.save(path)

            spec = DocxSurveyParser().parse(path)

        rule = next(rule for rule in spec.rules if rule.action == "terminate")
        self.assertEqual(rule.condition.atoms[0].question_id, "S1")
        self.assertEqual(rule.condition.atoms[0].operator, "not_in")
        self.assertEqual(rule.condition.atoms[0].values, ["1"])

    def test_ignores_full_and_partial_strikethrough(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strikethrough.docx"
            document = Document()
            question = document.add_paragraph()
            question.add_run("Q1. Какой ")
            removed = question.add_run("бургер ")
            removed.font.strike = True
            question.add_run("ролл Вам нравится?")
            document.add_paragraph("Один ответ")
            table = document.add_table(rows=1, cols=2)
            table.rows[0].cells[0].text = "1"
            table.rows[0].cells[1].text = "Чикен ролл"
            row = table.add_row()
            row.cells[0].text = "2"
            removed_option = row.cells[1].paragraphs[0].add_run("Воппер")
            removed_option.font.strike = True

            removed_rule = document.add_paragraph()
            rule_run = removed_rule.add_run("Показать Q3, если в Q1 выбран код 1")
            rule_run.font.strike = True

            removed_question = document.add_paragraph()
            removed_question_run = removed_question.add_run("Q2. Удалённый вопрос?")
            removed_question_run.font.strike = True
            document.add_paragraph("Открытый ответ")
            document.add_paragraph("Этот текст относился к удалённому вопросу")
            document.add_paragraph("Q3. Активный вопрос?")
            document.add_paragraph("Открытый ответ")
            document.save(path)

            spec = DocxSurveyParser().parse(path)

        self.assertEqual([item.id for item in spec.questions], ["Q1", "Q3"])
        self.assertEqual(spec.questions[0].text, "Какой ролл Вам нравится?")
        self.assertEqual([item.text for item in spec.questions[0].options], ["Чикен ролл"])
        self.assertEqual(spec.rules, [])
        self.assertNotIn("Этот текст относился", " ".join(spec.questions[0].instructions))

    def test_reads_shared_answer_tables_and_inline_last_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shared-tables.docx"
            document = Document()
            document.add_paragraph("S5. Какие марки Вы покупали?")
            document.add_paragraph("Несколько ответов")
            document.add_paragraph("Для S6 показать марки, отмеченные в S5")
            document.add_paragraph("S6. Какую марку Вы покупаете чаще всего?")
            document.add_paragraph("Один ответ")
            document.add_paragraph("S7. Какие марки Вы знаете?")
            brands = document.add_table(rows=1, cols=5)
            for index, value in enumerate(["", "", "S5", "S6", "S7"]):
                brands.rows[0].cells[index].text = value
            for code, brand in [("1", "Burger King"), ("2", "Ростикс")]:
                row = brands.add_row()
                for index, value in enumerate([code, brand, code, code, code]):
                    row.cells[index].text = value

            document.add_paragraph("S8. Что Вы покупали?")
            document.add_paragraph("S9. Что Вы не будете покупать?")
            products = document.add_table(rows=1, cols=3)
            for index, value in enumerate(["", "S8", "S9"]):
                products.rows[0].cells[index].text = value
            row = products.add_row()
            for index, value in enumerate(["Бургеры", "1", "1"]):
                row.cells[index].text = value
            row = products.add_row()
            for index, value in enumerate(["Ничего из перечисленного", "98", "98"]):
                row.cells[index].text = value
            row = products.add_row()
            for index, value in enumerate(
                [
                    "s8: Ничего из перечисленного не покупал(а) s9: Ничего из перечисленного не буду покупать",
                    "-",
                    "99",
                ]
            ):
                row.cells[index].text = value
            document.save(path)

            spec = DocxSurveyParser().parse(path)

        by_id = {question.id: question for question in spec.questions}
        self.assertEqual([option.text for option in by_id["S5"].options], ["Burger King", "Ростикс"])
        self.assertEqual([option.text for option in by_id["S6"].options], ["Burger King", "Ростикс"])
        self.assertEqual([option.text for option in by_id["S7"].options], ["Burger King", "Ростикс"])
        self.assertEqual(
            [(option.code, option.text) for option in by_id["S8"].options],
            [("1", "Бургеры"), ("98", "Ничего из перечисленного не покупал(а)")],
        )
        self.assertEqual(
            [(option.code, option.text) for option in by_id["S9"].options],
            [("1", "Бургеры"), ("98", "Ничего из перечисленного не буду покупать")],
        )
        self.assertEqual(spec.metadata["piped_options"], {"S6": "S5"})
        self.assertEqual([question.id for question in spec.questions].count("S8"), 1)

    def test_ignores_rotation_and_product_mapping_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "technical-tables.docx"
            document = Document()
            document.add_paragraph("S11. Вы согласны попробовать напитки?")
            document.add_paragraph("Один ответ")
            answers = document.add_table(rows=1, cols=2)
            answers.rows[0].cells[0].text = "1"
            answers.rows[0].cells[1].text = "Да"
            row = answers.add_row()
            row.cells[0].text = "2"
            row.cells[1].text = "Нет"
            document.add_paragraph("СУПЕРВАЙЗЕР, ОТМЕТЬТЕ РОТАЦИЮ")
            rotation = document.add_table(rows=1, cols=2)
            rotation.rows[0].cells[0].text = "1"
            rotation.rows[0].cells[1].text = "195 – 349"
            row = rotation.add_row()
            row.cells[0].text = "2"
            row.cells[1].text = "349 – 195"

            document.add_paragraph("P10. Как часто Вы покупаете напиток?")
            document.add_paragraph("Один ответ")
            frequency = document.add_table(rows=1, cols=2)
            frequency.rows[0].cells[0].text = "1"
            frequency.rows[0].cells[1].text = "Раз в неделю"
            row = frequency.add_row()
            row.cells[0].text = "2"
            row.cells[1].text = "Реже"
            products = document.add_table(rows=1, cols=3)
            for index, value in enumerate(["F4", "Тестируемые продукты", "Код в программе"]):
                products.rows[0].cells[index].text = value
            row = products.add_row()
            for index, value in enumerate(["1", "Шоколадный милкшейк БК", "Напиток 195"]):
                row.cells[index].text = value
            document.save(path)

            spec = DocxSurveyParser().parse(path)

        by_id = {question.id: question for question in spec.questions}
        self.assertEqual([option.text for option in by_id["S11"].options], ["Да", "Нет"])
        self.assertEqual([option.text for option in by_id["P10"].options], ["Раз в неделю", "Реже"])
        self.assertEqual(
            spec.metadata["test_products"],
            [{"name": "Шоколадный милкшейк БК", "code": "Напиток 195"}],
        )

    def test_explicit_one_answer_wins_over_option_wording(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "one-answer.docx"
            document = Document()
            document.add_paragraph("A1_2. Как часто Вы покупаете напитки?")
            document.add_paragraph("ОДИН ОТВЕТ")
            table = document.add_table(rows=1, cols=2)
            table.rows[0].cells[0].text = "1"
            table.rows[0].cells[1].text = "Несколько раз в неделю"
            row = table.add_row()
            row.cells[0].text = "2"
            row.cells[1].text = "Несколько раз в месяц"
            document.save(path)

            spec = DocxSurveyParser().parse(path)

        self.assertEqual(spec.questions[0].type, QuestionType.SINGLE)


if __name__ == "__main__":
    unittest.main()
