from app.database import SessionLocal
from app import models


def main():
    db = SessionLocal()
    try:
        scenario = db.query(models.Scenario).filter(models.Scenario.slug == "be-junior-shortener").first()
        if not scenario:
            print("Scenario with slug 'be-junior-shortener' not found")
            return

        existing = (
            db.query(models.Task)
            .filter(
                models.Task.scenario_id == scenario.id,
                models.Task.external_id == "code_sum_two_mvp",
            )
            .first()
        )
        if existing:
            print("MVP task already exists")
            return

        task = models.Task(
            scenario_id=scenario.id,
            external_id="code_sum_two_mvp",
            task_type="coding",
            title="Написать функцию sum_two",
            description_for_candidate="Реализуйте функцию sum_two(a, b), которая возвращает сумму двух чисел.",
            max_points=5,
            order_index=999,
            language="python",
            starter_code="def sum_two(a, b):\n    pass\n",
            extra_config={
                "tests_id": "sum_two_basic",
                "entrypoint_kind": "function",
                "entrypoint_name": "sum_two",
            },
        )
        db.add(task)
        db.commit()
        print("MVP task created")

    finally:
        db.close()


if __name__ == "__main__":
    main()