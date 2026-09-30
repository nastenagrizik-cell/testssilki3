from __future__ import annotations

import os
import re
from html import unescape
from urllib.parse import urlparse

from ..models import (
    AnswerOption,
    ObservedQuestion,
    ObservedSurvey,
    QuestionMatch,
    QuestionType,
    ScenarioResult,
    TestScenario,
)
from ..normalization import normalize_space, text_similarity
from .base import PlatformAdapter


TYPE_MAP = {
    "single-choice": QuestionType.SINGLE,
    "multiple-choice": QuestionType.MULTI,
    "multi-choice": QuestionType.MULTI,
    "text": QuestionType.TEXT,
    "number": QuestionType.NUMBER,
    "numeric": QuestionType.NUMBER,
    "ranking": QuestionType.RANKING,
    "information": QuestionType.INFO,
    "info": QuestionType.INFO,
    "matrix-single-choice": QuestionType.MATRIX_SINGLE,
    "matrix-multiple-choice": QuestionType.MATRIX_MULTI,
}


EXTRACT_QUESTION_JS = r"""
() => {
  const q = document.querySelector('[data-testid="question-content"]');
  if (!q) return null;
  const title = (q.querySelector('[data-testid="question-number"]')?.innerText || '').trim();
  const technical = (title.split('/')[0] || '').trim();
  let text = (q.querySelector('[data-testid="question-text"]')?.innerText || '').trim();
  const lines = text.split(/\n+/).map(value => value.trim()).filter(Boolean);
  if (lines.length > 1 && technical && lines[0].toLowerCase() === technical.toLowerCase()) {
    lines.shift();
  }
  text = lines.join(' ');
  const options = [];
  const seen = new Set();
  for (const answer of q.querySelectorAll('[data-part="answer"][data-answer-code]')) {
    const code = answer.getAttribute('data-answer-code') || '';
    const label = answer.querySelector('[data-part="answer-text"]');
    const answerText = ((label && (label.innerText || label.textContent)) || '').trim();
    const key = code + '\u0000' + answerText;
    if (!answerText || seen.has(key)) continue;
    seen.add(key);
    options.push({code, text: answerText, exclusive: false, fixed: false});
  }
  const matrix_rows = [];
  const rowSeen = new Set();
  const rowSelectors = '[data-part="matrix-row"], [data-testid="matrix-row"], [data-row-code]';
  for (const [index, row] of Array.from(q.querySelectorAll(rowSelectors)).entries()) {
    const label = row.querySelector('[data-part="row-text"], [data-testid="row-text"], [data-part="question-text"]');
    const rowText = ((label && (label.innerText || label.textContent)) || '').trim();
    const code = row.getAttribute('data-row-code') || String(index + 1);
    if (rowText && !rowSeen.has(code + '\u0000' + rowText)) {
      rowSeen.add(code + '\u0000' + rowText);
      matrix_rows.push({code, text: rowText, exclusive: false, fixed: false});
    }
  }
  const positionMatch = title.match(/\((\d+)\s+из\s+(\d+)\)/i);
  return {
    platform_id: q.getAttribute('data-question-number') || technical || title,
    reference_code: technical || ((title.match(/^([A-Za-zА-Яа-яЁё]{1,5}\d+(?:[._]\d+|[A-Za-zА-Яа-яЁё]+)*)/) || [])[1] || null),
    text,
    title,
    raw_type: q.getAttribute('data-question-type-name') || '',
    options,
    matrix_rows,
    position: positionMatch ? Number(positionMatch[1]) - 1 : 0,
    option_order: options.map(option => option.code),
    row_order: matrix_rows.map(row => row.code),
  };
}
"""


