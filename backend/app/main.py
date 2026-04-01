from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import models
from .config import settings
from .database import SessionLocal
from .routers import roles, scenarios, sessions, sql_scenarios
from .services import lm_client
from .scripts.sync_tasks_from_scenarios import sync as sync_tasks_from_scenarios


app = FastAPI(title="Chat-Review for candidates", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.allow_origins.split(",") if origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(roles.router)
app.include_router(scenarios.router)
#app.include_router(rag.router)
app.include_router(sql_scenarios.router)
app.include_router(sessions.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="frontend")
else:

    @app.get("/")
    def root_fallback() -> dict:
        return {"message": "Frontend build not found. Run `npm run build` in frontend/."}


def seed_defaults() -> None:
    """Insert demo roles/scenarios if DB is empty to avoid 404 on first run."""

    db = SessionLocal()
    try:
        ds = db.query(models.Role).filter_by(slug="ds").one_or_none()
        be = db.query(models.Role).filter_by(slug="backend").one_or_none()
        de = db.query(models.Role).filter_by(slug="de").one_or_none()

        if not ds:
            ds = models.Role(name="Data Scientist", slug="ds", description="ML, эксперименты, метрики")
            db.add(ds)
        if not be:
            be = models.Role(name="Backend", slug="backend", description="API, очереди, устойчивость")
            db.add(be)
        if not de:
            de = models.Role(name="Data Engineer", slug="de", description="ETL, SQL, пайплайны")
            db.add(de)
        db.flush()

        scenarios_payload: list[models.Scenario] = [
            models.Scenario(
                role_id=ds.id,
                name="DS — Junior ML",
                slug="ds-junior-ml",
                description="Регрессия, классификация, SQL основы",
                difficulty="junior",
                config={
                    "releated_topics": [
                        "ml_basics",
                        "regularization",
                        "logistic_regression",
                        "sql_basics",
                    ]
                },
                tasks=[
                    {
                        "id": "T1",
                        "type": "theory",
                        "title": "DS Junior — основы ML",
                        "max_points": 10,
                        "questions": [
                            "Чем отличаются задачи регрессии и классификации? Приведите по одному примеру для каждой.",
                            "Что такое переобучение и какие базовые способы борьбы с ним вы знаете?",
                            "В чём различие между L1- и L2-регуляризацией и как это влияет на веса модели?",
                            "Как работает логистическая регрессия и почему её результат удобно интерпретировать как вероятность?",
                        ],
                        "hints_allowed": False,
                    },
                    {
                        "id": "C1",
                        "type": "coding",
                        "language": "python",
                        "title": "Логистическая регрессия",
                        "max_points": 10,
                        "tests_id": "logreg_basic",
                        "entrypoint": "LogisticRegression",
                        "entrypoint_kind": "class",
                        "statement_md": """
Реализуйте класс `LogisticRegression` без использования `sklearn` для бинарной классификации.

Что должен уметь класс:  
- `fit(X, y)` — обучает модель на данных и возвращает `self`;  
- `predict_proba(X)` — возвращает список вероятностей положительного класса;  
- `predict(X)` — возвращает список предсказанных меток `0` или `1`.

Формат входных данных:  
- `X` — список объектов вида `list[list[float]]`;  
- `y` — список меток `list[int]`, где каждая метка равна `0` или `1`.

Пример 1:  
Вход:  
`X = [[0.0], [1.0], [2.0], [3.0]]`  
`y = [0, 0, 1, 1]`  
после `fit(X, y)` вызвать `predict([[0.0], [3.0]])`  
Выход: `[0, 1]`

Пример 2:  
Вход:  
после `fit(X, y)` вызвать `predict([[0.0], [1.0], [3.0]])`  
Выход: `[0, 0, 1]`

Пример 3:  
Вход:  
после `fit(X, y)` вызвать `predict_proba([[0.5], [2.5]])`  
Выход: список из двух чисел в диапазоне `[0, 1]`

Ограничения:  
- используйте только Python и базовые численные операции;  
- `fit(X, y)` должен возвращать `self`;  
- `predict_proba(X)` должен вернуть `list[float]` той же длины, что и `X`;  
- `predict(X)` должен вернуть `list[int]` той же длины, что и `X`.
"""
                        .strip(),
                        "starter_code": (
                            """
from __future__ import annotations

import math


class LogisticRegression:
    def __init__(self, lr: float, n_iters: int):
        self.lr = lr
        self.n_iters = n_iters

    def fit(self, X, y):
        raise NotImplementedError

    def predict_proba(self, X):
        raise NotImplementedError

    def predict(self, X):
        raise NotImplementedError
"""
                        ).strip(),
                        "interface": {
                            "init_args": ["lr", "n_iters"],
                            "methods": [
                                {"name": "fit", "args": ["X", "y"], "returns": "self"},
                                {
                                    "name": "predict_proba",
                                    "args": ["X"],
                                    "returns": "list[float]",
                                },
                                {"name": "predict", "args": ["X"], "returns": "list[int]"},
                            ],
                        },
                    },
                    {
                        "id": "SQL1",
                        "type": "sql",
                        "title": "Агрегация завершённых заказов",
                        "description_for_candidate": (
                            "По таблицам orders и customers посчитайте сумму завершённых заказов по городам клиентов. "
                            "Учитывайте только заказы со статусом paid или shipped. "
                            "Верните city, orders_cnt, total_revenue."
                        ),
                        "max_points": 10,
                        "sql_scenario_id": "ecommerce_basic",
                    },
                ],
            ),
            models.Scenario(
                role_id=ds.id,
                name="DS — Product ML",
                slug="ds-product-ml",
                description="A/B, продуктовые метрики, рекомендации — теория + код + SQL",
                difficulty="middle",
                config={
                    "releated_topics": [
                        "ab_testing",
                        "product_metrics",
                        "confidence_intervals",
                        "statistical_tests",
                    ]
                },
                tasks=[
                    {
                        "id": "T-DS-PRODUCT",
                        "type": "theory",
                        "title": "DS Product — эксперименты и продуктовые метрики",
                        "max_points": 10,
                        "questions": [
                            "Какие продуктовые метрики вы считаете ключевыми для оценки результата A/B-теста и как вы бы выбрали основную метрику?",
                            "Что такое p-value и доверительный интервал? Чем статистическая значимость отличается от практической значимости результата?",
                            "Что такое нарушение ожидаемого распределения пользователей между группами в A/B-тесте (SRM)? Как вы бы это проверяли и интерпретировали?",
                            "Что такое CUPED, стратификация и бакетизация? Зачем эти методы применяются в экспериментах?",
                        ],
                        "hints_allowed": False,
                    },
                    {
                        "id": "C-AB-REPORT",
                        "type": "coding",
                        "language": "python",
                        "title": "A/B Test Report Generator",
                        "max_points": 10,
                        "tests_id": "ab_report_basic",
                        "entrypoint": "ABReport",
                        "entrypoint_kind": "class",
                        "statement_md": (
                            """
Реализуйте класс `ABReport`, который собирает наблюдения по группам `A` и `B` и строит отчёт для бинарной метрики.

Метод `add(group, success)` принимает:  
- `group`: строка `"A"` или `"B"`;  
- `success`: число `0` или `1`.

Метод `report()` должен вернуть словарь со следующими полями:  
- `nA`, `nB` — размеры групп;  
- `convA`, `convB` — конверсии;  
- `diff` — абсолютная разница `convB - convA`;  
- `rel_uplift` — относительный uplift;  
- `ci_low`, `ci_high` — границы 95% доверительного интервала для `diff`;  
- `z` — z-score;  
- `p_value` — двусторонний p-value.

Пример 1:  
Вход: `add("A", 1)`, `add("A", 0)`, `add("B", 1)`, затем `report()`  
Выход:  
`nA = 2`, `nB = 1`, `convA = 0.5`, `convB = 1.0`, `diff = 0.5`

Пример 2:  
Вход: `add("A", 0)`, `add("B", 0)`, затем `report()`  
Выход:  
`nA = 1`, `nB = 1`, `convA = 0.0`, `convB = 0.0`, `diff = 0.0`

Ограничения:  
- поддерживаются только группы `A` и `B`;  
- одно наблюдение должно добавляться за `O(1)`;  
- численные поля отчёта должны быть корректно согласованы между собой.
"""
                        ).strip(),
                        "starter_code": (
                            """
from __future__ import annotations

import math


class ABReport:
    def __init__(self):
        raise NotImplementedError

    def add(self, group: str, success: int):
        raise NotImplementedError

    def report(self):
        raise NotImplementedError
"""
                        ).strip(),
                    },
                    {
                        "id": "SQL-ab",
                        "type": "sql",
                        "title": "Конверсия по дню регистрации",
                        "description_for_candidate": (
                            "Для каждой даты регистрации пользователей посчитайте "
                            "signup_date, users_cnt, converted_users и conversion_rate. "
                            "Пользователь считается сконвертировавшимся, если у него есть хотя бы одно событие purchase."
                        ),
                        "sql_scenario_id": "ab_product",
                        "max_points": 10,
                    },
                ],
            ),
            models.Scenario(
                role_id=be.id,
                name="Backend — Junior — URL Shortener",
                slug="be-junior-shortener",
                description=(
                    "Отличие между GET и Post запросами, значение статусов, "
                    "определение + пример идепотентности, реализация класса UrlShortener"
                ),
                difficulty="junior",
                config={
                    "releated_topics": [
                        "http",
                        "rest_basics",
                        "idempotency",
                        "in_memory_storage",
                    ]
                },
                tasks=[
                    {
                        "id": "T-JUNIOR-BE",
                        "type": "theory",
                        "title": "Backend Junior — базовые понятия HTTP и API",
                        "max_points": 10,
                        "questions": [
                            "Чем отличаются HTTP-методы GET и POST? Приведите пример для REST API.",
                            "Что означают HTTP-статусы 200, 201, 400, 404, 409 и 500?",
                            "Что такое идемпотентность? Приведите пример идемпотентного запроса.",
                        ],
                        "hints_allowed": False,
                    },
                    {
                        "id": "C-SHORTENER",
                        "type": "coding",
                        "language": "python",
                        "title": "Design URL Shortener",
                        "max_points": 10,
                        "tests_id": "shortener_basic",
                        "entrypoint": "UrlShortener",
                        "entrypoint_kind": "class",
                        "statement_md": (
                            """
Реализуйте in-memory сервис сокращения ссылок. Класс `UrlShortener` должен выдавать короткий код для URL и по этому коду возвращать исходную ссылку.

Методы:  
- `encode(url: str) -> str`  
- `decode(code: str) -> str`

Требования:  
- после `code = encode(url)` вызов `decode(code)` должен вернуть исходный `url`;  
- одинаковый `url` должен давать один и тот же код;  
- код должен быть строкой длины `6`;  
- код должен состоять только из латинских букв и цифр;  
- `decode` должен работать стабильно при повторных вызовах;  
- для неизвестного кода `decode(code)` должен выбрасывать `KeyError`.

Пример 1:  
Вход: `code = encode("https://example.com")`  
Выход: строка длины `6`, например `"aZ91Bc"`

Пример 2:  
Вход: `code = encode("https://example.com")`, затем `decode(code)`  
Выход: `"https://example.com"`

Пример 3:  
Вход: `code1 = encode("https://a.example.com")`, `code2 = encode("https://b.example.com")`, затем `decode(code2)`  
Выход: `"https://b.example.com"`

Пример 4:  
Вход: повторный вызов `encode("https://example.com")`  
Выход: тот же код, что и в первый раз
"""
                        ).strip(),
                        "starter_code": (
                            """
from __future__ import annotations

import secrets
import string


class UrlShortener:
    def __init__(self):
        raise NotImplementedError

    def encode(self, url: str):
        raise NotImplementedError

    def decode(self, code: str):
        raise NotImplementedError
"""
                        ).strip(),
                    },
                ],
            ),
            models.Scenario(
                role_id=be.id,
                name="Backend — REST",
                slug="be-rest",
                description="Дизайн API, идемпотентность, очереди — теория + код",
                difficulty="middle",
                config={
                    "releated_topics": [
                        "rest_api_design",
                        "errors",
                        "pagination",
                        "queues",
                        "idempotency_key",
                    ]
                },
                tasks=[
                    {
                        "id": "T-REST",
                        "type": "theory",
                        "title": "REST и надёжные API",
                        "max_points": 10,
                        "questions": [
                            "В чём разница между PUT и PATCH? Какие из этих методов считаются идемпотентными? Приведите пример тела запроса для каждого случая.",
                            "Как правильно выбирать коды ошибок 400, 401, 403, 404, 409 и 422? Что обычно должно быть в теле ответа с ошибкой?",
                            "Как можно реализовать пагинацию через offset/limit и через cursor? В чём плюсы и минусы каждого подхода, и когда какой вариант уместнее?",
                            "Что такое ключ идемпотентности и зачем он нужен для POST-запросов, например при создании заказа?",
                        ],
                        "hints_allowed": False,
                    },
                    {
                        "id": "C-BE-QUEUE",
                        "type": "coding",
                        "language": "python",
                        "title": "Task Queue with ack/nack",
                        "max_points": 10,
                        "tests_id": "queue_basic",
                        "entrypoint": "TaskQueue",
                        "entrypoint_kind": "class",
                        "statement_md": (
                            """
Реализуйте in-memory очередь задач `TaskQueue` с поддержкой `enqueue`, `dequeue`, `ack` и `nack`.

Поведение:  
- `enqueue(item)` кладёт задачу в конец очереди;  
- `dequeue()`:  
  - если очередь пуста, возвращает `None`;  
  - иначе возвращает пару `(token, item)`;  
- после `dequeue()` задача считается выданной в обработку;  
- `ack(token)` подтверждает успешную обработку и убирает задачу из inflight;  
- `nack(token)` возвращает задачу обратно в конец очереди;  
- `ack(token)` и `nack(token)` должны бросать `KeyError` для неизвестного токена.

Пример 1:  
Вход: `enqueue("A")`, `enqueue("B")`, затем два вызова `dequeue()`  
Выход:  
сначала `(token1, "A")`, затем `(token2, "B")`

Пример 2:  
Вход: `enqueue("A")`, `token, item = dequeue()`, затем `nack(token)` и ещё один `dequeue()`  
Выход: снова возвращается задача `"A"`

Пример 3:  
Вход: `ack("unknown")`  
Выход: `KeyError`

Ограничения:  
- каждый вызов `dequeue()` должен выдавать уникальный токен;  
- основные операции должны работать за `O(1)` в среднем.
"""
                        ).strip(),
                        "starter_code": (
                            """
from __future__ import annotations

import secrets
from typing import Any, Optional, Tuple


class TaskQueue:
    def __init__(self):
        raise NotImplementedError

    def enqueue(self, item: Any):
        raise NotImplementedError

    def dequeue(self):
        raise NotImplementedError

    def ack(self, token: str):
        raise NotImplementedError

    def nack(self, token: str):
        raise NotImplementedError
"""
                        ).strip(),
                    },
                ],
            ),
            models.Scenario(
                role_id=be.id,
                name="Backend — Resilience",
                slug="be-resilience",
                description="Ретраи, троттлинг, circuit breaker — теория + код",
                difficulty="senior",
                config={
                    "releated_topics": [
                        "retries",
                        "circuit_breaker",
                        "rate_limiting",
                        "timeouts",
                        "bulkheads",
                    ]
                },
                tasks=[
                    {
                        "id": "T-RESILIENCE",
                        "type": "theory",
                        "title": "Resilience patterns",
                        "max_points": 10,
                        "questions": [
                            "Какие подходы к выполнению повторных попыток вы знаете? В каких случаях повторные попытки полезны, а в каких могут ухудшить работу системы?",
                            "Что такое circuit breaker? Опишите его состояния, условия переходов и метрики, на которые вы бы опирались.",
                            "Какие способы ограничения частоты запросов вы знаете? Сравните их по принципу работы и по ситуациям, в которых их разумно применять.",
                            "Как вы бы настраивали тайм-ауты, изоляцию ресурсов и передачу ограничений по времени между сервисами в микросервисной архитектуре?",
                        ],
                        "hints_allowed": False,
                    },
                    {
                        "id": "C-RATE",
                        "type": "coding",
                        "language": "python",
                        "title": "Token Bucket Rate Limiter",
                        "description_for_candidate": "Реализуйте токен-бакет для rate limiting.",
                        "tests_id": "rate_limiter",
                        "entrypoint": "TokenBucket",
                        "entrypoint_kind": "class",
                        "max_points": 10,
                        "statement_md": (
                            """
Реализуйте rate limiter по алгоритму `Token Bucket`.

Класс `TokenBucket(capacity, refill_rate_per_sec)` должен:  
- хранить максимум `capacity` токенов;  
- пополнять токены со временем;  
- методом `allow(tokens=1)` решать, можно ли прямо сейчас списать указанное количество токенов.

Метод `allow(tokens)` должен:  
- вернуть `True`, если токенов хватает и списание разрешено;  
- вернуть `False`, если токенов не хватает или запрос больше `capacity`.

Пример 1:  
Вход: `bucket = TokenBucket(5, 0.0)`, затем `allow(3)`  
Выход: `True`

Пример 2:  
Вход: `bucket = TokenBucket(5, 0.0)`, затем `allow(3)` и сразу ещё раз `allow(3)`  
Выход: `False` для второго вызова

Пример 3:  
Вход: `bucket = TokenBucket(5, 1.0)`, затем списать токены, подождать время для пополнения и снова вызвать `allow(...)`  
Выход: запрос может снова пройти после refill

Ограничения:  
- используйте `time.monotonic()` для расчёта прошедшего времени;  
- количество токенов не должно превышать `capacity`;  
- вызов `allow()` должен работать за `O(1)`.
"""
                        ).strip(),
                        "starter_code": (
                            """
from __future__ import annotations

import time


class TokenBucket:
    def __init__(self, capacity: int, refill_rate_per_sec: float):
        self.capacity = capacity
        self.refill_rate_per_sec = refill_rate_per_sec

    def allow(self, tokens: int = 1):
        raise NotImplementedError
"""
                        ).strip(),
                    },
                ],
            ),
            models.Scenario(
                role_id=de.id,
                name="DE — Pipelines",
                slug="de-pipelines",
                description="Инкрементальные пайплайны, буферизация, SLA — теория + код + SQL",
                difficulty="middle",
                config={
                    "releated_topics": [
                        "incremental_processing",
                        "watermarks",
                        "delivery_guarantees",
                        "data_sla",
                    ]
                },
                tasks=[
                    {
                        "id": "T-DE-PIPELINES",
                        "type": "theory",
                        "title": "Data Engineering — инкременты и надёжность",
                        "max_points": 10,
                        "questions": [
                            "Что такое watermark и checkpoint в потоковой обработке данных? Как они помогают работать с опоздавшими событиями?",
                            "Чем отличаются гарантии exactly-once, at-least-once и at-most-once? Где и почему обычно используется каждый из этих вариантов?",
                            "Как бы вы спроектировали инкрементальную загрузку в таблицу фактов: через CDC, только добавление новых записей или слияние изменений? Когда какой подход уместен?",
                            "Чем отличаются SLA, SLO и SLI для пайплайна данных? Какие показатели вы бы отслеживали и как реагировали бы на их нарушение?",
                        ],
                        "hints_allowed": False,
                    },
                    {
                        "id": "C-WATERMARK",
                        "type": "coding",
                        "language": "python",
                        "title": "Incremental Aggregation with Watermark",
                        "max_points": 10,
                        "tests_id": "watermark_agg_basic",
                        "entrypoint": "DailyDistinctAggregator",
                        "entrypoint_kind": "class",
                        "statement_md": (
                            """
Реализуйте класс `DailyDistinctAggregator`, который принимает события и считает количество уникальных пользователей по дням и регионам.

Метод `add(event)` принимает словарь:  
- `user_id: int`  
- `ts: int` — UNIX timestamp в UTC  
- `region: str`

Метод `advance_watermark(wm_ts)` должен:  
- закрывать дни, которые полностью завершились к моменту `wm_ts`;  
- возвращать только финальные результаты для уже закрытых дней;  
- игнорировать поздние события за уже закрытые дни.

Формат результата:  
- список записей вида `["YYYY-MM-DD", region, distinct_count]`;  
- список должен быть отсортирован по `(day, region)`.

Пример 1:  
Вход:  
`{"user_id": 1, "ts": 1740787200, "region": "EU"}`  
`{"user_id": 2, "ts": 1740787201, "region": "EU"}`  
затем `advance_watermark(1740873600)`  
Выход: `[["2025-03-01", "EU", 2]]`

Пример 2:  
Вход: два события одного и того же пользователя в один день и одном регионе  
Выход: пользователь учитывается один раз

Пример 3:  
Вход: событие за день, который уже был закрыт watermark  
Выход: такое событие игнорируется

Ограничения:  
- день определяется в UTC по полю `ts`;  
- `add` и `advance_watermark` должны быть эффективными по времени и памяти.
"""
                        ).strip(),
                        "starter_code": (
                            """
from __future__ import annotations

from typing import Dict, List, Tuple


class DailyDistinctAggregator:
    def __init__(self):
        raise NotImplementedError

    def add(self, event: Dict):
        raise NotImplementedError

    def advance_watermark(self, wm_ts: int):
        raise NotImplementedError
"""
                        ).strip(),
                    },
                    {
                        "id": "SQL-de-agg",
                        "type": "sql",
                        "title": "DAU по регионам",
                        "description_for_candidate": (
                            "Посчитайте дневную активную аудиторию по регионам из таблицы events. "
                            "Активный пользователь — это уникальный user_id, у которого было хотя бы одно событие в этот день. "
                            "Верните event_date, region, dau."
                        ),
                        "sql_scenario_id": "events_basic",
                        "max_points": 10,
                    },
                ],
            ),
            models.Scenario(
                role_id=de.id,
                name="DE — Warehousing",
                slug="de-warehousing",
                description="Моделирование данных, SCD, оркестрация — теория + код + SQL",
                difficulty="senior",
                config={
                    "releated_topics": [
                        "dwh_modeling",
                        "scd",
                        "partitioning",
                        "clustering",
                        "data_quality",
                    ]
                },
                tasks=[
                    {
                        "id": "T-DE-WH",
                        "type": "theory",
                        "title": "Хранилище данных и моделирование",
                        "max_points": 10,
                        "questions": [
                            "В чём различие между схемами star schema и snowflake? Как это влияет на производительность и сопровождение хранилища?",
                            "Какие варианты хранения истории изменений в измерениях вы знаете? Что сохраняется в каждом случае и когда такой подход уместен?",
                            "Как выбирать между партиционированием и кластеризацией, например в BigQuery или Snowflake? Как это влияет на стоимость и производительность запросов?",
                            "Какие проверки качества данных вы бы заложили в пайплайн загрузки данных в хранилище? Что именно вы бы проверяли на этапе загрузки источника, на этапе преобразований и перед использованием данных в отчётах?",
                        ],
                        "hints_allowed": False,
                    },
                    {
                        "id": "C-SCD2",
                        "type": "coding",
                        "language": "python",
                        "title": "SCD Type 2 Merge",
                        "max_points": 10,
                        "tests_id": "scd2_merge_basic",
                        "entrypoint": "scd2_merge",
                        "entrypoint_kind": "function",
                        "statement_md": (
                            """
Реализуйте функцию `scd2_merge(current, updates)`, которая применяет обновления к измерению клиентов по правилам SCD Type 2.

Формат `current`:  
список словарей вида:  
- `customer_id: int`  
- `attrs: dict`  
- `valid_from: str`  
- `valid_to: str | None`

Формат `updates`:  
список словарей вида:  
- `customer_id: int`  
- `attrs: dict`  
- `as_of: str`

Правила:
- если для клиента нет активной записи, нужно создать новую активную запись;  
- если атрибуты не изменились, новая версия не создаётся;  
- если атрибуты изменились, текущая активная запись закрывается датой `as_of`, а новая открывается с `valid_from = as_of`.

Пример 1:  
Вход:  
`current = [{"customer_id": 1, "attrs": {"city": "A"}, "valid_from": "2026-01-01", "valid_to": None}]`  
`updates = [{"customer_id": 1, "attrs": {"city": "B"}, "as_of": "2026-02-01"}]`  
Выход:  
две записи для клиента `1`: старая закрыта датой `2026-02-01`, новая открыта с `2026-02-01`

Пример 2:  
Вход: активная запись и обновление с теми же `attrs`  
Выход: список записей не меняется

Ограничения:  
- после обработки у каждого клиента должна остаться не более чем одна активная запись с `valid_to = None`;  
- результат должен быть отсортирован по `(customer_id, valid_from)`.
"""
                        ).strip(),
                        "starter_code": (
                            """
from __future__ import annotations

from typing import Dict, List


def scd2_merge(current: List[Dict], updates: List[Dict]):
    raise NotImplementedError
"""
                        ).strip(),
                    },
                    {
                        "id": "SQL-scd",
                        "type": "sql",
                        "title": "SCD Type 2 обновление клиентов",
                        "description_for_candidate": (
                            "В таблице dim_customers хранится история клиентов по правилам SCD Type 2. "
                            "Нужно обработать изменения из customer_updates: "
                            "если атрибуты клиента изменились, закрыть текущую запись и вставить новую; "
                            "если изменений нет — ничего не делать."
                        ),
                        "sql_scenario_id": "scd_customers",
                        "max_points": 10,
                    },
                ],
            ),
        ]

        for sc in scenarios_payload:
            existing = db.query(models.Scenario).filter_by(slug=sc.slug).one_or_none()
            if existing is None:
                db.add(sc)
                continue

            existing.role_id = sc.role_id
            existing.name = sc.name
            existing.description = sc.description
            existing.difficulty = sc.difficulty
            existing.tasks = sc.tasks
            existing.rag_corpus_id = sc.rag_corpus_id
            existing.sql_scenario_id = sc.sql_scenario_id
            existing.config = sc.config

        db.commit()
    finally:
        db.close()


@app.on_event("startup")
def on_startup() -> None:
    seed_defaults()
    sync_tasks_from_scenarios()

@app.get("/lm/ping")
def lm_ping() -> dict:
    """Check connectivity to LM Studio."""
    try:
        resp = lm_client.ping()
        return {"status": "ok", "model": resp.get("model", settings.lm_model)}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"LM Studio not reachable: {exc}") from exc

