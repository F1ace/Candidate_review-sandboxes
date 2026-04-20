from __future__ import annotations

from app.routers.sessions_api.theory_contracts import (
    build_theory_final_message_contract,
    finalize_theory_final_message,
    sanitize_theory_final_message,
    theory_final_message_has_wrong_score,
    theory_final_message_too_generic,
)


def test_theory_contract_builds_from_points_and_comments() -> None:
    task = {
        "id": "T-DOCS",
        "type": "theory",
        "max_points": 10,
        "questions": ["Что такое идемпотентность и как она связана с POST?"],
    }
    score_result = {
        "ok": True,
        "task_id": "T-DOCS",
        "points": 4.0,
        "comment": "Общий итог по блоку без числовой оценки текстом.",
        "comments": [
            "Кандидат верно объяснил базовую идею идемпотентности, но не раскрыл границы применимости POST."
        ],
    }

    contract = build_theory_final_message_contract(task, score_result)

    assert contract is not None
    assert contract.points == 4
    assert contract.max_points == 10
    assert contract.summary_comment == "Общий итог по блоку без числовой оценки текстом."
    assert len(contract.question_comments) == 1
    assert contract.question_comments[0].question_index == 1
    assert contract.question_comments[0].comment.startswith("Кандидат верно объяснил")


def test_theory_contract_validators_detect_wrong_score_and_generic_text() -> None:
    task = {
        "id": "T-DOCS",
        "type": "theory",
        "max_points": 10,
        "questions": ["Что такое идемпотентность и как она связана с POST?"],
    }
    score_result = {
        "ok": True,
        "task_id": "T-DOCS",
        "points": 4.0,
        "comment": "Общий итог по блоку без числовой оценки текстом.",
        "comments": [
            "Кандидат верно объяснил базовую идею идемпотентности, но не раскрыл границы применимости POST."
        ],
    }
    contract = build_theory_final_message_contract(task, score_result)
    assert contract is not None

    bad_text = "Теоретический блок завершён. Итоговая оценка: 7/10. Переходим к практике."
    assert theory_final_message_has_wrong_score(bad_text, contract) is True
    assert theory_final_message_too_generic(bad_text, contract) is True

    good_text = (
        "Теоретический блок завершён. По ответу видно базовое понимание темы.\n\n"
        "- **Идемпотентность и POST:** Кандидат верно объяснил базовую идею идемпотентности, но не раскрыл границы применимости POST.\n\n"
        "**Сильные стороны:**\n"
        "- Хорошо понимает основу темы.\n\n"
        "**Зоны роста:**\n"
        "- Нужна более точная детализация по HTTP-семантике.\n\n"
        "**Итоговая оценка по теоретическому блоку:** 4/10."
    )
    assert theory_final_message_has_wrong_score(good_text, contract) is False
    assert theory_final_message_too_generic(good_text, contract) is False


def test_theory_contract_sanitizes_comment_table_into_clean_block() -> None:
    task = {
        "id": "T-DOCS",
        "type": "theory",
        "max_points": 10,
        "questions": [
            "Что такое идемпотентность и как она связана с POST?",
            "Что такое переобучение и как с ним бороться?",
        ],
    }
    score_result = {
        "ok": True,
        "task_id": "T-DOCS",
        "points": 2.0,
        "comment": "Общий итог по блоку без числовой оценки текстом.",
        "comments": [
            "Кандидат перепутал смысл идемпотентности и неверно описал поведение POST.",
            "Кандидат неточно объяснил переобучение и предложил слабые способы борьбы.",
        ],
    }
    contract = build_theory_final_message_contract(task, score_result)
    assert contract is not None

    dirty_text = (
        "Блок завершён\n\n"
        "Комментарии по каждому ответу\n\n"
        "| Вопрос | Что было правильно | Что нужно улучшить |\n"
        "|--------|--------------------|--------------------|\n"
        "| 1 | ... | ... |\n"
        "| 2 | ... | ... |\n\n"
        "**Сильные стороны:**\n"
        "- Пытается рассуждать.\n\n"
        "**Зоны роста:**\n"
        "- Нужна лучшая точность.\n\n"
        "**Итоговая оценка по теоретическому блоку:** 2/10."
    )

    cleaned = sanitize_theory_final_message(dirty_text, contract)

    assert "Комментарии по каждому ответу" in cleaned
    assert "Кандидат перепутал смысл идемпотентности" in cleaned
    assert "Кандидат неточно объяснил переобучение" in cleaned
    assert "| Вопрос |" not in cleaned
    assert "|--------|" not in cleaned


