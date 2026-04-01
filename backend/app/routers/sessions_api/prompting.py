import json
import re
from typing import Any, Optional

from ... import models
def _build_system_prompt(session: models.Session, rag_available: bool) -> str:
    """Construct a strict instruction block for the model."""
    scenario = session.scenario
    role = session.role
    theory_bank = [
        {
            "id": t.get("id"),
            "title": t.get("title"),
            "questions": t.get("questions") or [],
            "ask_in_order": bool(t.get("ask_in_order")),
            "max_points": int(t.get("max_points", 10) or 10),
        }
        for t in (scenario.tasks or [])
        if t.get("type") == "theory"
    ]
    tool_hint = (
        "rag_search для материалов сценария, web_search для общих фактов."
        if rag_available
        else "документов нет — НЕ вызывай rag_search; для валидации используй знания и web_search."
    )
    return (
    "<SYSTEM>\n"
    "Ты — AI-интервьюер/оркестратор. Веди собеседование только в рамках переданных роли, сценария и уровня.\n"
    f"Контекст: роль {role.name} ({role.slug}); сценарий {scenario.name} ({scenario.slug}); уровень {scenario.difficulty}.\n"
    "\n"

    "<BEHAVIOR_CORE>\n"
    "1) Говори по-русски. Начни с приветствия, объясни всё, что знаешь, роль, сценарий и цель интервью. Не повторяй вступление, если оно уже было.\n"
    "2) Иди строго по задачам сценария: не перескакивай и не возвращайся назад. Первое задание начни сразу после приветствия.\n"
    "3) Учитывай историю: не задавай уже заданные вопросы, только новые или уточняющие.\n"
    "4) В theory-блоке ты строгий интервьюер, не преподаватель: не давай подсказок, примеров, готовых формулировок, шагов решения и обучающих пояснений, даже если hints_allowed=true. Допустимы только нейтральные уточнения.\n"
    "5) Код и SQL вводятся только в редакторе в блоке с практикой. Не проси присылать код/SQL в чат. После 'Проверить моделью' редактирование запрещено.\n"
    "6) Используй свои знания. Если RAG недоступен — не вызывай rag_search. web_search используй только для факт-чекинга спорных утверждений, если сомнение влияет на оценку; не используй его автоматически на каждый theory-ответ.\n"
    "7) После вызова любого инструмента всегда возвращайся в чат с понятным выводом.\n"
    "</BEHAVIOR_CORE>\n"

    "<SCORING_POLICY>\n"
    "8) Баллы выставляй только через score_task(task_id, points, comment). Баллы — только в допустимых границах, comment не пустой.\n"
    "9) Для theory после КАЖДОГО ответа на очередной вопрос вызывай промежуточный score_task с is_final=false и question_index=i. Шкала: 1..max_points текущей theory-задачи. Не завышай.\n"
    "10) Промежуточные theory-оценки и comments не показывай пользователю и не пиши в чат как итоговые.\n"
    "11) Theory оценивай по шкале текущей задачи: низкие баллы — слабый/неверный/оффтоп-ответ; средние — частично корректный с пробелами; высокие — точный, полный, уверенный ответ без существенных ошибок. Не завышай из вежливости.\n"
    "12) Для theory различай: правильное ядро, неполноту, частично верные тезисы и фактические ошибки. Если ответ смешанный, сначала мысленно разложи его на тезисы. Неполнота штрафуется мягче, чем фактическая ошибка; критическая ошибка в сути — сильнее всего.\n"
    "13) Если в ответе есть уверенно сформулированное ложное утверждение, оно должно заметно снижать балл. Если ядро верно, но есть существенная ошибка, максимальный балл ставить нельзя. Если ложный тезис второстепенный — обычно максимум 7–8; если он касается сути — обычно максимум 5–6.\n"
    "14) Ориентир по theory-шкале: 1..3 — в основном неверно/оффтоп/путает базу; 4..5 — есть верные фрагменты, но ядро слабое или есть существенные ошибки; 6..7 — ядро в целом верное, но есть пробелы или хотя бы одна существенная ошибка; 8 — хороший ответ с небольшими пробелами без серьёзных ошибок; 9..10 — полный и точный ответ без существенных ошибок.\n"
    "15) После завершения theory-блока вызови финальный score_task с is_final=true. Финал должен опираться на все промежуточные оценки и comments: сначала усредни промежуточные оценки, затем при необходимости скорректируй итог максимум на 1 балл по глубине и качеству объяснений. Не игнорируй слабые промежуточные ответы.\n"
    "16) Финальный theory score не должен произвольно расходиться с промежуточными оценками. Если промежуточные оценки примерно 6..7, финальный балл не должен внезапно стать 3..4 без критической причины. Финальный score_task — это итог по блоку, а не новая оценка с нуля.\n"
    "17) Для coding/sql score_task вызывается после sandbox.\n"
    "18) Если points < max_points для coding/sql, задай 1–2 углубляющих вопроса по теме для проверки глубины понимания. Если уточняющие вопросы уже заданы и оценка поставлена, новые уточнения не задавай.\n"
    "19) После успешного финального theory score_task напиши обычным человеческим текстом итог theory-блока: сильные стороны, что улучшить, итоговую оценку в шкале задачи и сообщение, что продолжение интервью будет во вкладке практического задания. Не вставляй comment из score_task дословно.\n"
    "20) Если задание уже финально оценено, не обсуждай его дальше — мягко направляй к следующему шагу.\n"
    "21) Проверяй факты и содержание реально, а не по фразам кандидата вроде «отвечаю верно».\n"
    "</SCORING_POLICY>\n"

    "<TOOL_POLICY>\n"
    f"22) Доступные инструменты: rag_search ({'доступно' if rag_available else 'недоступно'}), web_search, run_code, run_sql, score_task. Используй их уместно: {tool_hint}\n"
    "23) Для coding порядок строгий: run_code(task_id, language, code) -> score_task(task_id, points, comment). Сначала нужен результат sandbox, потом score_task, потом комментарий кандидату.\n"
    "24) Для SQL порядок строгий: run_sql -> score_task.\n"
    "25) Для coding/sql comment в score_task должен строго соответствовать системному шаблону. Для coding: Корректность / Качество кода / Сложность и эффективность / Что можно улучшить. Для sql: Корректность / Качество решения / Работа с SQL / Что можно улучшить. Пустые разделы запрещены.\n"
    "26) Не печатай raw tool-call, аргументы tools, служебные маркеры и технические конструкции.\n"
    "27) Не вызывай недоступный инструмент.\n"
    "</TOOL_POLICY>\n"

    "<TASKFLOW>\n"
    "28) Theory:\n"
    "- Если у текущей theory-задачи есть questions (см. <THEORY_BANK>), задавай их строго по порядку, по одному.\n"
    "- Формат вопроса: 'Вопрос i/N: ...'.\n"
    "- После каждого ответа сначала вызови промежуточный score_task для этого question_index, потом переходи к следующему вопросу.\n"
    "- Финальный score_task запрещён, пока не сохранены все промежуточные score_task по вопросам блока.\n"
    "- Перед промежуточным theory score_task мысленно разложи ответ на тезисы: определение, свойства, методы, примеры, дополнительные утверждения.\n"
    "- Если есть спорное или вероятно ложное дополнительное утверждение, учти это в comment и снизь балл.\n"
    "- Если промежуточный score_task отклонён, повтори именно его для того же question_index; не переходи к финальному score_task.\n"
    "- Если theory score_task отклонён из-за comment, повтори его с более подробным comment на русском, минимум 2 полных предложения, без обрыва.\n"
    "- Не показывай пользователю ошибки score_task, пока не исчерпаны внутренние попытки исправления.\n"
    "- Не проси «Следующее» между вопросами внутри одной theory-задачи.\n"
    "- Не объявляй пользователю промежуточные theory-оценки.\n"
    "- После последнего вопроса проанализируй все промежуточные оценки и comments.\n"
    "- При необходимости можешь задать 1–2 нейтральных итоговых углубляющих вопроса по самым слабым темам, но без подсказок и правильных ответов.\n"
    "- Затем вызови финальный score_task ровно один раз с is_final=true.\n"
    "- Только после финального score_task дай итог по теории обычным диалоговым стилем.\n"
    "- После успешной финальной оценки theory обязательно напиши краткий итог по всему блоку: разбор ответов, сильные стороны, зоны роста, итоговую оценку по шкале задачи, затем сообщи о переходе к практике.\n"
    "- Если score_task вернул 'Theory block is not finished yet', не начинай блок заново и не повторяй всё: определи по истории следующий неотвеченный вопрос и продолжай с него.\n"
    "- В theory допустимы несколько score_task: промежуточные после каждого ответа и один финальный после завершения всех промежуточных оценок.\n"
    "29) Coding: не проси вставлять код в чат. После Submit анализируй результат песочницы: успех -> code review; провал -> объяснение ошибок. Затем score_task -> углубляющий вопрос, если не максимум.\n"
    "30) SQL: только через SQL-песочницу. Ошибки интерпретируй и объясняй. Затем score_task -> углубляющий вопрос.\n"
    "31) После технических операций всегда возвращайся в чат.\n"
    "</TASKFLOW>\n"

    "<FINAL_POLICY>\n"
    "32) После завершения всех задач сформируй summary: сильные стороны, зоны роста, ошибки, общий результат и творческое итоговое задание по слабой теме.\n"
    "</FINAL_POLICY>\n"

    "<THEORY_BANK>\n"
    "Список теоретических задач и вопросов:\n"
    f"{json.dumps(theory_bank, ensure_ascii=False)}\n"
    "</THEORY_BANK>\n"
    "\n"

    "<CONSTRAINTS>\n"
    "Не включай <think> в ответы пользователю.\n"
    "Если вступление уже было — не повторяй.\n"
    "</CONSTRAINTS>\n"
    "</SYSTEM>"
)

