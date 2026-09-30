from __future__ import annotations

import re

from .models import ConditionAtom, ConditionGroup, LogicRule, RotationSpec
from .normalization import normalize_id, normalize_space, parse_code_list


QUESTION_ID = r"[A-Za-zА-Яа-яЁё]{1,5}\d+(?:[._]\d+|[A-Za-zА-Яа-яЁё]+)*"


def looks_like_instruction(text: str) -> bool:
    upper = normalize_space(text).upper()
    markers = (
        "ЕСЛИ",
        "ЗАДАТЬ",
        "ПОКАЗАТЬ",
        "ПОКАЗЫВАТЬ",
        "НЕ ПОКАЗЫВАТЬ",
        "ПРОДОЛЖИТЬ",
        "ЗАВЕРШИТЬ",
        "ЗАКОНЧИТЬ",
        "РОТАЦ",
        "РОТИРОВАТЬ",
        "СЛУЧАЙН",
        "ИСКЛЮЧАЮЩ",
        "ЗАКРЕПЛ",
        "ВЫВЕСТИ",
        "ТОЛЬКО ДЛЯ",
        "ДЛЯ ВСЕХ",
    )
    return any(marker in upper for marker in markers)


def _action(text: str) -> str:
    upper = text.upper()
    if upper.lstrip().startswith("ПРОДОЛЖ"):
        return "continue"
    if upper.lstrip().startswith(("ЗАВЕРШ", "ЗАКОНЧ")):
        return "terminate"
    if re.search(r"НЕ\s+ПОКАЗ|НЕ\s+ЗАДА", upper):
        return "hide"
    if re.search(r"ЗАВЕРШ|ЗАКОНЧ", upper):
        return "terminate"
    if "ПРОДОЛЖ" in upper:
        return "continue"
    if re.search(r"ЗАДАТ|ПОКАЗ|ВЫВЕСТ", upper) or upper.startswith("ЕСЛИ"):
        return "show"
    if re.search(r"РОТАЦ|РОТИРОВ|СЛУЧАЙН", upper):
        return "rotate"
    return "other"


def _target(text: str) -> str | None:
    match = re.search(
        rf"(?:ЗАДАТЬ|ПОКАЗАТЬ|ПОКАЗЫВАТЬ|НЕ\s+ПОКАЗЫВАТЬ|НЕ\s+ЗАДАВАТЬ)\s+(?:ВОПРОС\s+)?({QUESTION_ID})",
        text,
        flags=re.I,
    )
    return normalize_id(match.group(1)) if match else None


def _condition(text: str) -> ConditionGroup | None:
    upper = normalize_space(text).upper().replace("Ё", "Е")
    if "ЕСЛИ" not in upper and "ПРИ ВЫБОРЕ" not in upper and "ПРИ КОД" not in upper:
        return None

    condition_text = re.split(r"\bЕСЛИ\b|ПРИ ВЫБОРЕ", upper, maxsplit=1)[-1]
    combinator = "or" if re.search(r"\sИЛИ\s", condition_text) and not re.search(r"\sИ\s", condition_text) else "and"
    atoms: list[ConditionAtom] = []

    symbolic = re.compile(
        rf"(?P<qid>{QUESTION_ID})\s*\.?\s*(?P<op>!=|<>|≠|=)\s*(?P<values>\d+(?:\s*[-–—,;/]\s*\d+)*)",
        flags=re.I,
    )
    for match in symbolic.finditer(condition_text):
        values = parse_code_list(match.group("values"))
        if not values:
            continue
        op = "not_in" if match.group("op") in {"!=", "<>", "≠"} else "in"
        atoms.append(ConditionAtom(question_id=normalize_id(match.group("qid")), operator=op, values=values))

    verbal = re.compile(
        rf"(?:В\s+)?(?P<qid>{QUESTION_ID}).{{0,45}}?(?:ОТМЕЧЕН|ВЫБРАН|ВЫБРАЛ|КОД(?:Ы)?|РАВЕН).{{0,12}}?(?P<values>\d+(?:\s*[-–—,;/]\s*\d+)*)",
        flags=re.I,
    )
    for match in verbal.finditer(condition_text):
        qid = normalize_id(match.group("qid"))
        values = parse_code_list(match.group("values"))
        if values and not any(atom.question_id == qid and atom.values == values for atom in atoms):
            atoms.append(ConditionAtom(question_id=qid, operator="in", values=values))

    negative = bool(re.search(r"НЕ\s+(?:ВЫБРАН|ОТМЕЧЕН)|КРОМЕ", condition_text))
    if negative:
        for atom in atoms:
            if atom.operator == "in":
                atom.operator = "not_in"

    return ConditionGroup(combinator=combinator, atoms=atoms) if atoms else None


