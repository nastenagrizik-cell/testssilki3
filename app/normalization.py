from __future__ import annotations

import re
from difflib import SequenceMatcher


ID_TRANSLATION = str.maketrans(
    {
        "А": "A",
        "В": "B",
        "С": "C",
        "Е": "E",
        "Н": "H",
        "К": "K",
        "М": "M",
        "О": "O",
        "Р": "P",
        "Т": "T",
        "Х": "X",
        "а": "A",
        "в": "B",
        "с": "C",
        "е": "E",
        "н": "H",
        "к": "K",
        "м": "M",
        "о": "O",
        "р": "P",
        "т": "T",
        "х": "X",
    }
)


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def normalize_id(value: str) -> str:
    value = normalize_space(value).translate(ID_TRANSLATION).upper()
    value = value.replace(",", ".").replace("-", "_")
    return re.sub(r"[^A-Z0-9._]", "", value)


def normalize_text(value: str) -> str:
    value = normalize_space(value).lower().replace("ё", "е")
    value = value.replace("–", "-").replace("—", "-")
    value = re.sub(r"\b[а-яa-z]{1,5}\d+(?:[._]\d+|[а-яa-z]+)*[.)]?\s*", "", value, count=1)
    value = re.sub(r"[^0-9a-zа-я]+", " ", value)
    return normalize_space(value)


def text_similarity(left: str, right: str) -> float:
    a, b = normalize_text(left), normalize_text(right)
    if not a or not b:
        return 0.0
    sequence = SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    jaccard = len(ta & tb) / max(1, len(ta | tb))
    containment = len(ta & tb) / max(1, min(len(ta), len(tb)))
    return round(0.5 * sequence + 0.3 * jaccard + 0.2 * containment, 4)


def parse_code_list(value: str) -> list[str]:
    value = value.replace("–", "-").replace("—", "-")
    codes: list[str] = []
    for part in re.split(r"[,;/]|\s+ИЛИ\s+|\s+И\s+", value, flags=re.I):
        part = part.strip(" .()[]")
        if not part:
            continue
        match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", part)
        if match:
            start, end = map(int, match.groups())
            if 0 <= end - start <= 100:
                codes.extend(str(item) for item in range(start, end + 1))
            continue
        number = re.fullmatch(r"\d+(?:\.\d+)?", part)
        if number:
            codes.append(number.group(0))
    return list(dict.fromkeys(codes))
