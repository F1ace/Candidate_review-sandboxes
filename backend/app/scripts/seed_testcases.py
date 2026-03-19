from app.database import SessionLocal
from app import models


TASK_EXTERNAL_ID = "code_sum_two_mvp"


def main():
    db = SessionLocal()
    try:
        task = (
            db.query(models.Task)
            .filter(models.Task.external_id == TASK_EXTERNAL_ID)
            .first()
        )

        if not task:
            print(f"Task '{TASK_EXTERNAL_ID}' not found")
            return

        existing_links = (
            db.query(models.TaskTestCase)
            .filter(models.TaskTestCase.task_id == task.id)
            .count()
        )

        if existing_links > 0:
            print(f"Task '{TASK_EXTERNAL_ID}' already has linked testcases, skipping")
            return

        testcases_data = [
            {
                "code": "sum_two_basic_case_1",
                "name": "sum_two basic positive numbers",
                "description": "Checks sum_two on positive integers",
                "input_data": {"args": [2, 3], "kwargs": {}},
                "expected_output": {"result": 5},
            },
            {
                "code": "sum_two_basic_case_2",
                "name": "sum_two mixed signs",
                "description": "Checks sum_two with negative and positive integers",
                "input_data": {"args": [-1, 4], "kwargs": {}},
                "expected_output": {"result": 3},
            },
            {
                "code": "sum_two_basic_case_3",
                "name": "sum_two zeros",
                "description": "Checks sum_two on zero values",
                "input_data": {"args": [0, 0], "kwargs": {}},
                "expected_output": {"result": 0},
            },
        ]

        for index, item in enumerate(testcases_data):
            existing_tc = (
                db.query(models.TestCase)
                .filter(models.TestCase.code == item["code"])
                .first()
            )

            if existing_tc:
                tc = existing_tc
                print(f"TestCase '{tc.code}' already exists, reusing")
            else:
                tc = models.TestCase(
                    code=item["code"],
                    name=item["name"],
                    description=item["description"],
                    language="python",
                    input_data=item["input_data"],
                    expected_output=item["expected_output"],
                    checker_source=None,
                    is_public=False,
                    is_hidden=True,
                    is_active=True,
                    version=1,
                )
                db.add(tc)
                db.flush()
                print(f"Created TestCase '{tc.code}'")

            link = models.TaskTestCase(
                task_id=task.id,
                test_case_id=tc.id,
                order_index=index,
                weight=1,
                is_required=True,
            )
            db.add(link)

        db.commit()
        print(f"Added {len(testcases_data)} testcases to task '{TASK_EXTERNAL_ID}'")

    finally:
        db.close()


if __name__ == "__main__":
    main()