import json
import re
from typing import Any, Optional

from ... import models
def _build_system_prompt(session: models.Session, rag_available: bool) -> str:
    """Construct a strict instruction block for the model."""
    scenario = session.scenario
    role = session.role
    tasks_descr = "\n".join(
        [
            f"- {t.get('id')}: {t.get('type')} {t.get('title')} (max {t.get('max_points', 'n/a')})"
            for t in scenario.tasks or []
        ]
    )
    theory_bank = [
        {
            "id": t.get("id"),
            "title": t.get("title"),
            "questions": t.get("questions") or [],
            "ask_in_order": bool(t.get("ask_in_order")),
        }
        for t in (scenario.tasks or [])
        if t.get("type") == "theory"
    ]
    tool_hint = (
        "rag_search для материалов сценария, web_search для общих фактов."
        if rag_available
        else "документов нет — НЕ вызывай rag_search; для валидации используй знания и web_search."
    )
    return (
    "<SYSTEM>\n"
    "Ты — AI-интервьюер/оркестратор. Тебе поручено вести собеседование с кандидатом на определенную роль и по определенному сценарию. Также есть и сложность сценария. "
    "Работай только в рамках переданных ролей, задач и контекста.\n"
    f"Контекст: роль {role.name} ({role.slug}); сценарий {scenario.name} ({scenario.slug}); уровень {scenario.difficulty}.\n"
    "\n"
    "<BEHAVIOR_CORE>\n"
    "1) Говори по-русски. Начни с приветствия, объясни всё, что знаешь, роль, сценарий и цель интервью. Не повторяй вступление и правила, если они уже звучали в истории.\n"
    "2) Двигайся строго по задачам сценария. Не перескакивай, не возвращайся назад."
        "Первое задание начни сразу после приветствия (без ожидания команды). "
        "Дальше переходи к следующему заданию только после команды пользователя «Следующее».\n"
    "3) Помни о контексте диалога: не задавай вопросы, уже звучавшие ранее; задавай только уточняющие или новые.\n"
    "4) Подсказки: если hints_allowed=true и ответ частичный — сначала дай подсказку/уточняющий вопрос, дождись ответа, после этого оценивай.\n"
    "5) Код и SQL вводятся только в редакторе ниже чата. Никогда не проси прислать код/SQL в чат. После Submit редактирование запрещено.\n"
    "6) Используй свои знания. Если RAG недоступен — не вызывай rag_search. web_search используй только для факт-чекинга при необходимости.\n"
    "7) После вызова любого инструмента обязательно вернись в чат с понятным выводом/комментарием.\n"
    "\n"
    "<SCORING_POLICY>\n"
    "8) Выставляй баллы через score_task(task_id, points, comment). Баллы строго в допустимых границах; comment не пустой. Для theory — после уточняющих вопросов; для coding/sql practice — после результатов sandbox.\n"
    "9) Если points < max_points, ты обязан задать кандидату 1–2 углубляющих вопроса по теме, направленных на проверку глубины понимания. Если уточняющие вопросы заданы и поставлена оценка - нельзя задавать новые уточняющие вопросы.\n"
    "Задай их после оценки, но до требования нажать «Следующее».\n"
    "10) После ответа на углубляющий вопрос дай краткий финальный комментарий и попроси кандидата нажать «Следующее».\n"
    "11) Если задание уже оценено, не разрешай обсуждать его дальше — мягко перенаправляй к кнопке «Следующее».\n"
    "Ответ должен быть верным, содержательным, отвечать всем стандартам и фактам. Для проверки нужно использовать инструменты. Не позволяй кандидату делать вид, что ответ правильный с помощью фраз вроде (даёт правильный ответ), (отвечает верно) и тому подобных. Ответ в действиетльности должен быть провалидирован тобой"
    "\n"
    "<TOOL_POLICY>\n"
    f"12) Доступные инструменты: rag_search ({'доступно' if rag_available else 'недоступно'}), "
    "web_search (валидация фактов), run_code, run_sql, score_task.\n"
    f"Инструменты выбирай уместно: {tool_hint}\n"
    "Для coding-проверки (после Submit) обязательный пайплайн: "
    "submit_code → server_run_tests → score_task.\n"
    "Для SQL-проверки обязательный шаг: run_sql → score_task.\n"
    "13) Не вызывай инструмент, если он недоступен.\n"
    "\n"
    "<TASKFLOW>\n"
    "14) Теоретические задания:\n"
    "- Если у текущей theory-задачи есть список questions (см. <THEORY_BANK>), то задавай вопросы СТРОГО по порядку, по одному за раз.\n"
    "- После ответа кандидата на вопрос i задавай вопрос i+1 (не вызывая score_task).\n"
    "- НЕЛЬЗЯ просить нажимать «Следующее» между вопросами внутри одной theory-задачи.\n"
    "- Только после ответа на ПОСЛЕДНИЙ вопрос: дай краткий итог по теории и вызови score_task ровно один раз для этой theory-задачи.\n"
    "15) Кодовые задания: не проси вставлять код в чат. После Submit анализируй результаты песочницы: "
    "успех → code review; провал → объясни ошибки. Затем score_task → углубляющий вопрос (если не максимум).\n"
    "16) SQL-задания: выполняются только через SQL-песочницу. Ошибки интерпретируй и объясняй. Затем score_task → углубляющий вопрос.\n"
    "17) Всегда возвращайся в чат после технических операций.\n"
    "\n"
    "<FINAL_POLICY>\n"
    "18) После завершения всех задач сформируй summary: сильные стороны, зоны роста, ошибки, общий результат, "
    "и придумай творческое итоговое задание по слабой теме.\n"
    "\n"
    "\n<THEORY_BANK>\n"
    "Список теоретических задач и вопросов (используй это как источник вопросов):\n"
    f"{json.dumps(theory_bank, ensure_ascii=False)}\n"
    "</THEORY_BANK>\n"
    "\n"
    "<CONSTRAINTS>\n"
    "Не включай <think> в ответы пользователю.\n"
    "Если вступление уже было — не повторяй.\n"
    "</CONSTRAINTS>\n"
    "</SYSTEM>"
)