DISCOVER_API_JS = r"""
async ({surveyKey}) => {
  const warnings = [];
  const createSession = async () => {
    const response = await fetch('/api/v1/sessions', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({surveyKey, timeZone: 'Europe/Moscow', isTesting: true}),
    });
    if (!response.ok) throw new Error(`Не удалось создать тестовую сессию: HTTP ${response.status}`);
    const created = await response.json();
    return {
      base: `/api/v1/sessions/${created.sessionKey}`,
      headers: {'Content-Type': 'application/json', Authorization: `Bearer ${created.accessToken}`},
    };
  };
  const readQuestion = async (session, item) => {
    const jump = await fetch(`${session.base}/navigation-commands`, {
      method: 'POST', headers: session.headers,
      body: JSON.stringify({command: 8, questionNumber: Number(item.id)}),
    });
    if (!jump.ok) throw new Error(`переход HTTP ${jump.status}`);
    const stateResponse = await fetch(session.base, {headers: session.headers});
    if (!stateResponse.ok) throw new Error(`чтение HTTP ${stateResponse.status}`);
    const state = await stateResponse.json();
    const question = (state.questions || []).find(q => String(q.number) === String(item.id));
    if (!question) throw new Error('платформа не открыла выбранный вопрос');
    return question;
  };
  let session = await createSession();
  const listResponse = await fetch(`${session.base}/questions`, {headers: session.headers});
  if (!listResponse.ok) throw new Error(`Не удалось получить список вопросов: HTTP ${listResponse.status}`);
  const listing = await listResponse.json();
  const payloads = [];
  for (const [position, item] of (listing.questionsList || []).entries()) {
    try {
      let question;
      try {
        question = await readQuestion(session, item);
      } catch (_) {
        session = await createSession();
        question = await readQuestion(session, item);
      }
      payloads.push({question, position, listId: item.id, listContent: item.content || ''});
    } catch (error) {
      warnings.push(`Не удалось прочитать вопрос ${item.id}: ${error.message || error}`);
    }
  }
  return {payloads, warnings, inventory: listing.questionsList || []};
}
"""


