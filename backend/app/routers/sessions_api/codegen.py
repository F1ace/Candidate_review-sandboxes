import json
import re
from typing import Any

from ...services.lm_client import lm_client
def _extract_json_object(text: str) -> dict[str, Any]:
    """
    LM РёРЅРѕРіРґР° РѕР±РѕСЂР°С‡РёРІР°РµС‚ JSON РІ С‚РµРєСЃС‚ РёР»Рё ```json.
    Р’С‹СЂРµР·Р°РµРј РїРµСЂРІС‹Р№ JSON-РѕР±СЉРµРєС‚ РІРёРґР° {...} Рё РїР°СЂСЃРёРј.
    """
    if not text:
        return {}
    # РІС‹СЂРµР·Р°С‚СЊ ```json ... ```
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if m:
        text = m.group(1)

    # РЅР°Р№С‚Рё РїРµСЂРІС‹Р№ РѕР±СЉРµРєС‚ {...}
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
    return _extract_json_object(content)  # Сѓ С‚РµР±СЏ СѓР¶Рµ РµСЃС‚СЊ РІ С„Р°Р№Р»Рµ

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
        # РњРёРЅРёРјР°Р»СЊРЅС‹Р№ sanity: РїСЂРѕРІРµСЂСЏРµРј, С‡С‚Рѕ entrypoint СЃСѓС‰РµСЃС‚РІСѓРµС‚ Рё С‡С‚Рѕ Сѓ РЅРµРіРѕ РµСЃС‚СЊ Р±Р°Р·РѕРІС‹Рµ РјРµС‚РѕРґС‹
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
        "Р’РµСЂРЅРё РЎРўР РћР“Рћ JSON Р±РµР· markdown Рё Р±РµР· РїРѕСЏСЃРЅРµРЅРёР№.\n"
        "РќСѓР¶РЅРѕ: JSON СЃРѕ СЃС‚СЂСѓРєС‚СѓСЂРѕР№:\n"
        "{\n"
        '  "sanity_checks": {\n'
        '    "code": "...."\n'
        "  }\n"
        "}\n"
        "Р“РґРµ code (python) СЃРѕРґРµСЂР¶РёС‚ С„СѓРЅРєС†РёСЋ:\n"
        "def run_sanity(ns):\n"
        "  # ns вЂ” namespace РєР°РЅРґРёРґР°С‚Р°\n"
        "  # return {\"passed\": int, \"failed\": int, \"failures\": [str,...]}\n"
        "SanityChecks РґРѕР»Р¶РЅС‹ РїСЂРѕРІРµСЂРёС‚СЊ РёРЅС‚РµСЂС„РµР№СЃ Рё 3-6 Р±Р°Р·РѕРІС‹С… СЃС†РµРЅР°СЂРёРµРІ.\n"
        f"Entrypoint: {entrypoint}\n"
        f"Р—Р°РґР°РЅРёРµ: {task.get('id')} {task.get('title','')}\n"
        f"РћРїРёСЃР°РЅРёРµ: {desc}\n"
    )

    # 1-СЏ РїРѕРїС‹С‚РєР°
    obj = _llm_json([
        {"role": "system", "content": "РћС‚РІРµС‡Р°Р№ С‚РѕР»СЊРєРѕ JSON-РѕР±СЉРµРєС‚РѕРј."},
        {"role": "user", "content": base_prompt},
    ])
    code = _extract_code(obj).strip()

    # 2-СЏ РїРѕРїС‹С‚РєР° (СЂРµС‚СЂР°Р№), РµСЃР»Рё РїСѓСЃС‚Рѕ
    if not code:
        retry_prompt = (
            base_prompt
            + "\nР’РђР–РќРћ: РІРµСЂРЅРё JSON СЃ РєР»СЋС‡Р°РјРё exactly sanity_checks.code. Р‘РµР· С‚РµРєСЃС‚Р°.\n"
            + 'РџСЂРёРјРµСЂ С„РѕСЂРјР°С‚Р°: {"sanity_checks": {"code": "def run_sanity(ns):\\n    ..."}}\n'
        )
        obj2 = _llm_json([
            {"role": "system", "content": "РћС‚РІРµС‡Р°Р№ С‚РѕР»СЊРєРѕ JSON-РѕР±СЉРµРєС‚РѕРј."},
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
    method_names_str = ", ".join(method_names) if method_names else "(РЅРµС‚ РґР°РЅРЅС‹С…)"

    # РџСЂРѕСЃС‚С‹Рµ РїРѕРґСЃРєР°Р·РєРё РїРѕ returns
    return_hints = []
    for m in methods:
        n = m.get("name")
        r = (m.get("returns") or "").strip()
        if n and r:
            return_hints.append(f"- {n}: РІРѕР·РІСЂР°С‰Р°РµС‚ {r}")

    return (
        "Р’РђР–РќРћ (РєРѕРЅС‚СЂР°РєС‚ РёРЅС‚РµСЂС„РµР№СЃР°):\n"
        f"- Р Р°Р·СЂРµС€С‘РЅРЅС‹Рµ РјРµС‚РѕРґС‹: {method_names_str}.\n"
        "- РќР• РёСЃРїРѕР»СЊР·СѓР№ РјРµС‚РѕРґС‹, РєРѕС‚РѕСЂС‹С… РЅРµС‚ РІ СЃРїРёСЃРєРµ.\n"
        "- РќР• РїСЂРѕРІРµСЂСЏР№ РёСЃРєР»СЋС‡РµРЅРёСЏ Рё РќР• РёСЃРїРѕР»СЊР·СѓР№ expect: 'error'.\n"
        "- РљР°Р¶РґС‹Р№ step РґРѕР»Р¶РµРЅ СЃРѕРґРµСЂР¶Р°С‚СЊ call РёР· СЂР°Р·СЂРµС€С‘РЅРЅС‹С… РјРµС‚РѕРґРѕРІ.\n"
        + ("\nРџРѕРґСЃРєР°Р·РєРё РїРѕ РѕР¶РёРґР°РµРјС‹Рј РІРѕР·РІСЂР°С‰Р°РµРјС‹Рј Р·РЅР°С‡РµРЅРёСЏРј:\n" + "\n".join(return_hints) + "\n"
           if return_hints else "")
    )

def _build_test_cases_with_llm(task: dict[str, Any], n: int) -> dict[str, Any]:
    iface = task.get("interface") or {}
    entrypoint = iface.get("entrypoint") or iface.get("class_name") or "TaskQueue"
    desc = task.get("description_for_candidate") or task.get("description") or task.get("prompt") or ""
    task_text = desc
    rules = _case_rules_from_interface(task)
    prompt = (
        "РўС‹ РіРµРЅРµСЂРёСЂСѓРµС€СЊ С‚РµСЃС‚-РєРµР№СЃС‹ РґР»СЏ РїСЂРѕРІРµСЂРєРё РєРѕРґР° РєР°РЅРґРёРґР°С‚Р°.\n"
        "Р’РµСЂРЅРё РЎРўР РћР“Рћ JSON СЃ РєР»СЋС‡РѕРј cases (list).\n"
        f"РљРђРќРћРќРР§Р•РЎРљРћР• РћРџРРЎРђРќРР• Р—РђР”РђРќРРЇ:\n{task_text}\n\n"
        f"РРќРўР•Р Р¤Р•Р™РЎ (РєРѕРЅС‚СЂР°РєС‚):\n{json.dumps(task.get('interface', {}), ensure_ascii=False)}\n\n"
        f"{rules}\n"
        "Р¤РѕСЂРјР°С‚ cases:\n"
        "[{name, init:{class,args}, steps:[{call,args,expect}], notes}]\n"
    )
    obj = _llm_json([
        {"role": "system", "content": "РћС‚РІРµС‡Р°Р№ С‚РѕР»СЊРєРѕ JSON-РѕР±СЉРµРєС‚РѕРј."},
        {"role": "user", "content": prompt},
    ])
    cases = obj.get("cases")
    if not isinstance(cases, list):
        cases = []
    return {"entrypoint": entrypoint, "cases": cases[:n]}

def _filter_cases(task: dict[str, Any], cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    РЈР±РёСЂР°РµС‚ РєРµР№СЃС‹, РєРѕС‚РѕСЂС‹Рµ РЅР°СЂСѓС€Р°СЋС‚ РєРѕРЅС‚СЂР°РєС‚ РёРЅС‚РµСЂС„РµР№СЃР° (РјРµС‚РѕРґС‹ РЅРµ РёР· interface.methods)
    РёР»Рё РёСЃРїРѕР»СЊР·СѓСЋС‚ Р·Р°РїСЂРµС‰С‘РЅРЅС‹Рµ РѕР¶РёРґР°РЅРёСЏ (expect == 'error').
    Р”РµР»Р°РµС‚ РїСЂРѕРІРµСЂРєСѓ СѓРЅРёРІРµСЂСЃР°Р»СЊРЅРѕР№ РґР»СЏ Р»СЋР±С‹С… Р·Р°РґР°С‡ РїРѕ РёС… task['interface'].
    """
    iface = task.get("interface") or {}
    allowed = {m.get("name") for m in (iface.get("methods") or []) if m.get("name")}

    # Р•СЃР»Рё РєРѕРЅС‚СЂР°РєС‚Р° РЅРµС‚ вЂ” РЅРµ СЂРµР¶РµРј, С‡С‚РѕР±С‹ РЅРµ СЃР»РѕРјР°С‚СЊ СЃС‚Р°СЂС‹Рµ СЃС†РµРЅР°СЂРёРё
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
    РџС‹С‚Р°РµРјСЃСЏ РёР·РІР»РµС‡СЊ РґРµС„РѕР»С‚РЅС‹Рµ Р°СЂРіСѓРјРµРЅС‚С‹ РєРѕРЅСЃС‚СЂСѓРєС‚РѕСЂР° РёР· interface.init_args.
    РџРѕРґРґРµСЂР¶РёРІР°РµРј РЅРµСЃРєРѕР»СЊРєРѕ С„РѕСЂРјР°С‚РѕРІ, С‡С‚РѕР±С‹ РЅРµ Р·Р°РІРёСЃРµС‚СЊ РѕС‚ СЃС‚СЂСѓРєС‚СѓСЂС‹ РІ JSON.
    """
    interface = (task or {}).get("interface") or {}
    init_args_spec = interface.get("init_args") or []

    defaults = []
    for item in init_args_spec:
        # item РјРѕР¶РµС‚ Р±С‹С‚СЊ:
        # - {"name": "...", "default": 123}
        # - {"name": "...", "example": 123}
        # - РїСЂРѕСЃС‚Рѕ Р·РЅР°С‡РµРЅРёРµ (СЂРµРґРєРѕ)
        if isinstance(item, dict):
            if "default" in item:
                defaults.append(item["default"])
            elif "example" in item:
                defaults.append(item["example"])
            else:
                # РµСЃР»Рё РЅРёС‡РµРіРѕ РЅРµС‚ вЂ” Р»СѓС‡С€Рµ РЅРµ РіР°РґР°С‚СЊ, РїСЂРѕРїСѓСЃС‚РёРј
                # (РјРѕР¶РЅРѕ РґРѕР±Р°РІРёС‚СЊ СЌРІСЂРёСЃС‚РёРєСѓ, РЅРѕ СЌС‚Рѕ СЂРёСЃРє)
                pass
        else:
            # РµСЃР»Рё СЌС‚Рѕ СѓР¶Рµ Р·РЅР°С‡РµРЅРёРµ
            defaults.append(item)

    return defaults

def _apply_default_init_args(task: dict, cases: list[dict]) -> list[dict]:
    """
    Р•СЃР»Рё РІ test case РЅРµС‚ init.args, Р° РїРѕ РёРЅС‚РµСЂС„РµР№СЃСѓ РµСЃС‚СЊ РґРµС„РѕР»С‚С‹ вЂ” РїРѕРґСЃС‚Р°РІР»СЏРµРј.
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

