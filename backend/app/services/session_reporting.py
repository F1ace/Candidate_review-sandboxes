from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from .. import models
from .lm_client import lm_client

RELEVANT_SYSTEM_PREFIXES = (
    "Переход к следующему заданию:",
)

IRRELEVANT_SYSTEM_PREFIXES = (
    "Code execution result for ",
    "run_code ->",
    "run_sql ->",
    "score_task ->",
    "Проверка завершена.",
    "Проверка не завершена автоматически.",
    "Ошибка сервиса LM Studio:",
)


@dataclass(slots=True)
class TaskSnapshot:
    task_id: str
    title: str
    task_type: str
    score: float | None
    max_points: float
    ratio: float | None
    score_comment: str
    transcript_excerpt: list[str]


@dataclass(slots=True)
class ReportContext:
    session_id: str
    candidate_id: str | None
    role_name: str
    role_slug: str
    scenario_name: str
    scenario_slug: str
    difficulty: str | None
    started_at: datetime | None
    finished_at: datetime | None
    duration_minutes: int
    overall_score: float
    overall_max: float
    overall_ratio: float | None
    scored_tasks: int
    total_tasks: int
    candidate_message_count: int
    model_message_count: int
    task_snapshots: list[TaskSnapshot]
    transcript: list[dict[str, Any]]


def effective_task_max_points(task: dict[str, Any]) -> float:
    if task.get("type") == "theory":
        return 10.0
    max_points = float(task.get("max_points") or 0)
    return max_points if max_points > 0 else 10.0


def _task_by_id(scenario: models.Scenario, task_id: str) -> dict[str, Any] | None:
    for task in scenario.tasks or []:
        if task.get("id") == task_id:
            return task
    return None


def _collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _truncate_text(text: str, limit: int = 280) -> str:
    text = _collapse_whitespace(text)
    if len(text) <= limit:
        return text
    shortened = text[: limit - 3].rsplit(" ", 1)[0].rstrip(".,;: ")
    if not shortened:
        shortened = text[: limit - 3]
    return f"{shortened}..."


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _message_relevant_for_report(msg: models.Message) -> bool:
    if msg.sender in {"candidate", "model"}:
        return bool((msg.text or "").strip())
    if msg.sender != "system":
        return False

    text = (msg.text or "").strip()
    if not text:
        return False
    if text.startswith(IRRELEVANT_SYSTEM_PREFIXES):
        return False
    return text.startswith(RELEVANT_SYSTEM_PREFIXES)


def _serialize_transcript(messages: list[models.Message]) -> list[dict[str, Any]]:
    transcript: list[dict[str, Any]] = []
    for msg in messages:
        if not _message_relevant_for_report(msg):
            continue
        transcript.append(
            {
                "sender": msg.sender,
                "task_id": msg.task_id,
                "text": _truncate_text(msg.text, limit=320),
            }
        )
    return transcript


def _latest_final_scores(
    session_id: str,
    db: Session,
) -> dict[str, models.Score]:
    latest: dict[str, models.Score] = {}
    entries = (
        db.query(models.Score)
        .filter(
            models.Score.session_id == session_id,
            models.Score.is_final.is_(True),
        )
        .order_by(models.Score.created_at.asc(), models.Score.id.asc())
        .all()
    )
    for entry in entries:
        latest[entry.task_id] = entry
    return latest