def parse_logic_rule(text: str, source_block: int | None = None) -> LogicRule | None:
    text = normalize_space(text)
    if not looks_like_instruction(text):
        return None
    action = _action(text)
    condition = _condition(text)
    target = _target(text)
    upper = text.upper().replace("Ё", "Е")
    if (
        action == "show"
        and condition is None
        and target is None
        and "ТЕСТИРУЕТ" not in upper
        and not re.search(r"ЗАДАТ|ПОКАЗ|ВЫВЕСТ", upper)
    ):
        # Natural question/answer wording can start with "если" without
        # describing survey routing (for example, "Если продукт появится...").
        return None
    confidence = 0.95 if condition and condition.atoms else 0.65
    if action in {"rotate", "other"}:
        confidence = 0.6
    return LogicRule(
        action=action,
        target_question_id=target,
        condition=condition,
        raw_text=text,
        confidence=confidence,
        source_block=source_block,
    )


def parse_logic_rules(text: str, source_block: int | None = None) -> list[LogicRule]:
    text = normalize_space(text)
    if not looks_like_instruction(text):
        return []
    fragments = re.split(
        r"\s*\+\s*|\s+(?=ИНАЧЕ\s+(?:ЗАВЕРШ|ЗАКОНЧ))|(?<=[.;])\s+(?=(?:ЗАВЕРШИТЬ|ЗАКОНЧИТЬ|ПРОДОЛЖИТЬ|ЗАДАТЬ|ПОКАЗАТЬ))",
        text,
        flags=re.I,
    )
    rules: list[LogicRule] = []
    for fragment in fragments:
        fragment = re.sub(r"^ИНАЧЕ\s+", "", fragment.strip(), flags=re.I)
        rule = parse_logic_rule(fragment, source_block=source_block)
        if rule:
            rules.append(rule)
    return rules or ([parse_logic_rule(text, source_block=source_block)] if parse_logic_rule(text, source_block=source_block) else [])


def parse_rotation(text: str) -> RotationSpec | None:
    upper = normalize_space(text).upper().replace("Ё", "Е")
    if not re.search(r"РОТАЦ|РОТИРОВ|ПЕРЕМЕШ|СЛУЧАЙН", upper):
        return None
    axis = "unknown"
    if "СТРОК" in upper:
        axis = "rows"
    elif "СТОЛБ" in upper:
        axis = "columns"
    elif "КОНЦЕП" in upper or "ПРОДУКТ" in upper:
        axis = "concepts"
    elif "КОД" in upper or "ВАРИАНТ" in upper:
        axis = "options"

    fixed: list[str] = []
    fixed_match = re.search(
        r"(?:КРОМЕ|ЗАКРЕПЛЕН(?:Ы|)|ЗАКРЕПЛЕННЫЕ)\s*(?:КОД(?:А|ОВ|Ы)?\s*)?([\d,;\s]+)",
        upper,
    )
    if fixed_match:
        fixed = parse_code_list(fixed_match.group(1))
    included: list[str] = []
    included_match = re.search(r"(?:РОТАЦИЯ|РОТИРОВАТЬ)\s*(?:КОД(?:ОВ|Ы)?\s*)?([\d\s,;–—-]+)", upper)
    if included_match:
        included = parse_code_list(included_match.group(1))
    return RotationSpec(enabled=True, axis=axis, included_codes=included, fixed_codes=fixed, raw_text=text)