def test_theory_contract_removes_duplicate_comment_sections() -> None:
    task = {
        "id": "T-DOCS",
        "type": "theory",
        "max_points": 10,
        "questions": [
            "Что такое идемпотентность и как она связана с POST?",
            "Что такое переобучение и как с ним бороться?",
        ],
    }
    score_result = {
        "ok": True,
        "task_id": "T-DOCS",
        "points": 2.0,
        "comment": "Общий итог по блоку без числовой оценки текстом.",
        "comments": [
            "Кандидат перепутал смысл идемпотентности и неверно описал поведение POST.",
            "Кандидат неточно объяснил переобучение и предложил слабые способы борьбы.",
        ],
    }
    contract = build_theory_final_message_contract(task, score_result)
    assert contract is not None

    dirty_text = (
        "Теоретический блок завершён.\n\n"
        "**Комментарии по каждому ответу**\n\n"
        "| Вопрос | Оценка | Ключевые замечания |\n"
        "|--------|--------|--------------------|\n"
        "| 1 | 0/2 | старый дублирующий текст |\n"
        "| 2 | 0/2 | ещё один дублирующий текст |\n\n"
        "**Сильные стороны:**\n"
        "- Пытается рассуждать.\n\n"
        "**Зоны роста:**\n"
        "- Нужна лучшая точность.\n\n"
        "**Комментарии по каждому ответу**\n\n"
        "- **1. Что такое идемпотентность:** черновик.\n"
        "- **2. Что такое переобучение:** черновик.\n\n"
        "**Итоговая оценка по теоретическому блоку:** 2/10."
    )

    cleaned = sanitize_theory_final_message(dirty_text, contract)

    assert cleaned.count("Комментарии по каждому ответу") == 1
    assert "Кандидат перепутал смысл идемпотентности" in cleaned
    assert "Кандидат неточно объяснил переобучение" in cleaned
    assert cleaned.index("Комментарии по каждому ответу") < cleaned.index("Сильные стороны")


def test_theory_contract_removes_duplicate_comment_sections_with_alternative_headers() -> None:
    task = {
        "id": "T-ML",
        "type": "theory",
        "max_points": 10,
        "questions": [
            "Чем отличаются задачи регрессии и классификации? Приведите по одному примеру для каждой.",
            "Что такое переобучение и какие базовые способы борьбы с ним вы знаете?",
            "В чём различие между L1- и L2-регуляризацией и как это влияет на веса модели?",
            "Как работает логистическая регрессия и почему её результат удобно интерпретировать как вероятность?",
        ],
    }
    score_result = {
        "ok": True,
        "task_id": "T-ML",
        "points": 2.0,
        "comment": "Итог по блоку.",
        "comments": [
            "Ответ содержит путаницу: регрессия предсказывает непрерывную переменную, классификация — категориальную. Пример для регрессии был неверным.",
            "Переобучение описано неверно: это не недостаточная, а избыточная подгонка под обучение. Способы борьбы названы неполно.",
            "L1 и L2 перепутаны: L1 работает через абсолютные значения весов, L2 — через квадраты, влияние на веса объяснено неверно.",
            "Логистическая регрессия описана без сигмоиды, поэтому интерпретация результата как вероятности объяснена некорректно.",
        ],
    }
    contract = build_theory_final_message_contract(task, score_result)
    assert contract is not None

    dirty_text = (
        "Вопросы теоретического блока завершены.\n\n"
        "Комментарий к каждому ответу кандидата\n\n"
        "- Вопрос 1/4: старый мусорный вариант.\n"
        "- Вопрос 2/4: старый мусорный вариант.\n\n"
        "Сильные стороны кандидата\n\n"
        "- Есть попытка рассуждать.\n\n"
        "Комментарии по каждому ответу\n\n"
        "- **1. Черновик:** дубль.\n\n"
        "Зоны роста\n\n"
        "- Нужна точность.\n\n"
        "Оценка\n\n"
        "Кандидат набрал 2 из 10 возможных баллов."
    )

    cleaned = sanitize_theory_final_message(dirty_text, contract)

    assert cleaned.count("Комментарий к каждому ответу кандидата") == 0
    assert cleaned.count("Комментарии по каждому ответу") == 1
    assert "старый мусорный вариант" not in cleaned
    assert "Ответ содержит путаницу" in cleaned
    assert "Логистическая регрессия описана без сигмоиды" in cleaned
    assert cleaned.index("Комментарии по каждому ответу") < cleaned.index("Сильные стороны кандидата")
    assert cleaned.index("Сильные стороны кандидата") < cleaned.index("Зоны роста")


