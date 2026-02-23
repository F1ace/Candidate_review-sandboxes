from typing import Any


def _compose_harness_code(*, candidate_code: str, sanity_code: str, cases: list[dict[str, Any]], entrypoint: str) -> str:
    """
    Safe harness:
    - candidate/sanity/cases are embedded via json.dumps + json.loads
      to stay robust with quotes in candidate code
    - namespace uses __name__='candidate_solution' so __main__ branch is not triggered
    - always prints RESULT_JSON: {...} to stdout
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
    return _textwrap.dedent(harness).lstrip()
