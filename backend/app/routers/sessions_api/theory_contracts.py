from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any


_SCORE_RE = re.compile(
    r"(?:(\d+)\s*/\s*(\d+))|(?:(\d+)\s+из\s+(\d+))",
    re.IGNORECASE,
)
_QUESTION_PROMPT_RE = re.compile(r"(?im)^\s*\*?\*?\s*вопрос\s+\d+\s*/\s*\d+")
_QUESTION_COMMENT_RE = re.compile(r"(?m)^\s*-\s+\*\*.+?\:\*\*")
_TRANSITION_RE = re.compile(
    r"(?i)(переходим к практической части|продолжайте во вкладке практического задания|что дальше)"
)
_COMMENTS_HEADER_RE = re.compile(
    r"(?im)^\s*(?:\*\*)?\s*комментари(?:й|и)\s+(?:по|к)\s+каждому\s+ответу(?:\s+кандидата)?(?:\*\*)?\s*:?\s*$"
)
_ANY_COMMENTS_HEADER_RE = re.compile(
    r"(?im)^\s*(?:\*\*)?\s*комментари(?:й|и)\s+(?:(?:по|к)\s+каждому\s+ответу(?:\s+кандидата)?|по\s+ответам\s+кандидата)(?:\*\*)?\s*:?\s*$"
)
_STRENGTHS_HEADER_RE = re.compile(
    r"(?im)^\s*(?:\*\*)?\s*сильные стороны(?:\s+кандидата)?(?:\*\*)?\s*:?\s*$"
)
_GROWTH_HEADER_RE = re.compile(r"(?im)^\s*(?:\*\*)?\s*зоны роста(?:\*\*)?\s*:?\s*$")
_FINAL_SCORE_RE = re.compile(r"(?im)^\s*(?:\*\*)?\s*(?:итоговая\s+)?оценка")
_MARKDOWN_TABLE_RE = re.compile(r"(?m)^\s*\|.*\|\s*$|^\s*:?-{3,}:?(?:\s*\|\s*:?-{3,}:?)+\s*$")
_SERVICE_NOISE_RE = re.compile(
    r"(?i)(theory_final_message_contract|финальный score_task|tool call|tool dump|"
    r"не используй|не добавляй|верни только|ключевые требования|обязательные поля|"
    r"summary_comment|question_comments|task_id|max_points|<\|channel\|>|<\|message\|>|"
    r"теперь нужно написать итоговое сообщение|напиши итоговое сообщение обычным текстом)"
)
_JSONISH_RE = re.compile(
    r'^\s*[{"\[]|^\s*"(?:task_id|points|max_points|summary_comment|question_comments|comment|comments)"\s*:'
)
_CONTRAST_RE = re.compile(r"\b(?:но|однако|при этом|в то же время)\b", re.IGNORECASE)
_INLINE_SCORE_LINE_RE = re.compile(
    r"(?im)^\s*(?:\*\*)?\s*(?:итоговая\s+)?оценка(?:\s+по\s+теоретическому\s+блоку)?(?:\*\*)?\s*[:\-]?\s*\d+\s*(?:/\s*\d+|из\s+\d+)(?:\s+возможных\s+балл(?:ов|а)?)?\.?\s*$"
)


@dataclass(frozen=True)
class TheoryQuestionComment:
    question_index: int
    question_text: str
    comment: str


@dataclass(frozen=True)
class TheoryFinalMessageContract:
    task_id: str
    points: int
    max_points: int
    summary_comment: str
    question_comments: tuple[TheoryQuestionComment, ...]

    def to_prompt_payload(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "points": self.points,
            "max_points": self.max_points,
            "summary_comment": self.summary_comment,
            "question_comments": [asdict(item) for item in self.question_comments],
        }


def _normalize_question_text(question: Any) -> str:
    if isinstance(question, dict):
        return str(
            question.get("text")
            or question.get("question")
            or question.get("prompt")
            or ""
        ).strip()
    return str(question or "").strip()


def _normalize_question_comments(raw_comments: Any) -> list[str]:
    if not isinstance(raw_comments, list):
        return []
    return [str(item).strip() for item in raw_comments if str(item).strip()]


def _question_comment_title(item: TheoryQuestionComment) -> str:
    question_text = str(item.question_text or "")
    normalized = re.sub(r"\s+", " ", question_text).strip(" \t\r\n.:;!?")
    if not normalized:
        return f"Ответ {item.question_index}"

    if len(normalized) > 84:
        normalized = normalized[:81].rstrip(" ,;:-") + "..."

    return f"{item.question_index}. {normalized}"


