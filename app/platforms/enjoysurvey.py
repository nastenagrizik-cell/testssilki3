from __future__ import annotations

import os
import re
import hashlib
from pathlib import PurePosixPath
from urllib.parse import parse_qs, unquote, urlparse

from ..models import (
    AnswerOption,
    ConceptRun,
    ObservedQuestion,
    ObservedSurvey,
    QuestionMatch,
    QuestionType,
    ScenarioResult,
    SurveySpec,
    TestScenario,
)
from ..normalization import normalize_space, text_similarity
from .base import PlatformAdapter


TYPE_MAP = {
    "type_multi": QuestionType.MULTI,
    "type_group": QuestionType.SINGLE,
    "type_single": QuestionType.SINGLE,
    "type_text": QuestionType.TEXT,
    # Enjoy names ordinary open-answer questions `free` in its client model.
    # They are rendered as a textarea and are not informational screens.
    "type_free": QuestionType.TEXT,
    "type_freedict": QuestionType.TEXT,
    "type_number": QuestionType.NUMBER,
    "type_rank": QuestionType.RANKING,
    "type_ranking": QuestionType.RANKING,
    "type_info": QuestionType.INFO,
    "type_grid": QuestionType.MATRIX_SINGLE,
    "type_matrix_single": QuestionType.MATRIX_SINGLE,
    "type_matrix_multi": QuestionType.MATRIX_MULTI,
}

LEAF_QUESTION_SELECTOR = (
    '.question-container[data-question-id]:not(:has(.question-container[data-question-id]))'
)
VISIBLE_LEAF_QUESTION_SELECTOR = f"{LEAF_QUESTION_SELECTOR}:visible"


EXTRACT_QUESTIONS_JS = r"""
() => {
const leaves = Array.from(document.querySelectorAll('.question-container[data-question-id]'))
  .filter(q => !q.querySelector('.question-container[data-question-id]'))
  .filter(q => {
    const style = window.getComputedStyle(q);
    return style.display !== 'none' && style.visibility !== 'hidden' && q.getClientRects().length > 0;
  });
const textFieldSelector = [
  'textarea',
  'input:not([type])',
  'input[type="text"]',
  'input[type="number"]',
  'input[type="range"]',
  '[contenteditable="true"]'
].join(', ');
const responseSelector = [
  '[data-value-code]',
  '.es__answers-row',
  '.grid-row-tr.row-is-simple',
  'select',
  textFieldSelector
].join(', ');
const ownTitle = q => {
  const nodes = Array.from(q.querySelectorAll('[data-testid="header-title"], .question-header'));
  const node = nodes.find(item => ((item.innerText || item.textContent || '').trim()));
  return ((node && (node.innerText || node.textContent)) || '').trim();
};
const mergedPromptById = new Map();
const suppressedPromptIds = new Set();

// Some Enjoy screens split one respondent question into an informational
// prompt/image plus a response component.  This is used not only for open
// answers, but also for grouped single/multiple and matrix questions.  The
// info component is presentation, not a separate questionnaire question.
for (const responseLeaf of leaves) {
  if (!responseLeaf.querySelector(responseSelector)) continue;
  const parent = responseLeaf.parentElement && responseLeaf.parentElement.closest(
    '.question-container[data-question-id]'
  );
  let promptLeaves = leaves.filter(candidate => {
    if (candidate === responseLeaf || !parent || !parent.contains(candidate)) return false;
    const candidateParent = candidate.parentElement && candidate.parentElement.closest('.question-container[data-question-id]');
    const candidateType = Array.from(candidate.classList).find(name => name.startsWith('type_'));
    return candidateParent === parent
      && candidateType === 'type_info'
      && !candidate.querySelector(responseSelector)
      && !candidate.querySelector('[data-value-code], .es__answers-row, .grid-row-tr.row-is-simple');
  });
  // In some Enjoy themes the two components have no data-question-id parent.
  // In that layout the immediately preceding visible INFO leaf is the concept
  // presentation belonging to this open response.
  if (!promptLeaves.length) {
    const responseIndex = leaves.indexOf(responseLeaf);
    const previous = responseIndex > 0 ? leaves[responseIndex - 1] : null;
    const previousType = previous
      ? Array.from(previous.classList).find(name => name.startsWith('type_'))
      : null;
    if (previous && previousType === 'type_info' && !previous.querySelector(responseSelector)) {
      promptLeaves = [previous];
    }
  }
  if (!promptLeaves.length) continue;
  const responseId = responseLeaf.getAttribute('data-question-id') || '';
  if (!ownTitle(responseLeaf)) {
    const promptTitle = promptLeaves.map(ownTitle).find(Boolean);
    if (promptTitle) mergedPromptById.set(responseId, promptTitle);
  }
  for (const promptLeaf of promptLeaves) {
    const promptId = promptLeaf.getAttribute('data-question-id') || '';
    if (promptId) suppressedPromptIds.add(promptId);
  }
}

const payloads = leaves
  .map((q, index) => {
  const classes = Array.from(q.classList);
  const domRawType = classes.find(name => name.startsWith('type_')) || null;
  const hasTextField = Boolean(q.querySelector(textFieldSelector));
  // EnjoySurvey can wrap an open answer in an image/info component.  The
  // response control is a more reliable type signal than the wrapper class.
  const rawType = domRawType === 'type_info' && hasTextField ? 'type_text' : domRawType;
  const platformId = q.getAttribute('data-question-id') || q.id || String(index);
  const group = q.parentElement && q.parentElement.closest('.question-container[data-question-id]');
  const groupId = group ? (group.getAttribute('data-question-id') || null) : null;
  const rawTitle = ownTitle(q) || mergedPromptById.get(platformId) || '';
  const titleText = rawTitle.split(/\n+/).map(line => line.trim()).filter(line =>
    line && !/^(?:(?:Выберите|Укажите)\s+(?:минимум|не менее|не более|максимум|ровно)\s+\d+|(?:Минимум|Максимум)\s+\d+)\s+(?:вариант\w*|ответ\w*)/i.test(line)
  ).join(' ').replace(
    /\s+(?:(?:Выберите|Укажите)\s+(?:минимум|не менее|не более|максимум|ровно)\s+\d+|(?:Минимум|Максимум)\s+\d+)\s+(?:вариант\w*|ответ\w*)(?:\s*\([^)]*\))?.*$/i,
    ''
  ).trim();
  const options = [];
  const seen = new Set();
  for (const item of q.querySelectorAll('[data-value-code]')) {
    const code = item.getAttribute('data-value-code') || '';
    const textNode = item.querySelector('[data-testid="value"]') || item;
    const text = (textNode.innerText || textNode.textContent || '').trim();
    const key = code + '\u0000' + text;
    if (!text || seen.has(key)) continue;
    seen.add(key);
    const exclusiveAncestor = item.closest('.exclusive, [data-exclusive="true"], [data-is-exclusive="true"]');
    options.push({
      code,
      text,
      exclusive: Boolean(
        item.querySelector('.exclusive, [data-exclusive="true"], [data-is-exclusive="true"]')
        || item.classList.contains('exclusive')
        || (exclusiveAncestor && q.contains(exclusiveAncestor))
      ),
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
  const media_urls = Array.from(q.querySelectorAll('img, source, video'))
    .flatMap(node => [
      node.currentSrc,
      node.getAttribute('src'),
      node.getAttribute('data-src'),
      node.getAttribute('srcset')
    ])
    .filter(value => value && !value.startsWith('data:'));
  return {
    platform_id: platformId,
    group_id: groupId,
    reference_code: ((rawTitle.match(/^([A-Za-zА-Яа-яЁё]{1,5}\d+(?:[._]\d+|[A-Za-zА-Яа-яЁё]+)*)/) || [])[1] || null),
    text: titleText,
    raw_type: rawType,
    options,
    rows,
    matrix_rows,
    row_order,
    has_text_field: hasTextField,
    dom_raw_type: domRawType,
    media_urls: Array.from(new Set(media_urls)),
  };
});
// Empty image-only children are demonstrations inside a question group, not
// standalone questionnaire questions.  Keeping them created false INFO
// matches for conditional open questions Q2/Q3.
return payloads.filter(payload => !(
  suppressedPromptIds.has(payload.platform_id)
  || (
    payload.dom_raw_type === 'type_info'
    && !payload.text
    && !payload.has_text_field
    && !payload.options.length
    && !payload.matrix_rows.length
  )
));
}
"""


