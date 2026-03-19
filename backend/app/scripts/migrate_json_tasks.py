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
}


def main():
    db = SessionLocal()
    try:
        scenarios = db.query(models.Scenario).all()

        for scenario in scenarios:
            old_tasks = scenario.tasks or []
            if not old_tasks:
                continue

            existing_count = (
                db.query(models.Task)
                .filter(models.Task.scenario_id == scenario.id)
                .count()
            )
            if existing_count > 0:
                print(f"Scenario {scenario.id}: tasks already migrated, skipping")
                continue

            for idx, old_task in enumerate(old_tasks):
                extra = {k: v for k, v in old_task.items() if k not in KNOWN_KEYS}

                if old_task.get("tests_id"):
                    extra["tests_id"] = old_task.get("tests_id")

                task = models.Task(
                    scenario_id=scenario.id,
                    external_id=old_task.get("id"),
                    task_type=old_task.get("type"),
                    title=old_task.get("title") or old_task.get("id") or f"task_{idx}",
                    description_for_candidate=old_task.get("description_for_candidate"),
                    max_points=int(old_task.get("max_points") or 0),
                    order_index=idx,
                    language=old_task.get("language"),
                    sql_scenario_ref=old_task.get("sql_scenario_id"),
                    starter_code=old_task.get("starter_code"),
                    statement_md=old_task.get("statement_md"),
                    related_topics=old_task.get("related_topics"),
                    questions=old_task.get("questions"),
                    extra_config=extra or None,
                )
                db.add(task)

            db.commit()
            print(f"Scenario {scenario.id}: migrated {len(old_tasks)} tasks")

    finally:
        db.close()


if __name__ == "__main__":
    main()