from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import models
from .config import settings
from .database import Base, SessionLocal, engine
from .routers import rag, roles, scenarios, sessions, sql_scenarios
from .services import lm_client

# Create DB tables for a simple demo; for production prefer migrations
Base.metadata.create_all(bind=engine)

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
app.include_router(rag.router)
app.include_router(sql_scenarios.router)
app.include_router(sessions.router)


@app.get("/health")
def health():
    return {"status": "ok"}

frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="frontend")
else:
    @app.get("/")
    def root_fallback():
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

        scenarios_payload = [
            models.Scenario(
                role_id=ds.id,
                name="DS — Junior ML",
                slug="ds-junior-ml",
                description="Регрессия, классификация, SQL основы",
                difficulty="junior",
                tasks=[
                    {
                        "id": "T1",
                        "type": "theory",
                        "title": "Основы регрессии",
                        "description": "Различия L1/L2: sparsity, устойчивость, геометрия штрафа.",
                        "max_points": 5,
                        "hints_allowed": True,
                        "evaluation_criteria": {
                            "full_answer": "Геометрия, sparsity, устойчивость",
                            "partial_answer": "Общее различие без деталей",
                        },
                        "related_topics": ["regularization", "linear_models"],
                    },
                    {
                        "id": "C1",
                        "type": "coding",
                        "language": "python",
                        "title": "Логистическая регрессия",
                        "description_for_candidate": "Реализуйте логистическую регрессию без sklearn.",
                        "max_points": 10,
                        "tests_id": "logreg_basic",

                        "entrypoint": "LogisticRegression",
                        "entrypoint_kind": "class",
                        "interface": {
                            "init_args": ["lr", "n_iters"],
                            "methods": [
                            {"name": "fit", "args": ["X", "y"], "returns": "self"},
                            {"name": "predict_proba", "args": ["X"], "returns": "list[float]"},
                            {"name": "predict", "args": ["X"], "returns": "list[int]"}
                            ]
                        },
                    },
                    {
                        "id": "SQL1",
                        "type": "sql",
                        "title": "Агрегация заказов",
                        "description_for_candidate": "По таблицам orders и customers посчитайте сумму заказов по городам.",
                        "max_points": 8,
                        "sql_scenario_id": "ecommerce_basic",
                        "related_topics": ["joins", "aggregation"],
                    },
                ],
            ),
            models.Scenario(
                role_id=ds.id,
                name="DS — Product ML",
                slug="ds-product-ml",
                description="A/B, метрики продукта, рекомендации",
                difficulty="middle",
                tasks=[
                    {
                        "id": "T-metrics",
                        "type": "theory",
                        "title": "Метрики A/B",
                        "max_points": 5,
                        "hints_allowed": True,
                        "related_topics": ["experimentation", "metrics"],
                    },
                    {
                        "id": "SQL-ab",
                        "type": "sql",
                        "title": "Конверсия по когорте",
                        "description_for_candidate": "Напишите запрос конверсии по дню регистрации.",
                        "sql_scenario_id": "ab_product",
                        "max_points": 8,
                        "related_topics": ["joins", "aggregation"],
                    },
                ],
            ),
            models.Scenario(
                role_id=be.id,
                name="Backend — Junior — URL Shortener",
                slug="be-junior-shortener",
                description="Отличие между GET и Post запросами, значение статусов, определение + пример индепендности, реализация класса UrlShortener",
                difficulty="junior",
                tasks=[
                    {
                        "id": "T-JUNIOR-BE",
                        "type": "theory",
                        "title": "Backend Junior — базовые понятия HTTP и API",
                        "max_points": 5,
                        "questions": [
                            "Чем отличаются GET и POST? Приведи пример для REST API.",
                            "Что означают статусы: 200, 201, 400, 404, 409, 500?",
                            "Что такое идемпотентность? Приведи пример идемпотентного запроса.",
                        ],
                        "hints_allowed": True,
                    },
                    {
                        "id": "C-SHORTENER",
                        "type": "coding",
                        "language": "python",
                        "title": "Design URL Shortener",
                        "max_points": 10,
                        "tests_id": "shortener_basic",

                        # LeetCode-like statement (markdown)
                        "statement_md": """
            Задача:
            Реализуйте упрощённый сокращатель ссылок.

            Нужно реализовать класс "UrlShortener", который поддерживает две операции:

            1. "encode(url: str) -> str" — возвращает короткий код для переданного URL  
            2. "decode(code: str) -> str" — по коду возвращает исходный URL

            Считайте, что сервис находится в памяти (без БД) в рамках одного запуска.

            Требования:
            1) "encode()" должен возвращать один и тот же код для одного и того же URL (идемпотентность).  
            2) "decode()" для неизвестного "code" должен выбрасывать "KeyError".  
            3) Код должен состоять только из символов "[A-Za-z0-9]" и иметь длину 6.  
            4) Ожидаемая сложность:
            - "encode": амортизированно O(1)
            - "decode": O(1)

            Пример:
            Ввод / действия:
            - "s.encode("https://example.com")" → "aB3xK1"
            - "s.encode("https://example.com")" → "aB3xK1" (тот же код)
            - "s.decode("aB3xK1")" → "https://example.com"
            """.strip(),

                        # Starter code shown in editor
                        "starter_code": '''
            from __future__ import annotations

            import secrets
            import string

            ALPHABET = string.ascii_letters + string.digits
            CODE_LEN = 6

            class UrlShortener:
                def __init__(self) -> None:
                    
                    self._url_to_code: dict[str, str] = {}
                    self._code_to_url: dict[str, str] = {}

                def _gen_code(self) -> str:

                    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))

                def encode(self, url: str) -> str:

                    raise NotImplementedError

                def decode(self, code: str) -> str:
                    
                    raise NotImplementedError
            '''.strip(),
                    }
                ],
            ),
            models.Scenario(
                role_id=be.id,
                name="Backend — REST",
                slug="be-rest",
                description="API дизайн, идемпотентность, очереди",
                difficulty="middle",
                tasks=[
                    {"id": "T-REST", "type": "theory", "title": "PUT vs PATCH идемпотентность", "max_points": 5},
                    {
                    "id": "C-BE",
                    "type": "coding",
                    "language": "python",
                    "title": "Очередь задач",
                    "description_for_candidate": "Реализуйте очередь с ack/nack.",
                    "tests_id": "queue_basic",
                    "max_points": 8,

                    "entrypoint": "TaskQueue",
                    "entrypoint_kind": "class",
                    "interface": {
                        "init_args": [],
                        "methods": [
                        {"name": "enqueue", "args": ["item"], "returns": "any"},
                        {"name": "dequeue", "args": [], "returns": "any"},
                        {"name": "ack", "args": ["token"], "returns": "null"},
                        {"name": "nack", "args": ["token"], "returns": "null"}
                        ]
                    },
                    }
                ],
            ),
            models.Scenario(
                role_id=be.id,
                name="Backend — Resilience",
                slug="be-resilience",
                description="Ретраи, троттлинг, circuit breaker",
                difficulty="senior",
                tasks=[
                    {
                    "id": "C-rate",
                    "type": "coding",
                    "language": "python",
                    "title": "Rate limiter",
                    "description_for_candidate": "Сделайте токен-бакет.",
                    "tests_id": "rate_limiter",
                    "max_points": 9,

                    "entrypoint": "TokenBucket",
                    "entrypoint_kind": "class",
                    "interface": {
                        "init_args": ["capacity", "refill_rate_per_sec"],
                        "methods": [
                        {"name": "allow", "args": ["tokens"], "returns": "bool"}
                        ]
                    },
                    }
                ],
            ),
            models.Scenario(
                role_id=de.id,
                name="DE — Pipelines",
                slug="de-pipelines",
                description="Инкрементальные пайплайны, буферизация, SLA",
                difficulty="middle",
                tasks=[
                    {"id": "T-de-incr", "type": "theory", "title": "Инкрементальные загрузки", "max_points": 5, "hints_allowed": True},
                    {
                        "id": "SQL-de-agg",
                        "type": "sql",
                        "title": "Агрегация событий",
                        "description_for_candidate": "Посчитайте DAU по регионам из таблицы events.",
                        "sql_scenario_id": "events_basic",
                        "max_points": 8,
                    },
                ],
            ),
            models.Scenario(
                role_id=de.id,
                name="DE — Warehousing",
                slug="de-warehousing",
                description="Моделирование данных, SCD, оркестрация",
                difficulty="senior",
                tasks=[
                    {"id": "T-scd", "type": "theory", "title": "SCD типы", "max_points": 6},
                    {
                        "id": "SQL-scd",
                        "type": "sql",
                        "title": "SCD Type 2 обновление",
                        "description_for_candidate": "Напишите SQL, который добавляет новую версию записи клиента.",
                        "sql_scenario_id": "scd_customers",
                        "max_points": 9,
                    },
                ],
            ),
        ]
        # Upsert scenarios by slug: insert new, update existing
        for sc in scenarios_payload:
            existing = db.query(models.Scenario).filter_by(slug=sc.slug).one_or_none()
            if existing is None:
                db.add(sc)
            else:
                # update mutable fields
                existing.role_id = sc.role_id
                existing.name = sc.name
                existing.description = sc.description
                existing.difficulty = sc.difficulty
                existing.tasks = sc.tasks
                existing.rag_corpus_id = sc.rag_corpus_id
                existing.sql_scenario_id = sc.sql_scenario_id

        db.commit()
    finally:
        db.close()


@app.on_event("startup")
def on_startup():
    seed_defaults()


@app.get("/lm/ping")
def lm_ping():
    """Check connectivity to LM Studio."""
    try:
        resp = lm_client.ping()
        return {"status": "ok", "model": resp.get("model", settings.lm_model)}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"LM Studio not reachable: {exc}") from exc