def test_theory_contract_removes_prefix_table_garbage_before_comments() -> None:
    task = {
        "id": "T-ML",
        "type": "theory",
        "max_points": 10,
        "questions": [
            "Чем отличаются задачи регрессии и классификации? Приведите по одному примеру для каждой.",
            "Что такое переобучение и какие базовые способы борьбы с ним вы знаете?",
            "В чём различие между L1- и L2-регуляризацией и как это влияет на веса модели?",
            "Как работает логистическая регрессия и почему её результат удобно интерпретировать как вероятность?",
        ],
    }
    score_result = {
        "ok": True,
        "task_id": "T-ML",
        "points": 8.0,
        "comment": "Итог по блоку.",
        "comments": [
            "Кандидат правильно различил задачи регрессии и классификации, привёл понятные примеры для каждой категории.",
            "Ответ о переобучении был полным: перечислены основные причины и методы борьбы, включая увеличение данных, регуляризацию и раннюю остановку.",
            "Объяснение L1- и L2-регуляризаций было точным: кандидат отметил разницу в штрафах и влияние на веса модели.",
            "Ответ по логистической регрессии охватил формулу линейного сочетания признаков, сигмоидную функцию и интерпретацию результата как вероятности.",
        ],
    }
    contract = build_theory_final_message_contract(task, score_result)
    assert contract is not None

    dirty_text = (
        "Блок завершён\n\n"
        "Кандидат продемонстрировал хорошее понимание фундаментальных концепций машинного обучения:\n\n"
        "| Вопрос | Оценка ||--------|---------|| 1. Регрессия vs классификация | ✅ || 2. Переобучение и способы борьбы | ✅ || 3. L1- vs L2-регуляризация | ✅ || 4. Логистическая регрессия | ✅ |\n\n"
        "Комментарии по каждому ответу\n\n"
        "- **1. Черновик:** старый дубль.\n\n"
        "Сильные стороны:\n\n"
        "- Есть хорошая база.\n\n"
        "Зоны роста:\n\n"
        "- Добавить больше практики.\n\n"
        "Итоговая оценка: 8/10."
    )

    cleaned = sanitize_theory_final_message(dirty_text, contract)

    assert "Кандидат продемонстрировал хорошее понимание фундаментальных концепций машинного обучения:" not in cleaned
    assert "| Вопрос |" not in cleaned
    assert "✅" not in cleaned
    assert "Блок завершён" in cleaned
    assert cleaned.count("Комментарии по каждому ответу") == 1
    assert "Кандидат правильно различил задачи регрессии и классификации" in cleaned
    assert cleaned.index("Комментарии по каждому ответу") < cleaned.index("Сильные стороны")