class EnjoySurveyAdapter(PlatformAdapter):
    name = "EnjoySurvey"

    def __init__(self, headless: bool = True, timeout_ms: int = 30_000, rotation_probe_runs: int = 3):
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.rotation_probe_runs = max(1, rotation_probe_runs)
        self.browser_executable_path = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None
        self.group_by_question_id: dict[str, str] = {}
        self.concept_attempt_limit = max(1, int(os.getenv("MAX_CONCEPT_ATTEMPTS", "18")))
        self._spec: SurveySpec | None = None
        self._baseline_assignments: dict[str, list[str]] = {}
        self._concept_sessions: dict[str, tuple[str, dict]] = {}
        # A go-pass URL is an entry point, not a fixed respondent URL.  Enjoy
        # uses browser state to issue/resume the next pass when that same link
        # is opened again.  Keep the state between discovery and logic routes
        # even though each route gets an isolated Playwright context.
        self._entry_storage_state: dict | None = None

    def configure_spec(self, spec: SurveySpec) -> None:
        """Provide Word-only routing information used by monadic probes."""
        from ..planner import _baseline_assignments

        self._spec = spec
        self._baseline_assignments = _baseline_assignments(spec)

    def _launch_options(self) -> dict:
        # Render's free instances have a tight memory limit.  Keeping Chromium
        # to a small number of renderer processes avoids the service being
        # killed while a long questionnaire is being inspected.  These flags
        # do not change page behaviour or the survey extraction rules.
        options: dict = {
            "headless": self.headless,
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
                "--disable-background-networking",
                "--no-first-run",
                "--no-zygote",
                "--renderer-process-limit=2",
            ],
        }
        if self.browser_executable_path:
            options["executable_path"] = self.browser_executable_path
        return options

    @staticmethod
    def _is_go_pass_url(url: str) -> bool:
        return "/survey/go-pass/" in urlparse(url).path.lower()

    async def _new_context(self, browser, url: str):
        options: dict = {"locale": "ru-RU"}
        if self._is_go_pass_url(url) and self._entry_storage_state:
            options["storage_state"] = self._entry_storage_state
        return await browser.new_context(**options)

    async def _close_context(self, context, url: str) -> None:
        try:
            if self._is_go_pass_url(url):
                self._entry_storage_state = await context.storage_state()
        finally:
            await context.close()

    @classmethod
    def supports(cls, url: str) -> bool:
        hostname = (urlparse(url).hostname or "").lower()
        return hostname.endswith("enjoysurvey.ru") or hostname.endswith("enjoysurvey.com")

    @staticmethod
    def _question_type(raw_type: str | None, rows: int, option_count: int) -> QuestionType:
        if rows > 1 and raw_type == "type_multi":
            return QuestionType.MATRIX_MULTI
        if rows > 1 and raw_type == "type_group":
            return QuestionType.MATRIX_SINGLE
        if raw_type in TYPE_MAP:
            return TYPE_MAP[raw_type]
        if raw_type and raw_type.startswith("type_free"):
            return QuestionType.TEXT
        if option_count:
            return QuestionType.SINGLE
        return QuestionType.INFO

    @staticmethod
    def _merge_grouped_matrix_payloads(payloads: list[dict]) -> list[dict]:
        """Collapse repeated response children that implement one visual matrix.

        Enjoy can program a matrix as a group containing one almost identical
        multi/single child per matrix unit.  Treating every child as a separate
        question creates several false "extra question" errors.  We merge only
        highly conservative candidates: same parent, near-identical prompt and
        strongly overlapping answer labels.
        """
        grouped: dict[str, list[tuple[int, dict]]] = {}
        for index, payload in enumerate(payloads):
            group_id = str(payload.get("group_id") or "")
            if group_id and payload.get("options"):
                grouped.setdefault(group_id, []).append((index, payload))

        replacements: dict[int, dict] = {}
        suppressed: set[int] = set()
        for group_id, entries in grouped.items():
            if len(entries) < 2:
                continue
            prompts = [normalize_space(str(item.get("text", ""))) for _, item in entries]
            if not prompts or not prompts[0] or any(
                text_similarity(prompts[0], prompt) < 0.92 for prompt in prompts[1:]
            ):
                continue
            option_sets = [
                {
                    normalize_space(str(option.get("text", ""))).lower()
                    for option in item.get("options", [])
                    if normalize_space(str(option.get("text", "")))
                }
                for _, item in entries
            ]
            baseline = option_sets[0]
            if not baseline or any(
                len(baseline & values) / max(1, min(len(baseline), len(values))) < 0.75
                for values in option_sets[1:]
            ):
                continue

            first_index, first = entries[0]
            merged = dict(first)
            merged["platform_id"] = group_id
            merged["group_id"] = None
            merged["raw_type"] = (
                "type_matrix_multi"
                if any(item.get("raw_type") == "type_multi" for _, item in entries)
                else "type_matrix_single"
            )
            merged["child_platform_ids"] = [
                str(item.get("platform_id", "")) for _, item in entries
            ]
            merged_rows: list[dict] = []
            seen_rows: set[str] = set()
            for _, item in entries:
                for row in item.get("matrix_rows", []):
                    key = normalize_space(str(row.get("text", ""))).lower()
                    if key and key not in seen_rows:
                        seen_rows.add(key)
                        merged_rows.append(row)
            merged["matrix_rows"] = merged_rows
            merged["rows"] = len(merged_rows)
            merged["row_order"] = [str(row.get("code", "")) for row in merged_rows]
            replacements[first_index] = merged
            suppressed.update(index for index, _ in entries[1:])

        return [
            replacements.get(index, payload)
            for index, payload in enumerate(payloads)
            if index not in suppressed
        ]

    def _convert(self, payload: dict, position: int) -> ObservedQuestion:
        platform_id = str(payload.get("platform_id", position))
        group_id = payload.get("group_id")
        if group_id and str(group_id) != platform_id:
            self.group_by_question_id[platform_id] = str(group_id)
        options = [AnswerOption.model_validate(option) for option in payload.get("options", [])]
        matrix_rows = [AnswerOption.model_validate(option) for option in payload.get("matrix_rows", [])]
        return ObservedQuestion(
            platform_id=platform_id,
            reference_code=payload.get("reference_code"),
            text=payload.get("text", ""),
            type=self._question_type(payload.get("raw_type"), payload.get("rows", 0), len(options)),
            raw_type=payload.get("raw_type"),
            options=options,
            matrix_rows=matrix_rows,
            position=position,
        )

    @staticmethod
    def _expected_id_for_runtime_container(
        platform_id: str,
        group_id: str,
        platform_to_expected: dict[str, str],
        group_to_expected: dict[str, str],
    ) -> str | None:
        """Resolve either the matched response child or any child of its visible group."""
        return platform_to_expected.get(platform_id) or group_to_expected.get(group_id)

    async def _discover_once(self, browser, url: str) -> tuple[list[dict], list[str]]:
        payloads_by_id: dict[str, dict] = {}
        sort_keys: dict[str, tuple[int, int]] = {}
        warnings: list[str] = []
        seen: set[str] = set()
        context = await self._new_context(browser, url)
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
                                timeout=min(self.timeout_ms, 900),
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
                        selected_value = await fast_move.input_value()
                        # A filtered fast-move item does not open its screen:
                        # Enjoy silently jumps to the next available question.
                        # Sort by the screen that actually opened, otherwise
                        # Q2/Q3 aliases corrupt the detected questionnaire order.
                        actual_option_index = direct_positions.get(
                            str(selected_value), option_index
                        )
                        payloads = await page.evaluate(EXTRACT_QUESTIONS_JS)
                        for payload_index, payload in enumerate(payloads):
                            platform_id = str(payload.get("platform_id", ""))
                            if platform_id:
                                sort_keys.setdefault(
                                    platform_id, (actual_option_index, payload_index)
                                )
                            if platform_id and platform_id not in seen:
                                seen.add(platform_id)
                                payloads_by_id[platform_id] = payload
                    except Exception as exc:  # one broken jump should not cancel the whole audit
                        if (
                            type(exc).__name__ != "TimeoutError"
                            and "ГРУППА ВОПРОСОВ" not in str(option.get("text", "")).upper()
                        ):
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
            await self._close_context(context, url)
        insertion_order = {platform_id: index for index, platform_id in enumerate(payloads_by_id)}
        payloads = sorted(
            payloads_by_id.values(),
            key=lambda payload: sort_keys.get(
                str(payload.get("platform_id", "")),
                (len(sort_keys) + 1, insertion_order.get(str(payload.get("platform_id", "")), 0)),
            ),
        )
        return self._merge_grouped_matrix_payloads(payloads), warnings

    def _payload_expected_map(self, payloads: list[dict]) -> dict[str, str]:
        if not self._spec:
            return {}
        candidates: list[tuple[float, int, int]] = []
        for expected_index, expected in enumerate(self._spec.questions):
            for payload_index, payload in enumerate(payloads):
                text = str(payload.get("text", ""))
                score = text_similarity(expected.text, text)
                reference = str(payload.get("reference_code") or "").upper()
                if reference and reference == expected.id.upper():
                    score += 1.0
                candidates.append((score, expected_index, payload_index))
        used_expected: set[int] = set()
        used_payloads: set[int] = set()
        result: dict[str, str] = {}
        for score, expected_index, payload_index in sorted(candidates, reverse=True):
            if score < 0.34 or expected_index in used_expected or payload_index in used_payloads:
                continue
            platform_id = str(payloads[payload_index].get("platform_id", ""))
            if not platform_id:
                continue
            used_expected.add(expected_index)
            used_payloads.add(payload_index)
            result[platform_id] = self._spec.questions[expected_index].id
        return result

    def _probe_answer_code_map(
        self,
        payloads: list[dict],
        platform_to_expected: dict[str, str],
    ) -> dict[str, dict[str, str]]:
        if not self._spec:
            return {}
        expected_by_id = {question.id: question for question in self._spec.questions}
        result: dict[str, dict[str, str]] = {}
        for payload in payloads:
            expected_id = platform_to_expected.get(str(payload.get("platform_id", "")))
            expected = expected_by_id.get(expected_id or "")
            actual_options = payload.get("options", [])
            if not expected or not actual_options:
                continue
            available = set(range(len(actual_options)))
            for option in expected.options:
                ranked = sorted(
                    (
                        (text_similarity(option.text, str(actual_options[index].get("text", ""))), index)
                        for index in available
                    ),
                    reverse=True,
                )
                if ranked and ranked[0][0] >= 0.5:
                    _, index = ranked[0]
                    available.remove(index)
                    result.setdefault(expected.id, {})[option.code] = str(
                        actual_options[index].get("code", "")
                    )
        return result

    @staticmethod
    def _transliterate(value: str) -> str:
        table = {
            "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
            "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "j", "к": "k",
            "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
            "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
            "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
            "э": "e", "ю": "yu", "я": "ya",
        }
        return "".join(table.get(char, char) for char in value.lower())

    def _concept_from_media(
        self,
        media_urls: list[str],
        observed_expected_ids: list[str],
        signature_to_concept: dict[str, str],
        signature: str,
    ) -> tuple[str | None, str | None]:
        concepts = list((self._spec.metadata if self._spec else {}).get("concepts", []))
        if not concepts:
            return None, None
        by_id = {str(item["id"]): item for item in concepts}
        if signature in signature_to_concept:
            concept_id = signature_to_concept[signature]
            return concept_id, str(by_id[concept_id]["label"])

        filenames = " ".join(
            unquote(PurePosixPath(urlparse(url).path).stem) for url in media_urls
        )
        clean_filename = re.sub(r"[-_][0-9a-f]{7,}$", "", filenames, flags=re.I)
        normalized_filename = re.sub(r"[^a-z0-9]+", " ", clean_filename.lower()).strip()
        version_match = re.search(
            r"(?:vafl\w*|waffl\w*|variant\w*|monad\w*|concept\w*|v)\s*[-_ ]?(\d+)",
            clean_filename,
            flags=re.I,
        )
        if version_match:
            version_id = f"concept-{version_match.group(1)}"
            if version_id in by_id:
                signature_to_concept[signature] = version_id
                return version_id, str(by_id[version_id]["label"])

        ranked: list[tuple[float, str]] = []
        for concept in concepts:
            aliases = [str(concept.get("label", "")), *map(str, concept.get("aliases", []))]
            score = max(
                (
                    text_similarity(
                        normalized_filename,
                        re.sub(r"[^a-z0-9]+", " ", self._transliterate(alias)).strip(),
                    )
                    for alias in aliases
                    if alias
                ),
                default=0.0,
            )
            ranked.append((score, str(concept["id"])))
        if ranked and max(ranked)[0] >= 0.42:
            concept_id = max(ranked)[1]
            signature_to_concept[signature] = concept_id
            return concept_id, str(by_id[concept_id]["label"])

        direct_targets = (self._spec.metadata if self._spec else {}).get(
            "direct_concept_question_ids", {}
        )
        candidates = {
            concept_id
            for question_id in observed_expected_ids
            for concept_id in direct_targets.get(question_id, [])
        }
        if not candidates:
            candidates = set(by_id)
        already_used = set(signature_to_concept.values())
        concept_id = next(
            (item for item in sorted(candidates) if item not in already_used),
            next(iter(sorted(candidates)), None),
        )
        if concept_id:
            signature_to_concept[signature] = concept_id
            return concept_id, str(by_id[concept_id]["label"])
        return None, None

    async def _assigned_inventory(
        self,
        page,
        platform_to_expected: dict[str, str],
    ) -> tuple[list[dict], list[str], list[str]]:
        payloads_by_id: dict[str, dict] = {}
        media_urls: list[str] = []
        warnings: list[str] = []
        direct_expected_ids: list[str] = []

        async def capture() -> None:
            for payload in await page.evaluate(EXTRACT_QUESTIONS_JS):
                platform_id = str(payload.get("platform_id", ""))
                if platform_id:
                    payloads_by_id[platform_id] = payload
                media_urls.extend(str(value) for value in payload.get("media_urls", []) if value)

        await capture()
        fast_move = page.locator("#fastMoveSelect")
        if await fast_move.count():
            options = await fast_move.locator("option").evaluate_all(
                "els => els.map(o => ({value:o.value, text:(o.textContent||'').trim(), disabled:o.disabled})).filter(o => o.value && !o.disabled)"
            )
            current_value = await fast_move.input_value()
            visible_ids = set(payloads_by_id)
            visible_group_ids = {
                str(payload.get("group_id"))
                for payload in payloads_by_id.values()
                if payload.get("group_id")
            }
            current_index = next(
                (
                    index
                    for index, option in enumerate(options)
                    if str(option["value"]) in visible_ids | visible_group_ids
                ),
                -1,
            )
            if current_index < 0:
                current_index = next(
                    (
                        index
                        for index, option in enumerate(options)
                        if str(option["value"]) == str(current_value)
                    ),
                    0,
                )
            # Do not jump back into the screener after the random monad has
            # been assigned: some Enjoy projects recalculate/reset variables
            # on such a backward jump. Only inventory the concept/main block.
            options = options[current_index:]
            expected_by_id = {
                question.id: question for question in (self._spec.questions if self._spec else [])
            }
            direct_targets = (self._spec.metadata if self._spec else {}).get(
                "direct_concept_question_ids", {}
            )

            def stems(value: str) -> set[str]:
                return {
                    token[:4]
                    for token in re.findall(r"[a-zа-яё]+", value.lower())
                    if len(token) >= 5
                }

            for question_id in direct_targets:
                question = expected_by_id.get(question_id)
                if not question:
                    continue
                expected_stems = stems(question.text)
                ranked = sorted(
                    (
                        (
                            len(expected_stems & stems(str(option.get("text", "")))),
                            option,
                        )
                        for option in options
                    ),
                    key=lambda item: item[0],
                    reverse=True,
                )
                if not ranked or ranked[0][0] < 1:
                    continue
                option = ranked[0][1]
                try:
                    await fast_move.select_option(
                        str(option["value"]), timeout=min(self.timeout_ms, 3_000)
                    )
                    await page.wait_for_timeout(350)
                    selected_value = await fast_move.input_value()
                    screen_payloads = await page.evaluate(EXTRACT_QUESTIONS_JS)
                    visible_ids = {
                        str(payload.get("platform_id", "")) for payload in screen_payloads
                    }
                    visible_group_ids = {
                        str(payload.get("group_id", "")) for payload in screen_payloads
                    }
                    if (
                        selected_value == str(option["value"])
                        or str(option["value"]) in visible_ids | visible_group_ids
                    ):
                        direct_expected_ids.append(question_id)
                    for payload in screen_payloads:
                        platform_id = str(payload.get("platform_id", ""))
                        if platform_id:
                            payloads_by_id[platform_id] = payload
                except Exception:
                    continue
            for option in options:
                try:
                    await fast_move.select_option(
                        str(option["value"]), timeout=min(self.timeout_ms, 3_000)
                    )
                    # The change event is synchronous but the SPA paints the
                    # replacement screen on the next ticks. A fixed settle is
                    # faster and more predictable than waiting on the select
                    # value, which can legally stay unchanged for group aliases.
                    await page.wait_for_timeout(350)
                    await capture()
                except Exception:
                    # A filtered debug entry may disappear after another
                    # jump. This is expected for branch-only questions; the
                    # explicit concept-presence comparison below decides
                    # whether a real questionnaire error exists.
                    continue
        platform_to_expected.update(
            self._payload_expected_map(list(payloads_by_id.values()))
        )
        expected_ids: list[str] = []
        for payload in payloads_by_id.values():
            platform_id = str(payload.get("platform_id", ""))
            group_id = str(payload.get("group_id") or "")
            expected_id = platform_to_expected.get(platform_id)
            if not expected_id and group_id:
                expected_id = next(
                    (
                        value
                        for child_id, value in platform_to_expected.items()
                        if self.group_by_question_id.get(child_id) == group_id
                    ),
                    None,
                )
            if expected_id and expected_id not in expected_ids:
                expected_ids.append(expected_id)
        expected_ids = list(dict.fromkeys(expected_ids + direct_expected_ids))
        return list(payloads_by_id.values()), list(dict.fromkeys(media_urls)), warnings

    async def _probe_concept_once(
        self,
        browser,
        url: str,
        attempt: int,
        platform_to_expected: dict[str, str],
        answer_code_map: dict[str, dict[str, str]],
        known_signatures: set[str],
        signature_to_concept: dict[str, str],
    ) -> ConceptRun | None:
        if not self._spec:
            return None
        context = await self._new_context(browser, url)
        page = await context.new_page()
        page.set_default_timeout(self.timeout_ms)
        warnings: list[str] = []
        try:
            response = await page.goto(url, wait_until="domcontentloaded")
            if response and response.status >= 400:
                warnings.append(f"HTTP {response.status} при создании прохождения")
                return None
            await page.wait_for_selector(
                VISIBLE_LEAF_QUESTION_SELECTOR,
                state="attached",
                timeout=self.timeout_ms,
            )
            media_urls: list[str] = []
            for _ in range(45):
                if "status=2" in page.url:
                    warnings.append("Скринер завершил тестового респондента до показа концепции.")
                    return ConceptRun(
                        attempt=attempt,
                        signature=f"screenout-{attempt}",
                        warnings=warnings,
                    )
                payloads = await page.evaluate(EXTRACT_QUESTIONS_JS)
                media_urls.extend(
                    str(value)
                    for payload in payloads
                    for value in payload.get("media_urls", [])
                    if value
                )
                # Screening screens in the supported questionnaires do not
                # contain respondent-facing media. The first non-empty media
                # URL is therefore the stable concept assignment signal and
                # avoids walking the whole main questionnaire.
                if media_urls:
                    break
                containers = page.locator(VISIBLE_LEAF_QUESTION_SELECTOR)
                for index in range(await containers.count()):
                    container = containers.nth(index)
                    platform_id = await container.get_attribute("data-question-id") or ""
                    group_id = await container.evaluate(
                        """q => { const g=q.parentElement&&q.parentElement.closest('.question-container[data-question-id]'); return g ? (g.getAttribute('data-question-id')||'') : ''; }"""
                    )
                    expected_id = self._expected_id_for_runtime_container(
                        platform_id,
                        str(group_id or ""),
                        platform_to_expected,
                        {
                            self.group_by_question_id[child]: expected
                            for child, expected in platform_to_expected.items()
                            if child in self.group_by_question_id
                        },
                    )
                    await self._answer_container(
                        container,
                        expected_id,
                        self._baseline_assignments,
                        answer_code_map,
                    )
                next_button = page.locator(
                    '#nextSurvey, [data-testid="next"], button:has-text("Вперед"), button:has-text("Далее")'
                ).first
                if not await next_button.count() or not await next_button.is_visible():
                    warnings.append("Не найдена кнопка перехода до экрана концепции.")
                    return ConceptRun(
                        attempt=attempt,
                        signature=f"incomplete-{attempt}",
                        warnings=warnings,
                    )
                before = await page.locator(VISIBLE_LEAF_QUESTION_SELECTOR).evaluate_all(
                    "els => els.map(q => q.getAttribute('data-question-id')).join('|')"
                )
                await next_button.click()
                try:
                    await page.wait_for_function(
                        "before => Array.from(document.querySelectorAll('.question-container[data-question-id]')).filter(q => !q.querySelector('.question-container[data-question-id]')).map(q => q.getAttribute('data-question-id')).join('|') !== before",
                        arg=before,
                        timeout=min(self.timeout_ms, 8_000),
                    )
                except Exception:
                    pass
                await page.wait_for_timeout(250)

            media_urls = list(dict.fromkeys(media_urls))
            if not media_urls:
                warnings.append("Платформа не отдала изображение/идентификатор выпавшей концепции.")
                return ConceptRun(
                    attempt=attempt,
                    signature=f"unknown-{attempt}",
                    warnings=warnings,
                )
            signature = hashlib.sha256("\n".join(sorted(media_urls)).encode("utf-8")).hexdigest()[:16]
            inventory_payloads: list[dict] = []
            expected_ids: list[str] = []
            concept_id, concept_label = self._concept_from_media(
                media_urls,
                expected_ids,
                signature_to_concept,
                signature,
            )
            if signature not in known_signatures:
                (
                    inventory_payloads,
                    inventory_media,
                    expected_ids,
                    inventory_warnings,
                ) = await self._walk_assigned_concept(
                    page,
                    concept_id,
                    platform_to_expected,
                    answer_code_map,
                )
                media_urls = list(dict.fromkeys(media_urls + inventory_media))
                warnings.extend(inventory_warnings)
            if concept_id and concept_id not in self._concept_sessions:
                self._concept_sessions[concept_id] = (page.url, await context.storage_state())
            return ConceptRun(
                attempt=attempt,
                signature=signature,
                concept_id=concept_id,
                concept_label=concept_label,
                media_urls=media_urls,
                observed_expected_ids=expected_ids,
                observed_questions=[
                    self._convert(payload, index)
                    for index, payload in enumerate(inventory_payloads)
                ],
                warnings=list(dict.fromkeys(warnings)),
            )
        except Exception as exc:
            return ConceptRun(
                attempt=attempt,
                signature=f"error-{attempt}",
                warnings=[f"Проверка концепции остановлена: {type(exc).__name__}: {exc}"],
            )
        finally:
            await self._close_context(context, url)

    async def _walk_assigned_concept(
        self,
        page,
        concept_id: str | None,
        platform_to_expected: dict[str, str],
        answer_code_map: dict[str, dict[str, str]],
    ) -> tuple[list[dict], list[str], list[str], list[str]]:
        """Walk the real respondent path through concept-specific questions."""
        if not self._spec:
            return [], [], [], []
        direct_targets = self._spec.metadata.get("direct_concept_question_ids", {})
        target_ids = {
            question_id
            for question_id, concept_ids in direct_targets.items()
            if not concept_id or concept_id in concept_ids
        }
        target_ids.update(self._spec.metadata.get("dynamic_concept_question_ids", []))
        expected_positions = {
            question.id: index for index, question in enumerate(self._spec.questions)
        }
        stop_position = max(
            (expected_positions.get(question_id, -1) for question_id in target_ids),
            default=-1,
        )
        payloads_by_id: dict[str, dict] = {}
        expected_ids: list[str] = []
        media_urls: list[str] = []
        warnings: list[str] = []
        group_to_expected: dict[str, str] = {}

        for _ in range(18):
            if "status=2" in page.url:
                warnings.append("Основной блок неожиданно завершился до проверки вопросов концепции.")
                break
            screen_payloads = await page.evaluate(EXTRACT_QUESTIONS_JS)
            screen_map = self._payload_expected_map(screen_payloads)
            platform_to_expected.update(screen_map)
            for child_id, expected_id in platform_to_expected.items():
                group_id = self.group_by_question_id.get(child_id)
                if group_id:
                    group_to_expected[group_id] = expected_id
            screen_positions: list[int] = []
            for payload in screen_payloads:
                platform_id = str(payload.get("platform_id", ""))
                if platform_id:
                    payloads_by_id[platform_id] = payload
                media_urls.extend(str(value) for value in payload.get("media_urls", []) if value)
                expected_id = platform_to_expected.get(platform_id)
                if expected_id and expected_id not in expected_ids:
                    expected_ids.append(expected_id)
                if expected_id in expected_positions:
                    screen_positions.append(expected_positions[expected_id])

            if screen_positions and stop_position >= 0 and min(screen_positions) > stop_position:
                break

            containers = page.locator(VISIBLE_LEAF_QUESTION_SELECTOR)
            if not await containers.count():
                break
            for index in range(await containers.count()):
                container = containers.nth(index)
                platform_id = await container.get_attribute("data-question-id") or ""
                group_id = await container.evaluate(
                    """q => { const g=q.parentElement&&q.parentElement.closest('.question-container[data-question-id]'); return g ? (g.getAttribute('data-question-id')||'') : ''; }"""
                )
                expected_id = self._expected_id_for_runtime_container(
                    platform_id,
                    str(group_id or ""),
                    platform_to_expected,
                    group_to_expected,
                )
                await self._answer_container(
                    container,
                    expected_id,
                    self._baseline_assignments,
                    answer_code_map,
                )
            survey_root = page.locator(".es__root").first
            if await survey_root.count():
                await self._fill_text_fields(survey_root)
            next_button = page.locator(
                '#nextSurvey, [data-testid="next"], button:has-text("Вперед"), button:has-text("Далее")'
            ).first
            if not await next_button.count() or not await next_button.is_visible():
                break
            before = await page.locator(VISIBLE_LEAF_QUESTION_SELECTOR).evaluate_all(
                "els => els.map(q => q.getAttribute('data-question-id')).join('|')"
            )
            await next_button.click()
            try:
                await page.wait_for_function(
                    "before => Array.from(document.querySelectorAll('.question-container[data-question-id]')).filter(q => !q.querySelector('.question-container[data-question-id]')).map(q => q.getAttribute('data-question-id')).join('|') !== before",
                    arg=before,
                    timeout=min(self.timeout_ms, 8_000),
                )
            except Exception:
                validation = " ".join(await page.locator(
                    '.question-error-message:visible, [data-testid="errors"]:visible, [role="alert"]:visible'
                ).all_inner_texts()).strip()
                warnings.append(
                    f"Платформа не приняла ответ в основном блоке: {validation or 'экран не изменился'}"
                )
                break
            await page.wait_for_timeout(250)

        return (
            list(payloads_by_id.values()),
            list(dict.fromkeys(media_urls)),
            expected_ids,
            list(dict.fromkeys(warnings)),
        )

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
        all_payloads_by_id: dict[str, dict] = {}
        concept_runs: list[ConceptRun] = []
        concept_attempts = 0
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
                        all_payloads_by_id[platform_id] = payload
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

                concepts = list((self._spec.metadata if self._spec else {}).get("concepts", []))
                if concepts:
                    structural_payloads = list(all_payloads_by_id.values())
                    platform_to_expected = self._payload_expected_map(structural_payloads)
                    answer_code_map = self._probe_answer_code_map(
                        structural_payloads, platform_to_expected
                    )
                    known_signatures: set[str] = set()
                    signature_to_concept: dict[str, str] = {}
                    expected_ids = {str(item["id"]) for item in concepts}
                    for attempt in range(1, self.concept_attempt_limit + 1):
                        concept_attempts = attempt
                        run = await self._probe_concept_once(
                            browser,
                            url,
                            attempt,
                            platform_to_expected,
                            answer_code_map,
                            known_signatures,
                            signature_to_concept,
                        )
                        if run:
                            concept_runs.append(run)
                            warnings.extend(run.warnings)
                            if not run.signature.startswith(("error-", "unknown-", "screenout-", "incomplete-")):
                                known_signatures.add(run.signature)
                        covered = {
                            item.concept_id for item in concept_runs if item.concept_id
                        }
                        if expected_ids.issubset(covered):
                            break
                    covered = {
                        item.concept_id for item in concept_runs if item.concept_id
                    }
                    missing = [
                        str(item["label"])
                        for item in concepts
                        if str(item["id"]) not in covered
                    ]
                    if missing:
                        warnings.append(
                            "Не все случайные концепции встретились за "
                            f"{concept_attempts} новых прохождений: {', '.join(missing)}. "
                            "Проверка этих концепций не считается выполненной."
                        )
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
            concept_runs=concept_runs,
            concept_attempts=concept_attempts,
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

    async def _fill_text_fields(self, scope) -> int:
        text_fields = scope.locator(
            'textarea:visible, input:not([type]):visible, input[type="text"]:visible, '
            'input[type="number"]:visible, input[type="range"]:visible, '
            '[contenteditable="true"]:visible'
        )
        filled = 0
        for index in range(await text_fields.count()):
            field = text_fields.nth(index)
            field_type = await field.get_attribute("type")
            if await field.get_attribute("contenteditable") == "true":
                current_value = (await field.text_content() or "").strip()
            else:
                current_value = (await field.input_value()).strip()
            if current_value:
                continue
            await field.click()
            if field_type == "range":
                minimum = float(await field.get_attribute("min") or 0)
                maximum = float(await field.get_attribute("max") or 100)
                value = str((minimum + maximum) / 2)
            elif field_type == "number":
                value = "30"
            else:
                minimum_length = int(await field.get_attribute("minlength") or 0)
                maximum_length = int(await field.get_attribute("maxlength") or 0)
                desired_length = max(28, minimum_length)
                if maximum_length > 0:
                    desired_length = min(desired_length, maximum_length)
                seed = "Тестовый подробный ответ для проверки логики"
                value = (seed * ((desired_length // len(seed)) + 1))[:desired_length]
            await field.fill(value)
            # Enjoy stores the open response in its Svelte model on native
            # input/change events. Blur finalises masks and inline validation.
            await field.dispatch_event("input")
            await field.dispatch_event("change")
            await field.press("Tab")
            filled += 1
        return filled

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

        await self._fill_text_fields(container)

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
        # Conditional display rules in Enjoy are attached to a parent group,
        # while the questionnaire question is usually matched to one of its
        # children (for example, an image/info child plus a free-text child).
        # A group is already visible when either child is rendered, so use the
        # parent ID as an additional runtime signal for the expected question.
        group_to_expected = {
            self.group_by_question_id[match.observed_id]: match.expected_id
            for match in matches
            if match.observed_id in self.group_by_question_id
        }
        expected_position_by_id = {
            match.expected_id: match.expected_position for match in matches
        }
        target_ids = list(dict.fromkeys(scenario.expected_visible + scenario.expected_hidden))
        target_positions = [
            expected_position_by_id[target_id]
            for target_id in target_ids
            if target_id in expected_position_by_id
        ]
        assigned_matches = [
            match
            for match in sorted(matches, key=lambda item: item.expected_position)
            if (
                match.expected_id in scenario.assignments
                and (not target_positions or match.expected_position < min(target_positions))
            )
        ]
        preferred_start_match = assigned_matches[-1] if assigned_matches else None
        preferred_start_id = (
            self.group_by_question_id.get(
                preferred_start_match.observed_id, preferred_start_match.observed_id
            )
            if preferred_start_match
            else None
        )
        visited_platform: list[str] = []
        visited_expected: list[str] = []
        observed_questions: list[ObservedQuestion] = []
        warnings: list[str] = []
        completed = False
        concept_session = next(
            (
                self._concept_sessions[concept_id]
                for concept_id in scenario.concept_ids
                if concept_id in self._concept_sessions
            ),
            None,
        )
        route_url = concept_session[0] if concept_session else url
        if scenario.concept_ids and not concept_session:
            return ScenarioResult(
                scenario_id=scenario.id,
                completed=False,
                warnings=[
                    "Ни одна подходящая случайная концепция не встретилась; "
                    "логика этого вопроса не проверена."
                ],
            )

        def route_is_proven() -> bool:
            if any(target in visited_expected for target in scenario.expected_hidden):
                return False
            if any(target not in visited_expected for target in scenario.expected_visible):
                return False
            if not scenario.expected_hidden:
                return True
            hidden_positions = [
                expected_position_by_id[target]
                for target in scenario.expected_hidden
                if target in expected_position_by_id
            ]
            if len(hidden_positions) != len(scenario.expected_hidden):
                return False
            visited_positions = [
                expected_position_by_id[target]
                for target in visited_expected
                if target in expected_position_by_id
            ]
            return bool(visited_positions) and max(visited_positions) > max(hidden_positions)

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**self._launch_options())
            context = (
                await browser.new_context(locale="ru-RU", storage_state=concept_session[1])
                if concept_session
                else await self._new_context(browser, route_url)
            )
            page = await context.new_page()
            page.set_default_timeout(self.timeout_ms)
            try:
                response = None
                # Enjoy test links keep one respondent state on the server.
                # A normal GET may therefore reopen the screen left by the
                # previous route or even an already screened-out respondent.
                # The platform's own debug panel starts at a chosen question
                # by POSTing to /goto/. Do the same for the nearest controller.
                if (
                    preferred_start_id
                    and route_url.startswith(("http://", "https://"))
                    and "/index/" in route_url
                ):
                    jump_url = route_url.replace("/index/", "/goto/", 1)
                    try:
                        await page.set_content(
                            '<form id="survey-qa-jump" method="post"></form>'
                        )
                        async with page.expect_navigation(
                            wait_until="domcontentloaded", timeout=self.timeout_ms
                        ) as navigation:
                            await page.locator("#survey-qa-jump").evaluate(
                                """(form, data) => {
                                  form.action = data.url;
                                  const input = document.createElement('input');
                                  input.type = 'hidden';
                                  input.name = 'questionId';
                                  input.value = data.questionId;
                                  form.appendChild(input);
                                  form.submit();
                                }""",
                                {"url": jump_url, "questionId": preferred_start_id},
                            )
                        response = await navigation.value
                    except Exception:
                        response = await page.goto(route_url, wait_until="domcontentloaded")
                else:
                    response = await page.goto(route_url, wait_until="domcontentloaded")
                if response and response.status >= 400:
                    raise RuntimeError(f"EnjoySurvey вернул HTTP {response.status}")
                if "/user/login" in page.url:
                    raise RuntimeError(
                        "EnjoySurvey перенаправил на страницу входа; тестовая ссылка недоступна"
                    )
                if "status=2" in page.url:
                    raise RuntimeError(
                        "Тестовый респондент EnjoySurvey уже завершён по скринингу; "
                        "этот маршрут нельзя считать проверкой логики."
                    )
                try:
                    await page.wait_for_selector(
                        VISIBLE_LEAF_QUESTION_SELECTOR,
                        state="attached",
                        timeout=self.timeout_ms,
                    )
                except Exception:
                    # The redirect can happen while Playwright is waiting for
                    # the first question.  Replace its long internal call log
                    # with the actual platform state; this route is
                    # inconclusive, not proof that the target question is
                    # missing.
                    if "status=2" in page.url:
                        raise RuntimeError(
                            "Тестовый респондент EnjoySurvey завершён по скринингу; "
                            "для проверки ветки нужен новый тестовый запуск."
                        )
                    raise
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
                        available_start_ids = {
                            str(candidate["value"]) for candidate in start_candidates
                        }
                        assigned_starts = [
                            (match, self.group_by_question_id.get(match.observed_id, match.observed_id))
                            for match in sorted(matches, key=lambda item: item.expected_position)
                            if (
                                self.group_by_question_id.get(match.observed_id, match.observed_id)
                                in available_start_ids
                                and match.expected_id in scenario.assignments
                                and (
                                    not target_positions
                                    or match.expected_position < min(target_positions)
                                )
                            )
                        ]
                        matched_starts = [
                            (match, self.group_by_question_id.get(match.observed_id, match.observed_id))
                            for match in sorted(matches, key=lambda item: item.expected_position)
                            if self.group_by_question_id.get(match.observed_id, match.observed_id)
                            in available_start_ids
                        ]
                        # A logic route should start at the nearest controlling
                        # question (Q1 for Q2/Q3), not traverse the entire
                        # questionnaire from S1. This both speeds up checks and
                        # prevents unrelated later validation from making a
                        # proven branch look inconclusive.
                        start_id = (
                            assigned_starts[-1][1]
                            if assigned_starts
                            else matched_starts[0][1]
                            if matched_starts
                            else str(start_candidates[0]["value"])
                        )
                        before_start = await page.locator(VISIBLE_LEAF_QUESTION_SELECTOR).evaluate_all(
                            "els => els.map(q => q.getAttribute('data-question-id')).join('|')"
                        )
                        if await fast_move.input_value() != start_id:
                            await fast_move.select_option(start_id)
                        try:
                            await page.wait_for_function(
                                """before => Array.from(document.querySelectorAll('.question-container[data-question-id]'))
                                  .filter(q => !q.querySelector('.question-container[data-question-id]'))
                                  .filter(q => {
                                    const style = getComputedStyle(q);
                                    return style.display !== 'none' && style.visibility !== 'hidden' && q.getClientRects().length > 0;
                                  })
                                  .map(q => q.getAttribute('data-question-id')).join('|') !== before""",
                                arg=before_start,
                                timeout=min(self.timeout_ms, 8_000),
                            )
                        except Exception:
                            pass
                        await page.wait_for_timeout(400)
                for _ in range(180):
                    containers = page.locator(VISIBLE_LEAF_QUESTION_SELECTOR)
                    if not await containers.count():
                        if "status=2" in page.url:
                            warnings.append(
                                "Опрос завершил респондента по скринингу до проверки целевой ветки."
                            )
                        else:
                            completed = True
                        break
                    current_ids: list[str] = []
                    screen_payloads = await page.evaluate(EXTRACT_QUESTIONS_JS)
                    payloads_by_id = {
                        str(payload.get("platform_id", "")): payload
                        for payload in screen_payloads
                        if payload.get("platform_id")
                    }
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
                        group_id = await container.evaluate(
                            """q => {
                              const group = q.parentElement && q.parentElement.closest(
                                '.question-container[data-question-id]'
                              );
                              return group ? (group.getAttribute('data-question-id') || '') : '';
                            }"""
                        )
                        expected_id = self._expected_id_for_runtime_container(
                            platform_id,
                            str(group_id or ""),
                            platform_to_expected,
                            group_to_expected,
                        )
                        if not expected_id and scenario.expected_texts:
                            # EXTRACT_QUESTIONS_JS may have joined the wording
                            # from an INFO/image sibling with this open input.
                            title = str(payloads_by_id.get(platform_id, {}).get("text", "")).strip()
                            if not title:
                                title_nodes = container.locator('[data-testid="header-title"], .question-header')
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
                    # Some themes render the actual textarea next to (rather
                    # than inside) the data-question-id leaf. Fill any such
                    # visible response control before submitting the screen.
                    survey_root = page.locator(".es__root").first
                    if await survey_root.count():
                        await self._fill_text_fields(survey_root)
                    if route_is_proven():
                        completed = True
                        break
                    next_button = page.locator('#nextSurvey, [data-testid="next"], button:has-text("Вперед"), button:has-text("Далее")').first
                    if not await next_button.count() or not await next_button.is_visible():
                        if "status=2" in page.url:
                            warnings.append(
                                "Опрос завершил респондента по скринингу до проверки целевой ветки."
                            )
                        else:
                            completed = True
                        break
                    await page.wait_for_timeout(150)
                    before = await page.evaluate(
                        """() => JSON.stringify({
                          url: location.href,
                          fast: document.querySelector('#fastMoveSelect')?.value || '',
                          progress: document.querySelector('[role="progressbar"]')?.getAttribute('aria-valuenow') || '',
                          leaves: Array.from(document.querySelectorAll('.question-container[data-question-id]'))
                            .filter(q => !q.querySelector('.question-container[data-question-id]'))
                            .filter(q => {
                              const style = getComputedStyle(q);
                              return style.display !== 'none' && style.visibility !== 'hidden' && q.getClientRects().length > 0;
                            })
                            .map(q => ({
                              id: q.getAttribute('data-question-id') || '',
                              cls: q.className,
                              text: (q.innerText || '').trim(),
                              values: Array.from(q.querySelectorAll('textarea, input, [contenteditable="true"]'))
                                .map(field => field.value ?? field.textContent ?? '')
                            }))
                        })"""
                    )
                    await next_button.click()
                    try:
                        await page.wait_for_function(
                            """before => JSON.stringify({
                              url: location.href,
                              fast: document.querySelector('#fastMoveSelect')?.value || '',
                              progress: document.querySelector('[role="progressbar"]')?.getAttribute('aria-valuenow') || '',
                              leaves: Array.from(document.querySelectorAll('.question-container[data-question-id]'))
                                .filter(q => !q.querySelector('.question-container[data-question-id]'))
                                .filter(q => {
                                  const style = getComputedStyle(q);
                                  return style.display !== 'none' && style.visibility !== 'hidden' && q.getClientRects().length > 0;
                                })
                                .map(q => ({
                                  id: q.getAttribute('data-question-id') || '',
                                  cls: q.className,
                                  text: (q.innerText || '').trim(),
                                  values: Array.from(q.querySelectorAll('textarea, input, [contenteditable="true"]'))
                                    .map(field => field.value ?? field.textContent ?? '')
                                }))
                            }) !== before""",
                            arg=before,
                            timeout=min(self.timeout_ms, 8_000),
                        )
                        # The SPA briefly removes all question nodes before
                        # mounting the next screen.  Wait for the second phase
                        # so that a transient empty DOM is not treated as the
                        # end of the questionnaire.
                        try:
                            await page.wait_for_selector(
                                VISIBLE_LEAF_QUESTION_SELECTOR,
                                state="attached",
                                timeout=min(self.timeout_ms, 4_000),
                            )
                        except Exception:
                            if "status=2" in page.url:
                                warnings.append(
                                    "Опрос завершил респондента по скринингу до проверки целевой ветки."
                                )
                                break
                            if "enjoysurvey" not in page.url:
                                completed = True
                                break
                            raise
                        await page.wait_for_timeout(300)
                    except Exception:
                        if not await page.locator(VISIBLE_LEAF_QUESTION_SELECTOR).count():
                            if "status=2" in page.url:
                                warnings.append(
                                    "Опрос завершил респондента по скринингу до проверки целевой ветки."
                                )
                            else:
                                completed = True
                            break
                        validation_messages = await page.locator(
                            '.invalid-feedback:visible, .has-error:visible, '
                            '.alert-danger:visible, [role="alert"]:visible, '
                            '.question-error-message:visible, [data-testid="errors"]:visible, '
                            '.noty_body:visible'
                        ).all_inner_texts()
                        validation_text = " ".join(
                            dict.fromkeys(
                                text.strip() for text in validation_messages if text.strip()
                            )
                        )
                        current_expected = [
                            platform_to_expected.get(platform_id, platform_id)
                            for platform_id in current_ids
                        ]
                        if validation_text:
                            warnings.append(
                                "Платформа не приняла ответ на экране "
                                f"{', '.join(current_expected)}: {validation_text}"
                            )
                        else:
                            warnings.append(
                                "После нажатия «Далее» остался тот же экран "
                                f"({', '.join(current_expected)}); ответ не был принят платформой."
                            )
                        break
            except Exception as exc:
                warnings.append(f"Прохождение остановлено: {type(exc).__name__}: {exc}")
            finally:
                await self._close_context(context, route_url)
                await browser.close()
        return ScenarioResult(
            scenario_id=scenario.id,
            completed=completed,
            visited_platform_ids=visited_platform,
            visited_expected_ids=visited_expected,
            observed_questions=observed_questions,
            warnings=warnings,
        )