class SurveyStudioAdapter(PlatformAdapter):
    name = "SurveyStudio"

    def __init__(self, headless: bool = True, timeout_ms: int = 30_000, rotation_probe_runs: int = 2):
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.rotation_probe_runs = max(1, rotation_probe_runs)
        self.browser_executable_path = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None

    def _launch_options(self) -> dict:
        options: dict = {"headless": self.headless, "args": ["--no-sandbox"]}
        if self.browser_executable_path:
            options["executable_path"] = self.browser_executable_path
        return options

    @classmethod
    def supports(cls, url: str) -> bool:
        hostname = (urlparse(url).hostname or "").lower()
        return hostname == "surveystudio.ru" or hostname.endswith(".surveystudio.ru")

    @staticmethod
    def _question_type(raw_type: str, option_count: int, row_count: int) -> QuestionType:
        normalized = raw_type.lower().strip()
        if normalized in TYPE_MAP:
            return TYPE_MAP[normalized]
        if "matrix" in normalized:
            return QuestionType.MATRIX_MULTI if "multi" in normalized else QuestionType.MATRIX_SINGLE
        if row_count:
            return QuestionType.MATRIX_SINGLE
        if option_count:
            return QuestionType.SINGLE
        return QuestionType.INFO

    def _convert(self, payload: dict, fallback_position: int) -> ObservedQuestion:
        options = [AnswerOption.model_validate(option) for option in payload.get("options", [])]
        rows = [AnswerOption.model_validate(row) for row in payload.get("matrix_rows", [])]
        raw_type = str(payload.get("raw_type", ""))
        return ObservedQuestion(
            platform_id=str(payload.get("platform_id", fallback_position)),
            reference_code=payload.get("reference_code"),
            text=normalize_space(str(payload.get("text", ""))),
            type=self._question_type(raw_type, len(options), len(rows)),
            raw_type=raw_type,
            options=options,
            matrix_rows=rows,
            position=int(payload.get("position", fallback_position)),
        )

    @staticmethod
    def _html_text(value: str | None) -> str:
        value = value or ""
        value = re.sub(r"<(?:br|/p|/div)\s*/?>", " ", value, flags=re.I)
        value = re.sub(r"<[^>]+>", "", value)
        return normalize_space(unescape(value))

    def _api_payload(self, item: dict) -> dict:
        question = item.get("question") or {}
        data = question.get("data") or {}
        title = normalize_space(str(question.get("title") or ""))
        technical = normalize_space(title.split("/", 1)[0]) if "/" in title else ""
        reference_match = re.match(r"([A-Za-zА-Яа-яЁё]{1,5}\d+(?:[._]\d+|[A-Za-zА-Яа-яЁё]+)*)", title)
        reference_code = technical or (reference_match.group(1) if reference_match else None)
        text = self._html_text(question.get("text"))
        if technical and text.lower().startswith(technical.lower()):
            text = normalize_space(text[len(technical):])

        options: list[dict] = []

        def add_options(values) -> None:
            for option in values or []:
                option_text = self._html_text(option.get("content"))
                if option_text:
                    flags = option.get("answerFlags") or {}
                    options.append(
                        {
                            "code": str(option.get("code", "")),
                            "text": option_text,
                            "exclusive": bool(flags.get("blocking")),
                            "fixed": bool(flags.get("disableReordering")),
                        }
                    )
                add_options(option.get("children"))

        add_options(data.get("optionsData"))
        table = data.get("tableData") or {}
        rows_source = table.get("rows") or table.get("rowData") or table.get("rowsData") or []
        columns_source = table.get("columns") or table.get("columnData") or table.get("columnsData") or []
        matrix_rows = [
            {
                "code": str(row.get("code", index + 1)),
                "text": self._html_text(row.get("content") or row.get("text") or row.get("title")),
                "exclusive": False,
                "fixed": False,
            }
            for index, row in enumerate(rows_source)
            if self._html_text(row.get("content") or row.get("text") or row.get("title"))
        ]
        if not options and columns_source:
            add_options(columns_source)

        raw_type = f"api:{question.get('type', '')}"
        api_type = question.get("type")
        rules = question.get("rules") or {}
        if matrix_rows:
            inferred_type = "matrix-multiple-choice" if "MaxAnswerCount" not in rules else "matrix-single-choice"
        elif api_type == 4:
            inferred_type = "single-choice"
        elif api_type == 5:
            inferred_type = "multiple-choice"
        elif api_type == 2:
            inferred_type = "text"
        elif api_type == 3:
            inferred_type = "number"
        elif options:
            maximum = (rules.get("MaxAnswerCount") or {}).get("value")
            inferred_type = "single-choice" if maximum == 1 else "multiple-choice"
        elif data.get("plainData") is not None:
            label = normalize_space(str((data.get("plainData") or {}).get("label") or "")).lower()
            inferred_type = "number" if any(word in label for word in ("чис", "number", "цифр")) else "text"
        else:
            inferred_type = "information"
        flags = question.get("questionFlags") or {}
        return {
            "platform_id": str(question.get("number") or item.get("listId") or ""),
            "reference_code": reference_code,
            "text": text or self._html_text(item.get("listContent")),
            "title": title,
            "raw_type": inferred_type or raw_type,
            "options": options,
            "matrix_rows": matrix_rows,
            "position": int(item.get("position", 0)),
            "option_order": [option["code"] for option in options],
            "row_order": [row["code"] for row in matrix_rows],
            "rotation_declared": bool(flags.get("randomizeAnswers") or flags.get("rotateAnswers")),
        }

    async def _open(self, browser, url: str):
        context = await browser.new_context(locale="ru-RU")
        page = await context.new_page()
        page.set_default_timeout(self.timeout_ms)
        response = await page.goto(url, wait_until="domcontentloaded")
        if response and response.status >= 400:
            await context.close()
            raise RuntimeError(f"SurveyStudio вернул HTTP {response.status}; проверьте тестовую ссылку")
        try:
            await page.wait_for_selector('[data-testid="question-content"]', state="attached")
        except Exception as exc:
            title = await page.title()
            await context.close()
            raise RuntimeError(
                f"На странице SurveyStudio не найден экран анкеты (заголовок: {title or 'без заголовка'})"
            ) from exc
        return context, page

    async def _goto_inventory(self, page) -> list[dict[str, str]]:
        await page.locator('[data-testid="survey-goto-question-btn"]').click()
        dialog = page.locator('[role="dialog"]').first
        await dialog.locator('[aria-haspopup="listbox"]').click()
        await page.wait_for_selector('[role="option"]', state="attached")
        await page.wait_for_timeout(200)
        options = page.locator('[role="option"]')
        labels: list[str] = []
        seen_labels: set[str] = set()
        for _ in range(40):
            visible_labels = await options.evaluate_all(
                "items => items.map(item => (item.innerText || item.textContent || '').trim())"
            )
            for label in visible_labels:
                if label and label not in seen_labels:
                    seen_labels.add(label)
                    labels.append(label)
            moved = await page.locator('[role="listbox"]').evaluate(
                """root => {
                  const candidates = [root, ...root.querySelectorAll('*')]
                    .filter(item => item.scrollHeight > item.clientHeight + 4);
                  let changed = false;
                  for (const item of candidates) {
                    const before = item.scrollTop;
                    item.scrollTop = Math.min(item.scrollHeight, before + Math.max(item.clientHeight * 0.9, 180));
                    if (item.scrollTop !== before) changed = true;
                  }
                  return changed;
                }"""
            )
            if not moved:
                break
            await page.wait_for_timeout(80)
        inventory: list[dict[str, str]] = []
        seen_platform_ids: set[str] = set()
        for label_value in labels:
            label = normalize_space(label_value)
            match = re.search(r"(?:/\s*)?Q(\d+)\s*:", label, flags=re.I)
            platform_id = match.group(1) if match else ""
            if not platform_id or platform_id in seen_platform_ids:
                continue
            seen_platform_ids.add(platform_id)
            inventory.append({"platform_id": platform_id, "label": label})
        await page.keyboard.press("Escape")
        return inventory

    async def _jump_to(self, page, index: int, expected_platform_id: str) -> None:
        current = page.locator('[data-testid="question-content"]')
        if await current.get_attribute("data-question-number") == expected_platform_id:
            return
        await page.locator('[data-testid="survey-goto-question-btn"]').click()
        dialog = page.locator('[role="dialog"]').first
        await dialog.locator('[aria-haspopup="listbox"]').click()
        await page.wait_for_selector('[role="option"]', state="attached")
        option = page.locator('[role="option"]').nth(index)
        await option.scroll_into_view_if_needed()
        await option.click()
        await page.get_by_role("button", name="Перейти", exact=True).click()
        if expected_platform_id:
            await page.wait_for_function(
                "value => document.querySelector('[data-testid=question-content]')?.getAttribute('data-question-number') === value",
                arg=expected_platform_id,
                timeout=min(self.timeout_ms, 8_000),
            )
        await page.wait_for_timeout(150)

    async def _discover_once(self, browser, url: str) -> tuple[list[dict], list[str]]:
        warnings: list[str] = []
        payloads: list[dict] = []
        context, page = await self._open(browser, url)
        try:
            ui_inventory = await self._goto_inventory(page)
            survey_key_match = re.search(r"/(?:test/)?([^/?#]+)(?:/start)?(?:[?#]|$)", urlparse(url).path)
            survey_key = survey_key_match.group(1) if survey_key_match else ""
            if not survey_key:
                raise RuntimeError("Не удалось определить ключ анкеты SurveyStudio из ссылки")
            result = await page.evaluate(DISCOVER_API_JS, {"surveyKey": survey_key})
            api_payloads = [self._api_payload(item) for item in result.get("payloads", [])]
            api_by_id: dict[str, list[dict]] = {}
            for payload in api_payloads:
                api_by_id.setdefault(str(payload.get("platform_id", "")), []).append(payload)
            fallback_count = 0
            for position, item in enumerate(ui_inventory):
                platform_id = str(item.get("platform_id", ""))
                if not platform_id:
                    continue
                available = api_by_id.get(platform_id, [])
                if available:
                    payload = dict(available.pop(0))
                    payload["position"] = position
                    payloads.append(payload)
                    continue
                fallback_count += 1
                content = normalize_space(str(item.get("label", "")))
                reference_match = re.match(
                    r"([A-Za-zА-Яа-яЁё]{1,5}\d+(?:[._]\d+|[A-Za-zА-Яа-яЁё]+)*)\s*(?:/|:)", content
                )
                text = re.sub(r"^(?:[^:/]+\s*/\s*)?Q\d+\s*:\s*", "", content, flags=re.I)
                payloads.append(
                    {
                        "platform_id": platform_id,
                        "reference_code": reference_match.group(1) if reference_match else None,
                        "text": text,
                        "title": content,
                        "raw_type": "inventory-only",
                        "options": [],
                        "matrix_rows": [],
                        "position": position,
                        "option_order": [],
                        "row_order": [],
                    }
                )
            unavailable_count = fallback_count
            if unavailable_count:
                warnings.append(
                    f"Для {unavailable_count} вопросов SurveyStudio прочитана формулировка, "
                    "но варианты ответа недоступны без прохождения соответствующей ветки."
                )
        finally:
            await context.close()
        payloads.sort(key=lambda item: int(item.get("position", 0)))
        seen_ids: dict[str, int] = {}
        for payload in payloads:
            platform_id = str(payload.get("platform_id", ""))
            seen_ids[platform_id] = seen_ids.get(platform_id, 0) + 1
            if seen_ids[platform_id] > 1:
                payload["platform_id"] = f"{platform_id}@{int(payload.get('position', 0)) + 1}"
        return payloads, warnings

    async def discover(self, url: str) -> ObservedSurvey:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("Playwright не установлен. Выполните pip install -r requirements.txt") from exc

        questions: list[ObservedQuestion] = []
        warnings: list[str] = []
        option_samples: dict[str, list[list[str]]] = {}
        row_samples: dict[str, list[list[str]]] = {}
        question_order_samples: list[list[str]] = []
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**self._launch_options())
            try:
                for _ in range(self.rotation_probe_runs):
                    payloads, run_warnings = await self._discover_once(browser, url)
                    warnings.extend(run_warnings)
                    run_order: list[str] = []
                    for payload in payloads:
                        platform_id = str(payload.get("platform_id", ""))
                        if not platform_id:
                            continue
                        run_order.append(platform_id)
                        option_samples.setdefault(platform_id, []).append(payload.get("option_order", []))
                        row_samples.setdefault(platform_id, []).append(payload.get("row_order", []))
                        if not any(question.platform_id == platform_id for question in questions):
                            questions.append(self._convert(payload, len(questions)))
                    if run_order:
                        question_order_samples.append(run_order)
            finally:
                await browser.close()
        questions.sort(key=lambda question: question.position)
        for position, question in enumerate(questions):
            question.position = position
            question.option_order_samples = option_samples.get(question.platform_id, [])
            question.row_order_samples = row_samples.get(question.platform_id, [])
        return ObservedSurvey(
            platform=self.name,
            source_url=url,
            questions=questions,
            question_order_samples=question_order_samples,
            warnings=list(dict.fromkeys(warnings)),
        )

    async def _answer_question(
        self,
        question,
        expected_id: str | None,
        assignments: dict[str, list[str]],
        answer_code_map: dict[str, dict[str, str]],
    ) -> None:
        expected_codes = assignments.get(expected_id or "", [])
        code_map = answer_code_map.get(expected_id or "", {})
        selected_codes = [code_map.get(code, code) for code in expected_codes]
        answers = question.locator('[data-part="answer"][data-answer-code]')
        if await answers.count():
            if selected_codes:
                targets = [question.locator(f'[data-part="answer"][data-answer-code="{code}"]').first for code in selected_codes]
            else:
                targets = [answers.first]
            for target in targets:
                if await target.count() and await target.is_visible():
                    await target.click()
            return

        fields = question.locator('textarea:visible, input:not([type="hidden"]):not([type="radio"]):not([type="checkbox"]):visible')
        question_text = (await question.inner_text()).lower()
        for index in range(await fields.count()):
            field = fields.nth(index)
            field_type = (await field.get_attribute("type") or "text").lower()
            if field_type == "file":
                continue
            if field_type == "range":
                minimum = float(await field.get_attribute("min") or 0)
                maximum = float(await field.get_attribute("max") or 100)
                await field.fill(str((minimum + maximum) / 2))
            else:
                if field_type == "tel" or "телефон" in question_text:
                    value = "79991234567"
                elif field_type == "number":
                    value = "25" if "возраст" in question_text or "полных лет" in question_text else "1"
                else:
                    value = "Тестовый ответ"
                await field.fill(value)

        selects = question.locator('select:visible')
        for index in range(await selects.count()):
            select = selects.nth(index)
            values = await select.locator('option:not([disabled])').evaluate_all(
                "options => options.map(option => option.value).filter(Boolean)"
            )
            if values:
                await select.select_option(values[0])

    async def run_scenario(
        self,
        url: str,
        scenario: TestScenario,
        matches: list[QuestionMatch],
        answer_code_map: dict[str, dict[str, str]] | None = None,
    ) -> ScenarioResult:
        from playwright.async_api import async_playwright

        platform_to_expected: dict[str, str] = {}
        for match in matches:
            platform_to_expected[match.observed_id] = match.expected_id
            platform_to_expected.setdefault(match.observed_id.split("@", 1)[0], match.expected_id)
        visited_platform: list[str] = []
        visited_expected: list[str] = []
        observed_questions: list[ObservedQuestion] = []
        warnings: list[str] = []
        completed = False
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**self._launch_options())
            context = None
            try:
                context, page = await self._open(browser, url)
                for _ in range(240):
                    question = page.locator('[data-testid="question-content"]').first
                    if not await question.count():
                        completed = True
                        break
                    platform_id = await question.get_attribute("data-question-number") or ""
                    payload = await page.evaluate(EXTRACT_QUESTION_JS)
                    if payload and not any(item.platform_id == platform_id for item in observed_questions):
                        observed_questions.append(self._convert(payload, len(observed_questions)))
                    if platform_id and platform_id not in visited_platform:
                        visited_platform.append(platform_id)
                    expected_id = platform_to_expected.get(platform_id)
                    if not expected_id and scenario.expected_texts:
                        question_text = str((payload or {}).get("text", ""))
                        ranked = sorted(
                            (
                                (text_similarity(question_text, expected_text), candidate_id)
                                for candidate_id, expected_text in scenario.expected_texts.items()
                            ),
                            reverse=True,
                        )
                        if ranked and ranked[0][0] >= 0.58:
                            expected_id = ranked[0][1]
                    if expected_id and expected_id not in visited_expected:
                        visited_expected.append(expected_id)
                    await self._answer_question(
                        question,
                        expected_id,
                        scenario.assignments,
                        answer_code_map or {},
                    )
                    next_button = page.locator('[data-testid="survey-next-btn"]').first
                    if not await next_button.count() or not await next_button.is_visible():
                        completed = True
                        break
                    before = platform_id
                    await next_button.click()
                    try:
                        await page.wait_for_function(
                            "before => { const q = document.querySelector('[data-testid=question-content]'); return !q || q.getAttribute('data-question-number') !== before; }",
                            arg=before,
                            timeout=min(self.timeout_ms, 8_000),
                        )
                        await page.wait_for_timeout(150)
                    except Exception:
                        if not await page.locator('[data-testid="question-content"]').count():
                            completed = True
                            break
                        error = page.locator('[data-testid="question-main-error"]')
                        detail = normalize_space(await error.inner_text()) if await error.count() else "экран не изменился"
                        warnings.append(f"Прохождение остановлено: {detail}")
                        break
            except Exception as exc:
                warnings.append(f"Прохождение остановлено: {type(exc).__name__}: {exc}")
            finally:
                if context:
                    await context.close()
                await browser.close()
        return ScenarioResult(
            scenario_id=scenario.id,
            completed=completed,
            visited_platform_ids=visited_platform,
            visited_expected_ids=visited_expected,
            observed_questions=observed_questions,
            warnings=warnings,
        )
