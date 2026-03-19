from __future__ import annotations

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app import models


TASK_TESTCASES = {
    "C-SHORTENER": {
        "extra_config": {
            "entrypoint_kind": "class",
            "entrypoint_name": "UrlShortener",
        },
        "cases": [
            {
                "code": "shortener_decode_after_encode",
                "name": "decode(encode(url)) returns original url",
                "language": "python",
                "input_data": {
                    "constructor": {"args": [], "kwargs": {}},
                    "calls": [
                        {
                            "method": "encode",
                            "args": ["https://example.com"],
                            "kwargs": {},
                            "save_as": "short_code",
                        },
                        {
                            "method": "decode",
                            "args_from_saved": ["short_code"],
                            "kwargs": {},
                        },
                    ],
                },
                "expected_output": "https://example.com",
            },
            {
                "code": "shortener_second_url_decode",
                "name": "second encoded url decodes correctly",
                "language": "python",
                "input_data": {
                    "constructor": {"args": [], "kwargs": {}},
                    "calls": [
                        {
                            "method": "encode",
                            "args": ["https://a.example.com"],
                            "kwargs": {},
                            "save_as": "code1",
                        },
                        {
                            "method": "encode",
                            "args": ["https://b.example.com"],
                            "kwargs": {},
                            "save_as": "code2",
                        },
                        {
                            "method": "decode",
                            "args_from_saved": ["code2"],
                            "kwargs": {},
                        },
                    ],
                },
                "expected_output": "https://b.example.com",
            },
            {
                "code": "shortener_repeat_decode_stable",
                "name": "decode is stable for the same short code",
                "language": "python",
                "input_data": {
                    "constructor": {"args": [], "kwargs": {}},
                    "calls": [
                        {
                            "method": "encode",
                            "args": ["https://stable.example.com"],
                            "kwargs": {},
                            "save_as": "stable_code",
                        },
                        {
                            "method": "decode",
                            "args_from_saved": ["stable_code"],
                            "kwargs": {},
                            "save_as": "decoded_once",
                        },
                        {
                            "method": "decode",
                            "args_from_saved": ["stable_code"],
                            "kwargs": {},
                        },
                    ],
                },
                "expected_output": "https://stable.example.com",
            },
            {
                "code": "shortener_same_url_same_code",
                "name": "same URL returns same short code",
                "language": "python",
                "input_data": {
                    "constructor": {"args": [], "kwargs": {}},
                    "calls": [
                        {
                            "method": "encode",
                            "args": ["https://same.example.com"],
                            "kwargs": {},
                            "save_as": "code1",
                        },
                        {
                            "method": "encode",
                            "args": ["https://same.example.com"],
                            "kwargs": {},
                            "save_as": "code2",
                        },
                        {
                            "method": "decode",
                            "args_from_saved": ["code2"],
                            "kwargs": {},
                        },
                    ],
                },
                "expected_output": "https://same.example.com",
                "checker_source": """
import re
def check(actual, expected, saved_values):
    code1 = saved_values.get("code1")
    code2 = saved_values.get("code2")
    return (
        actual == expected
        and code1 == code2
        and isinstance(code1, str)
        and len(code1) == 6
        and re.fullmatch(r"[A-Za-z0-9]{6}", code1) is not None
    )
""".strip(),
            },
            {
                "code": "shortener_code_format",
                "name": "generated code has length 6 and allowed alphabet",
                "language": "python",
                "input_data": {
                    "constructor": {"args": [], "kwargs": {}},
                    "calls": [
                        {
                            "method": "encode",
                            "args": ["https://format.example.com"],
                            "kwargs": {},
                        },
                    ],
                },
                "expected_output": "",
                "checker_source": """
import re
def check(actual, expected, saved_values):
    return isinstance(actual, str) and re.fullmatch(r"[A-Za-z0-9]{6}", actual) is not None
""".strip(),
            },
            {
                "code": "shortener_unknown_code_raises_keyerror",
                "name": "decode unknown code raises KeyError",
                "language": "python",
                "input_data": {
                    "__expected_error__": "KeyError",
                    "constructor": {"args": [], "kwargs": {}},
                    "calls": [
                        {
                            "method": "decode",
                            "args": ["ABC123"],
                            "kwargs": {},
                        },
                    ],
                },
                "expected_output": None,
            },
        ],
    },

    "C1": {
        "extra_config": {
            "entrypoint_kind": "class",
            "entrypoint_name": "LogisticRegression",
        },
        "cases": [
            {
                "code": "logreg_basic_predict",
                "name": "fit on simple dataset and predict endpoints",
                "language": "python",
                "input_data": {
                    "constructor": {"args": [], "kwargs": {"lr": 0.1, "n_iters": 500}},
                    "calls": [
                        {
                            "method": "fit",
                            "args": [[[0.0], [1.0], [2.0], [3.0]], [0, 0, 1, 1]],
                            "kwargs": {},
                        },
                        {
                            "method": "predict",
                            "args": [[[0.0], [3.0]]],
                            "kwargs": {},
                        },
                    ],
                },
                "expected_output": [0, 1],
            },
            {
                "code": "logreg_predict_training_subset",
                "name": "predict returns expected labels for simple subset",
                "language": "python",
                "input_data": {
                    "constructor": {"args": [], "kwargs": {"lr": 0.1, "n_iters": 500}},
                    "calls": [
                        {
                            "method": "fit",
                            "args": [[[0.0], [1.0], [2.0], [3.0]], [0, 0, 1, 1]],
                            "kwargs": {},
                        },
                        {
                            "method": "predict",
                            "args": [[[0.0], [1.0], [3.0]]],
                            "kwargs": {},
                        },
                    ],
                },
                "expected_output": [0, 0, 1],
            },
            {
                "code": "logreg_predict_proba_range",
                "name": "predict_proba returns probabilities in [0, 1]",
                "language": "python",
                "input_data": {
                    "constructor": {"args": [], "kwargs": {"lr": 0.1, "n_iters": 500}},
                    "calls": [
                        {
                            "method": "fit",
                            "args": [[[0.0], [1.0], [2.0], [3.0]], [0, 0, 1, 1]],
                            "kwargs": {},
                        },
                        {
                            "method": "predict_proba",
                            "args": [[[0.5], [2.5]]],
                            "kwargs": {},
                        },
                    ],
                },
                "expected_output": [0.0, 0.0],
                "checker_source": """
def check(actual, expected, saved_values):
    return (
        isinstance(actual, list)
        and len(actual) == 2
        and all(isinstance(x, (int, float)) for x in actual)
        and all(0.0 <= float(x) <= 1.0 for x in actual)
    )
""".strip(),
            },
            {
                "code": "logreg_fit_returns_self",
                "name": "fit returns self",
                "language": "python",
                "input_data": {
                    "constructor": {"args": [], "kwargs": {"lr": 0.1, "n_iters": 500}},
                    "calls": [
                        {
                            "method": "fit",
                            "args": [[[0.0], [1.0], [2.0], [3.0]], [0, 0, 1, 1]],
                            "kwargs": {},
                            "save_as": "fit_result",
                        },
                        {
                            "method": "predict",
                            "args": [[[0.0], [3.0]]],
                            "kwargs": {},
                        },
                    ],
                },
                "expected_output": [0, 1],
                "checker_source": """
def check(actual, expected, saved_values):
    return (
        saved_values.get("fit_result") is saved_values.get("__self__")
        and actual == [0, 1]
    )
""".strip(),
            },
        ],
    },

    "C-AB-REPORT": {
        "extra_config": {
            "entrypoint_kind": "class",
            "entrypoint_name": "ABReport",
        },
        "cases": [
            {
                "code": "ab_report_basic_case",
                "name": "simple A/B report basic metrics",
                "language": "python",
                "input_data": {
                    "constructor": {"args": [], "kwargs": {}},
                    "calls": [
                        {"method": "add", "args": ["A", 1], "kwargs": {}},
                        {"method": "add", "args": ["A", 0], "kwargs": {}},
                        {"method": "add", "args": ["B", 1], "kwargs": {}},
                        {"method": "report", "args": [], "kwargs": {}},
                    ],
                },
                "expected_output": {
                    "nA": 2,
                    "nB": 1,
                    "convA": 0.5,
                    "convB": 1.0,
                    "diff": 0.5,
                },
            },
            {
                "code": "ab_report_zero_case",
                "name": "zero conversions in both groups",
                "language": "python",
                "input_data": {
                    "constructor": {"args": [], "kwargs": {}},
                    "calls": [
                        {"method": "add", "args": ["A", 0], "kwargs": {}},
                        {"method": "add", "args": ["B", 0], "kwargs": {}},
                        {"method": "report", "args": [], "kwargs": {}},
                    ],
                },
                "expected_output": {
                    "nA": 1,
                    "nB": 1,
                    "convA": 0.0,
                    "convB": 0.0,
                    "diff": 0.0,
                },
            },
            {
                "code": "ab_report_extended_fields_are_consistent",
                "name": "report includes extended stats and keeps them consistent",
                "language": "python",
                "input_data": {
                    "constructor": {"args": [], "kwargs": {}},
                    "calls": [
                        {"method": "add", "args": ["A", 1], "kwargs": {}},
                        {"method": "add", "args": ["A", 0], "kwargs": {}},
                        {"method": "add", "args": ["A", 1], "kwargs": {}},
                        {"method": "add", "args": ["B", 1], "kwargs": {}},
                        {"method": "add", "args": ["B", 1], "kwargs": {}},
                        {"method": "add", "args": ["B", 0], "kwargs": {}},
                        {"method": "report", "args": [], "kwargs": {}},
                    ],
                },
                "expected_output": {},
                "checker_source": """
def check(actual, expected, saved_values):
    required = ["nA", "nB", "convA", "convB", "diff", "rel_uplift", "ci_low", "ci_high", "z", "p_value"]
    if not isinstance(actual, dict):
        return False
    if any(k not in actual for k in required):
        return False
    if actual["nA"] != 3 or actual["nB"] != 3:
        return False
    if not (0.0 <= actual["convA"] <= 1.0 and 0.0 <= actual["convB"] <= 1.0):
        return False
    if abs(actual["diff"] - (actual["convB"] - actual["convA"])) > 1e-6:
        return False
    if not (actual["ci_low"] <= actual["diff"] <= actual["ci_high"]):
        return False
    if not (0.0 <= actual["p_value"] <= 1.0):
        return False
    return True
""".strip(),
            },
        ],
    },

    "C-BE-QUEUE": {
        "extra_config": {
            "entrypoint_kind": "class",
            "entrypoint_name": "TaskQueue",
        },
        "cases": [
            {
                "code": "queue_empty_dequeue",
                "name": "empty dequeue returns None",
                "language": "python",
                "input_data": {
                    "constructor": {"args": [], "kwargs": {}},
                    "calls": [
                        {"method": "dequeue", "args": [], "kwargs": {}},
                    ],
                },
                "expected_output": None,
            },
            {
                "code": "queue_enqueue_dequeue_first_item",
                "name": "dequeue returns first enqueued item payload",
                "language": "python",
                "input_data": {
                    "constructor": {"args": [], "kwargs": {}},
                    "calls": [
                        {"method": "enqueue", "args": ["A"], "kwargs": {}},
                        {"method": "dequeue", "args": [], "kwargs": {}},
                    ],
                },
                "expected_output": ["token", "A"],
                "checker_source": """
def check(actual, expected, saved_values):
    return isinstance(actual, list) and len(actual) == 2 and actual[1] == "A"
""".strip(),
            },
            {
                "code": "queue_fifo_second_item",
                "name": "second dequeue returns second item payload",
                "language": "python",
                "input_data": {
                    "constructor": {"args": [], "kwargs": {}},
                    "calls": [
                        {"method": "enqueue", "args": ["A"], "kwargs": {}},
                        {"method": "enqueue", "args": ["B"], "kwargs": {}},
                        {"method": "dequeue", "args": [], "kwargs": {}, "save_as": "first_pair"},
                        {"method": "dequeue", "args": [], "kwargs": {}},
                    ],
                },
                "expected_output": ["token", "B"],
                "checker_source": """
def check(actual, expected, saved_values):
    return isinstance(actual, list) and len(actual) == 2 and actual[1] == "B"
""".strip(),
            },
            {
                "code": "queue_ack_removes_inflight",
                "name": "ack removes inflight item and queue proceeds",
                "language": "python",
                "input_data": {
                    "constructor": {"args": [], "kwargs": {}},
                    "calls": [
                        {"method": "enqueue", "args": ["A"], "kwargs": {}},
                        {"method": "enqueue", "args": ["B"], "kwargs": {}},
                        {
                            "method": "dequeue",
                            "args": [],
                            "kwargs": {},
                            "save_as": "pair1",
                            "save_index_as": {"token1": 0, "item1": 1},
                        },
                        {"method": "ack", "args_from_saved": ["token1"], "kwargs": {}},
                        {"method": "dequeue", "args": [], "kwargs": {}},
                    ],
                },
                "expected_output": ["token", "B"],
                "checker_source": """
def check(actual, expected, saved_values):
    return isinstance(actual, list) and len(actual) == 2 and actual[1] == "B"
""".strip(),
            },
            {
                "code": "queue_nack_requeues_item",
                "name": "nack returns item back to queue",
                "language": "python",
                "input_data": {
                    "constructor": {"args": [], "kwargs": {}},
                    "calls": [
                        {"method": "enqueue", "args": ["A"], "kwargs": {}},
                        {
                            "method": "dequeue",
                            "args": [],
                            "kwargs": {},
                            "save_as": "pair1",
                            "save_index_as": {"token1": 0, "item1": 1},
                        },
                        {"method": "nack", "args_from_saved": ["token1"], "kwargs": {}},
                        {"method": "dequeue", "args": [], "kwargs": {}},
                    ],
                },
                "expected_output": ["token", "A"],
                "checker_source": """
def check(actual, expected, saved_values):
    return isinstance(actual, list) and len(actual) == 2 and actual[1] == "A"
""".strip(),
            },
            {
                "code": "queue_unknown_token_raises_keyerror",
                "name": "ack unknown token raises KeyError",
                "language": "python",
                "input_data": {
                    "__expected_error__": "KeyError",
                    "constructor": {"args": [], "kwargs": {}},
                    "calls": [
                        {"method": "ack", "args": ["unknown-token"], "kwargs": {}},
                    ],
                },
                "expected_output": None,
            },
            {
                "code": "queue_tokens_are_unique",
                "name": "dequeue tokens are unique",
                "language": "python",
                "input_data": {
                    "constructor": {"args": [], "kwargs": {}},
                    "calls": [
                        {"method": "enqueue", "args": ["A"], "kwargs": {}},
                        {"method": "enqueue", "args": ["B"], "kwargs": {}},
                        {
                            "method": "dequeue",
                            "args": [],
                            "kwargs": {},
                            "save_as": "pair1",
                            "save_index_as": {"token1": 0, "item1": 1},
                        },
                        {
                            "method": "dequeue",
                            "args": [],
                            "kwargs": {},
                            "save_as": "pair2",
                            "save_index_as": {"token2": 0, "item2": 1},
                        },
                    ],
                },
                "expected_output": None,
                "checker_source": """
def check(actual, expected, saved_values):
    token1 = saved_values.get("token1")
    token2 = saved_values.get("token2")
    item1 = saved_values.get("item1")
    item2 = saved_values.get("item2")
    return (
        token1 is not None
        and token2 is not None
        and token1 != token2
        and item1 == "A"
        and item2 == "B"
    )
""".strip(),
            },
        ],
    },

    "C-RATE": {
        "extra_config": {
            "entrypoint_kind": "class",
            "entrypoint_name": "TokenBucket",
        },
        "cases": [
            {
                "code": "rate_allow_within_capacity",
                "name": "request within capacity is allowed",
                "language": "python",
                "input_data": {
                    "constructor": {"args": [5, 0.0], "kwargs": {}},
                    "calls": [
                        {"method": "allow", "args": [3], "kwargs": {}},
                    ],
                },
                "expected_output": True,
            },
            {
                "code": "rate_second_large_request_denied",
                "name": "second large request is denied without refill",
                "language": "python",
                "input_data": {
                    "constructor": {"args": [5, 0.0], "kwargs": {}},
                    "calls": [
                        {"method": "allow", "args": [3], "kwargs": {}},
                        {"method": "allow", "args": [3], "kwargs": {}},
                    ],
                },
                "expected_output": False,
            },
            {
                "code": "rate_more_than_capacity_denied",
                "name": "request larger than capacity is denied",
                "language": "python",
                "input_data": {
                    "constructor": {"args": [5, 0.0], "kwargs": {}},
                    "calls": [
                        {"method": "allow", "args": [6], "kwargs": {}},
                    ],
                },
                "expected_output": False,
            },
            {
                "code": "rate_refill_after_time_passes",
                "name": "tokens refill after time passes",
                "language": "python",
                "input_data": {
                    "constructor": {"args": [5, 1.0], "kwargs": {}},
                    "monotonic_sequence": [0.0, 0.0, 0.0, 3.0],
                    "calls": [
                        {"method": "allow", "args": [5], "kwargs": {}},
                        {"method": "allow", "args": [1], "kwargs": {}},
                        {"method": "allow", "args": [1], "kwargs": {}},
                    ],
                },
                "expected_output": True,
            },
        ],
    },

    "C-WATERMARK": {
        "extra_config": {
            "entrypoint_kind": "class",
            "entrypoint_name": "DailyDistinctAggregator",
        },
        "cases": [
            {
                "code": "watermark_single_day_single_region",
                "name": "single day single region distinct count",
                "language": "python",
                "input_data": {
                    "constructor": {"args": [], "kwargs": {}},
                    "calls": [
                        {"method": "add", "args": [{"user_id": 1, "ts": 1740787200, "region": "EU"}], "kwargs": {}},
                        {"method": "add", "args": [{"user_id": 2, "ts": 1740787201, "region": "EU"}], "kwargs": {}},
                        {"method": "advance_watermark", "args": [1740873600], "kwargs": {}},
                    ],
                },
                "expected_output": [["2025-03-01", "EU", 2]],
            },
            {
                "code": "watermark_dedup_same_user_same_day",
                "name": "same user same day same region counted once",
                "language": "python",
                "input_data": {
                    "constructor": {"args": [], "kwargs": {}},
                    "calls": [
                        {"method": "add", "args": [{"user_id": 1, "ts": 1740787200, "region": "EU"}], "kwargs": {}},
                        {"method": "add", "args": [{"user_id": 1, "ts": 1740787201, "region": "EU"}], "kwargs": {}},
                        {"method": "advance_watermark", "args": [1740873600], "kwargs": {}},
                    ],
                },
                "expected_output": [["2025-03-01", "EU", 1]],
            },
            {
                "code": "watermark_late_event_is_ignored",
                "name": "late event for closed day is ignored",
                "language": "python",
                "input_data": {
                    "constructor": {"args": [], "kwargs": {}},
                    "calls": [
                        {"method": "add", "args": [{"user_id": 1, "ts": 1740787200, "region": "EU"}], "kwargs": {}},
                        {"method": "advance_watermark", "args": [1740873600], "kwargs": {}},
                        {"method": "add", "args": [{"user_id": 2, "ts": 1740787205, "region": "EU"}], "kwargs": {}},
                        {"method": "advance_watermark", "args": [1740960000], "kwargs": {}},
                    ],
                },
                "expected_output": [],
            },
            {
                "code": "watermark_multiple_regions_sorted",
                "name": "results are sorted by day and region",
                "language": "python",
                "input_data": {
                    "constructor": {"args": [], "kwargs": {}},
                    "calls": [
                        {"method": "add", "args": [{"user_id": 1, "ts": 1740787200, "region": "US"}], "kwargs": {}},
                        {"method": "add", "args": [{"user_id": 2, "ts": 1740787201, "region": "EU"}], "kwargs": {}},
                        {"method": "advance_watermark", "args": [1740873600], "kwargs": {}},
                    ],
                },
                "expected_output": [["2025-03-01", "EU", 1], ["2025-03-01", "US", 1]],
            },
        ],
    },

    "C-SCD2": {
        "extra_config": {
            "entrypoint_kind": "function",
            "entrypoint_name": "scd2_merge",
        },
        "cases": [
            {
                "code": "scd2_insert_new_customer",
                "name": "new customer creates one active row",
                "language": "python",
                "input_data": {
                    "args": [
                        [],
                        [
                            {
                                "customer_id": 1,
                                "attrs": {"city": "A"},
                                "as_of": "2026-01-01",
                            }
                        ],
                    ],
                    "kwargs": {},
                },
                "expected_output": [
                    {
                        "customer_id": 1,
                        "attrs": {"city": "A"},
                        "valid_from": "2026-01-01",
                        "valid_to": None,
                    }
                ],
            },
            {
                "code": "scd2_same_attrs_no_change",
                "name": "same attrs do not create new version",
                "language": "python",
                "input_data": {
                    "args": [
                        [
                            {
                                "customer_id": 1,
                                "attrs": {"city": "A"},
                                "valid_from": "2026-01-01",
                                "valid_to": None,
                            }
                        ],
                        [
                            {
                                "customer_id": 1,
                                "attrs": {"city": "A"},
                                "as_of": "2026-02-01",
                            }
                        ],
                    ],
                    "kwargs": {},
                },
                "expected_output": [
                    {
                        "customer_id": 1,
                        "attrs": {"city": "A"},
                        "valid_from": "2026-01-01",
                        "valid_to": None,
                    }
                ],
            },
            {
                "code": "scd2_changed_attrs_close_old_and_open_new",
                "name": "changed attrs create closed old row and new active row",
                "language": "python",
                "input_data": {
                    "args": [
                        [
                            {
                                "customer_id": 1,
                                "attrs": {"city": "A"},
                                "valid_from": "2026-01-01",
                                "valid_to": None,
                            }
                        ],
                        [
                            {
                                "customer_id": 1,
                                "attrs": {"city": "B"},
                                "as_of": "2026-02-01",
                            }
                        ],
                    ],
                    "kwargs": {},
                },
                "expected_output": [
                    {
                        "customer_id": 1,
                        "attrs": {"city": "A"},
                        "valid_from": "2026-01-01",
                        "valid_to": "2026-02-01",
                    },
                    {
                        "customer_id": 1,
                        "attrs": {"city": "B"},
                        "valid_from": "2026-02-01",
                        "valid_to": None,
                    },
                ],
            },
        ],
    },
}