def build_report_context(session: models.Session, db: Session, now: datetime | None = None) -> ReportContext:
    messages = (
        db.query(models.Message)
        .filter(models.Message.session_id == session.id)
        .order_by(models.Message.created_at.asc(), models.Message.id.asc())
        .all()
    )
    transcript = _serialize_transcript(messages)
    final_scores = _latest_final_scores(session.id, db)

    task_snapshots: list[TaskSnapshot] = []
    total_score = 0.0
    total_max = 0.0
    scored_tasks = 0

    for task in session.scenario.tasks or []:
        task_id = task.get("id")
        if not task_id:
            continue

        score_entry = final_scores.get(task_id)
        score_value = float(score_entry.points) if score_entry else None
        max_points = effective_task_max_points(task)
        ratio = (score_value / max_points) if score_value is not None and max_points > 0 else None
        comment = _collapse_whitespace(score_entry.comment or "") if score_entry else ""

        excerpt = [
            _truncate_text(msg.text, limit=220)
            for msg in messages
            if msg.sender in {"candidate", "model"} and msg.task_id == task_id and (msg.text or "").strip()
        ][:6]

        if score_value is not None:
            total_score += score_value
            total_max += max_points
            scored_tasks += 1
        else:
            total_max += max_points

        task_snapshots.append(
            TaskSnapshot(
                task_id=task_id,
                title=str(task.get("title") or task_id),
                task_type=str(task.get("type") or "unknown"),
                score=score_value,
                max_points=max_points,
                ratio=round(ratio, 4) if ratio is not None else None,
                score_comment=comment,
                transcript_excerpt=excerpt,
            )
        )

    active_end = session.finished_at or now or _utcnow_naive()
    if session.started_at:
        duration_minutes = max(1, int((active_end - session.started_at).total_seconds() // 60) or 1)
    else:
        duration_minutes = 0

    candidate_message_count = sum(1 for item in transcript if item["sender"] == "candidate")
    model_message_count = sum(1 for item in transcript if item["sender"] == "model")
    overall_ratio = round(total_score / total_max, 4) if total_max > 0 else None

    return ReportContext(
        session_id=session.id,
        candidate_id=session.candidate_id,
        role_name=session.role.name,
        role_slug=session.role.slug,
        scenario_name=session.scenario.name,
        scenario_slug=session.scenario.slug,
        difficulty=session.scenario.difficulty,
        started_at=session.started_at,
        finished_at=session.finished_at,
        duration_minutes=duration_minutes,
        overall_score=round(total_score, 1),
        overall_max=round(total_max, 1),
        overall_ratio=overall_ratio,
        scored_tasks=scored_tasks,
        total_tasks=len(task_snapshots),
        candidate_message_count=candidate_message_count,
        model_message_count=model_message_count,
        task_snapshots=task_snapshots,
        transcript=transcript,
    )


def _recommendation_label(overall_ratio: float | None) -> str:
    if overall_ratio is None:
        return "Недостаточно данных"
    if overall_ratio >= 0.85:
        return "Сильный кандидат"
    if overall_ratio >= 0.7:
        return "Рекомендуется следующий этап"
    if overall_ratio >= 0.55:
        return "Пограничный результат"
    return "Есть заметные риски"


def _overall_tone(overall_ratio: float | None) -> str:
    if overall_ratio is None:
        return "Интервью прошло частично, поэтому отчёт опирается на ограниченный объём данных."
    if overall_ratio >= 0.85:
        return "Кандидат стабильно демонстрировал сильный уровень и держал качество ответов на протяжении всего сценария."
    if overall_ratio >= 0.7:
        return "По сценарию видно уверенную рабочую базу и достаточный запас для продолжения процесса."
    if overall_ratio >= 0.55:
        return "Результат неоднородный: сильные эпизоды есть, но по нескольким блокам требуется дополнительная проверка."
    return "По интервью накопилось несколько заметных пробелов, поэтому к итоговой оценке стоит относиться осторожно."


def _task_label(task_type: str) -> str:
    return {
        "theory": "теоретический блок",
        "coding": "кодовое задание",
        "sql": "SQL-блок",
    }.get(task_type, task_type)


def _task_fallback_summary(task: TaskSnapshot) -> str:
    if task.score is None:
        return f"По заданию «{task.title}» итоговая оценка ещё не зафиксирована, поэтому раздел отмечен как незавершённый."
    if task.score_comment:
        return _truncate_text(task.score_comment, limit=260)
    ratio = task.ratio or 0.0
    if ratio >= 0.85:
        return f"Задание «{task.title}» выполнено уверенно: кандидат показал сильный результат в формате «{_task_label(task.task_type)}»."
    if ratio >= 0.65:
        return f"По заданию «{task.title}» результат рабочий, но не без шероховатостей: базовое понимание подтверждено, глубина раскрыта не полностью."
    return f"Задание «{task.title}» выявило заметные зоны роста: ответ или решение не закрыли ключевые ожидания сценария."


def _derive_highlights(task: TaskSnapshot) -> list[str]:
    highlights: list[str] = []
    if task.score is not None:
        highlights.append(f"Баллы: {task.score:g}/{task.max_points:g}")
    if task.transcript_excerpt:
        highlights.append(f"В отчёте учтён диалог по этому блоку ({len(task.transcript_excerpt)} реплик).")
    if task.score_comment:
        highlights.append(_truncate_text(task.score_comment, limit=180))

    if not highlights:
        highlights.append("Для этого блока пока нет достаточного количества артефактов, поэтому вывод ограничен.")
    return highlights[:3]


def _build_strengths(task_snapshots: list[TaskSnapshot]) -> list[str]:
    strong_tasks = [task for task in task_snapshots if task.ratio is not None]
    strong_tasks.sort(key=lambda item: item.ratio or 0.0, reverse=True)

    strengths = [
        f"Лучший результат показан в блоке «{task.title}» ({task.score:g}/{task.max_points:g})."
        for task in strong_tasks[:3]
        if task.score is not None
    ]

    if not strengths:
        strengths.append("Интервью содержит слишком мало оценённых блоков, чтобы надёжно выделить сильные стороны.")
    return strengths


def _build_growth_areas(task_snapshots: list[TaskSnapshot]) -> list[str]:
    weak_tasks = [task for task in task_snapshots if task.ratio is not None]
    weak_tasks.sort(key=lambda item: item.ratio if item.ratio is not None else 1.0)

    growth_areas = [
        f"Дополнительная проверка нужна по блоку «{task.title}» ({task.score:g}/{task.max_points:g})."
        for task in weak_tasks[:3]
        if task.score is not None and (task.ratio or 0.0) < 0.7
    ]

    if not growth_areas:
        growth_areas.append("Критичных провалов по итоговым баллам не видно, но полезен следующий практический раунд на реальном кейсе.")
    return growth_areas


def build_fallback_report_payload(context: ReportContext) -> dict[str, Any]:
    theory_tasks = [task for task in context.task_snapshots if task.task_type == "theory"]
    practice_tasks = [task for task in context.task_snapshots if task.task_type in {"coding", "sql"}]

    strengths = _build_strengths(context.task_snapshots)
    growth_areas = _build_growth_areas(context.task_snapshots)
    recommendation_label = _recommendation_label(context.overall_ratio)

    sections = [
        {
            "title": "Итог по сценарию",
            "summary": (
                f"Сценарий «{context.scenario_name}» завершён с результатом {context.overall_score:g}/{context.overall_max:g}. "
                f"{_overall_tone(context.overall_ratio)}"
            ),
            "highlights": [
                f"Оценено заданий: {context.scored_tasks}/{context.total_tasks}",
                f"Длительность интервью: {context.duration_minutes} мин",
                f"Реплик кандидата: {context.candidate_message_count}",
            ],
        },
        {
            "title": "Теоретическая часть",
            "summary": (
                "Теоретический контур отражает, насколько кандидат держит базу, аргументацию и точность формулировок. "
                + (
                    "Финальные оценки по theory-блокам уже позволяют судить о глубине ответов."
                    if theory_tasks
                    else "В сценарии не было теоретического блока."
                )
            ),
            "highlights": [
                (
                    f"{task.title}: {task.score:g}/{task.max_points:g}"
                    if task.score is not None
                    else f"{task.title}: оценка не сохранена"
                )
                for task in theory_tasks[:3]
            ]
            or ["Теоретические задания в этом сценарии отсутствуют."],
        },
        {
            "title": "Практическое исполнение",
            "summary": (
                "Практические задания показывают, насколько кандидат превращает знания в решение: через код, SQL и качество разбора результата. "
                + (
                    "Именно эта часть лучше всего отражает рабочую пригодность."
                    if practice_tasks
                    else "Практических блоков в сценарии не было."
                )
            ),
            "highlights": [
                (
                    f"{task.title}: {task.score:g}/{task.max_points:g}"
                    if task.score is not None
                    else f"{task.title}: оценка не сохранена"
                )
                for task in practice_tasks[:3]
            ]
            or ["Практические задания в этом сценарии отсутствуют."],
        },
        {
            "title": "Рекомендация по следующему шагу",
            "summary": (
                f"Текущий автоматический вывод: «{recommendation_label}». "
                "Решение о продолжении лучше принимать вместе с задачами, где кандидат показал сильный и слабый контекст."
            ),
            "highlights": (strengths[:2] + growth_areas[:2])[:4],
        },
    ]

    task_breakdown = [
        {
            "task_id": task.task_id,
            "title": task.title,
            "task_type": task.task_type,
            "score": round(task.score, 1) if task.score is not None else None,
            "max_points": round(task.max_points, 1),
            "ratio": task.ratio,
            "summary": _task_fallback_summary(task),
            "highlights": _derive_highlights(task),
            "score_comment": task.score_comment or None,
        }
        for task in context.task_snapshots
    ]

    return {
        "session_id": context.session_id,
        "generated_at": _utcnow_naive(),
        "started_at": context.started_at,
        "finished_at": context.finished_at,
        "duration_minutes": context.duration_minutes,
        "candidate_id": context.candidate_id,
        "role_name": context.role_name,
        "role_slug": context.role_slug,
        "scenario_name": context.scenario_name,
        "scenario_slug": context.scenario_slug,
        "difficulty": context.difficulty,
        "headline": f"{context.role_name}: автоматический отчёт по сценарию «{context.scenario_name}»",
        "executive_summary": (
            f"Отчёт собран автоматически по всей сессии: сценарию, диалогу, итоговым баллам и комментариям оценки. "
            f"Зафиксировано {context.scored_tasks} оценённых блоков из {context.total_tasks}, суммарный результат — "
            f"{context.overall_score:g}/{context.overall_max:g}. {_overall_tone(context.overall_ratio)}"
        ),
        "overall_assessment": (
            f"Сценарий «{context.scenario_name}» показывает кандидата в роли «{context.role_name}» через теоретические и практические блоки. "
            f"{_overall_tone(context.overall_ratio)} "
            "Финальный вывод опирается не только на баллы, но и на характер ответов, качество формулировок и комментарии к заданиям."
        ),
        "closing_note": (
            "Автоматический отчёт полезен как быстрый слой принятия решения, "
            "но финальный вердикт стоит подтверждать просмотром исходного диалога и артефактов по самым важным заданиям."
        ),
        "recommendation_label": recommendation_label,
        "recommendation_summary": (
            f"Система относит сессию к категории «{recommendation_label}». "
            "Если нужен следующий шаг, лучше сфокусировать его на темах с самым большим разбросом между сильными и слабыми блоками."
        ),
        "generation_mode": "fallback",
        "overall_score": context.overall_score,
        "overall_max": context.overall_max,
        "overall_ratio": context.overall_ratio,
        "scored_tasks": context.scored_tasks,
        "total_tasks": context.total_tasks,
        "candidate_message_count": context.candidate_message_count,
        "model_message_count": context.model_message_count,
        "strengths": strengths,
        "growth_areas": growth_areas,
        "sections": sections,
        "task_breakdown": task_breakdown,
    }


def _extract_json_object(text: str) -> dict[str, Any] | None:
    if not text:
        return None

    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.DOTALL)
    payload = fenced.group(1) if fenced else None
    if payload is None:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        payload = text[start : end + 1]

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None

    return parsed if isinstance(parsed, dict) else None


def _clean_string_list(value: Any, limit: int = 5) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        cleaned = _truncate_text(item, limit=180)
        if cleaned:
            result.append(cleaned)
        if len(result) >= limit:
            break
    return result


def _clean_section_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []

    sections: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        title = _truncate_text(str(item.get("title") or ""), limit=80)
        summary = _truncate_text(str(item.get("summary") or ""), limit=420)
        highlights = _clean_string_list(item.get("highlights"), limit=4)
        if not title or not summary:
            continue
        sections.append({"title": title, "summary": summary, "highlights": highlights})
        if len(sections) >= 6:
            break
    return sections


def merge_llm_report_payload(
    raw_payload: dict[str, Any],
    fallback_payload: dict[str, Any],
    context: ReportContext,
) -> dict[str, Any]:
    merged = dict(fallback_payload)

    headline = _truncate_text(str(raw_payload.get("headline") or ""), limit=140)
    executive_summary = _truncate_text(str(raw_payload.get("executive_summary") or ""), limit=900)
    overall_assessment = _truncate_text(str(raw_payload.get("overall_assessment") or ""), limit=1400)
    closing_note = _truncate_text(str(raw_payload.get("closing_note") or ""), limit=500)
    recommendation_label = _truncate_text(str(raw_payload.get("recommendation_label") or ""), limit=80)
    recommendation_summary = _truncate_text(str(raw_payload.get("recommendation_summary") or ""), limit=600)

    if headline:
        merged["headline"] = headline
    if executive_summary:
        merged["executive_summary"] = executive_summary
    if overall_assessment:
        merged["overall_assessment"] = overall_assessment
    if closing_note:
        merged["closing_note"] = closing_note
    if recommendation_label:
        merged["recommendation_label"] = recommendation_label
    if recommendation_summary:
        merged["recommendation_summary"] = recommendation_summary

    strengths = _clean_string_list(raw_payload.get("strengths"), limit=5)
    growth_areas = _clean_string_list(raw_payload.get("growth_areas"), limit=5)
    sections = _clean_section_list(raw_payload.get("sections"))

    if strengths:
        merged["strengths"] = strengths
    if growth_areas:
        merged["growth_areas"] = growth_areas
    if sections:
        merged["sections"] = sections

    raw_breakdown_map: dict[str, dict[str, Any]] = {}
    if isinstance(raw_payload.get("task_breakdown"), list):
        for item in raw_payload["task_breakdown"]:
            if not isinstance(item, dict):
                continue
            task_id = str(item.get("task_id") or "").strip()
            if task_id:
                raw_breakdown_map[task_id] = item

    fallback_items = {item["task_id"]: item for item in merged.get("task_breakdown", []) if item.get("task_id")}
    task_breakdown: list[dict[str, Any]] = []
    for task in context.task_snapshots:
        base_item = dict(fallback_items.get(task.task_id) or {})
        raw_item = raw_breakdown_map.get(task.task_id) or {}
        summary = _truncate_text(str(raw_item.get("summary") or ""), limit=420)
        highlights = _clean_string_list(raw_item.get("highlights"), limit=4)

        if summary:
            base_item["summary"] = summary
        if highlights:
            base_item["highlights"] = highlights
        task_breakdown.append(base_item)

    if task_breakdown:
        merged["task_breakdown"] = task_breakdown

    merged["generation_mode"] = "llm"
    return merged


def _report_prompt_payload(context: ReportContext) -> dict[str, Any]:
    return {
        "session": {
            "session_id": context.session_id,
            "candidate_id": context.candidate_id,
            "role": context.role_name,
            "role_slug": context.role_slug,
            "scenario": context.scenario_name,
            "scenario_slug": context.scenario_slug,
            "difficulty": context.difficulty,
            "duration_minutes": context.duration_minutes,
        },
        "metrics": {
            "overall_score": context.overall_score,
            "overall_max": context.overall_max,
            "overall_ratio": context.overall_ratio,
            "scored_tasks": context.scored_tasks,
            "total_tasks": context.total_tasks,
            "candidate_message_count": context.candidate_message_count,
            "model_message_count": context.model_message_count,
        },
        "tasks": [
            {
                "task_id": task.task_id,
                "title": task.title,
                "task_type": task.task_type,
                "score": task.score,
                "max_points": task.max_points,
                "ratio": task.ratio,
                "score_comment": task.score_comment,
                "transcript_excerpt": task.transcript_excerpt,
            }
            for task in context.task_snapshots
        ],
        "transcript": context.transcript,
    }


def _generate_llm_payload(context: ReportContext) -> dict[str, Any] | None:
    prompt_payload = _report_prompt_payload(context)
    messages = [
        {
            "role": "system",
            "content": (
                "Ты готовишь качественный итоговый отчёт по техническому интервью на русском языке. "
                "Верни только JSON без markdown и без пояснений. "
                "Не придумывай новые баллы и не противоречь переданным метрикам. "
                "Сделай содержательный, профессиональный и пригодный для PDF-отчёта текст.\n\n"
                "Ожидаемая схема JSON:\n"
                "{\n"
                '  "headline": "короткий заголовок",\n'
                '  "executive_summary": "1-2 абзаца с выжимкой",\n'
                '  "overall_assessment": "подробная оценка по интервью",\n'
                '  "recommendation_label": "краткий вердикт",\n'
                '  "recommendation_summary": "что делать дальше",\n'
                '  "strengths": ["...", "..."],\n'
                '  "growth_areas": ["...", "..."],\n'
                '  "sections": [\n'
                '    {"title": "Название секции", "summary": "Абзац", "highlights": ["...", "..."]}\n'
                "  ],\n"
                '  "task_breakdown": [\n'
                '    {"task_id": "ID", "summary": "Итог по задаче", "highlights": ["...", "..."]}\n'
                "  ],\n"
                '  "closing_note": "финальная ремарка"\n'
                "}\n"
                "Секции должны быть разными по смыслу и не дублировать друг друга. "
                "Фокус: сила кандидата, риски, глубина знаний, практическая применимость, рекомендация по следующему этапу."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(prompt_payload, ensure_ascii=False),
        },
    ]

    response = lm_client.chat(messages, tools=None, temperature=0.3)
    content = response.get("choices", [{}])[0].get("message", {}).get("content", "")
    return _extract_json_object(content)


def generate_session_report(session: models.Session, db: Session) -> dict[str, Any]:
    context = build_report_context(session, db)
    fallback_payload = build_fallback_report_payload(context)

    try:
        raw_payload = _generate_llm_payload(context)
    except Exception:
        raw_payload = None

    if not raw_payload:
        return fallback_payload

    return merge_llm_report_payload(raw_payload, fallback_payload, context)
