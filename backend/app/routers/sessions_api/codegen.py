import json
import re
from typing import Any

from ...services.lm_client import lm_client


def _resolve_entrypoint(task: dict[str, Any]) -> str:
    iface = task.get("interface") or {}
    return (
        task.get("entrypoint")
        or iface.get("entrypoint")
        or iface.get("class_name")
        or "TaskQueue"
    )


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
    entrypoint = _resolve_entrypoint(task)
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
    entrypoint = _resolve_entrypoint(task)
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


def _build_test_cases_from_task(task: dict[str, Any], n: int) -> dict[str, Any]:
    """Deterministic source of test cases from scenario/task definition."""
    raw_cases = task.get("success_cases") or task.get("test_cases") or []
    if not isinstance(raw_cases, list):
        raw_cases = []
    cases = [c for c in raw_cases if isinstance(c, dict)]
    return {
        "entrypoint": _resolve_entrypoint(task),
        "cases": cases[: max(0, int(n))],
    }

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

