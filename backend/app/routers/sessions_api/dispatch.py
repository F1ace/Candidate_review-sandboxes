import json
from typing import Any

from sqlalchemy.orm import Session

from ... import models
from ...services import sandbox, web_search
from ...services.rag import search_documents
from .codegen import (
    _apply_default_init_args,
    _build_sanity_checks_with_llm,
    _build_test_cases_with_llm,
    _filter_cases,
)
from .harness import _compose_harness_code
from .state import _get_task_by_id
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

def _dispatch_tool_call(session, tool_call, db):
    fn = tool_call.get("function") or {}
    name = fn.get("name") or ""
    name = (name or "").strip().replace("вЂ¦", "")

    raw_args = fn.get("arguments")
    # 1) Р±РµР·РѕРїР°СЃРЅРѕ СЂР°СЃРїР°СЂСЃРёС‚СЊ arguments -> dict
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

    # Р’РЎР•Р“Р”Рђ РїСЂРѕСЃС‚Р°РІР»СЏРµРј task_id РїРѕСЃР»Рµ unwrap
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

        # РµСЃР»Рё tests_id РЅРµ РїРµСЂРµРґР°РЅ вЂ” РїРѕРїС‹С‚РєР° РІР·СЏС‚СЊ РёР· task
        tests_id = args.get("tests_id")
        if not tests_id and task_id:
            task = _get_task_by_id(session.scenario, task_id)
            if task:
                tests_id = task.get("tests_id") or task.get("tests")  # РЅР° СЃР»СѓС‡Р°Р№ РґСЂСѓРіРѕРіРѕ РєР»СЋС‡Р°

        # sandbox.run_code РѕР¶РёРґР°РµС‚ tests_id СЃС‚СЂРѕРєРѕР№ вЂ” РїРµСЂРµРґР°С‡Р° РїСѓСЃС‚РѕР№, РµСЃР»Рё РЅРµС‚
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

        # 1) С„РёР»СЊС‚СЂСѓРµРј РЅРµРІР°Р»РёРґРЅС‹Рµ РєРµР№СЃС‹
        cases = _filter_cases(task, out.get("cases") or [])

        # 2) РїРѕРґСЃС‚Р°РІР»СЏРµРј РґРµС„РѕР»С‚РЅС‹Рµ init.args, РµСЃР»Рё LLM Р·Р°Р±С‹Р» РёС… СѓРєР°Р·Р°С‚СЊ
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
        entrypoint = (task.get("interface") or {}).get("entrypoint") or (task.get("interface") or {}).get("class_name") or "TaskQueue"

        harness = _compose_harness_code(
            candidate_code=candidate_code,
            sanity_code=sanity_code,
            cases=cases,
            entrypoint=entrypoint,
        )

        return {"task_id": task_id, "entrypoint": entrypoint, "harness_code": harness}
    
    if name == "score_task":
        return _apply_score(session, args, db)

    return {"error": f"Unsupported tool {name}"}

