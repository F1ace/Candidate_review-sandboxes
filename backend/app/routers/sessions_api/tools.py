TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "rag_search",
            "description": "РџРѕРёСЃРє РїРѕ Р·Р°РіСЂСѓР¶РµРЅРЅРѕР№ РґРѕРєСѓРјРµРЅС‚Р°С†РёРё СЃС†РµРЅР°СЂРёСЏ.",
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
            "description": "РџРѕРёСЃРє РІ РёРЅС‚РµСЂРЅРµС‚Рµ РґР»СЏ РІР°Р»РёРґР°С†РёРё РѕС‚РІРµС‚Р° РєР°РЅРґРёРґР°С‚Р°.",
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
            "description": "Р—Р°РїСѓСЃС‚РёС‚СЊ РєРѕРґ РєР°РЅРґРёРґР°С‚Р° РІ РїРµСЃРѕС‡РЅРёС†Рµ Рё РІРµСЂРЅСѓС‚СЊ stdout/stderr/exit_code. РСЃРїРѕР»СЊР·СѓР№ РґР»СЏ РїСЂРѕРІРµСЂРєРё СЂРµС€РµРЅРёСЏ РїРѕ coding-Р·Р°РґР°С‡Р°Рј.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "ID Р·Р°РґР°РЅРёСЏ (РµСЃР»Рё РёР·РІРµСЃС‚РЅРѕ)"},
                    "language": {"type": "string", "description": "РЇР·С‹Рє, РЅР°РїСЂРёРјРµСЂ python"},
                    "code": {"type": "string", "description": "РСЃС…РѕРґРЅС‹Р№ РєРѕРґ РєР°РЅРґРёРґР°С‚Р°"},
                    "tests_id": {"type": "string", "description": "ID С‚РµСЃС‚РѕРІ (РµСЃР»Рё Р·Р°РґР°РЅРѕ РІ task)"}
                },
                "required": ["language", "code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_sql",
            "description": "Р’С‹РїРѕР»РЅРёС‚СЊ SQL-Р·Р°РїСЂРѕСЃ РєР°РЅРґРёРґР°С‚Р° РІ РїРµСЃРѕС‡РЅРёС†Рµ РїРѕ sql_scenario_id Рё РІРµСЂРЅСѓС‚СЊ СЂРµР·СѓР»СЊС‚Р°С‚ (columns/rows) РёР»Рё РѕС€РёР±РєСѓ.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "ID Р·Р°РґР°РЅРёСЏ (РµСЃР»Рё РёР·РІРµСЃС‚РЅРѕ)"},
                    "sql_scenario_id": {"type": "string", "description": "ID SQL-СЃС†РµРЅР°СЂРёСЏ РёР· Р‘Р”"},
                    "query": {"type": "string", "description": "SQL Р·Р°РїСЂРѕСЃ РєР°РЅРґРёРґР°С‚Р°"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "build_sanity_checks",
            "description": "РЎРіРµРЅРµСЂРёСЂРѕРІР°С‚СЊ SanityChecks (Р±Р°Р·РѕРІС‹Рµ РїСЂРѕРІРµСЂРєРё) РґР»СЏ coding-Р·Р°РґР°С‡Рё. Р’РѕР·РІСЂР°С‰Р°РµС‚ python-РєРѕРґ СЃ С„СѓРЅРєС†РёРµР№ run_sanity(ns).",
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
            "description": "РЎРіРµРЅРµСЂРёСЂРѕРІР°С‚СЊ N С‚РµСЃС‚-РєРµР№СЃРѕРІ (СЃС‚СЂСѓРєС‚СѓСЂРёСЂРѕРІР°РЅРЅС‹Рµ steps/expect) РґР»СЏ coding-Р·Р°РґР°С‡Рё.",
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
            "description": "РЎРѕР±СЂР°С‚СЊ РµРґРёРЅС‹Р№ python-harness: РєР°РЅРґРёРґР°С‚СЃРєРёР№ РєРѕРґ + sanity + runner РґР»СЏ РєРµР№СЃРѕРІ. Р РµР·СѓР»СЊС‚Р°С‚ РїРµС‡Р°С‚Р°РµС‚ JSON СЃ passrate.",
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
            "description": "РџРѕСЃС‚Р°РІРёС‚СЊ Р±Р°Р»Р»С‹ Р·Р° Р·Р°РґР°РЅРёРµ РєР°РЅРґРёРґР°С‚Сѓ.",
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

