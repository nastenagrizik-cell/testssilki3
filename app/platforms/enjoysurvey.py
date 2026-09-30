from __future__ import annotations

import os
import re
from urllib.parse import parse_qs, urlparse

from ..models import (
    AnswerOption,
    ObservedQuestion,
    ObservedSurvey,
    QuestionMatch,
    QuestionType,
    ScenarioResult,
    TestScenario,
)
from ..normalization import text_similarity
from .base import PlatformAdapter


TYPE_MAP = {
    "type_multi": QuestionType.MULTI,
    "type_group": QuestionType.SINGLE,
    "type_single": QuestionType.SINGLE,
    "type_text": QuestionType.TEXT,
    "type_number": QuestionType.NUMBER,
    "type_rank": QuestionType.RANKING,
    "type_ranking": QuestionType.RANKING,
    "type_info": QuestionType.INFO,
    "type_grid": QuestionType.MATRIX_SINGLE,
}

LEAF_QUESTION_SELECTOR = (
    '.question-container[data-question-id]:not(:has(.question-container[data-question-id]))'
)


EXTRACT_QUESTIONS_JS = r"""
() => Array.from(document.querySelectorAll('.question-container[data-question-id]'))
  .filter(q => !q.querySelector('.question-container[data-question-id]'))
  .map((q, index) => {
  const classes = Array.from(q.classList);
  const rawType = classes.find(name => name.startsWith('type_')) || null;
  const titleNodes = Array.from(q.querySelectorAll('[data-testid="header-title"], .question-header'));
  const titleNode = titleNodes.find(node => ((node.innerText || node.textContent || '').trim()));
  const rawTitle = ((titleNode && (titleNode.innerText || titleNode.textContent)) || '').trim();
  const titleText = rawTitle.split(/\\n+/).map(line => line.trim()).filter(line =>
    line && !/^(Выберите|Оцените|Ответьте|Укажите минимум|Укажите не более)/i.test(line)
  ).join(' ');
  const options = [];
  const seen = new Set();
  for (const item of q.querySelectorAll('[data-value-code]')) {
    const code = item.getAttribute('data-value-code') || '';
    const textNode = item.querySelector('[data-testid="value"]') || item;
    const text = (textNode.innerText || textNode.textContent || '').trim();
    const key = code + '\u0000' + text;
    if (!text || seen.has(key)) continue;
    seen.add(key);
    options.push({
      code,
      text,
      exclusive: Boolean(item.querySelector('.exclusive') || item.classList.contains('exclusive')),
      fixed: false,
    });
  }
  const rowNodes = Array.from(q.querySelectorAll('.es__answers-row, .grid-row-tr.row-is-simple'));
  const rows = rowNodes.length;
  const matrix_rows = rowNodes.map((row, rowIndex) => {
    const code = row.getAttribute('data-row-code') || row.getAttribute('data-value-code');
    const label = row.querySelector('.es__answers-row-title, .es__answers-row-label, [data-testid="row-title"], .first-td-label');
    const text = ((label && (label.innerText || label.textContent)) || '').trim();
    return {code: code || String(rowIndex + 1), text, exclusive: false, fixed: false};
  }).filter(row => row.text);
  const row_order = matrix_rows.map(row => row.code);
  return {
    platform_id: q.getAttribute('data-question-id') || q.id || String(index),
    reference_code: ((rawTitle.match(/^([A-Za-zА-Яа-яЁё]{1,5}\d+(?:[._]\d+|[A-Za-zА-Яа-яЁё]+)*)/) || [])[1] || null),
    text: titleText,
    raw_type: rawType,
    options,
    rows,
    matrix_rows,
    row_order,
  };
})
"""


