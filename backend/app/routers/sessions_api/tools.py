from __future__ import annotations

from typing import Any


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "rag_search",
            "description": (
                "Поиск по загруженной документации сценария для проверки ответа кандидата "
                "в теоретическом блоке. Используй перед промежуточным score_task, если "
                "для сценария доступен RAG."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "task_id": {
                        "type": "string",
                        "description": "ID theory-задачи, например T1",
                    },
                    "question_index": {
                        "type": "integer",
                        "description": "Номер вопроса theory-блока (1-based)",
                    },
                    "top_k": {"type": "integer"},
                },
                "required": ["query", "task_id", "question_index"],
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
            "description": (
                "Безопасно выполнить код кандидата в песочнице с тест-кейсами, связанными с задачей в БД. "
                "Используй для проверки coding-задач. Возвращайся к score_task только после получения результата тестов."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "ID задания из сценария, например C-SHORTENER",
                    },
                    "language": {
                        "type": "string",
                        "description": "Язык решения, например python",
                    },
                    "code": {
                        "type": "string",
                        "description": "Исходный код кандидата",
                    },
                },
                "required": ["task_id", "language", "code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_sql",
            "description": (
                "Выполнить SQL-запрос кандидата в песочнице по sql_scenario_id и вернуть результат "
                "(columns/rows) или ошибку."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "ID задания (если известно)"},
                    "sql_scenario_id": {"type": "string", "description": "ID SQL-сценария из БД"},
                    "query": {"type": "string", "description": "SQL-запрос кандидата"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "score_task",
            "description": (
                "Поставить баллы за задание кандидату. Для theory можно сохранять промежуточные оценки после "
                "каждого вопроса и одну финальную оценку после завершения всего блока."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "points": {
                        "type": "number",
                        "multipleOf": 1,
                        "description": (
                            "Оценка должна быть целым числом, но кодироваться как число с плавающей точкой со "
                            "значением .0 (например, 7.0). Для theory используй диапазон 1..max_points текущей "
                            "задачи. Для coding/sql — диапазон 0..max_points."
                        ),
                    },
                    "comment": {
                        "type": "string",
                        "description": (
                            "Обязательный непустой комментарий. Theory: по-русски, минимум 2 полных предложения, "
                            "желательно в формате 'Верно / Не хватает / Ошибка/сомнение'. Для финального theory "
                            "score_task это общий качественный итог по блоку без числовой оценки текстом. Coding: "
                            "'Корректность / Качество кода / Сложность и эффективность / Что можно улучшить'. SQL: "
                            "'Корректность / Качество решения / Работа с SQL / Что можно улучшить'."
                        ),
                    },
                    "comments": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Только для финального theory score_task: список комментариев по каждому вопросу текущей "
                            "theory-задачи в порядке вопросов. Каждый элемент должен описывать ответ кандидата по "
                            "соответствующему вопросу без числовой оценки текстом."
                        ),
                    },
                    "is_final": {
                        "type": "boolean",
                        "description": (
                            "Для theory: false после каждого отдельного вопроса, true только один раз после завершения "
                            "всего блока. Для coding/sql оставляй true."
                        ),
                    },
                    "question_index": {
                        "type": "integer",
                        "description": "Для theory: номер вопроса в блоке (1-based) для промежуточной оценки.",
                    },
                },
                "required": ["task_id", "points", "comment"],
            },
        },
    },
]

ALL_TOOLS = TOOLS


def get_tools_by_names(names: list[str]) -> list[dict[str, Any]]:
    allowed = set(names)
    return [
        tool for tool in ALL_TOOLS
        if tool.get("function", {}).get("name") in allowed
    ]


def theory_tools(rag_available: bool) -> list[dict[str, Any]]:
    names = ["score_task", "web_search"]
    if rag_available:
        names.insert(0, "rag_search")
    return get_tools_by_names(names)


def coding_tools() -> list[dict[str, Any]]:
    return get_tools_by_names(["run_code", "score_task"])


def sql_tools() -> list[dict[str, Any]]:
    return get_tools_by_names(["run_sql", "score_task"])


def rag_search_only_tools() -> list[dict[str, Any]]:
    return get_tools_by_names(["rag_search"])


def tools_for_task(current_task: dict[str, Any] | None, rag_available: bool) -> list[dict[str, Any]] | None:
    task_type = (current_task or {}).get("type")
    if task_type == "theory":
        return theory_tools(rag_available=rag_available)
    if task_type == "coding":
        return coding_tools()
    if task_type == "sql":
        return sql_tools()
    return None


def tool_names(tools: list[dict[str, Any]] | None) -> set[str]:
    names: set[str] = set()
    for tool in tools or []:
        name = (tool.get("function") or {}).get("name")
        if name:
            names.add(str(name))
    return names


def restrict_inline_tool_names(
    allowed_tool_names: set[str] | None,
    task_type: str | None,
) -> set[str]:
    names = set(allowed_tool_names or set())
    if task_type == "theory":
        names &= {"rag_search", "web_search", "score_task"}
    return names
