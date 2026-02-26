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