def test_finalize_theory_message_removes_system_noise_and_restores_required_structure() -> None:
    task = {
        "id": "T-DOCS",
        "type": "theory",
        "max_points": 10,
        "questions": ["Что такое идемпотентность и как она связана с POST?"],
    }
    score_result = {
        "ok": True,
        "task_id": "T-DOCS",
        "points": 4.0,
        "comment": (
            "Кандидат понимает базовую идею идемпотентности и корректно связывает её с POST, "
            "но ответу не хватило более точного разведения HTTP-семантики и поведения конкретного API."
        ),
        "comments": [
            (
                "Кандидат верно описал базовую идею идемпотентности и отметил, что POST обычно не считается "
                "идемпотентным, но не раскрыл границу между свойством метода и конкретной реализацией API."
            )
        ],
    }
    contract = build_theory_final_message_contract(task, score_result)
    assert contract is not None

    dirty_text = (
        "Финальный score_task по теоретическому блоку уже успешно выполнен.\n"
        "Теперь нужно написать итоговое сообщение обычным текстом.\n"
        "<THEORY_FINAL_MESSAGE_CONTRACT>\n"
        '{"task_id":"T-DOCS","points":4,"max_points":10}\n'
        "</THEORY_FINAL_MESSAGE_CONTRACT>\n\n"
        "**Комментарии по каждому ответу**\n\n"
        "| Вопрос | Оценка | Комментарий |\n"
        "|--------|--------|-------------|\n"
        "| 1 | 9/10 | старый мусор |\n"
    )

    cleaned = finalize_theory_final_message(dirty_text, contract)

    assert cleaned.startswith("Теоретический блок завершён.")
    assert "Комментарии по каждому ответу" in cleaned
    assert "Сильные стороны" in cleaned
    assert "Зоны роста" in cleaned
    assert "**Итоговая оценка по теоретическому блоку:** 4/10." in cleaned
    assert "Кандидат верно описал базовую идею идемпотентности" in cleaned
    assert "Финальный score_task" not in cleaned
    assert "THEORY_FINAL_MESSAGE_CONTRACT" not in cleaned
    assert "| Вопрос |" not in cleaned


def test_finalize_theory_message_deduplicates_existing_score_lines() -> None:
    task = {
        "id": "T-ML",
        "type": "theory",
        "max_points": 10,
        "questions": [
            "Чем отличаются задачи регрессии и классификации?",
        ],
    }
    score_result = {
        "ok": True,
        "task_id": "T-ML",
        "points": 8.0,
        "comment": "Кандидат дал сильный итог по блоку.",
        "comments": [
            "Кандидат чётко различил задачи регрессии и классификации и привёл корректные примеры."
        ],
    }
    contract = build_theory_final_message_contract(task, score_result)
    assert contract is not None

    dirty_text = (
        "Теоретический блок завершён.\n\n"
        "**Комментарии по каждому ответу**\n\n"
        "- **1. Чем отличаются задачи регрессии и классификации?:** Кандидат чётко различил задачи регрессии и классификации и привёл корректные примеры.\n\n"
        "**Сильные стороны кандидата**\n\n"
        "- Хорошо ориентируется в базовых постановках задач.\n\n"
        "**Зоны роста**\n\n"
        "- Можно глубже раскрывать практические ограничения моделей.\n\n"
        "Оценка 8/10\n\n"
        "**Итоговая оценка по теоретическому блоку:** 8/10.\n\n"
        "**Итоговая оценка по теоретическому блоку:** 8/10."
    )

    cleaned = finalize_theory_final_message(dirty_text, contract)

    assert cleaned.count("Оценка 8/10") == 0
    assert cleaned.count("**Итоговая оценка по теоретическому блоку:** 8/10.") == 1


def test_finalize_theory_message_removes_verbose_score_line_variant() -> None:
    task = {
        "id": "T-ML",
        "type": "theory",
        "max_points": 10,
        "questions": [
            "Как работает логистическая регрессия?",
        ],
    }
    score_result = {
        "ok": True,
        "task_id": "T-ML",
        "points": 3.0,
        "comment": "Кандидат частично разобрался в теме, но допустил несколько заметных ошибок.",
        "comments": [
            "Ответ содержит несколько ошибок в описании сигмоидальной функции и интерпретации вероятности."
        ],
    }
    contract = build_theory_final_message_contract(task, score_result)
    assert contract is not None

    dirty_text = (
        "Теоретический блок завершён.\n\n"
        "**Комментарии по каждому ответу**\n\n"
        "- **1. Как работает логистическая регрессия?:** Ответ содержит несколько ошибок в описании сигмоидальной функции и интерпретации вероятности.\n\n"
        "**Сильные стороны**\n\n"
        "- Быстро реагирует на вопросы.\n\n"
        "**Зоны роста**\n\n"
        "- Нужна более точная интерпретация вероятностного вывода.\n\n"
        "**Оценка:** 3 из 10 возможных баллов.\n\n"
        "**Итоговая оценка по теоретическому блоку:** 3/10."
    )

    cleaned = finalize_theory_final_message(dirty_text, contract)

    assert "3 из 10 возможных баллов" not in cleaned
    assert cleaned.count("**Итоговая оценка по теоретическому блоку:** 3/10.") == 1