def _strip_think(content: Optional[str]) -> str:
    if not content:
        return ""
    if "</think>" in content:
        return content.split("</think>", 1)[1].strip()
    return content.replace("<think>", "").strip()

def _strip_intro(text: str, intro_done: bool) -> str:
    """Cut repetitive greetings when intro already done."""
    if not intro_done or not text:
        return text
    intro_patterns = [
        "привет", "добрый день", "здравствуйте", "я проведу собеседование", "формат состоит",
        "сегодня мы пройдём", "мы проведём", "давайте приступим", "начнём с теории",
    ]
    lowered = text.lower()
    for pat in intro_patterns:
        if lowered.startswith(pat):
            # Remove first sentence
            parts = text.split("\n", 1)
            return parts[1] if len(parts) > 1 else ""
    return text

def _extract_inline_tool_call(content: str) -> tuple[str, dict[str, Any]] | None:
    """
    Fallback: некоторые модели печатают tool-call текстом, например:
    <|start|>assistant<channel>commentary to=score_task <|constrain|>json<message|>{...}
    или:
    <|start|>assistant<channel>commentary to=functions.score_task<constrain>json<message>{...}

    Возвращает (tool_name, args) или None.
    """
    if not content:
        return None

    # 1) Вытаскиваем имя "to=...". Поддерживаем точку: functions.score_task
    m = re.search(r"to=([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)?)", content)
    if not m:
        return None

    raw_name = m.group(1)
    # если пришло functions.score_task — берём последнюю часть
    tool_name = raw_name.split(".")[-1].strip()

    # 2) Вытаскиваем JSON между { ... }
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    raw_json = content[start : end + 1]
    try:
        payload = json.loads(raw_json)
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None

    return tool_name, payload

def _analyze_candidate_message(text: str) -> list[str]:
    """Detect placeholder/meta/roleplay/offtopic/empty/code_in_chat/sql_in_chat flags."""
    flags: list[str] = []
    t = text.strip().lower()
    if not t:
        flags.append("empty")
    placeholder_phrases = [
        "(отвечает правильно)",
        "(правильный ответ)",
        "код верный",
        "решение корректное",
        "ответ правильный",
        "(пишет правильный код)",
        "(solution)",
    ]
    if any(p in t for p in placeholder_phrases):
        flags.append("placeholder")
    if len(t) < 40 and ("регресс" not in t and "join" not in t and "select" not in t):
        flags.append("too_short")
    roleplay = ["представим", "я бот", "как модель", "как ассистент", "роль"]
    if any(p in t for p in roleplay):
        flags.append("roleplay")
    if "def " in t or "print(" in t or "import " in t:
        flags.append("code_in_chat")
    if "select " in t or "from " in t:
        flags.append("sql_in_chat")
    return flags