def build_theory_question_comments_section(contract: TheoryFinalMessageContract) -> str:
    rendered_items = [
        f"- **{_question_comment_title(item)}:** {item.comment.strip()}"
        for item in contract.question_comments
        if item.comment.strip()
    ]
    if not rendered_items:
        return ""

    return "**Комментарии по каждому ответу**\n\n" + "\n".join(rendered_items)


def _normalize_theory_section_header(line: str) -> str:
    value = re.sub(r"[*_`]+", "", str(line or ""))
    value = re.sub(r"\s+", " ", value).strip(" \t\r\n:.-")
    return value.casefold()


def _classify_theory_section_header(line: str) -> str | None:
    normalized = _normalize_theory_section_header(line)
    if not normalized:
        return None

    if normalized in {
        "комментарий по каждому ответу",
        "комментарий по каждому ответу кандидата",
        "комментарии по каждому ответу",
        "комментарии к каждому ответу",
        "комментарии по ответам кандидата",
        "комментарии по каждому ответу кандидата",
        "комментарии к каждому ответу кандидата",
        "комментарий к каждому ответу",
        "комментарий к каждому ответу кандидата",
    }:
        return "comments"

    if normalized in {
        "сильные стороны",
        "сильные стороны кандидата",
    }:
        return "strengths"

    if normalized == "зоны роста":
        return "growth"

    if normalized in {"оценка", "итоговая оценка"}:
        return "score"

    if normalized.startswith("оценка "):
        return "score"

    if normalized.startswith("итоговая оценка"):
        return "score"

    return None


def _is_theory_score_line(line: str) -> bool:
    stripped = str(line or "").strip()
    if not stripped:
        return False

    if _INLINE_SCORE_LINE_RE.search(stripped):
        return True

    normalized = _normalize_theory_section_header(stripped)
    return "оценка" in normalized and _SCORE_RE.search(stripped) is not None


def _split_theory_message_sections(
    text: str,
) -> tuple[list[str], list[tuple[str, list[str]]]]:
    lines = str(text or "").splitlines()
    headers: list[tuple[str, int]] = []

    for idx, line in enumerate(lines):
        kind = _classify_theory_section_header(line)
        if kind:
            headers.append((kind, idx))

    if not headers:
        return lines, []

    prefix = lines[: headers[0][1]]
    sections: list[tuple[str, list[str]]] = []
    for idx, (kind, start) in enumerate(headers):
        end = headers[idx + 1][1] if idx + 1 < len(headers) else len(lines)
        sections.append((kind, lines[start:end]))

    return prefix, sections


def _is_theory_garbage_prefix_line(line: str) -> bool:
    stripped = str(line or "").strip()
    if not stripped:
        return False

    if _MARKDOWN_TABLE_RE.search(stripped):
        return True

    if _SERVICE_NOISE_RE.search(stripped):
        return True

    if _JSONISH_RE.search(stripped):
        return True

    normalized = _normalize_theory_section_header(stripped)
    if "|" in stripped and ("вопрос" in normalized or "оценка" in normalized):
        return True

    if _is_theory_score_line(stripped):
        return True

    return False


def _clean_theory_prefix_lines(lines: list[str]) -> list[str]:
    cleaned: list[str] = []

    for line in lines:
        if _is_theory_garbage_prefix_line(line):
            while cleaned and not cleaned[-1].strip():
                cleaned.pop()

            if cleaned:
                previous_line = cleaned[-1].strip()
                if previous_line.endswith(":") and _classify_theory_section_header(previous_line) is None:
                    cleaned.pop()
                    while cleaned and not cleaned[-1].strip():
                        cleaned.pop()

            continue

        cleaned.append(line)

    while cleaned and not cleaned[0].strip():
        cleaned = cleaned[1:]

    while cleaned and not cleaned[-1].strip():
        cleaned.pop()

    return cleaned