class EnjoySurveyAdapter(PlatformAdapter):
    name = "EnjoySurvey"

    def __init__(self, headless: bool = True, timeout_ms: int = 30_000, rotation_probe_runs: int = 3):
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
        return hostname.endswith("enjoysurvey.ru") or hostname.endswith("enjoysurvey.com")

    @staticmethod
    def _question_type(raw_type: str | None, rows: int, option_count: int) -> QuestionType:
        if rows > 1 and raw_type == "type_group":
            return QuestionType.MATRIX_SINGLE
        if raw_type in TYPE_MAP:
            return TYPE_MAP[raw_type]
        if option_count:
            return QuestionType.SINGLE
        return QuestionType.INFO

    def _convert(self, payload: dict, position: int) -> ObservedQuestion:
        options = [AnswerOption.model_validate(option) for option in payload.get("options", [])]
        matrix_rows = [AnswerOption.model_validate(option) for option in payload.get("matrix_rows", [])]
        return ObservedQuestion(
            platform_id=str(payload.get("platform_id", position)),
            reference_code=payload.get("reference_code"),
            text=payload.get("text", ""),
            type=self._question_type(payload.get("raw_type"), payload.get("rows", 0), len(options)),
            raw_type=payload.get("raw_type"),
            options=options,
            matrix_rows=matrix_rows,
            position=position,
        )

    async def _discover_once(self, browser, url: str) -> tuple[list[dict], list[str]]:
        payloads_by_id: dict[str, dict] = {}
        sort_keys: dict[str, tuple[int, int]] = {}
        warnings: list[str] = []
        seen: set[str] = set()
        context = await browser.new_context(locale="ru-RU")
        page = await context.new_page()
        page.set_default_timeout(self.timeout_ms)
        try:
            response = await page.goto(url, wait_until="domcontentloaded")
            if response and response.status >= 400:
                raise RuntimeError(f"EnjoySurvey вернул HTTP {response.status}; проверьте, что тестовая ссылка ещё активна")
            if "/user/login" in page.url:
                raise RuntimeError(
                    "EnjoySurvey перенаправил на страницу входа. "
                    "Тестовая ссылка истекла, закрыта или требует авторизацию."
                )
            try:
                await page.wait_for_selector('.question-container[data-question-id]', state="attached")
            except Exception as exc:
                title = await page.title()
                raise RuntimeError(
                    f"На странице не найден экран анкеты (заголовок: {title or 'без заголовка'})"
                ) from exc
            initial_payloads = await page.evaluate(EXTRACT_QUESTIONS_JS)
            for payload in initial_payloads:
                platform_id = str(payload.get("platform_id", ""))
                if platform_id:
                    seen.add(platform_id)
                    payloads_by_id[platform_id] = payload
            initial_ids = {str(payload.get("platform_id", "")) for payload in initial_payloads}

            # On the newer n.enjoysurvey.com frontend the questionnaire is
            # painted first and the test navigation panel is mounted shortly
            # afterwards.  Checking immediately made filtered questions look
            # absent even though the panel appeared a fraction of a second
            # later.
            try:
                await page.wait_for_selector(
                    "#fastMoveSelect",
                    state="attached",
                    timeout=min(self.timeout_ms, 5_000),
                )
            except Exception:
                pass
            fast_move = page.locator('#fastMoveSelect')
            if await fast_move.count():
                options = await fast_move.locator('option').evaluate_all(
                    "els => els.map(o => ({value: o.value, text: (o.textContent || '').trim(), disabled: o.disabled}))"
                )
                direct_positions = {
                    str(option.get("value")): option_index
                    for option_index, option in enumerate(options)
                    if option.get("value")
                }
                for platform_id in initial_ids:
                    if platform_id in direct_positions:
                        sort_keys[platform_id] = (direct_positions[platform_id], 0)
                hide_panel = "_hide_panel_group_" in parse_qs(urlparse(url).query)
                reached_survey = not hide_panel
                option_entries = list(enumerate(options))
                # The new test frontend can retain the last debug screen for
                # the shared token. Walking backwards leaves it near the start
                # so subsequent logic scenarios can move forward reliably.
                if not hide_panel:
                    option_entries.reverse()
                for option_index, option in option_entries:
                    value = option.get("value")
                    if hide_panel and value in initial_ids:
                        reached_survey = True
                    if not reached_survey or not value or option.get("disabled"):
                        continue
                    try:
                        before = await page.locator(LEAF_QUESTION_SELECTOR).evaluate_all(
                            "els => els.map(q => q.getAttribute('data-question-id')).join('|')"
                        )
                        await fast_move.select_option(value, timeout=min(self.timeout_ms, 3_000))
                        try:
                            await page.wait_for_function(
                                "before => Array.from(document.querySelectorAll('.question-container[data-question-id]')).filter(q => !q.querySelector('.question-container[data-question-id]')).map(q => q.getAttribute('data-question-id')).join('|') !== before",
                                arg=before,
                                timeout=min(self.timeout_ms, 3_000),
                            )
                        except Exception:
                            # Some menu entries are group aliases or point to
                            # the already open screen.  Reading the resulting
                            # DOM is more reliable than requiring equal IDs.
                            pass
                        # EnjoySurvey replaces a screen in two DOM phases.  A
                        # short settle delay prevents reading the intermediate
                        # empty/partial phase after the first mutation.
                        await page.wait_for_timeout(400)
                        payloads = await page.evaluate(EXTRACT_QUESTIONS_JS)
                        for payload_index, payload in enumerate(payloads):
                            platform_id = str(payload.get("platform_id", ""))
                            if platform_id:
                                sort_keys.setdefault(platform_id, (option_index, payload_index))
                            if platform_id and platform_id not in seen:
                                seen.add(platform_id)
                                payloads_by_id[platform_id] = payload
                    except Exception as exc:  # one broken jump should not cancel the whole audit
                        if "ГРУППА ВОПРОСОВ" not in str(option.get("text", "")).upper():
                            warnings.append(f"Пункт быстрого перехода {value} недоступен: {type(exc).__name__}")
            else:
                payloads = await page.evaluate(EXTRACT_QUESTIONS_JS)
                for payload in payloads:
                    platform_id = str(payload.get("platform_id", ""))
                    if platform_id and platform_id not in seen:
                        seen.add(platform_id)
                        payloads_by_id[platform_id] = payload
                warnings.append("Панель быстрого перехода не найдена; получен только текущий экран.")
        finally:
            await context.close()
        insertion_order = {platform_id: index for index, platform_id in enumerate(payloads_by_id)}
        payloads = sorted(
            payloads_by_id.values(),
            key=lambda payload: sort_keys.get(
                str(payload.get("platform_id", "")),
                (len(sort_keys) + 1, insertion_order.get(str(payload.get("platform_id", "")), 0)),
            ),
        )
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
                        option_samples.setdefault(platform_id, []).append(
                            [str(option.get("code", "")) for option in payload.get("options", [])]
                        )
                        row_samples.setdefault(platform_id, []).append(
                            [str(value) for value in payload.get("row_order", [])]
                        )
                        if not any(question.platform_id == platform_id for question in questions):
                            questions.append(self._convert(payload, len(questions)))
                    if run_order:
                        question_order_samples.append(run_order)
            finally:
                await browser.close()
        for question in questions:
            question.option_order_samples = option_samples.get(question.platform_id, [])
            question.row_order_samples = row_samples.get(question.platform_id, [])
        return ObservedSurvey(
            platform=self.name,
            source_url=url,
            questions=questions,
            question_order_samples=question_order_samples,
            warnings=list(dict.fromkeys(warnings)),
        )

    async def _click_answer_items(self, items, selected_codes: list[str], minimum: int = 1) -> int:
        targets = []
        if selected_codes:
            for code in selected_codes:
                targets.append(items.locator(f'[data-value-code="{code}"]').first)
        else:
            non_exclusive = items.locator('[data-value-code]:not(:has(.exclusive))')
            source = non_exclusive if await non_exclusive.count() else items.locator('[data-value-code]')
            targets.extend(source.nth(index) for index in range(min(minimum, await source.count())))
        clicked = 0
        for target in targets:
            if await target.count() and await target.is_visible():
                click_target = target.locator('[data-testid="value"]').first
                await (click_target if await click_target.count() else target).click()
                clicked += 1
        return clicked

    async def _answer_container(
        self,
        container,
        expected_id: str | None,
        assignments: dict[str, list[str]],
        answer_code_map: dict[str, dict[str, str]],
    ) -> None:
        expected_codes = assignments.get(expected_id or "", [])
        code_map = answer_code_map.get(expected_id or "", {})
        selected_codes = [code_map.get(code, code) for code in expected_codes]
        rows = container.locator('.es__answers-row, .grid-row-tr.row-is-simple')
        if await rows.count():
            for row_index in range(await rows.count()):
                row = rows.nth(row_index)
                if await row.locator('[data-value-code]').count():
                    await self._click_answer_items(row, selected_codes, minimum=1)
            return

        answer_items = container.locator('[data-value-code]')
        if await answer_items.count():
            text = (await container.inner_text()).upper().replace("Ё", "Е")
            minimum_match = re.search(r"УКАЖИТЕ\s+МИНИМУМ\s+(\d+)", text)
            minimum = int(minimum_match.group(1)) if minimum_match else 1
            is_ranking = await container.evaluate(
                "el => el.classList.contains('type_rank') || el.classList.contains('type_ranking')"
            )
            if is_ranking and not selected_codes:
                minimum = await answer_items.count()
            await self._click_answer_items(container, selected_codes, minimum=minimum)
            return

        text_fields = container.locator(
            'textarea:visible, input[type="text"]:visible, input[type="number"]:visible, input[type="range"]:visible'
        )
        for index in range(await text_fields.count()):
            field = text_fields.nth(index)
            field_type = await field.get_attribute("type")
            if field_type == "range":
                minimum = float(await field.get_attribute("min") or 0)
                maximum = float(await field.get_attribute("max") or 100)
                await field.fill(str((minimum + maximum) / 2))
            else:
                await field.fill("1" if field_type == "number" else "Тестовый ответ")

        selects = container.locator('select:visible')
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

        platform_to_expected = {match.observed_id: match.expected_id for match in matches}
        visited_platform: list[str] = []
        visited_expected: list[str] = []
        observed_questions: list[ObservedQuestion] = []
        warnings: list[str] = []
        completed = False
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**self._launch_options())
            context = await browser.new_context(locale="ru-RU")
            page = await context.new_page()
            page.set_default_timeout(self.timeout_ms)
            try:
                response = await page.goto(url, wait_until="domcontentloaded")
                if response and response.status >= 400:
                    raise RuntimeError(f"EnjoySurvey вернул HTTP {response.status}")
                if "/user/login" in page.url:
                    raise RuntimeError(
                        "EnjoySurvey перенаправил на страницу входа; тестовая ссылка недоступна"
                    )
                await page.wait_for_selector(
                    LEAF_QUESTION_SELECTOR,
                    state="attached",
                    timeout=self.timeout_ms,
                )
                # Test links can remember the last debug jump made during the
                # structural inventory pass.  Start every scenario from the
                # earliest matched questionnaire question instead of whatever
                # screen the shared test token happened to retain.
                fast_move = page.locator("#fastMoveSelect")
                if await fast_move.count():
                    start_candidates = await fast_move.locator("option").evaluate_all(
                        "options => options.map(option => ({value: option.value, disabled: option.disabled})).filter(option => option.value && !option.disabled)"
                    )
                    if start_candidates:
                        start_id = str(start_candidates[0]["value"])
                        before_start = await page.locator(LEAF_QUESTION_SELECTOR).evaluate_all(
                            "els => els.map(q => q.getAttribute('data-question-id')).join('|')"
                        )
                        await fast_move.select_option(start_id)
                        try:
                            await page.wait_for_function(
                                "before => Array.from(document.querySelectorAll('.question-container[data-question-id]')).filter(q => !q.querySelector('.question-container[data-question-id]')).map(q => q.getAttribute('data-question-id')).join('|') !== before",
                                arg=before_start,
                                timeout=min(self.timeout_ms, 8_000),
                            )
                        except Exception:
                            pass
                        await page.wait_for_timeout(400)
                for _ in range(180):
                    containers = page.locator(LEAF_QUESTION_SELECTOR)
                    if not await containers.count():
                        completed = True
                        break
                    current_ids: list[str] = []
                    screen_payloads = await page.evaluate(EXTRACT_QUESTIONS_JS)
                    for payload in screen_payloads:
                        platform_id = str(payload.get("platform_id", ""))
                        if platform_id and not any(item.platform_id == platform_id for item in observed_questions):
                            observed_questions.append(self._convert(payload, len(observed_questions)))
                    for index in range(await containers.count()):
                        container = containers.nth(index)
                        platform_id = await container.get_attribute('data-question-id') or f"screen-{index}"
                        current_ids.append(platform_id)
                        if platform_id not in visited_platform:
                            visited_platform.append(platform_id)
                        expected_id = platform_to_expected.get(platform_id)
                        if not expected_id and scenario.expected_texts:
                            title_nodes = container.locator('[data-testid="header-title"], .question-header')
                            title = ""
                            for title_index in range(await title_nodes.count()):
                                candidate = (await title_nodes.nth(title_index).inner_text()).strip()
                                if candidate:
                                    title = candidate
                                    break
                            if not title:
                                title = (await container.inner_text()).split("\n", 1)[0].strip()
                            ranked = sorted(
                                (
                                    (text_similarity(title, expected_text), candidate_id)
                                    for candidate_id, expected_text in scenario.expected_texts.items()
                                ),
                                reverse=True,
                            )
                            if ranked and ranked[0][0] >= 0.58:
                                expected_id = ranked[0][1]
                        if expected_id and expected_id not in visited_expected:
                            visited_expected.append(expected_id)
                        await self._answer_container(
                            container,
                            expected_id,
                            scenario.assignments,
                            answer_code_map or {},
                        )
                    next_button = page.locator('#nextSurvey, [data-testid="next"], button:has-text("Вперед"), button:has-text("Далее")').first
                    if not await next_button.count() or not await next_button.is_visible():
                        completed = True
                        break
                    before = await containers.evaluate_all(
                        "els => els.map(q => (q.getAttribute('data-question-id') || '') + ':' + (q.innerText || '')).join('|')"
                    )
                    await next_button.click()
                    try:
                        await page.wait_for_function(
                            "before => Array.from(document.querySelectorAll('.question-container[data-question-id]')).filter(q => !q.querySelector('.question-container[data-question-id]')).map(q => (q.getAttribute('data-question-id') || '') + ':' + (q.innerText || '')).join('|') !== before",
                            arg=before,
                            timeout=min(self.timeout_ms, 8_000),
                        )
                        # The SPA briefly removes all question nodes before
                        # mounting the next screen.  Wait for the second phase
                        # so that a transient empty DOM is not treated as the
                        # end of the questionnaire.
                        try:
                            await page.wait_for_selector(
                                LEAF_QUESTION_SELECTOR,
                                state="attached",
                                timeout=min(self.timeout_ms, 4_000),
                            )
                        except Exception:
                            if "enjoysurvey" not in page.url:
                                completed = True
                                break
                            raise
                        await page.wait_for_timeout(300)
                    except Exception:
                        if not await page.locator(LEAF_QUESTION_SELECTOR).count():
                            completed = True
                            break
                        warnings.append("Экран не изменился после нажатия «Далее».")
                        break
            except Exception as exc:
                warnings.append(f"Прохождение остановлено: {type(exc).__name__}: {exc}")
            finally:
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
