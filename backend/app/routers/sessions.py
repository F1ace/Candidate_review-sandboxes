import json
import logging
import re
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import SessionLocal, get_db
from ..services import sandbox, web_search
from ..services.lm_client import lm_client
from ..services.rag import search_documents
from pydantic import BaseModel

__DEBUG_MARKER__ = "HOST_SESSIONS_2026_02_20"

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sessions", tags=["sessions"])

class PracticeCodeRequest(BaseModel):
    task_id: str
    language: str = "python"
    code: str

class PracticeSqlRequest(BaseModel):
    task_id: str
    sql_scenario_id: str
    query: str

# Tools exposed to the model (LM Studio)
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "rag_search",
            "description": "Поиск по загруженной документации сценария.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Поиск в интернете для валидации ответа кандидата.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_code",
            "description": "Запустить код кандидата в песочнице и вернуть stdout/stderr/exit_code. Используй для проверки решения по coding-задачам.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "ID задания (если известно)"},
                    "language": {"type": "string", "description": "Язык, например python"},
                    "code": {"type": "string", "description": "Исходный код кандидата"},
                    "tests_id": {"type": "string", "description": "ID тестов (если задано в task)"}
                },
                "required": ["language", "code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_sql",
            "description": "Выполнить SQL-запрос кандидата в песочнице по sql_scenario_id и вернуть результат (columns/rows) или ошибку.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "ID задания (если известно)"},
                    "sql_scenario_id": {"type": "string", "description": "ID SQL-сценария из БД"},
                    "query": {"type": "string", "description": "SQL запрос кандидата"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "build_sanity_checks",
            "description": "Сгенерировать SanityChecks (базовые проверки) для coding-задачи. Возвращает python-код с функцией run_sanity(ns).",
            "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "language": {"type": "string"}
            },
            "required": ["task_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "generate_test_cases",
            "description": "Сгенерировать N тест-кейсов (структурированные steps/expect) для coding-задачи.",
            "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "n": {"type": "integer"}
            },
            "required": ["task_id", "n"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "compose_harness",
            "description": "Собрать единый python-harness: кандидатский код + sanity + runner для кейсов. Результат печатает JSON с passrate.",
            "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "language": {"type": "string"},
                "candidate_code": {"type": "string"},
                "sanity_code": {"type": "string"},
                "cases": {"type": "array", "items": {"type": "object"}}
            },
            "required": ["task_id", "candidate_code", "sanity_code", "cases"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "score_task",
            "description": "Поставить баллы за задание кандидату.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "points": {"type": "number"},
                    "comment": {"type": "string"},
                },
                "required": ["task_id", "points"],
            },
        },
    },
]