def sanitize_theory_final_message(
    text: str,
    contract: TheoryFinalMessageContract,
) -> str:
    raw_text = str(text or "").strip()
    if not raw_text:
        return raw_text

    comments_section = build_theory_question_comments_section(contract)
    if not comments_section:
        return raw_text

    prefix_lines, sections = _split_theory_message_sections(raw_text)
    if not sections:
        return f"{raw_text}\n\n{comments_section}".strip()

    blocks: list[str] = []
    prefix_block = "\n".join(_clean_theory_prefix_lines(prefix_lines)).strip()
    if prefix_block:
        blocks.append(prefix_block)

    has_comment_section = any(kind == "comments" for kind, _ in sections)
    inserted_comments = False

    for kind, section_lines in sections:
        if not has_comment_section and not inserted_comments:
            blocks.append(comments_section)
            inserted_comments = True

        if kind == "comments":
            if not inserted_comments:
                blocks.append(comments_section)
                inserted_comments = True
            continue

        section_block = "\n".join(section_lines).strip()
        if section_block:
            blocks.append(section_block)

    if not inserted_comments:
        blocks.append(comments_section)

    return "\n\n".join(blocks).strip()


def _clean_theory_section_body(lines: list[str]) -> list[str]:
    if not lines:
        return []

    body = lines[1:] if _classify_theory_section_header(lines[0]) else list(lines)
    cleaned: list[str] = []
    for line in body:
        stripped = str(line or "").strip()
        if not stripped:
            if cleaned and cleaned[-1] == "":
                continue
            cleaned.append("")
            continue

        if _is_theory_garbage_prefix_line(stripped):
            continue

        cleaned.append(stripped)

    while cleaned and not cleaned[0]:
        cleaned = cleaned[1:]

    while cleaned and not cleaned[-1]:
        cleaned.pop()

    return cleaned


def _extract_section_body(
    sections: list[tuple[str, list[str]]],
    kind: str,
) -> str:
    for section_kind, lines in sections:
        if section_kind != kind:
            continue
        body = "\n".join(_clean_theory_section_body(lines)).strip()
        if body:
            return body
    return ""


def _preferred_section_header(
    sections: list[tuple[str, list[str]]],
    kind: str,
) -> str:
    for section_kind, lines in sections:
        if section_kind != kind or not lines:
            continue
        normalized = _normalize_theory_section_header(lines[0])
        if kind == "strengths" and normalized == "сильные стороны кандидата":
            return "**Сильные стороны кандидата**"
        if kind == "growth":
            return "**Зоны роста**"
    if kind == "strengths":
        return "**Сильные стороны**"
    if kind == "growth":
        return "**Зоны роста**"
    return ""


