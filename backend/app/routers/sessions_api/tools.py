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
            "description": "Безопасно выполнить код кандидата в песочнице с тест-кейсами, связанными с задачей в БД. Используй для проверки coding-задач. Возвращайся к score_task только после получения результата тестов.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "ID задания из сценария, например C-SHORTENER"
                    },
                    "language": {
                        "type": "string",
                        "description": "Язык решения, например python"
                    },
                    "code": {
                        "type": "string",
                        "description": "Исходный код кандидата"
                    }
                },
                "required": ["task_id", "language", "code"],
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
            "name": "score_task",
            "description": "Поставить баллы за задание кандидату. Для theory можно сохранять промежуточные оценки после каждого вопроса и одну финальную оценку после завершения всего блока.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "points": {
                        "type": "number",
                        "multipleOf": 1,
                        "description": "Оценка должна быть целым числом, но кодироваться как число с плавающей точкой со значением .0 (например, 7.0). Для theory используй диапазон 1..max_points текущей задачи. Для coding/sql — диапазон 0..max_points."
                    },
                    "comment": {
                        "type": "string",
                        "description": "Обязательный непустой комментарий. Theory: по-русски, минимум 2 полных предложения, желательно в формате 'Верно / Не хватает / Ошибка/сомнение'; финальный theory-comment — по всему блоку. Coding: 'Корректность / Качество кода / Сложность и эффективность / Что можно улучшить'. SQL: 'Корректность / Качество решения / Работа с SQL / Что можно улучшить'."
                    },
                    "is_final": {
                        "type": "boolean",
                        "description": "Для theory: false после каждого отдельного вопроса, true только один раз после завершения всего блока. Для coding/sql оставляй true."
                    },
                    "question_index": {
                        "type": "integer",
                        "description": "Для theory: номер вопроса в блоке (1-based) для промежуточной оценки."
                    },
                },
                "required": ["task_id", "points", "comment"],
            },
        },
    },
]