def _get_task_by_id(scenario: models.Scenario, task_id: str) -> Optional[dict[str, Any]]:
    for task in scenario.tasks or []:
        if task.get("id") == task_id:
            return task
    return None


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
    "8) Выставляй баллы через score_task(task_id, points, comment). Баллы строго в допустимых границах; comment не пустой. Оценку необходимо выставлять исключительно после уточняющих вопросов.\n"
    "9) Если points < max_points, ты обязан задать кандидату 1–2 углубляющих вопроса по теме, направленных на проверку глубины понимания. Если уточняющие вопросы заданы и поставлена оценка - нельзя задавать новые уточняющие вопросы.\n"
    "Задай их после оценки, но до требования нажать «Следующее».\n"
    "10) После ответа на углубляющий вопрос дай краткий финальный комментарий и попроси кандидата нажать «Следующее».\n"
    "11) Если задание уже оценено, не разрешай обсуждать его дальше — мягко перенаправляй к кнопке «Следующее».\n"
    "Ответ должен быть верным, содержательным, отвечать всем стандартам и фактам. Для проверки нужно использовать инструменты. Не позволяй кандидату делать вид, что ответ правильный с помощью фраз вроде (даёт правильный ответ), (отвечает верно) и тому подобных. Ответ в действиетльности должен быть провалидирован тобой"
    "\n"
    "<TOOL_POLICY>\n"
    f"12) Доступные инструменты: rag_search ({'доступно' if rag_available else 'недоступно'}), "
    "web_search (валидация фактов), build_sanity_checks, generate_test_cases, compose_harness, run_code, run_sql, score_task.\n"
    f"Инструменты выбирай уместно: {tool_hint}\n"
    "Для coding-проверки (после Submit) обязательный пайплайн: "
    "build_sanity_checks → generate_test_cases → compose_harness → run_code → score_task.\n"
    "Для SQL-проверки обязательный шаг: run_sql → score_task.\n"
    "13) Не вызывай инструмент, если он недоступен.\n"
    "\n"
    "<TASKFLOW>\n"
    "14) Теоретические задания: задай вопрос → получи ответ → анализ → score_task → углубляющий вопрос (если не максимум).\n"
    "15) Кодовые задания: не проси вставлять код в чат. После Submit анализируй результаты песочницы: "
    "успех → code review; провал → объясни ошибки. Затем score_task → углубляющий вопрос (если не максимум).\n"
    "16) SQL-задания: выполняются только через SQL-песочницу. Ошибки интерпретируй и объясняй. Затем score_task → углубляющий вопрос.\n"
    "17) Всегда возвращайся в чат после технических операций.\n"
    "\n"
    "<FINAL_POLICY>\n"
    "18) После завершения всех задач сформируй summary: сильные стороны, зоны роста, ошибки, общий результат, "
    "и придумай творческое итоговое задание по слабой теме.\n"
    "\n"
    "<TASKS>\n"
    "Список задач сценария:\n"
    f"{tasks_descr}\n"
    "</TASKS>\n"
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
    Fallback: некоторые модели печатают tool-call как текст вида:
    <|channel|>commentary to=score_task <|constrain|>json<message|>{...}
    Возвращает (tool_name, args) или None.
    """
    if not content:
        return None

    m = re.search(r"to=([a-zA-Z_][a-zA-Z0-9_]*)", content)
    if not m:
        return None
    tool_name = m.group(1)

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


def _control_state(session: models.Session, history: list[models.Message]) -> dict[str, Any]:
    intro_done = any(m.sender == "model" for m in history)
    scores = session.scores or {}
    task_status = {tid: "scored" for tid in scores.keys()}
    current_task = session.current_task_id or (session.scenario.tasks[0]["id"] if session.scenario.tasks else "нет")
    awaiting_next = current_task in task_status
    return {
        "intro_done": intro_done,
        "current_task_id": current_task,
        "task_status": task_status,
        "hint_count": {},
        "awaiting_next_click": awaiting_next,
        "code_submitted": {},
        "sql_submitted": {},
    }


def _semantic_memory(session: models.Session) -> dict[str, Any]:
    """Derive simple strengths/weaknesses from scores."""
    strengths: set[str] = set()
    weaknesses: set[str] = set()
    issues: list[dict[str, str]] = []
    scores = session.scores or {}
    for task in session.scenario.tasks or []:
        tid = task.get("id")
        if not tid or tid not in scores:
            continue
        pts = scores[tid]
        max_pts = task.get("max_points") or 1
        ratio = float(pts) / float(max_pts)
        topics = task.get("related_topics") or []
        if ratio >= 0.8:
            strengths.update(topics)
        elif ratio <= 0.5:
            weaknesses.update(topics)
            for t in topics:
                issues.append({"key": f"weak_{t}", "text": f"Низкий балл по теме {t}"})
    return {
        "strengths": list(strengths),
        "weaknesses": list(weaknesses),
        "issues": issues,
    }


def _episodic_memory(history: list[models.Message]) -> list[str]:
    events: list[str] = []
    for m in history[-60:]:
        if m.sender == "tool":
            events.append(f"tool:{m.text[:120]}")
        elif m.sender == "system" and "result" in m.text:
            events.append(f"system:{m.text[:120]}")
    return events[-30:]


def _convert_history(messages: list[models.Message]) -> list[dict[str, Any]]:
    converted = []
    for msg in messages:
        if msg.sender == "candidate":
            role = "user"
        elif msg.sender == "model":
            role = "assistant"
        else:
            role = "system"
        converted.append({"role": role, "content": msg.text})
    return converted


def _conversation_snapshot(session: models.Session, history: list[models.Message]) -> str:
    """Short, explicit state for the model to avoid repetition."""
    control = _control_state(session, history)
    sem = _semantic_memory(session)
    episodic = _episodic_memory(history)
    last_user = next((m for m in reversed(history) if m.sender == "candidate"), None)
    last_user_text = (last_user.text if last_user else "нет последних вопросов")[:200]
    last_model = next((m for m in reversed(history) if m.sender == "model"), None)
    last_model_text = (last_model.text if last_model else "нет")[:200]
    return (
        "<CONTROL_STATE>"
        f"<INTRO_DONE>{control['intro_done']}</INTRO_DONE>"
        f"<CURRENT_TASK_ID>{control['current_task_id']}</CURRENT_TASK_ID>"
        f"<AWAITING_NEXT_CLICK>{control['awaiting_next_click']}</AWAITING_NEXT_CLICK>"
        f"<TASK_STATUS>{json.dumps(control['task_status'], ensure_ascii=False)}</TASK_STATUS>"
        f"<HINT_COUNT>{json.dumps(control['hint_count'], ensure_ascii=False)}</HINT_COUNT>"
        f"<CODE_SUBMITTED>{json.dumps(control['code_submitted'], ensure_ascii=False)}</CODE_SUBMITTED>"
        f"<SQL_SUBMITTED>{json.dumps(control['sql_submitted'], ensure_ascii=False)}</SQL_SUBMITTED>"
        "</CONTROL_STATE>"
        "<SEMANTIC_MEMORY>"
        f"<STRENGTHS>{', '.join(sem.get('strengths', []))}</STRENGTHS>"
        f"<WEAKNESSES>{', '.join(sem.get('weaknesses', []))}</WEAKNESSES>"
        f"<ISSUES>{json.dumps(sem.get('issues', []), ensure_ascii=False)}</ISSUES>"
        "</SEMANTIC_MEMORY>"
        "<EPISODIC_MEMORY>"
        f"{json.dumps(episodic, ensure_ascii=False)}"
        "</EPISODIC_MEMORY>"
        f"<LAST_USER>{last_user_text}</LAST_USER>"
        f"<LAST_MODEL>{last_model_text}</LAST_MODEL>"
        "Не повторяй уже сказанное; продолжай диалог логично и не начинай новую задачу без явного перехода."
    )

def _theory_tasks(scenario: models.Scenario) -> list[dict[str, Any]]:
    return [t for t in (scenario.tasks or []) if t.get("type") == "theory"]


def _first_practice_task(scenario: models.Scenario) -> Optional[dict[str, Any]]:
    for t in (scenario.tasks or []):
        if t.get("type") in ("coding", "sql"):
            return t
    return None


def _theory_is_complete(session: models.Session) -> bool:
    theory = _theory_tasks(session.scenario)
    if not theory:
        return True
    scores = session.scores or {}
    return all((t.get("id") in scores) for t in theory)


def _theory_summary_text(session: models.Session) -> str:
    theory = _theory_tasks(session.scenario)
    scores = session.scores or {}

    earned = 0.0
    maximum = 0.0
    for t in theory:
        tid = t.get("id")
        max_pts = float(t.get("max_points") or 0)
        maximum += max_pts
        earned += float(scores.get(tid, 0))

    # если max_points не заполнены, всё равно покажем сколько заданий оценено
    if maximum <= 0:
        return f"Теория завершена. Оценено заданий: {sum(1 for t in theory if t.get('id') in scores)}/{len(theory)}."
    return f"Теория завершена. Итог: {earned:g}/{maximum:g}."


def _apply_score(session: models.Session, args: dict[str, Any], db: Session) -> dict[str, Any]:
    task_id = args.get("task_id")
    points = float(args.get("points", 0))
    comment = args.get("comment")
    task = _get_task_by_id(session.scenario, task_id)
    if not task:
        return {"error": f"Task {task_id} not found in scenario"}
    max_points = task.get("max_points", 0)
    if points < 0 or points > max_points:
        return {"error": f"Points should be within [0, {max_points}]"}
    score = models.Score(session_id=session.id, task_id=task_id, points=points, comment=comment)
    current_scores = session.scores or {}
    session.scores = {**current_scores, task_id: points}
    db.add(score)
    db.commit()
    db.refresh(score)
    return {"ok": True, "task_id": task_id, "points": points, "comment": comment}

def _extract_json_object(text: str) -> dict[str, Any]:
    """
    LM иногда оборачивает JSON в текст или ```json.
    Вырезаем первый JSON-объект вида {...} и парсим.
    """
    if not text:
        return {}
    # вырезать ```json ... ```
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if m:
        text = m.group(1)

    # найти первый объект {...}
    m2 = re.search(r"(\{.*\})", text, flags=re.DOTALL)
    if not m2:
        return {}
    raw = m2.group(1)
    try:
        return json.loads(raw)
    except Exception:
        return {}

def _llm_json(messages: list[dict[str, Any]]) -> dict[str, Any]:
    resp = lm_client.chat(messages, tools=[], temperature=0.2)
    content = resp["choices"][0]["message"].get("content") or ""
    return _extract_json_object(content)  # у тебя уже есть в файле


def _build_sanity_checks_with_llm(task: dict[str, Any], language: str) -> dict[str, Any]:
    iface = task.get("interface") or {}
    entrypoint = iface.get("entrypoint") or iface.get("class_name") or "TaskQueue"
    desc = task.get("description_for_candidate") or task.get("description") or task.get("prompt") or ""

    def _extract_code(obj: dict[str, Any]) -> str:
        sc = obj.get("sanity_checks") or {}
        return (
            sc.get("code")
            or obj.get("code")
            or obj.get("sanity_code")
            or ""
        )

    def _fallback_code() -> str:
        # Минимальный sanity: проверяем, что entrypoint существует и что у него есть базовые методы
        return (
            "def run_sanity(ns):\n"
            "    failures = []\n"
            "    passed = 0\n"
            "    failed = 0\n"
            f"    if '{entrypoint}' not in ns:\n"
            f"        return {{'passed': 0, 'failed': 1, 'failures': ['Missing {entrypoint} in namespace']}}\n"
            f"    cls = ns.get('{entrypoint}')\n"
            "    try:\n"
            "        obj = cls()\n"
            "    except Exception as e:\n"
            "        return {'passed': 0, 'failed': 1, 'failures': [f'Cannot instantiate: {e}']}\n"
            "    for m in ['enqueue','dequeue','ack','nack']:\n"
            "        if not hasattr(obj, m):\n"
            "            failed += 1\n"
            "            failures.append(f'Missing method: {m}')\n"
            "        else:\n"
            "            passed += 1\n"
            "    return {'passed': passed, 'failed': failed, 'failures': failures}\n"
        )

    base_prompt = (
        "Верни СТРОГО JSON без markdown и без пояснений.\n"
        "Нужно: JSON со структурой:\n"
        "{\n"
        '  "sanity_checks": {\n'
        '    "code": "...."\n'
        "  }\n"
        "}\n"
        "Где code (python) содержит функцию:\n"
        "def run_sanity(ns):\n"
        "  # ns — namespace кандидата\n"
        "  # return {\"passed\": int, \"failed\": int, \"failures\": [str,...]}\n"
        "SanityChecks должны проверить интерфейс и 3-6 базовых сценариев.\n"
        f"Entrypoint: {entrypoint}\n"
        f"Задание: {task.get('id')} {task.get('title','')}\n"
        f"Описание: {desc}\n"
    )

    # 1-я попытка
    obj = _llm_json([
        {"role": "system", "content": "Отвечай только JSON-объектом."},
        {"role": "user", "content": base_prompt},
    ])
    code = _extract_code(obj).strip()

    # 2-я попытка (ретрай), если пусто
    if not code:
        retry_prompt = (
            base_prompt
            + "\nВАЖНО: верни JSON с ключами exactly sanity_checks.code. Без текста.\n"
            + 'Пример формата: {"sanity_checks": {"code": "def run_sanity(ns):\\n    ..."}}\n'
        )
        obj2 = _llm_json([
            {"role": "system", "content": "Отвечай только JSON-объектом."},
            {"role": "user", "content": retry_prompt},
        ])
        code = _extract_code(obj2).strip()

    if not code:
        code = _fallback_code()

    return {"entrypoint": entrypoint, "code": code}

def _case_rules_from_interface(task: dict[str, Any]) -> str:
    iface = task.get("interface") or {}
    methods = iface.get("methods") or []
    method_names = [m.get("name") for m in methods if m.get("name")]
    method_names_str = ", ".join(method_names) if method_names else "(нет данных)"

    # Простые подсказки по returns
    return_hints = []
    for m in methods:
        n = m.get("name")
        r = (m.get("returns") or "").strip()
        if n and r:
            return_hints.append(f"- {n}: возвращает {r}")

    return (
        "ВАЖНО (контракт интерфейса):\n"
        f"- Разрешённые методы: {method_names_str}.\n"
        "- НЕ используй методы, которых нет в списке.\n"
        "- НЕ проверяй исключения и НЕ используй expect: 'error'.\n"
        "- Каждый step должен содержать call из разрешённых методов.\n"
        + ("\nПодсказки по ожидаемым возвращаемым значениям:\n" + "\n".join(return_hints) + "\n"
           if return_hints else "")
    )


def _build_test_cases_with_llm(task: dict[str, Any], n: int) -> dict[str, Any]:
    iface = task.get("interface") or {}
    entrypoint = iface.get("entrypoint") or iface.get("class_name") or "TaskQueue"
    desc = task.get("description_for_candidate") or task.get("description") or task.get("prompt") or ""
    task_text = desc
    rules = _case_rules_from_interface(task)
    prompt = (
        "Ты генерируешь тест-кейсы для проверки кода кандидата.\n"
        "Верни СТРОГО JSON с ключом cases (list).\n"
        f"КАНОНИЧЕСКОЕ ОПИСАНИЕ ЗАДАНИЯ:\n{task_text}\n\n"
        f"ИНТЕРФЕЙС (контракт):\n{json.dumps(task.get('interface', {}), ensure_ascii=False)}\n\n"
        f"{rules}\n"
        "Формат cases:\n"
        "[{name, init:{class,args}, steps:[{call,args,expect}], notes}]\n"
    )
    obj = _llm_json([
        {"role": "system", "content": "Отвечай только JSON-объектом."},
        {"role": "user", "content": prompt},
    ])
    cases = obj.get("cases")
    if not isinstance(cases, list):
        cases = []
    return {"entrypoint": entrypoint, "cases": cases[:n]}

def _filter_cases(task: dict[str, Any], cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Убирает кейсы, которые нарушают контракт интерфейса (методы не из interface.methods)
    или используют запрещённые ожидания (expect == 'error').
    Делает проверку универсальной для любых задач по их task['interface'].
    """
    iface = task.get("interface") or {}
    allowed = {m.get("name") for m in (iface.get("methods") or []) if m.get("name")}

    # Если контракта нет — не режем, чтобы не сломать старые сценарии
    if not allowed:
        return cases

    cleaned: list[dict[str, Any]] = []
    for c in cases:
        steps = c.get("steps") or []
        ok = True
        for s in steps:
            call = s.get("call")
            if call not in allowed:
                ok = False
                break
            if s.get("expect") == "error":
                ok = False
                break
        if ok:
            cleaned.append(c)

    return cleaned

def _default_init_args_from_interface(task: dict) -> list:
    """
    Пытаемся извлечь дефолтные аргументы конструктора из interface.init_args.
    Поддерживаем несколько форматов, чтобы не зависеть от структуры в JSON.
    """
    interface = (task or {}).get("interface") or {}
    init_args_spec = interface.get("init_args") or []

    defaults = []
    for item in init_args_spec:
        # item может быть:
        # - {"name": "...", "default": 123}
        # - {"name": "...", "example": 123}
        # - просто значение (редко)
        if isinstance(item, dict):
            if "default" in item:
                defaults.append(item["default"])
            elif "example" in item:
                defaults.append(item["example"])
            else:
                # если ничего нет — лучше не гадать, пропустим
                # (можно добавить эвристику, но это риск)
                pass
        else:
            # если это уже значение
            defaults.append(item)

    return defaults


def _apply_default_init_args(task: dict, cases: list[dict]) -> list[dict]:
    """
    Если в test case нет init.args, а по интерфейсу есть дефолты — подставляем.
    """
    defaults = _default_init_args_from_interface(task)
    if not defaults:
        return cases

    out = []
    for tc in cases or []:
        if not isinstance(tc, dict):
            continue
        init = tc.get("init")
        if not isinstance(init, dict):
            init = {}
        args = init.get("args")
        kwargs = init.get("kwargs")

        if not args:
            init["args"] = defaults

        if not isinstance(kwargs, dict):
            init["kwargs"] = {}

        tc["init"] = init
        out.append(tc)

    return out

def _compose_harness_code(*, candidate_code: str, sanity_code: str, cases: list[dict[str, Any]], entrypoint: str) -> str:
    """
    Safe harness:
    - candidate/sanity/cases встраиваются через json.dumps + json.loads
      (устойчиво к ''' и \"\"\" внутри кода кандидата)
    - ns содержит __name__='candidate_solution' чтобы не срабатывал if __name__ == '__main__'
    - ВСЕГДА печатает в stdout строку: RESULT_JSON: {...}
    """
    import json as _json
    import textwrap as _textwrap

    cand_json = _json.dumps(candidate_code or "", ensure_ascii=False)
    sanity_json = _json.dumps(sanity_code or "", ensure_ascii=False)
    cases_json = _json.dumps(cases or [], ensure_ascii=False)
    entry_json = _json.dumps(entrypoint or "", ensure_ascii=False)

    harness = f"""
# --- AUTO-GENERATED HARNESS (safe) ---
import json, sys, traceback

CANDIDATE = json.loads({cand_json})
SANITY    = json.loads({sanity_json})
CASES     = json.loads({cases_json})
ENTRYPOINT = json.loads({entry_json})

def _safe_exec(src: str, ns: dict, label: str):
    try:
        exec(src, ns)
        return True, None
    except Exception as e:
        return False, f"{{label}} exec error: {{type(e).__name__}}: {{e}}\\n" + traceback.format_exc()

def _load_candidate():
    ns = {{"__name__": "candidate_solution"}}
    ok, err = _safe_exec(CANDIDATE, ns, "candidate")
    if not ok:
        raise RuntimeError(err)
    return ns

def _load_sanity(ns: dict):
    ok, err = _safe_exec(SANITY, ns, "sanity")
    if not ok:
        raise RuntimeError(err)
    fn = ns.get("run_sanity")
    if not callable(fn):
        raise RuntimeError("sanity_code must define run_sanity(ns)")
    return fn

def _run_cases(ns: dict, cases: list[dict]):
    res = {{"passed": 0, "failed": 0, "failures": []}}
    cls = ns.get(ENTRYPOINT)
    if not callable(cls):
        res["failed"] += 1
        res["failures"].append(f"Entrypoint '{{ENTRYPOINT}}' not found or not callable")
        return res

    for i, tc in enumerate(cases or []):
        name = tc.get("name") or f"case_{{i}}"
        try:
            init = tc.get("init") or {{}}
            args = init.get("args") or []
            kwargs = init.get("kwargs") or {{}}
            obj = cls(*args, **kwargs)

            for step in (tc.get("steps") or []):
                call = step.get("call")
                s_args = step.get("args") or []
                s_kwargs = step.get("kwargs") or {{}}
                has_expect = "expect" in step
                expect = step.get("expect")

                fn = getattr(obj, call, None)
                if not callable(fn):
                    raise RuntimeError(f"Method not found: {{call}}")

                got = fn(*s_args, **s_kwargs)

                if has_expect and got != expect:
                    raise AssertionError(f"Expected {{expect!r}}, got {{got!r}} for {{call}}")

            res["passed"] += 1

        except Exception as e:
            res["failed"] += 1
            res["failures"].append(f"{{name}}: {{type(e).__name__}}: {{e}}")

    return res

def main():
    out = {{
        "sanity": {{"passed": 0, "failed": 0, "failures": []}},
        "cases":  {{"passed": 0, "failed": 0, "failures": []}},
        "passrate": 0.0,
        "traceback": None,
    }}

    try:
        ns = _load_candidate()
        run_sanity = _load_sanity(ns)

        sanity_res = run_sanity(ns)
        if not isinstance(sanity_res, dict):
            out["sanity"]["failed"] = 1
            out["sanity"]["failures"] = ["run_sanity(ns) must return dict"]
        else:
            out["sanity"]["passed"] = int(sanity_res.get("passed", 0))
            out["sanity"]["failed"] = int(sanity_res.get("failed", 0))
            out["sanity"]["failures"] = list(sanity_res.get("failures", []))[:50]

        cases_res = _run_cases(ns, CASES)
        out["cases"]["passed"] = int(cases_res.get("passed", 0))
        out["cases"]["failed"] = int(cases_res.get("failed", 0))
        out["cases"]["failures"] = list(cases_res.get("failures", []))[:50]

        total = out["sanity"]["passed"] + out["sanity"]["failed"] + out["cases"]["passed"] + out["cases"]["failed"]
        ok = out["sanity"]["passed"] + out["cases"]["passed"]
        out["passrate"] = (ok / total) if total else 0.0

    except Exception:
        out["traceback"] = traceback.format_exc()

    print("RESULT_JSON:", json.dumps(out, ensure_ascii=False))
    failed = out["sanity"]["failed"] + out["cases"]["failed"]
    raise SystemExit(0 if failed == 0 and out["traceback"] is None else 1)

if __name__ == "__main__":
    main()
"""

    # Ключевое: убираем любой ведущий отступ и пустые строки в начале
    return _textwrap.dedent(harness).lstrip()


def _dispatch_tool_call(session, tool_call, db):
    fn = tool_call.get("function") or {}
    name = fn.get("name") or ""
    name = (name or "").strip().replace("…", "")

    raw_args = fn.get("arguments")
    # 1) безопасно распарсить arguments -> dict
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
    except Exception:
        args = {}
    if not isinstance(args, dict):
        args = {}

    # unwrap
    if name == "functions" and "name" in args and "arguments" in args:
        real_name = args.get("name")
        real_args = args.get("arguments")
        if isinstance(real_args, str):
            try:
                real_args = json.loads(real_args)
            except Exception:
                real_args = {}
        if isinstance(real_name, str) and real_name:
            name = real_name
        args = real_args if isinstance(real_args, dict) else {}

    # ВСЕГДА проставляем task_id после unwrap
    if "task_id" not in args and session.current_task_id:
        args["task_id"] = session.current_task_id

    if name == "rag_search":
        if not session.scenario.rag_corpus_id:
            return {"error": "No RAG corpus configured for this scenario. Use web_search instead."}
        docs = db.query(models.Document).filter_by(rag_corpus_id=session.scenario.rag_corpus_id).all()
        if not docs:
            return {"error": "No RAG documents available. Use web_search instead."}
        doc_dicts = [{"id": d.id, "filename": d.filename, "content": d.content} for d in docs]
        results = search_documents(doc_dicts, args.get("query", ""), args.get("top_k", 3))
        return {"results": [r.model_dump() for r in results]}
    if name == "web_search":
        return {"results": web_search.web_search(args.get("query", ""), args.get("top_k", 3))}
    if name == "run_code":
        language = (args.get("language") or "python").strip()
        code = args.get("code") or ""
        task_id = args.get("task_id") or session.current_task_id

        # если tests_id не передан — попытка взять из task
        tests_id = args.get("tests_id")
        if not tests_id and task_id:
            task = _get_task_by_id(session.scenario, task_id)
            if task:
                tests_id = task.get("tests_id") or task.get("tests")  # на случай другого ключа

        # sandbox.run_code ожидает tests_id строкой — передача пустой, если нет
        result = sandbox.run_code(language=language, code=code, tests_id=str(tests_id or ""))
        result["task_id"] = task_id
        result["language"] = language
        return result
    if name == "run_sql":
        query = args.get("query") or ""
        task_id = args.get("task_id") or session.current_task_id

        sql_scenario_id = args.get("sql_scenario_id")
        if not sql_scenario_id and task_id:
            task = _get_task_by_id(session.scenario, task_id)
            if task:
                sql_scenario_id = task.get("sql_scenario_id") or task.get("scenario_id")

        if not sql_scenario_id:
            return {"error": "sql_scenario_id is required (provide it or ensure current task has sql_scenario_id)"}

        result = sandbox.run_sql(sql_scenario_id=str(sql_scenario_id), query=query)
        result["task_id"] = task_id
        result["sql_scenario_id"] = str(sql_scenario_id)
        return result
    if name == "build_sanity_checks":
        task_id = args.get("task_id") or session.current_task_id
        task = _get_task_by_id(session.scenario, task_id) if task_id else None
        if not task:
            return {"error": "Task not found"}
        language = (args.get("language") or "python").strip()
        out = _build_sanity_checks_with_llm(task, language)
        out["task_id"] = task_id
        out["language"] = language
        return out
    if name == "generate_test_cases":
        task_id = args.get("task_id") or session.current_task_id
        task = _get_task_by_id(session.scenario, task_id) if task_id else None
        if not task:
            return {"error": "Task not found"}

        n = int(args.get("n") or 10)
        out = _build_test_cases_with_llm(task, n)

        # 1) фильтруем невалидные кейсы
        cases = _filter_cases(task, out.get("cases") or [])

        # 2) подставляем дефолтные init.args, если LLM забыл их указать
        cases = _apply_default_init_args(task, cases)

        if not cases:
            return {"error": "generate_test_cases: produced 0 valid cases after filtering"}
        out["cases"] = cases
        out["task_id"] = task_id
        out["n"] = n
        return out

    if name == "compose_harness":
        task_id = args.get("task_id") or session.current_task_id
        candidate_code = (args.get("candidate_code") or "").strip()
        sanity_code = (args.get("sanity_code") or "").strip()
        cases = args.get("cases")

        if not candidate_code:
            return {"error": "compose_harness: candidate_code is empty"}
        if not sanity_code:
            return {"error": "compose_harness: sanity_code is empty"}
        if "def run_sanity" not in sanity_code:
            return {"error": "compose_harness: sanity_code missing 'def run_sanity(ns)'"}
        if not isinstance(cases, list):
            return {"error": "compose_harness: cases must be a list"}
        if len(cases) == 0:
            return {"error": "compose_harness: cases is empty"}

        task = _get_task_by_id(session.scenario, task_id) if task_id else None
        if not task:
            return {"error": "Task not found"}

        cases = _apply_default_init_args(task, cases)

        harness = _compose_harness_code(
            task=task,
            candidate_code=candidate_code,
            sanity_code=sanity_code,
            cases=cases,
        )

        entrypoint = (task.get("interface") or {}).get("entrypoint") or (task.get("interface") or {}).get("class_name") or "TaskQueue"
        return {"task_id": task_id, "entrypoint": entrypoint, "harness_code": harness}
    
    if name == "score_task":
        return _apply_score(session, args, db)

    return {"error": f"Unsupported tool {name}"}

def _practice_agent_review(
    *,
    session: models.Session,
    db: Session,
    instruction: str,
    task_id: str,
) -> dict[str, Any]:
    history_db = (
        db.query(models.Message)
        .filter_by(session_id=session.id)
        .order_by(models.Message.created_at)
        .all()
    )

    rag_available = False
    if session.scenario.rag_corpus_id:
        rag_available = db.query(models.Document).filter_by(rag_corpus_id=session.scenario.rag_corpus_id).count() > 0

    system_prompt = _build_system_prompt(session, rag_available)
    snapshot = _conversation_snapshot(session, history_db)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": snapshot},
    ]
    messages.extend(_convert_history(history_db))

    messages.append({
    "role": "system",
    "content": (
        "PRACTICE_MODE.\n"
        "Кандидат УЖЕ нажал Submit и отправил код на проверку.\n"
        "Запрещено повторять условие задачи, запрещено просить нажать Submit, запрещено просить 'Следующее'.\n"
        "Ты обязан выполнить инструментальный пайплайн до run_code и только потом отвечать.\n"
        "Если инструмент вернул ошибку/пустой результат — исправь и продолжи, а не отвечай общими словами."
        )
    })

    messages.append({"role": "user", "content": instruction})

    # --- extract candidate_code from instruction ---
    candidate_code = ""
    marker = "КОД КАНДИДАТА:"
    if marker in instruction:
        candidate_code = instruction.split(marker, 1)[1].strip()
    if not candidate_code.strip():
        return {
            "reply": "Внутренняя ошибка: candidate_code не извлечён из instruction (маркер не найден или код пуст).",
            "tool_results": tool_results_for_ui,
        }

    tool_results_for_ui: list[dict[str, Any]] = []

    last_sanity_code: str | None = None
    last_cases: list[dict[str, Any]] | None = None
    last_harness_code: str | None = None

    def _last_run_code_report() -> dict | None:
        for tr in reversed(tool_results_for_ui):
            if tr.get("name") != "run_code":
                continue

            res = tr.get("result") or {}
            stdout = (res.get("stdout") or "").strip()
            if not stdout:
                return None

            marker = "RESULT_JSON:"
            if marker in stdout:
                payload = stdout.split(marker, 1)[1].strip()
                try:
                    return json.loads(payload)
                except Exception:
                    return None

            # fallback: если stdout — это просто JSON без маркера
            if stdout.startswith("{") and stdout.endswith("}"):
                try:
                    return json.loads(stdout)
                except Exception:
                    return None

            return None
        return None
    
    def _has_tool(name: str) -> bool:
        return any((tr.get("name") == name) for tr in tool_results_for_ui)


    # Несколько итераций: модель может вызывать tools цепочкой
    max_iters = 8
    final_msg: dict[str, Any] | None = None

    for _ in range(max_iters):
        resp = lm_client.chat(messages, tools=TOOLS)
        assistant_msg = resp["choices"][0]["message"]
        messages.append(assistant_msg)

        tool_calls = assistant_msg.get("tool_calls") or []

         # Fallback: модель может напечатать tool-call текстом, без tool_calls
        if not tool_calls:
            content = assistant_msg.get("content") or ""
            inline = _extract_inline_tool_call(content)
            if inline:
                tool_name, args = inline
                tool_calls = [{
                    "id": "inline_toolcall",
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                }]
                assistant_msg["content"] = None  # чтобы не показать "<|channel|>..."

        # Если tool_calls нет — модель пытается дать финальный ответ.
        # Но в практике мы НЕ принимаем финал, пока не был выполнен обязательный run_code.
        if not tool_calls:
            content = (assistant_msg.get("content") or "").strip()

            if not _has_tool("compose_harness"):
                messages.append({
                    "role": "user",
                    "content": (
                        "СТОП. Ты ещё НЕ вызвал compose_harness.\n"
                        "Обязательный порядок: build_sanity_checks → generate_test_cases → compose_harness → run_code.\n"
                        "Сначала вызови compose_harness и получи harness_code, потом вызывай run_code с code=<harness_code>."
                    )
                })
                continue

            # run_code ещё не был вызван — заставляем продолжать инструментальный пайплайн
            if not _has_tool("run_code"):
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "СТОП. Ты ещё НЕ выполнил обязательный шаг run_code.\n"
                            "Продолжай строго по пайплайну инструментов:\n"
                            "2) generate_test_cases(task_id, n=10)\n"
                            "3) compose_harness(task_id, language, candidate_code, sanity_code, cases)\n"
                            "4) run_code(language, code=<harness_code>)  # ВАЖНО: запускать нужно harness_code, не candidate_code\n"
                            "После run_code: интерпретируй JSON из stdout и вызови score_task.\n"
                            "Запрещено отвечать кандидату до run_code."
                        ),
                    }
                )
                continue

            report = _last_run_code_report()
            if report is None:
                messages.append({
                    "role": "user",
                    "content": (
                        "СТОП. run_code не вернул отчёт проверки.\n"
                        "Обязательно: вызови compose_harness, затем вызови run_code на сгенерированном harness-коде.\n"
                        "Harness ОБЯЗАН печатать в stdout строку вида:\n"
                        "RESULT_JSON: {\"passrate\": ..., \"sanity\": ..., \"cases\": ...}\n"
                        "Запрещено отвечать кандидату до получения RESULT_JSON."
                    )
                })
                continue

            # run_code уже был — теперь можно принять финальный ответ
            if content:
                final_msg = assistant_msg
                break

            # если run_code был, но контент пустой — просим сформировать итог
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Сформируй финальный ответ по результатам run_code: "
                        "краткий итог, что прошло/упало, passrate, и выставь оценку через score_task."
                    ),
                }
            )
            continue

        tool_messages: list[dict[str, Any]] = []
        retry_tools = False
        for tc in tool_calls:
            name = (tc.get("function") or {}).get("name") or ""
            tc_id = tc.get("id") or f"{name}_call"

            # --- parse args once so we can fix them ---
            try:
                args = json.loads((tc.get("function") or {}).get("arguments") or "{}")
            except Exception:
                args = {}
            if not isinstance(args, dict):
                args = {}

            # --- FORCE task_id for practice tools (fix "Task not found") ---
            if name in {"build_sanity_checks", "generate_test_cases", "compose_harness"}:
                if not args.get("task_id"):
                    args["task_id"] = task_id

            # --- FORCE compose_harness inputs (model often forgets them) ---
            if name == "compose_harness":
                if not args.get("candidate_code"):
                    args["candidate_code"] = candidate_code
                # --- HARD GATE: do not allow compose_harness without sanity + cases ---
                if not last_sanity_code or last_cases is None:
                    retry_tools = True
                    messages.append({
                        "role": "user",
                        "content": (
                            "СТОП. Нельзя вызывать compose_harness, пока нет sanity_code и cases.\n"
                            "Сначала обязательно вызови:\n"
                            "1) build_sanity_checks(task_id, language)\n"
                            "2) generate_test_cases(task_id, n=10)\n"
                            "Только потом вызывай compose_harness."
                        )
                    })
                    continue

                if (not args.get("sanity_code")) and last_sanity_code:
                    args["sanity_code"] = last_sanity_code

                if (not args.get("cases")) and last_cases is not None:
                    args["cases"] = last_cases

            # --- FORCE run_code to execute harness_code (always), unless it's already a harness ---
            if name == "run_code":
                if not args.get("language"):
                    args["language"] = "python"

                code_val = args.get("code")
                code_str = code_val.strip() if isinstance(code_val, str) else ""

                looks_like_harness = (
                    "RESULT_JSON:" in code_str
                    or "AUTO-GENERATED HARNESS" in code_str
                    or "def main():" in code_str and "sys.exit" in code_str
                )

                # если harness уже есть — запускаем ЕГО, а не то что прислала модель
                if last_harness_code and not looks_like_harness:
                    args["code"] = last_harness_code

            # --- FORCE score_task args ---
            if name == "score_task":
                if not args.get("task_id"):
                    args["task_id"] = task_id

                if args.get("points") is None:
                    # берём отчёт из последнего run_code
                    report = _last_run_code_report() or {}
                    passrate = float(report.get("passrate") or 0.0)

                    t = _get_task_by_id(session.scenario, task_id)
                    max_pts = float((t or {}).get("max_points") or 0.0)

                    args["points"] = round(max_pts * passrate, 2)

                if not args.get("comment"):
                    args["comment"] = "Оценка выставлена автоматически по результатам прогона тестов."

            # write back patched args so _dispatch_tool_call sees them
            tc["function"]["arguments"] = json.dumps(args, ensure_ascii=False)

            if name == "run_code" and not last_harness_code:
                retry_tools = True
                messages.append({
                    "role": "user",
                    "content": (
                        "СТОП. Нельзя вызывать run_code до compose_harness.\n"
                        "Сначала вызови compose_harness, получи harness_code, затем run_code с code=<harness_code>."
                    )
                })
                continue

            try:
                result = _dispatch_tool_call(session, tc, db)
            except Exception as e:
                logger.exception("Tool failed: %s", name)
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            tool_results_for_ui.append({"name": name, "result": result})

            # --- cache tool outputs for later tool calls ---
            if name == "build_sanity_checks" and isinstance(result, dict):
                code = result.get("code")
                if isinstance(code, str) and code.strip():
                    last_sanity_code = code

            if name == "generate_test_cases" and isinstance(result, dict):
                cases = result.get("cases")
                if isinstance(cases, list):
                    last_cases = cases

            if name == "compose_harness" and isinstance(result, dict):
                hc = result.get("harness_code")
                if isinstance(hc, str) and hc.strip():
                    last_harness_code = hc

            if isinstance(result, dict) and "error" in result:
                retry_tools = True
                messages.append({
                    "role": "user",
                    "content": (
                        f"Инструмент {name} вернул ошибку: {result.get('error')}. "
                        f"Повтори вызов {name} ещё раз, обязательно с task_id='{task_id}'. "
                        "Не переходи к следующим шагам, пока не получишь успешный результат."
                    )
                })

            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )

            # логируем tool-результат в БД
            db.add(
                models.Message(
                    session_id=session.id,
                    sender="tool",
                    text=f"{name} -> {result}",
                    task_id=task_id,
                )
            )

        if retry_tools:
            continue

        # прокидываем результаты tools обратно модели и продолжаем цикл
        messages.extend(tool_messages)

    # если по каким-то причинам не получили финал — вернём последний
    if final_msg is None:
        final_msg = messages[-1] if messages else {"content": ""}

    content = final_msg.get("content") or ""

    # логируем итог проверки
    db.add(models.Message(session_id=session.id, sender="system", text=content, task_id=task_id))
    db.commit()

    return {"reply": content, "tool_results": tool_results_for_ui}

def _score_feedback(result: dict[str, Any]) -> str:
    if not isinstance(result, dict):
        result = {}
    task_id = result.get("task_id") or ""
    pts = result.get("points")
    comment = result.get("comment") or ""
    pts_txt = f"{pts} балл(ов)" if pts is not None else "оценка выставлена"
    return f"Оценка сохранена: {pts_txt} за {task_id}. Комментарий: {comment}. Нажмите «Следующее», чтобы перейти далее."


@router.post("/", response_model=schemas.SessionOut, status_code=status.HTTP_201_CREATED)
@router.post("", response_model=schemas.SessionOut, status_code=status.HTTP_201_CREATED)
def create_session(payload: schemas.SessionCreate, db: Session = Depends(get_db)):
    scenario = db.get(models.Scenario, payload.scenario_id)
    role = db.get(models.Role, payload.role_id)
    if not scenario or not role:
        raise HTTPException(status_code=400, detail="Scenario or role not found")
    if scenario.role_id != role.id:
        raise HTTPException(status_code=400, detail="Scenario does not belong to the selected role")
    session = models.Session(
        scenario_id=payload.scenario_id,
        role_id=payload.role_id,
        candidate_id=payload.candidate_id,
        state="active",
        current_task_id=None,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


@router.get("/{session_id}", response_model=schemas.SessionOut)
def get_session(session_id: str, db: Session = Depends(get_db)):
    session = db.get(models.Session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.get("/{session_id}/messages", response_model=list[schemas.MessageOut])
def list_messages(session_id: str, db: Session = Depends(get_db)):
    session = db.get(models.Session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return db.query(models.Message).filter_by(session_id=session_id).order_by(models.Message.created_at).all()


@router.post("/{session_id}/messages", response_model=schemas.MessageOut)
def post_message(session_id: str, payload: schemas.MessageCreate, db: Session = Depends(get_db)):
    session = db.get(models.Session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    message = models.Message(session_id=session_id, **payload.model_dump())
    db.add(message)
    db.commit()
    db.refresh(message)
    return message


@router.post("/{session_id}/score", response_model=schemas.ScoreOut)
def score_task(session_id: str, payload: schemas.ScoreCreate, db: Session = Depends(get_db)):
    session = db.get(models.Session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    scenario = session.scenario
    task = _get_task_by_id(scenario, payload.task_id)
    if not task:
        raise HTTPException(status_code=400, detail="Task not found in scenario")
    max_points = task.get("max_points", 0)
    if payload.points < 0 or payload.points > max_points:
        raise HTTPException(
            status_code=400,
            detail=f"Points should be within [0, {max_points}]",
        )
    score = models.Score(session_id=session_id, **payload.model_dump())
    current_scores = session.scores or {}
    session.scores = {**current_scores, payload.task_id: payload.points}
    db.add(score)
    db.commit()
    db.refresh(score)
    return score

@router.post("/{session_id}/tasks/{task_id}/submit_code")
def submit_code(session_id: str, task_id: str, payload: schemas.CodeSubmission, db: Session = Depends(get_db)):
    session = db.get(models.Session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    task = _get_task_by_id(session.scenario, task_id)
    if not task or task.get("type") != "coding":
        raise HTTPException(status_code=400, detail="Task is not a coding task")
    result = sandbox.run_code(payload.language, payload.code, payload.tests_id)
    system_msg = models.Message(
        session_id=session_id,
        sender="system",
        text=f"Code execution result for {task_id}: {result}",
        task_id=task_id,
    )
    db.add(system_msg)
    db.commit()
    return {"task_id": task_id, "result": result}

@router.post("/{session_id}/tasks/{task_id}/submit_sql")
def submit_sql(session_id: str, task_id: str, payload: schemas.SqlSubmission, db: Session = Depends(get_db)):
    session = db.get(models.Session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    task = _get_task_by_id(session.scenario, task_id)
    if not task or task.get("type") != "sql":
        raise HTTPException(status_code=400, detail="Task is not a SQL task")
    result = sandbox.run_sql(payload.sql_scenario_id, payload.query)
    system_msg = models.Message(
        session_id=session_id,
        sender="system",
        text=f"SQL execution result for {task_id}: {result}",
        task_id=task_id,
    )
    db.add(system_msg)
    db.commit()
    return {"task_id": task_id, "result": result}

@router.post("/{session_id}/practice/sql")
def practice_sql(session_id: str, payload: PracticeSqlRequest, db: Session = Depends(get_db)):
    session = db.get(models.Session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    task = _get_task_by_id(session.scenario, payload.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found in scenario")

    # Инструкция модели: она должна вызвать run_sql
    instruction = (
        f"Проверь решение кандидата для sql-задачи {payload.task_id} ({task.get('title','')}).\n"
        f"СНАЧАЛА вызови инструмент run_sql с sql_scenario_id='{payload.sql_scenario_id}' и query.\n"
        f"ПОТОМ объясни результат (ошибки/замечания), дай рекомендации и при необходимости оцени через score_task.\n\n"
        f"SQL:\n{payload.query}"
    )

    return _practice_agent_review(session=session, db=db, instruction=instruction, task_id=payload.task_id)


@router.post("/{session_id}/complete")
def complete_session(session_id: str, db: Session = Depends(get_db)):
    session = db.get(models.Session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    session.state = "completed"
    session.finished_at = datetime.utcnow()
    db.commit()
    return {"status": "ok"}


@router.post("/{session_id}/web-search")
def run_web_search(session_id: str, payload: schemas.WebSearchRequest, db: Session = Depends(get_db)):
    if not db.get(models.Session, session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    results = web_search.web_search(payload.query, payload.top_k)
    return {"results": results}


@router.post("/{session_id}/lm/chat")
def call_model(session_id: str, db: Session = Depends(get_db)):
    """Non-streaming call (fallback)."""
    session = db.get(models.Session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    history_db = (
        db.query(models.Message)
        .filter_by(session_id=session_id)
        .order_by(models.Message.created_at)
        .all()
    )
    rag_available = False
    if session.scenario.rag_corpus_id:
        rag_available = db.query(models.Document).filter_by(rag_corpus_id=session.scenario.rag_corpus_id).count() > 0
    system_prompt = _build_system_prompt(session, rag_available)
    snapshot = _conversation_snapshot(session, history_db)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": snapshot},
    ]
    messages.extend(_convert_history(history_db))

    try:
        first_resp = lm_client.chat(messages, tools=TOOLS)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"LM request failed: {exc}") from exc

    assistant_msg = first_resp["choices"][0]["message"]
    tool_calls = assistant_msg.get("tool_calls")

    # Fallback: если tool_calls нет, но модель напечатала tool-call текстом
    if not tool_calls:
        content = assistant_msg.get("content") or ""
        inline = _extract_inline_tool_call(content)
        if inline:
            tool_name, args = inline
            tool_calls = [{
                "id": "inline_toolcall",
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                },
            }]
            # чтобы "<|channel|>commentary ..." не показывался в UI
            assistant_msg["content"] = None

    messages.append(assistant_msg)

    tool_results_db: list[models.Message] = []
    last_score_result: dict[str, Any] | None = None
    final_msg = assistant_msg
    if tool_calls:
        tool_messages = []
        for tc in tool_calls:
            result = _dispatch_tool_call(session, tc, db)
            if tc["function"]["name"] == "score_task":
                last_score_result = result
            # --- theory->practice transition for NON-stream chat too ---
            try:
                args_sc = json.loads(tc["function"].get("arguments", "{}"))
            except Exception:
                args_sc = {}
            task_id_scored = args_sc.get("task_id")
            task_obj = _get_task_by_id(session.scenario, task_id_scored) if task_id_scored else None

            if task_obj and task_obj.get("type") == "theory" and _theory_is_complete(session):
                summary = _theory_summary_text(session)
                practice_task = _first_practice_task(session.scenario)

                practice_title = practice_task.get("title") if practice_task else ""
                practice_id = practice_task.get("id") if practice_task else ""
                practice_type = practice_task.get("type") if practice_task else ""
                practice_desc = ""
                if practice_task:
                    practice_desc = (
                        practice_task.get("description_for_candidate")
                        or practice_task.get("description")
                        or practice_task.get("prompt")
                        or ""
                    )

                messages.append({
                    "role": "system",
                    "content": (
                        "ТЕОРИЯ ЗАВЕРШЕНА.\n"
                        "Нужно: 1) кратко сообщить итог теории, 2) объявить переход к практике, "
                        "3) сказать, что пользователю нужно перейти на вкладку «Практика», вставить решение в редактор и нажать «Проверить моделью», "
                        "4) назвать следующее практическое задание.\n\n"
                        f"{summary}\n"
                        f"Следующее практическое задание: {practice_id} {practice_title} (тип: {practice_type}).\n"
                        f"Описание: {practice_desc}\n"
                    )
                })
            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )
            try:
                args = json.loads(tc["function"].get("arguments", "{}"))
            except Exception:
                args = {}
            task_id_for_db = args.get("task_id")

            tool_results_db.append(
                models.Message(
                    session_id=session_id,
                    sender="tool",
                    text=f"{tc['function']['name']} -> {result}",
                    task_id=task_id_for_db,
                )
            )

        messages.extend(tool_messages)
        try:
            second_resp = lm_client.chat(messages, tools=TOOLS)
            final_msg = second_resp["choices"][0]["message"]
            # --- Fallback 2: модель могла "напечатать" tool-call текстом во втором ответе ---
            if not (final_msg.get("tool_calls") or []):
                inline = _extract_inline_tool_call(final_msg.get("content") or "")
                if inline:
                    tool_name, args = inline
                    # выполним tool вручную, как если бы это был tool_call
                    fake_tc = {
                        "id": "inline_toolcall_2",
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": json.dumps(args, ensure_ascii=False),
                        },
                    }
                    result = _dispatch_tool_call(session, fake_tc, db)

                    # залогируем tool в БД (как ты делаешь выше для tool_calls)
                    db.add(models.Message(
                        session_id=session_id,
                        sender="tool",
                        text=f"{tool_name} -> {result}",
                        task_id=args.get("task_id"),
                    ))
                    db.commit()

                    # добавим tool-ответ в messages и спросим модель ещё раз
                    messages.append({"role": "assistant", "content": None})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": "inline_toolcall_2",
                        "content": json.dumps(result, ensure_ascii=False),
                    })

                    third_resp = lm_client.chat(messages, tools=TOOLS)
                    final_msg = third_resp["choices"][0]["message"]
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"LM request failed after tool calls: {exc}") from exc
        if (not final_msg.get("content")) and last_score_result:
            final_msg["content"] = _score_feedback(last_score_result)

    for tm in tool_results_db:
        db.add(tm)
    db.add(
        models.Message(
            session_id=session_id,
            sender="model",
            text=final_msg.get("content") or "",
        )
    )
    db.commit()

    return {"message": final_msg}

@router.post("/{session_id}/practice/code")
def practice_code(session_id: str, payload: PracticeCodeRequest, db: Session = Depends(get_db)):
    session = db.get(models.Session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    task = _get_task_by_id(session.scenario, payload.task_id)
    if not task:
        raise HTTPException(status_code=400, detail=f"Task {payload.task_id} not found in scenario")
    session.current_task_id = payload.task_id
    db.commit()

    # Агентная проверка: модель сама вызывает tools по протоколу
    instruction = (
        f"Ты проверяешь coding-задачу {payload.task_id} ({task.get('title','')}).\n"
        "Ты ДОЛЖЕН выполнить проверку строго по шагам через инструменты:\n"
        "1) build_sanity_checks(task_id, language)\n"
        "2) generate_test_cases(task_id, n=10)\n"
        "3) compose_harness(task_id, language, candidate_code, sanity_code, cases)\n"
        "4) run_code(language, code=<harness_code>)\n"
        "5) На основе JSON из stdout (sanity/cases/passrate) дай короткий итог и вызови score_task.\n\n"
        "ВАЖНО:\n"
        "- Запрещено писать 'score_task -> {...}' текстом. Используй только tool-вызов.\n"
        "- Запрещено отвечать кандидату ДО выполнения run_code.\n\n"
        f"КОД КАНДИДАТА:\n{payload.code}\n"
    )

    review = _practice_agent_review(
        session=session,
        db=db,
        instruction=instruction,
        task_id=payload.task_id,
    )

    return {"reply": review["reply"], "tool_results": review.get("tool_results", [])}

@router.get("/{session_id}/lm/chat-stream")
def stream_model(session_id: str):
    """Stream tokens from LM Studio. Runs tool calls first, then streams/returns final answer."""
    base_db = SessionLocal()
    session = base_db.get(models.Session, session_id)
    if not session:
        base_db.close()
        raise HTTPException(status_code=404, detail="Session not found")
    history_db = (
        base_db.query(models.Message)
        .filter_by(session_id=session_id)
        .order_by(models.Message.created_at)
        .all()
    )
    # Pre-validate last candidate message for placeholders/offtopic
    last_msg = history_db[-1] if history_db else None
    if last_msg and last_msg.sender == "candidate":
        flags = _analyze_candidate_message(last_msg.text)
        if flags:
            warn = "Ответ не принят: дайте содержательный ответ по сути вопроса."
            if "code_in_chat" in flags or "sql_in_chat" in flags:
                warn = "Не вставляйте код/SQL в чат. Введите решение в редактор ниже и нажмите Submit."
            base_db.add(models.Message(session_id=session_id, sender="system", text=warn))
            base_db.commit()
            base_db.close()

            def reject_stream():
                yield "data: " + json.dumps({"type": "token", "content": warn}, ensure_ascii=False) + "\n\n"
                yield "data: " + json.dumps({"type": "done", "content": warn}, ensure_ascii=False) + "\n\n"

            return StreamingResponse(reject_stream(), media_type="text/event-stream")

    rag_available = False
    if session.scenario.rag_corpus_id:
        rag_available = (
            base_db.query(models.Document).filter_by(rag_corpus_id=session.scenario.rag_corpus_id).count() > 0
        )
    system_prompt = _build_system_prompt(session, rag_available)
    snapshot = _conversation_snapshot(session, history_db)
    base_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": snapshot},
    ]
    base_messages.extend(_convert_history(history_db))

    try:
        first_resp = lm_client.chat(base_messages, tools=TOOLS)
    except Exception as exc:  # noqa: BLE001
        logger.exception("LM request failed before streaming")
        base_db.close()
        raise HTTPException(status_code=500, detail=f"LM request failed: {exc}") from exc

    assistant_msg = first_resp["choices"][0]["message"]
    tool_calls = assistant_msg.get("tool_calls")

    # Fallback: если tool_calls нет, но модель вывела tool-call текстом — распознаём и исполняем
    if not tool_calls:
        content = assistant_msg.get("content") or ""
        inline = _extract_inline_tool_call(content)
        if inline:
            tool_name, args = inline
            tool_calls = [{
                "id": "inline_toolcall",
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                },
            }]
            # чтобы этот мусор не улетел пользователю как "ответ модели"
            assistant_msg["content"] = None

    stream_messages = list(base_messages)
    tool_results_payload: list[dict[str, Any]] = []
    status_events: list[str] = []

    score_result_payload: dict[str, Any] | None = None
    transition_added = False
    if tool_calls:
        stream_messages.append(assistant_msg)

        for tc in tool_calls:
            fname = tc["function"]["name"]

            # распарсить args безопасно
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except Exception:
                args = {}

            task_id_for_db = args.get("task_id")

            if fname == "web_search":
                status_text = f"Ищем в интернете: {args.get('query', '')}"
                base_db.add(models.Message(session_id=session_id, sender="system", text=status_text))
                base_db.commit()
                status_events.append(status_text)

            # сначала вычисляем result (и не роняем стрим, если tool упал)
            try:
                result = _dispatch_tool_call(session, tc, base_db)
            except Exception as e:
                logger.exception("Tool failed: %s", fname)
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}

            if fname == "score_task":
                score_result_payload = result

                # Если это оценка теоретического задания и теория теперь завершена —
                # просим модель в финальном ответе подвести итог и переключить на практику.
                if not transition_added:
                    task_id_scored = args.get("task_id")
                    task_obj = _get_task_by_id(session.scenario, task_id_scored) if task_id_scored else None

                    if task_obj and task_obj.get("type") == "theory" and _theory_is_complete(session):
                        transition_added = True
                        summary = _theory_summary_text(session)
                        practice_task = _first_practice_task(session.scenario)

                        practice_title = practice_task.get("title") if practice_task else None
                        practice_id = practice_task.get("id") if practice_task else None
                        practice_type = practice_task.get("type") if practice_task else None
                        practice_desc = ""
                        if practice_task:
                            practice_desc = (
                                practice_task.get("description_for_candidate")
                                or practice_task.get("description")
                                or practice_task.get("prompt")
                                or ""
                            )

                        stream_messages.append(
                            {
                                "role": "system",
                                "content": (
                                    "ТЕОРИЯ ЗАВЕРШЕНА.\n"
                                    "Запрещено: приветствия, повтор вашего плана, повтор формулировок теоретических вопросов.\n"
                                    "Нужно: 1) кратко сообщить итог теории, 2) объявить переход к практике, "
                                    "3) сказать, что пользователю нужно перейти на вкладку «Практика», вставить решение в редактор и нажать «Проверить моделью», "
                                    "4) назвать следующее практическое задание.\n\n"
                                    f"{summary}\n"
                                    f"Следующее практическое задание: {practice_id or ''} {practice_title or ''} (тип: {practice_type or ''}).\n"
                                    f"Описание: {practice_desc}\n"
                                ),
                            }
                        )

            # tool_call_id нужен модели для привязки результата к вызову
            tc_id = tc.get("id") or f"{fname}_call"

            # добавляем tool-result в историю для модели
            stream_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )

            # сохраняем для записи в БД (sender=tool)
            tool_results_payload.append(
                {
                    "name": fname,
                    "result": result,
                    "sender": "tool",
                    "text": f"{fname} -> {result}",
                    "task_id": task_id_for_db,
                }
            )
    else:
        stream_messages = base_messages

    base_db.close()

    def event_stream():
        local_db = SessionLocal()
        local_session = local_db.get(models.Session, session_id)
        history_local = (
        local_db.query(models.Message)
        .filter_by(session_id=session_id)
        .order_by(models.Message.created_at)
        .all()
    )
        control_state = _control_state(local_session, history_local)

        final_chunks: list[str] = []
        hidden_buffer = ""
        revealed = False
        saw_think = False
        fallback_text = _strip_think(assistant_msg.get("content"))
        # If the model only called score_task and stayed silent, prepare a minimal feedback
        if not fallback_text:
            score_calls = [t for t in tool_results_payload if t.get("name") == "score_task"]
            if score_calls:
                res = score_calls[-1].get("result") or {}
                fallback_text = _score_feedback(res)
        received_tokens = False
        final_text = ""
        try:
            for status_text in status_events:
                yield "data: " + json.dumps({"type": "token", "content": status_text}, ensure_ascii=False) + "\n\n"

            if tool_calls:
                try:
                    sync_resp = lm_client.chat(stream_messages, tools=[])
                    final_text = _strip_think(sync_resp["choices"][0]["message"].get("content"))
                except Exception:
                    final_text = fallback_text or ""
                if score_result_payload and (not final_text or final_text.strip() == fallback_text.strip()):
                    final_text = _score_feedback(score_result_payload)
                chunk_size = 120
                for i in range(0, len(final_text), chunk_size):
                    piece = final_text[i : i + chunk_size]
                    yield "data: " + json.dumps({"type": "token", "content": piece}, ensure_ascii=False) + "\n\n"
                    final_chunks.append(piece)
                final_text = "".join(final_chunks)
            else:
                for chunk in lm_client.stream_chat(stream_messages, tools=TOOLS):
                    if "<think>" in chunk:
                        saw_think = True
                    if not saw_think and not revealed:
                        revealed = True  # нет блока размышлений – стримим сразу
                    if saw_think and not revealed:
                        hidden_buffer += chunk
                        if "</think>" in hidden_buffer:
                            revealed = True
                            after = hidden_buffer.split("</think>", 1)[1]
                            hidden_buffer = ""
                            if after:
                                final_chunks.append(after)
                                yield "data: " + json.dumps({"type": "token", "content": after}, ensure_ascii=False) + "\n\n"
                                received_tokens = True
                        continue
                    final_chunks.append(chunk)
                    yield "data: " + json.dumps({"type": "token", "content": chunk}, ensure_ascii=False) + "\n\n"
                    received_tokens = True
                final_text = "".join(final_chunks)
                if not received_tokens and not final_text:
                    try:
                        sync_resp = lm_client.chat(stream_messages, tools=[])
                        final_text = _strip_think(sync_resp["choices"][0]["message"].get("content"))
                    except Exception:
                        final_text = fallback_text or ""

            for payload in tool_results_payload:
                msg = models.Message(
                    session_id=session_id,
                    sender=payload["sender"],
                    text=payload["text"],
                    task_id=payload.get("task_id"),
                )
                local_db.add(msg)

            if final_text:
                trimmed = _strip_intro(final_text, control_state.get("intro_done", False))
                local_db.add(models.Message(session_id=session_id, sender="model", text=trimmed))
            elif fallback_text:
                trimmed = _strip_intro(fallback_text, control_state.get("intro_done", False))
                local_db.add(models.Message(session_id=session_id, sender="model", text=trimmed))
                final_text = trimmed
            local_db.commit()
            yield "data: " + json.dumps({"type": "done", "content": final_text}, ensure_ascii=False) + "\n\n"
        except Exception as exc:  # noqa: BLE001
            logger.exception("LM streaming failed")
            local_db.add(
                models.Message(
                    session_id=session_id,
                    sender="system",
                    text=f"Ошибка сервиса LM Studio: {exc}",
                )
            )
            local_db.commit()
            yield "data: " + json.dumps({"type": "error", "detail": str(exc)}, ensure_ascii=False) + "\n\n"
            if fallback_text:
                yield "data: " + json.dumps({"type": "done", "content": fallback_text}, ensure_ascii=False) + "\n\n"
        finally:
            local_db.close()

    return StreamingResponse(event_stream(), media_type="text/event-stream")