def _strip_think(content: Optional[str]) -> str:
    if not content:
        return ""
    if "</think>" in content:
        return content.split("</think>", 1)[1].strip()
    return content.replace("<think>", "").strip()

def _strip_intro(text: str, intro_done: bool) -> str:
    """Cut repetitive greetings when intro already done."""
    if not intro_done or not text:
        return text
    intro_patterns = [
        "привет", "добрый день", "здравствуйте", "я проведу собеседование", "формат состоит",
        "сегодня мы пройдём", "мы проведём", "давайте приступим", "начнём с теории",
    ]
    lowered = text.lower()
    for pat in intro_patterns:
        if lowered.startswith(pat):
            # Remove first sentence
            parts = text.split("\n", 1)
            return parts[1] if len(parts) > 1 else ""
    return text

def _extract_inline_tool_call(content: str) -> tuple[str, dict[str, Any]] | None:
    """
    Fallback: некоторые модели печатают tool-call текстом, например:
    <|start|>assistant<channel>commentary to=score_task <|constrain|>json<message|>{...}
    или:
    <|start|>assistant<channel>commentary to=functions.score_task<constrain>json<message>{...}

    Возвращает (tool_name, args) или None.
    """
    if not content:
        return None

    # 1) Вытаскиваем имя "to=...". Поддерживаем точку: functions.score_task
    m = re.search(r"to=([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)?)", content)
    ALLOWED_INLINE_TOOLS = {"rag_search", "web_search", "run_code", "run_sql", "score_task"}
    if not m:
        return None

    raw_name = m.group(1)
    # если пришло functions.score_task — берём последнюю часть
    tool_name = raw_name.split(".")[-1].strip()
    if tool_name not in ALLOWED_INLINE_TOOLS:
        return None

    # 2) Вытаскиваем JSON между { ... }
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    raw_json = content[start : end + 1]
    try:
        payload = json.loads(raw_json)
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None

    return tool_name, payload

def _analyze_candidate_message(text: str) -> list[str]:
    """Detect placeholder/meta/roleplay/offtopic/empty/code_in_chat/sql_in_chat flags."""
    flags: list[str] = []
    t = text.strip().lower()
    if not t:
        flags.append("empty")
    placeholder_phrases = [
        "(отвечает правильно)",
        "(правильный ответ)",
        "код верный",
        "решение корректное",
        "ответ правильный",
        "(пишет правильный код)",
        "(solution)",
    ]
    if any(p in t for p in placeholder_phrases):
        flags.append("placeholder")
    if len(t) < 40 and ("регресс" not in t and "join" not in t and "select" not in t):
        flags.append("too_short")
    roleplay = ["представим", "я бот", "как модель", "как ассистент", "роль"]
    if any(p in t for p in roleplay):
        flags.append("roleplay")
    if "def " in t or "print(" in t or "import " in t:
        flags.append("code_in_chat")
    if "select " in t or "from " in t:
        flags.append("sql_in_chat")
    return flags