def get_task_by_external_id(db: Session, external_id: str) -> models.Task | None:
    return db.query(models.Task).filter(models.Task.external_id == external_id).first()


def deactivate_missing_cases(db: Session, *, task: models.Task, active_codes: set[str]) -> None:
    linked_cases = (
        db.query(models.TestCase)
        .join(models.TaskTestCase, models.TaskTestCase.test_case_id == models.TestCase.id)
        .filter(models.TaskTestCase.task_id == task.id)
        .all()
    )

    for tc in linked_cases:
        if tc.code not in active_codes:
            tc.is_active = False
            db.add(tc)


def ensure_testcase(
    db: Session,
    *,
    task: models.Task,
    code: str,
    name: str,
    language: str,
    input_data: dict,
    expected_output,
    description: str | None = None,
    checker_source: str | None = None,
) -> models.TestCase:
    existing = (
        db.query(models.TestCase)
        .join(models.TaskTestCase, models.TaskTestCase.test_case_id == models.TestCase.id)
        .filter(
            models.TaskTestCase.task_id == task.id,
            models.TestCase.code == code,
        )
        .first()
    )

    stored_input = dict(input_data or {})

    if existing:
        existing.name = name
        existing.language = language
        existing.description = description
        existing.input_data = stored_input
        existing.expected_output = expected_output
        existing.checker_source = checker_source
        existing.is_active = True
        db.add(existing)
        return existing

    tc = models.TestCase(
        code=code,
        name=name,
        description=description,
        language=language,
        input_data=stored_input,
        expected_output=expected_output,
        checker_source=checker_source,
        is_active=True,
    )
    db.add(tc)
    db.flush()

    link = models.TaskTestCase(task_id=task.id, test_case_id=tc.id)
    db.add(link)
    return tc


def seed() -> None:
    db = SessionLocal()
    try:
        for external_id, payload in TASK_TESTCASES.items():
            task = get_task_by_external_id(db, external_id)
            if not task:
                print(f"[WARN] Task not found: {external_id}")
                continue

            extra = dict(task.extra_config or {})
            extra.update(payload.get("extra_config") or {})
            task.extra_config = extra
            db.add(task)
            db.flush()

            active_codes = {case["code"] for case in payload.get("cases") or []}
            deactivate_missing_cases(db, task=task, active_codes=active_codes)

            for case in payload.get("cases") or []:
                ensure_testcase(
                    db,
                    task=task,
                    code=case["code"],
                    name=case["name"],
                    language=case.get("language", "python"),
                    input_data=case.get("input_data") or {},
                    expected_output=case.get("expected_output"),
                    description=case.get("description"),
                    checker_source=case.get("checker_source"),
                )

            print(f"[OK] Seeded testcases for {external_id}")

        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    seed()