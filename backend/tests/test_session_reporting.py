from datetime import datetime

from app.services.session_reporting import (
    ReportContext,
    TaskSnapshot,
    build_fallback_report_payload,
    effective_task_max_points,
    merge_llm_report_payload,
)


def make_context() -> ReportContext:
    return ReportContext(
        session_id="session-1",
        candidate_id="candidate-42",
        role_name="Backend Engineer",
        role_slug="backend",
        scenario_name="REST API interview",
        scenario_slug="be-rest",
        difficulty="middle",
        started_at=datetime(2026, 4, 22, 10, 0, 0),
        finished_at=datetime(2026, 4, 22, 10, 45, 0),
        duration_minutes=45,
        overall_score=14.0,
        overall_max=18.0,
        overall_ratio=0.7778,
        scored_tasks=2,
        total_tasks=2,
        candidate_message_count=8,
        model_message_count=5,
        task_snapshots=[
            TaskSnapshot(
                task_id="T1",
                title="HTTP semantics",
                task_type="theory",
                score=8.0,
                max_points=10.0,
                ratio=0.8,
                score_comment="Explained PUT/PATCH well, but skipped edge cases.",
                transcript_excerpt=[
                    "Candidate compared PUT and PATCH semantics.",
                    "Candidate missed retry behaviour for idempotency.",
                ],
            ),
            TaskSnapshot(
                task_id="C1",
                title="Queue worker",
                task_type="coding",
                score=6.0,
                max_points=8.0,
                ratio=0.75,
                score_comment="Implementation works, but shutdown handling is incomplete.",
                transcript_excerpt=[
                    "Candidate implemented ack and nack paths.",
                ],
            ),
        ],
        transcript=[
            {"sender": "candidate", "task_id": "T1", "text": "PUT replaces a resource."},
            {"sender": "model", "task_id": "T1", "text": "How do retries affect that?"},
        ],
    )


def test_effective_task_max_points_forces_theory_scale() -> None:
    assert effective_task_max_points({"type": "theory", "max_points": 3}) == 10.0
    assert effective_task_max_points({"type": "coding", "max_points": 8}) == 8.0
    assert effective_task_max_points({"type": "sql"}) == 10.0


def test_build_fallback_report_payload_includes_task_breakdown_and_scores() -> None:
    context = make_context()

    payload = build_fallback_report_payload(context)

    assert payload["generation_mode"] == "fallback"
    assert payload["session_id"] == context.session_id
    assert payload["overall_score"] == 14.0
    assert payload["overall_max"] == 18.0
    assert payload["scored_tasks"] == 2
    assert len(payload["sections"]) == 4
    assert len(payload["task_breakdown"]) == 2
    assert payload["task_breakdown"][0]["task_id"] == "T1"
    assert payload["task_breakdown"][0]["max_points"] == 10.0
    assert payload["task_breakdown"][1]["task_id"] == "C1"
    assert payload["task_breakdown"][1]["max_points"] == 8.0
    assert payload["strengths"]
    assert payload["growth_areas"]


def test_merge_llm_report_payload_overrides_text_and_preserves_fallback_tasks() -> None:
    context = make_context()
    fallback_payload = build_fallback_report_payload(context)

    raw_payload = {
        "headline": "Backend interview summary",
        "executive_summary": "Candidate showed solid fundamentals and acceptable execution.",
        "overall_assessment": "Strong on HTTP basics, weaker on production edge cases.",
        "closing_note": "Worth discussing reliability in the next round.",
        "recommendation_label": "Proceed to next stage",
        "recommendation_summary": "Use a deeper production design interview next.",
        "strengths": ["Clear HTTP reasoning", "Understands queue semantics"],
        "growth_areas": ["Needs stronger shutdown and retry handling"],
        "sections": [
            {
                "title": "Decision",
                "summary": "The candidate is viable for a deeper technical screen.",
                "highlights": ["Good fundamentals", "Some production gaps"],
            }
        ],
        "task_breakdown": [
            {
                "task_id": "T1",
                "summary": "Theory answer was confident and mostly complete.",
                "highlights": ["Explained idempotency", "Missed a few retry nuances"],
            },
            {
                "task_id": "missing-task",
                "summary": "Should be ignored.",
                "highlights": ["Unexpected"],
            },
        ],
    }

    merged = merge_llm_report_payload(raw_payload, fallback_payload, context)

    assert merged["generation_mode"] == "llm"
    assert merged["headline"] == "Backend interview summary"
    assert merged["strengths"] == ["Clear HTTP reasoning", "Understands queue semantics"]
    assert merged["sections"] == [
        {
            "title": "Decision",
            "summary": "The candidate is viable for a deeper technical screen.",
            "highlights": ["Good fundamentals", "Some production gaps"],
        }
    ]
    assert len(merged["task_breakdown"]) == 2
    assert merged["task_breakdown"][0]["task_id"] == "T1"
    assert merged["task_breakdown"][0]["summary"] == "Theory answer was confident and mostly complete."
    assert merged["task_breakdown"][1]["task_id"] == "C1"
    assert merged["task_breakdown"][1]["summary"] == fallback_payload["task_breakdown"][1]["summary"]