def _dedupe_lines(lines: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for line in lines:
        normalized = re.sub(r"\s+", " ", str(line or "")).strip().casefold()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(str(line).strip())
    return unique


def _split_strength_and_growth(comment: str) -> tuple[str | None, str | None]:
    normalized = re.sub(r"\s+", " ", str(comment or "")).strip(" \t\r\n.-")
    if not normalized:
        return None, None

    match = _CONTRAST_RE.search(normalized)
    if match:
        left = normalized[: match.start()].strip(" ,;:-")
        right = normalized[match.end() :].strip(" ,;:-")
        strength = left if left else None
        growth = right if right else None
        return strength, growth

    lowered = normalized.casefold()
    positive_markers = (
        "верно",
        "правильно",
        "корректно",
        "уверенно",
        "хорошо",
        "понима",
        "объяснил",
        "объяснила",
        "раскрыл",
        "раскрыла",
        "привёл",
        "привела",
        "отметил",
        "отметила",
        "показал",
        "показала",
    )
    negative_markers = (
        "не ",
        "нехват",
        "недостат",
        "ошиб",
        "перепут",
        "слаб",
        "неточ",
        "зона роста",
        "стоит",
        "нужно",
        "полезно",
        "лучше",
    )

    has_positive = any(marker in lowered for marker in positive_markers)
    has_negative = any(marker in lowered for marker in negative_markers)

    if has_positive and not has_negative:
        return normalized, None
    if has_negative and not has_positive:
        return None, normalized
    return normalized, None


def _derive_strengths_and_growth(
    contract: TheoryFinalMessageContract,
) -> tuple[list[str], list[str]]:
    strengths: list[str] = []
    growth: list[str] = []

    for item in contract.question_comments:
        comment = str(item.comment or "").strip()
        if not comment:
            continue

        strength_part, growth_part = _split_strength_and_growth(comment)

        if strength_part:
            strengths.append(
                f"- По вопросу {item.question_index}: {strength_part.rstrip(' .!?:;')}."
            )
        if growth_part:
            growth.append(
                f"- По вопросу {item.question_index}: {growth_part.rstrip(' .!?:;')}."
            )

    strengths = _dedupe_lines(strengths)[:3]
    growth = _dedupe_lines(growth)[:3]

    if not strengths:
        strengths = [
            "- В ответах есть база для дальнейшего обсуждения темы и точки, на которые можно опереться в следующем раунде."
        ]

    if not growth:
        growth = [
            "- Для более сильного результата важно точнее раскрывать детали ответов и подкреплять их более конкретными формулировками."
        ]

    return strengths, growth


def _build_theory_intro_block(
    prefix_block: str,
    contract: TheoryFinalMessageContract,
) -> str:
    prefix_lines = [
        str(line or "").strip()
        for line in str(prefix_block or "").splitlines()
        if str(line or "").strip() and not _is_theory_score_line(line)
    ]
    cleaned_prefix = re.sub(r"\s+", " ", " ".join(prefix_lines)).strip()
    cleaned_prefix = re.sub(_SCORE_RE, "", cleaned_prefix).strip(" \t\r\n,;:-")
    cleaned_prefix = re.sub(r"\s{2,}", " ", cleaned_prefix).strip()

    summary = re.sub(r"\s+", " ", contract.summary_comment or "").strip()
    summary = re.sub(_SCORE_RE, "", summary).strip(" \t\r\n,;:-")
    summary = re.sub(r"\s{2,}", " ", summary).strip()

    if cleaned_prefix:
        return cleaned_prefix

    if summary:
        return f"Теоретический блок завершён. {summary}"

    return "Теоретический блок завершён."


def finalize_theory_final_message(
    text: str,
    contract: TheoryFinalMessageContract,
) -> str:
    sanitized = sanitize_theory_final_message(text, contract)
    prefix_lines, sections = _split_theory_message_sections(sanitized)
    prefix_block = "\n".join(_clean_theory_prefix_lines(prefix_lines)).strip()

    intro_block = _build_theory_intro_block(prefix_block, contract)
    comments_block = build_theory_question_comments_section(contract).strip()

    strengths_body = _extract_section_body(sections, "strengths")
    growth_body = _extract_section_body(sections, "growth")
    strengths_header = _preferred_section_header(sections, "strengths")
    growth_header = _preferred_section_header(sections, "growth")
    derived_strengths, derived_growth = _derive_strengths_and_growth(contract)

    strengths_block = (
        f"{strengths_header}\n\n" + (strengths_body or "\n".join(derived_strengths))
    ).strip()
    growth_block = (
        f"{growth_header}\n\n" + (growth_body or "\n".join(derived_growth))
    ).strip()
    score_block = f"**Итоговая оценка по теоретическому блоку:** {contract.points}/{contract.max_points}."

    blocks = [
        intro_block,
        comments_block,
        strengths_block,
        growth_block,
        score_block,
    ]
    return "\n\n".join(block for block in blocks if block).strip()


def build_theory_final_message_contract(
    task: dict[str, Any] | None,
    score_result_payload: dict[str, Any] | None,
) -> TheoryFinalMessageContract | None:
    if not task or task.get("type") != "theory" or not isinstance(score_result_payload, dict):
        return None

    raw_points = score_result_payload.get("points", 0)
    try:
        points = int(round(float(raw_points or 0)))
    except Exception:
        points = 0

    max_points = int(task.get("max_points", 10) or 10)
    summary_comment = str(score_result_payload.get("comment") or "").strip()

    raw_comments = _normalize_question_comments(score_result_payload.get("comments"))
    if not raw_comments:
        aggregated = score_result_payload.get("aggregated") or {}
        raw_comments = _normalize_question_comments(aggregated.get("comments"))

    question_comments: list[TheoryQuestionComment] = []
    for idx, question in enumerate(task.get("questions") or [], start=1):
        question_text = _normalize_question_text(question)
        comment = raw_comments[idx - 1] if idx - 1 < len(raw_comments) else ""
        if not question_text and not comment:
            continue
        question_comments.append(
            TheoryQuestionComment(
                question_index=idx,
                question_text=question_text,
                comment=comment,
            )
        )

    return TheoryFinalMessageContract(
        task_id=str(task.get("id") or ""),
        points=points,
        max_points=max_points,
        summary_comment=summary_comment,
        question_comments=tuple(question_comments),
    )


def build_theory_final_message_prompt(contract: TheoryFinalMessageContract) -> str:
    payload = json.dumps(contract.to_prompt_payload(), ensure_ascii=False, indent=2)
    return (
        "Финальный score_task по теоретическому блоку уже успешно выполнен. "
        "Теперь нужно написать итоговое сообщение обычным текстом, без tool-call.\n\n"
        "Напиши сообщение своими словами, не по шаблону и не как технический отчёт.\n"
        "Обязательно включи в него:\n"
        "- короткую фразу о том, что теоретический блок завершён\n"
        "- отдельный блок с комментарием по каждому ответу кандидата\n"
        "- блок с сильными сторонами кандидата\n"
        "- блок с зонами роста\n"
        f"- финальную строку с оценкой {contract.points}/{contract.max_points}\n\n"
        "Ключевые требования:\n"
        '- используй ровно один заголовок для блока комментариев: "Комментарии по каждому ответу"\n'
        "- комментарии по вопросам должны буквально воспроизводить по смыслу соответствующие элементы массива question_comments\n"
        "- используй только один блок комментариев по вопросам, без дублей и альтернативных версий этого блока\n"
        "- не сокращай конкретные замечания до общих слов\n"
        "- не объединяй несколько вопросов в один пункт\n"
        "- не пиши числовые оценки внутри комментариев по отдельным вопросам\n"
        "- не используй markdown-таблицы, символы '|' или строки-разделители '---' в блоке комментариев\n"
        "- не добавляй переход к практической части: это покажет система отдельно\n"
        "- не используй JSON, tool dump, channel tags или служебную разметку\n"
        "- не задавай новых вопросов кандидату\n\n"
        "<THEORY_FINAL_MESSAGE_CONTRACT>\n"
        f"{payload}\n"
        "</THEORY_FINAL_MESSAGE_CONTRACT>"
    )


def build_theory_final_message_repair_prompt(
    contract: TheoryFinalMessageContract,
    *,
    previous_text: str,
    score_issue: bool,
    quality_issue: bool,
) -> str:
    issues: list[str] = []
    if score_issue:
        issues.append(
            f"- в тексте нет точной итоговой оценки {contract.points}/{contract.max_points} "
            "или встречается конфликтующий балл"
        )
    if quality_issue:
        issues.append("- структура сообщения нарушена или часть обязательных блоков пропущена")

    payload = json.dumps(contract.to_prompt_payload(), ensure_ascii=False, indent=2)
    issue_block = "\n".join(issues) if issues else "- исправь сообщение по контракту"

    return (
        "Предыдущий итоговый текст по теоретическому блоку нужно переписать.\n"
        "Проблемы:\n"
        f"{issue_block}\n\n"
        "Перепиши сообщение заново, обычным текстом, без tool-call.\n"
        "Сохрани смысл комментариев по каждому вопросу из контракта и используй точную итоговую оценку.\n"
        'Используй ровно один заголовок для блока комментариев: "Комментарии по каждому ответу".\n'
        "Не добавляй переход к практической части, не задавай новых вопросов, не пиши JSON или tool dump.\n"
        "Оставь только один блок комментариев по вопросам, без дублей и альтернативных заголовков вроде 'Комментарии к каждому ответу'.\n"
        "Не используй markdown-таблицы, символы '|' или строки-разделители '---' в блоке комментариев.\n\n"
        "Предыдущий вариант:\n"
        f"{previous_text.strip()}\n\n"
        "<THEORY_FINAL_MESSAGE_CONTRACT>\n"
        f"{payload}\n"
        "</THEORY_FINAL_MESSAGE_CONTRACT>"
    )


def theory_final_message_has_wrong_score(
    text: str,
    contract: TheoryFinalMessageContract,
) -> bool:
    if not text:
        return True

    found_scores: list[tuple[int, int]] = []
    for match in _SCORE_RE.finditer(text):
        left = match.group(1) or match.group(3)
        right = match.group(2) or match.group(4)
        if left is None or right is None:
            continue
        try:
            found_scores.append((int(left), int(right)))
        except ValueError:
            continue

    if not found_scores:
        return True

    expected = (contract.points, contract.max_points)
    return any(item != expected for item in found_scores)


def theory_final_message_too_generic(
    text: str,
    contract: TheoryFinalMessageContract,
) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return True

    if _QUESTION_PROMPT_RE.search(text or ""):
        return True

    if _TRANSITION_RE.search(text or ""):
        return True

    if _MARKDOWN_TABLE_RE.search(text or ""):
        return True

    if "теорет" not in normalized or "заверш" not in normalized:
        return True

    if "сильные стороны" not in normalized:
        return True

    if "зоны роста" not in normalized:
        return True

    expected_question_comments = sum(
        1 for item in contract.question_comments if item.comment.strip()
    )
    rendered_question_comments = len(_QUESTION_COMMENT_RE.findall(text or ""))
    if expected_question_comments and rendered_question_comments < expected_question_comments:
        return True

    return False
