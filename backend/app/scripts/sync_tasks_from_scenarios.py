from app.database import SessionLocal
from app import models


KNOWN_KEYS = {
    "id",
    "type",
    "title",
    "description_for_candidate",
    "max_points",
    "language",
    "sql_scenario_id",
    "starter_code",
    "statement_md",
    "related_topics",
    "questions",
    "tests_id",
    "entrypoint",
    "entrypoint_kind",
    "interface",
    "hints_allowed",
}


def sync():
    db = SessionLocal()
    try:
        scenarios = db.query(models.Scenario).all()

        for scenario in scenarios:
            payload_tasks = scenario.tasks or []
            existing = {
                t.external_id: t
                for t in db.query(models.Task).filter(models.Task.scenario_id == scenario.id).all()
            }

            for idx, item in enumerate(payload_tasks):
                extra = {k: v for k, v in item.items() if k not in KNOWN_KEYS}
                if item.get("tests_id"):
                    extra["tests_id"] = item.get("tests_id")
                if item.get("entrypoint"):
                    extra["entrypoint"] = item.get("entrypoint")
                if item.get("entrypoint_kind"):
                    extra["entrypoint_kind"] = item.get("entrypoint_kind")
                if item.get("interface"):
                    extra["interface"] = item.get("interface")
                if item.get("hints_allowed") is not None:
                    extra["hints_allowed"] = item.get("hints_allowed")

                task = existing.get(item["id"])
                if task is None:
                    task = models.Task(
                        scenario_id=scenario.id,
                        external_id=item["id"],
                        task_type=item["type"],
                        title=item.get("title") or item["id"],
                    )
                    db.add(task)

                task.task_type = item["type"]
                task.title = item.get("title") or item["id"]
                task.description_for_candidate = item.get("description_for_candidate")
                task.max_points = int(item.get("max_points") or 0)
                task.order_index = idx
                task.language = item.get("language")
                task.sql_scenario_ref = item.get("sql_scenario_id")
                task.starter_code = item.get("starter_code")
                task.statement_md = item.get("statement_md")
                task.related_topics = item.get("related_topics")
                task.questions = item.get("questions")
                task.extra_config = extra or None

            db.commit()
            print(f"[OK] Synced tasks for scenario {scenario.slug}")

    finally:
        db.close()


if __name__ == "__main__":
    sync()