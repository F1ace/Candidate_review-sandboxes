import json
import os
from typing import Any

from sqlalchemy import create_engine, text


def get_database_url() -> str:
    # 1) обычный env
    db_url = os.getenv("DATABASE_URL")
    if db_url:
        return db_url

    # 2) compose-style env pieces
    pg_user = os.getenv("POSTGRES_USER", "postgres")
    pg_password = os.getenv("POSTGRES_PASSWORD", "postgres")
    pg_host = os.getenv("POSTGRES_HOST", "localhost")
    pg_port = os.getenv("POSTGRES_PORT", "5432")
    pg_db = os.getenv("POSTGRES_DB", "reviewer")

    return f"postgresql+psycopg2://{pg_user}:{pg_password}@{pg_host}:{pg_port}/{pg_db}"


def build_scenarios() -> list[dict[str, Any]]:
    return [
        {
            "name": "ecommerce_basic",
            "description": (
                "Посчитать сумму завершённых заказов по городам клиентов. "
                "Использовать таблицы orders и customers. "
                "В выборке должны быть только заказы со статусом paid или shipped. "
                "Вернуть city, orders_cnt, total_revenue. "
                "Сортировка по total_revenue DESC, затем city ASC."
            ),
            "db_schema": """
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS customers;

CREATE TABLE customers (
    customer_id INTEGER PRIMARY KEY,
    customer_name TEXT NOT NULL,
    city TEXT NOT NULL,
    signup_date TEXT NOT NULL
);

CREATE TABLE orders (
    order_id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    order_date TEXT NOT NULL,
    amount NUMERIC NOT NULL,
    status TEXT NOT NULL,
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
);

INSERT INTO customers (customer_id, customer_name, city, signup_date) VALUES
(1, 'Alice', 'Moscow', '2026-01-05'),
(2, 'Bob', 'Moscow', '2026-01-11'),
(3, 'Clara', 'Berlin', '2026-01-20'),
(4, 'Dan', 'Berlin', '2026-02-01'),
(5, 'Eva', 'Paris', '2026-02-10');

INSERT INTO orders (order_id, customer_id, order_date, amount, status) VALUES
(101, 1, '2026-02-01', 120, 'paid'),
(102, 1, '2026-02-03', 80, 'cancelled'),
(103, 2, '2026-02-04', 150, 'shipped'),
(104, 3, '2026-02-05', 200, 'paid'),
(105, 3, '2026-02-07', 50, 'paid'),
(106, 4, '2026-02-08', 70, 'pending'),
(107, 5, '2026-02-09', 300, 'shipped'),
(108, 5, '2026-02-11', 20, 'cancelled');
""".strip(),
            "reference_solutions": {
                "candidate_goal": "Посчитать сумму завершённых заказов по городам клиентов",
                "expected_columns": ["city", "orders_cnt", "total_revenue"],
                "order_sensitive": True,
                "compare_mode": "exact",
                "solution_queries": [
                    "SELECT c.city, COUNT(*) AS orders_cnt, SUM(o.amount) AS total_revenue "
                    "FROM orders o "
                    "JOIN customers c ON c.customer_id = o.customer_id "
                    "WHERE o.status IN ('paid', 'shipped') "
                    "GROUP BY c.city "
                    "ORDER BY total_revenue DESC, c.city ASC"
                ],
                "notes_for_evaluator": [
                    "Если кандидат считает все статусы, это ошибка бизнес-логики",
                    "COUNT(order_id) и COUNT(*) одинаково допустимы"
                ],
            },
        },
        {
            "name": "ab_product",
            "description": (
                "Для каждой даты регистрации пользователей посчитать signup_date, "
                "users_cnt, converted_users и conversion_rate. "
                "Пользователь считается сконвертировавшимся, если у него есть хотя бы одно событие purchase. "
                "Использовать таблицы users и events. "
                "Сортировка по signup_date."
            ),
            "db_schema": """
DROP TABLE IF EXISTS events;
DROP TABLE IF EXISTS users;

CREATE TABLE users (
    user_id INTEGER PRIMARY KEY,
    signup_date TEXT NOT NULL,
    experiment_group TEXT NOT NULL
);

CREATE TABLE events (
    event_id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    event_name TEXT NOT NULL,
    event_date TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

INSERT INTO users (user_id, signup_date, experiment_group) VALUES
(1, '2026-03-01', 'A'),
(2, '2026-03-01', 'A'),
(3, '2026-03-01', 'B'),
(4, '2026-03-02', 'A'),
(5, '2026-03-02', 'B'),
(6, '2026-03-02', 'B'),
(7, '2026-03-03', 'A');

INSERT INTO events (event_id, user_id, event_name, event_date) VALUES
(1, 1, 'visit', '2026-03-01'),
(2, 1, 'purchase', '2026-03-02'),
(3, 2, 'visit', '2026-03-01'),
(4, 3, 'purchase', '2026-03-01'),
(5, 4, 'visit', '2026-03-02'),
(6, 5, 'purchase', '2026-03-03'),
(7, 5, 'purchase', '2026-03-04'),
(8, 6, 'visit', '2026-03-02'),
(9, 7, 'visit', '2026-03-03');
""".strip(),
            "reference_solutions": {
                "candidate_goal": "Посчитать конверсию по дню регистрации",
                "expected_columns": ["signup_date", "users_cnt", "converted_users", "conversion_rate"],
                "order_sensitive": True,
                "compare_mode": "exact",
                "solution_queries": [
                    "SELECT u.signup_date, "
                    "COUNT(*) AS users_cnt, "
                    "COUNT(DISTINCT CASE WHEN e.event_name = 'purchase' THEN u.user_id END) AS converted_users, "
                    "ROUND(1.0 * COUNT(DISTINCT CASE WHEN e.event_name = 'purchase' THEN u.user_id END) / COUNT(*), 4) AS conversion_rate "
                    "FROM users u "
                    "LEFT JOIN events e ON e.user_id = u.user_id "
                    "GROUP BY u.signup_date "
                    "ORDER BY u.signup_date"
                ],
                "notes_for_evaluator": [
                    "Если считает количество purchase-событий вместо пользователей — это ошибка",
                    "Если INNER JOIN, то теряются когорты без конверсии"
                ],
            },
        },
        {
            "name": "events_basic",
            "description": (
                "Посчитать дневную активную аудиторию по регионам. "
                "Активный пользователь — уникальный user_id, у которого было хотя бы одно событие в этот день. "
                "Использовать таблицу events. "
                "Вернуть event_date, region, dau. "
                "Сортировка по event_date, затем region."
            ),
            "db_schema": """
DROP TABLE IF EXISTS events;

CREATE TABLE events (
    event_id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    event_name TEXT NOT NULL,
    event_ts TEXT NOT NULL,
    event_date TEXT NOT NULL,
    region TEXT NOT NULL
);

INSERT INTO events (event_id, user_id, event_name, event_ts, event_date, region) VALUES
(1, 101, 'open_app', '2026-03-10 09:00:00', '2026-03-10', 'EU'),
(2, 101, 'click',    '2026-03-10 09:05:00', '2026-03-10', 'EU'),
(3, 102, 'open_app', '2026-03-10 10:00:00', '2026-03-10', 'EU'),
(4, 201, 'open_app', '2026-03-10 11:00:00', '2026-03-10', 'US'),
(5, 201, 'purchase', '2026-03-10 11:20:00', '2026-03-10', 'US'),
(6, 101, 'open_app', '2026-03-11 08:00:00', '2026-03-11', 'EU'),
(7, 103, 'open_app', '2026-03-11 08:10:00', '2026-03-11', 'EU'),
(8, 202, 'open_app', '2026-03-11 12:00:00', '2026-03-11', 'US'),
(9, 203, 'open_app', '2026-03-11 12:30:00', '2026-03-11', 'US'),
(10, 203, 'click',   '2026-03-11 12:31:00', '2026-03-11', 'US');
""".strip(),
            "reference_solutions": {
                "candidate_goal": "Посчитать DAU по регионам",
                "expected_columns": ["event_date", "region", "dau"],
                "order_sensitive": True,
                "compare_mode": "exact",
                "solution_queries": [
                    "SELECT event_date, region, COUNT(DISTINCT user_id) AS dau "
                    "FROM events "
                    "GROUP BY event_date, region "
                    "ORDER BY event_date, region"
                ],
                "notes_for_evaluator": [
                    "COUNT(*) вместо COUNT(DISTINCT user_id) — ключевая ошибка"
                ],
            },
        },
        {
            "name": "scd_customers",
            "description": (
                "В таблице dim_customers хранится история клиентов по правилам SCD Type 2. "
                "Нужно обработать входящие изменения из customer_updates: "
                "если атрибуты клиента изменились, закрыть текущую запись и вставить новую. "
                "Если изменений нет — ничего не делать."
            ),
            "db_schema": """
DROP TABLE IF EXISTS dim_customers;
DROP TABLE IF EXISTS customer_updates;

CREATE TABLE dim_customers (
    customer_id INTEGER NOT NULL,
    customer_name TEXT NOT NULL,
    city TEXT NOT NULL,
    tier TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    is_current INTEGER NOT NULL
);

CREATE TABLE customer_updates (
    customer_id INTEGER NOT NULL,
    customer_name TEXT NOT NULL,
    city TEXT NOT NULL,
    tier TEXT NOT NULL,
    as_of_date TEXT NOT NULL
);

INSERT INTO dim_customers (customer_id, customer_name, city, tier, valid_from, valid_to, is_current) VALUES
(1, 'Alice', 'Moscow', 'silver', '2026-01-01', NULL, 1),
(2, 'Bob',   'Berlin', 'gold',   '2026-01-01', NULL, 1),
(3, 'Clara', 'Paris',  'silver', '2026-01-01', NULL, 1);

INSERT INTO customer_updates (customer_id, customer_name, city, tier, as_of_date) VALUES
(1, 'Alice', 'Moscow', 'gold',   '2026-03-01'),
(2, 'Bob',   'Berlin', 'gold',   '2026-03-01'),
(3, 'Clara', 'Lyon',   'silver', '2026-03-01');
""".strip(),
            "reference_solutions": {
                "candidate_goal": "Применить SCD Type 2 обновления",
                "expected_columns": [
                    "customer_id",
                    "customer_name",
                    "city",
                    "tier",
                    "valid_from",
                    "valid_to",
                    "is_current",
                ],
                "order_sensitive": True,
                "compare_mode": "post_state",
                "solution_queries": [
                    "UPDATE dim_customers "
                    "SET valid_to = ("
                    "    SELECT cu.as_of_date "
                    "    FROM customer_updates cu "
                    "    WHERE cu.customer_id = dim_customers.customer_id"
                    "), "
                    "is_current = 0 "
                    "WHERE is_current = 1 "
                    "  AND EXISTS ("
                    "      SELECT 1 "
                    "      FROM customer_updates cu "
                    "      WHERE cu.customer_id = dim_customers.customer_id "
                    "        AND ("
                    "            cu.customer_name <> dim_customers.customer_name OR "
                    "            cu.city <> dim_customers.city OR "
                    "            cu.tier <> dim_customers.tier"
                    "        )"
                    "  ); "
                    "INSERT INTO dim_customers ("
                    "    customer_id, customer_name, city, tier, valid_from, valid_to, is_current"
                    ") "
                    "SELECT "
                    "    cu.customer_id, "
                    "    cu.customer_name, "
                    "    cu.city, "
                    "    cu.tier, "
                    "    cu.as_of_date, "
                    "    NULL, "
                    "    1 "
                    "FROM customer_updates cu "
                    "JOIN dim_customers d "
                    "  ON d.customer_id = cu.customer_id "
                    "WHERE d.valid_to = cu.as_of_date "
                    "  AND d.is_current = 0"
                ],
                "validation_query": (
                    "SELECT customer_id, customer_name, city, tier, valid_from, valid_to, is_current "
                    "FROM dim_customers "
                    "ORDER BY customer_id, valid_from"
                ),
                "notes_for_evaluator": [
                    "Для customer_id=2 новая запись не должна создаваться",
                    "Для customer_id=1 и 3 должны появиться новые версии"
                ],
            },
        },
    ]


def main() -> None:
    db_url = get_database_url()
    engine = create_engine(db_url)

    delete_stmt = text(
        """
        DELETE FROM sql_scenarios
        WHERE name IN (
            'ecommerce_basic',
            'ab_product',
            'events_basic',
            'scd_customers'
        )
        """
    )

    insert_stmt = text(
        """
        INSERT INTO sql_scenarios (
            name,
            description,
            db_schema,
            reference_solutions
        )
        VALUES (
            :name,
            :description,
            :db_schema,
            CAST(:reference_solutions AS json)
        )
        """
    )

    scenarios = build_scenarios()

    with engine.begin() as conn:
        conn.execute(delete_stmt)

        for scenario in scenarios:
            conn.execute(
                insert_stmt,
                {
                    "name": scenario["name"],
                    "description": scenario["description"],
                    "db_schema": scenario["db_schema"],
                    "reference_solutions": json.dumps(
                        scenario["reference_solutions"],
                        ensure_ascii=False,
                    ),
                },
            )

    print(f"Inserted {len(scenarios)} SQL scenarios into sql_scenarios")


if __name__ == "__main__":
    main()