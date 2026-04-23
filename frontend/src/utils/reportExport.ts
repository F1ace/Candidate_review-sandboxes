import type { InterviewReport, InterviewReportSection, InterviewReportTask } from "../types/interview";

function escapeHtml(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function formatDateTime(value?: string | null): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(parsed);
}

function formatPercent(ratio?: number | null): string {
  if (ratio === undefined || ratio === null) return "—";
  return `${Math.round(ratio * 100)}%`;
}

function formatTaskScore(task: InterviewReportTask): string {
  if (task.score === undefined || task.score === null) {
    return `не оценено / ${task.max_points}`;
  }
  return `${task.score}/${task.max_points}`;
}

function ratioTone(ratio?: number | null): string {
  if (ratio === undefined || ratio === null) return "muted";
  if (ratio >= 0.85) return "strong";
  if (ratio >= 0.7) return "good";
  if (ratio >= 0.55) return "warn";
  return "risk";
}

function renderBulletList(items: string[], emptyText: string): string {
  if (!items.length) {
    return `<p class="empty">${escapeHtml(emptyText)}</p>`;
  }

  return `
    <ul class="bullet-list">
      ${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}
    </ul>
  `;
}

function renderSection(section: InterviewReportSection): string {
  return `
    <section class="report-section">
      <div class="section-head">
        <p class="section-kicker">Секция</p>
        <h3>${escapeHtml(section.title)}</h3>
      </div>
      <p class="section-summary">${escapeHtml(section.summary)}</p>
      ${renderBulletList(section.highlights, "Для этой секции нет дополнительных тезисов.")}
    </section>
  `;
}

function renderTaskCard(task: InterviewReportTask): string {
  return `
    <article class="task-card">
      <div class="task-card-head">
        <div>
          <p class="task-type">${escapeHtml(task.task_type.toUpperCase())}</p>
          <h4>${escapeHtml(task.title)}</h4>
          <p class="task-id">${escapeHtml(task.task_id)}</p>
        </div>
        <div class="score-pill ${ratioTone(task.ratio)}">
          <span>${escapeHtml(formatTaskScore(task))}</span>
          <small>${escapeHtml(formatPercent(task.ratio))}</small>
        </div>
      </div>
      <p class="task-summary">${escapeHtml(task.summary)}</p>
      ${renderBulletList(task.highlights, "Подробные заметки по этой задаче не сформированы.")}
    </article>
  `;
}

function buildReportHtml(report: InterviewReport): string {
  const generatedLabel = report.generation_mode === "llm" ? "LLM synthesis" : "Fallback summary";

  return `<!doctype html>
<html lang="ru">
  <head>
    <meta charset="utf-8" />
    <title>${escapeHtml(report.headline)}</title>
    <style>
      @page {
        size: A4;
        margin: 14mm;
      }

      :root {
        color-scheme: light;
      }

      * {
        box-sizing: border-box;
      }

      html, body {
        margin: 0;
        padding: 0;
        color: #13202b;
        background: #f4f1ea;
        font-family: "Aptos", "Trebuchet MS", "Segoe UI", sans-serif;
      }

      body {
        padding: 20px;
      }

      .report-shell {
        max-width: 960px;
        margin: 0 auto;
        display: grid;
        gap: 18px;
      }

      .cover {
        position: relative;
        overflow: hidden;
        border-radius: 28px;
        padding: 28px;
        background:
          radial-gradient(circle at top right, rgba(255, 196, 92, 0.26), transparent 28%),
          linear-gradient(135deg, #17344b 0%, #23556a 52%, #12303f 100%);
        color: #f7f3eb;
      }

      .cover::after {
        content: "";
        position: absolute;
        inset: 18px;
        border: 1px solid rgba(255, 255, 255, 0.12);
        border-radius: 22px;
        pointer-events: none;
      }

      .cover-top {
        display: flex;
        justify-content: space-between;
        gap: 20px;
        align-items: flex-start;
      }

      .eyebrow {
        margin: 0 0 10px;
        font-size: 11px;
        letter-spacing: 0.24em;
        text-transform: uppercase;
        opacity: 0.78;
      }

      h1 {
        margin: 0;
        font-family: Georgia, "Times New Roman", serif;
        font-size: 31px;
        line-height: 1.08;
      }

      .subtitle {
        margin: 14px 0 0;
        max-width: 720px;
        font-size: 15px;
        line-height: 1.7;
        color: rgba(247, 243, 235, 0.92);
      }

      .meta-card {
        min-width: 220px;
        padding: 16px;
        border-radius: 18px;
        background: rgba(255, 255, 255, 0.08);
        border: 1px solid rgba(255, 255, 255, 0.12);
        backdrop-filter: blur(10px);
      }

      .meta-card p {
        margin: 0 0 10px;
        font-size: 12px;
        color: rgba(247, 243, 235, 0.78);
      }

      .meta-card strong {
        display: block;
        margin-bottom: 6px;
        font-size: 15px;
        color: #fffaf1;
      }

      .metric-grid {
        display: grid;
        grid-template-columns: repeat(4, minmax(0, 1fr));
        gap: 12px;
      }

      .metric-card {
        padding: 18px;
        border-radius: 20px;
        background: #fffdf9;
        border: 1px solid rgba(24, 42, 56, 0.08);
        box-shadow: 0 14px 40px rgba(19, 32, 43, 0.08);
      }

      .metric-label {
        margin: 0 0 6px;
        font-size: 11px;
        letter-spacing: 0.16em;
        text-transform: uppercase;
        color: #7d6a4d;
      }

      .metric-value {
        margin: 0;
        font-size: 28px;
        font-weight: 700;
        color: #17344b;
      }

      .metric-hint {
        margin: 8px 0 0;
        color: #55616f;
        font-size: 13px;
      }

      .content-grid {
        display: grid;
        grid-template-columns: 1.5fr 0.95fr;
        gap: 16px;
      }

      .card {
        padding: 20px;
        border-radius: 22px;
        background: #fffdf9;
        border: 1px solid rgba(24, 42, 56, 0.08);
        box-shadow: 0 12px 36px rgba(19, 32, 43, 0.08);
      }

      .card h2 {
        margin: 0 0 12px;
        font-size: 22px;
        color: #17344b;
      }

      .card p {
        margin: 0;
        line-height: 1.7;
      }

      .tag {
        display: inline-flex;
        padding: 7px 12px;
        border-radius: 999px;
        background: #eff4f7;
        border: 1px solid rgba(23, 52, 75, 0.1);
        color: #17344b;
        font-size: 12px;
        font-weight: 700;
        letter-spacing: 0.08em;
        text-transform: uppercase;
      }

      .bullet-columns {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 16px;
      }

      .bullet-list {
        margin: 0;
        padding-left: 20px;
        display: grid;
        gap: 8px;
      }

      .bullet-list li {
        line-height: 1.55;
      }

      .empty {
        color: #667280;
      }

      .section-grid,
      .task-grid {
        display: grid;
        gap: 14px;
      }

      .report-section,
      .task-card {
        padding: 18px;
        border-radius: 20px;
        background: #fffdf9;
        border: 1px solid rgba(24, 42, 56, 0.08);
        box-shadow: 0 10px 30px rgba(19, 32, 43, 0.07);
      }

      .section-head,
      .task-card-head {
        display: flex;
        justify-content: space-between;
        gap: 16px;
        align-items: flex-start;
      }

      .section-kicker,
      .task-type,
      .task-id {
        margin: 0;
        color: #7d6a4d;
        font-size: 11px;
        letter-spacing: 0.14em;
        text-transform: uppercase;
      }

      .report-section h3,
      .task-card h4 {
        margin: 6px 0 0;
        color: #17344b;
      }

      .section-summary,
      .task-summary {
        margin: 14px 0;
        color: #283746;
      }

      .score-pill {
        min-width: 108px;
        padding: 10px 12px;
        border-radius: 16px;
        text-align: right;
        border: 1px solid transparent;
      }

      .score-pill span {
        display: block;
        font-size: 18px;
        font-weight: 700;
      }

      .score-pill small {
        font-size: 12px;
        letter-spacing: 0.08em;
        text-transform: uppercase;
      }

      .score-pill.strong {
        background: #ecf7ef;
        border-color: #b8dfc2;
        color: #1f6b3d;
      }

      .score-pill.good {
        background: #eef6ff;
        border-color: #bdd6ef;
        color: #215b93;
      }

      .score-pill.warn {
        background: #fff6e7;
        border-color: #edd2a2;
        color: #9a6418;
      }

      .score-pill.risk,
      .score-pill.muted {
        background: #fff0ef;
        border-color: #efc3bf;
        color: #9f4139;
      }

      .footer-note {
        padding: 18px 20px;
        border-radius: 18px;
        background: #17344b;
        color: #f6f1e8;
      }

      .footer-note p {
        margin: 0;
        line-height: 1.7;
      }

      .loader-shell,
      .error-shell {
        max-width: 720px;
        margin: 48px auto;
        padding: 28px;
        border-radius: 24px;
        background: #fffdf9;
        border: 1px solid rgba(24, 42, 56, 0.08);
        box-shadow: 0 18px 44px rgba(19, 32, 43, 0.12);
      }

      @media print {
        body {
          padding: 0;
          background: #fff;
        }

        .report-shell {
          max-width: none;
        }

        .card,
        .metric-card,
        .report-section,
        .task-card {
          break-inside: avoid;
          box-shadow: none;
        }
      }

      @media (max-width: 860px) {
        .metric-grid,
        .content-grid,
        .bullet-columns {
          grid-template-columns: 1fr;
        }
      }
    </style>
  </head>
  <body>
    <main class="report-shell">
      <section class="cover">
        <div class="cover-top">
          <div>
            <p class="eyebrow">Interview Report</p>
            <h1>${escapeHtml(report.headline)}</h1>
            <p class="subtitle">${escapeHtml(report.executive_summary)}</p>
          </div>
          <aside class="meta-card">
            <p>Сессия</p>
            <strong>${escapeHtml(report.session_id)}</strong>
            <p>Роль: ${escapeHtml(report.role_name)}</p>
            <p>Сценарий: ${escapeHtml(report.scenario_name)}</p>
            <p>Сложность: ${escapeHtml(report.difficulty || "—")}</p>
            <p>Сгенерировано: ${escapeHtml(formatDateTime(report.generated_at))}</p>
            <p>Режим: ${escapeHtml(generatedLabel)}</p>
          </aside>
        </div>
      </section>

      <section class="metric-grid">
        <article class="metric-card">
          <p class="metric-label">Итоговый счёт</p>
          <p class="metric-value">${escapeHtml(`${report.overall_score}/${report.overall_max}`)}</p>
          <p class="metric-hint">Нормализованный итог по всему сценарию</p>
        </article>
        <article class="metric-card">
          <p class="metric-label">Покрытие</p>
          <p class="metric-value">${escapeHtml(`${report.scored_tasks}/${report.total_tasks}`)}</p>
          <p class="metric-hint">Оценённых блоков в интервью</p>
        </article>
        <article class="metric-card">
          <p class="metric-label">Вердикт</p>
          <p class="metric-value">${escapeHtml(report.recommendation_label)}</p>
          <p class="metric-hint">${escapeHtml(formatPercent(report.overall_ratio))}</p>
        </article>
        <article class="metric-card">
          <p class="metric-label">Длительность</p>
          <p class="metric-value">${escapeHtml(`${report.duration_minutes} мин`)}</p>
          <p class="metric-hint">Диалог: ${escapeHtml(`${report.candidate_message_count} / ${report.model_message_count}`)} реплик</p>
        </article>
      </section>

      <section class="content-grid">
        <article class="card">
          <h2>Общая оценка</h2>
          <p>${escapeHtml(report.overall_assessment)}</p>
        </article>
        <article class="card">
          <span class="tag">Рекомендация</span>
          <h2 style="margin-top: 12px;">Следующий шаг</h2>
          <p>${escapeHtml(report.recommendation_summary)}</p>
        </article>
      </section>

      <section class="card">
        <div class="bullet-columns">
          <div>
            <h2>Сильные стороны</h2>
            ${renderBulletList(report.strengths, "Сильные стороны не были выделены автоматически.")}
          </div>
          <div>
            <h2>Зоны роста</h2>
            ${renderBulletList(report.growth_areas, "Зоны роста не были выделены автоматически.")}
          </div>
        </div>
      </section>

      <section class="section-grid">
        ${report.sections.map(renderSection).join("")}
      </section>

      <section class="task-grid">
        ${report.task_breakdown.map(renderTaskCard).join("")}
      </section>

      <section class="footer-note">
        <p>${escapeHtml(report.closing_note)}</p>
      </section>
    </main>
  </body>
</html>`;
}

export function renderInterviewReportLoadingWindow(targetWindow: Window): void {
  targetWindow.document.open();
  targetWindow.document.write(`<!doctype html>
<html lang="ru">
  <head>
    <meta charset="utf-8" />
    <title>Подготовка отчёта</title>
    <style>
      body {
        margin: 0;
        min-height: 100vh;
        display: grid;
        place-items: center;
        background: linear-gradient(135deg, #17344b, #0f2433);
        color: #fffaf2;
        font-family: "Aptos", "Trebuchet MS", "Segoe UI", sans-serif;
      }

      .loader-shell {
        max-width: 720px;
        padding: 32px;
        border-radius: 24px;
        background: rgba(255, 255, 255, 0.08);
        border: 1px solid rgba(255, 255, 255, 0.12);
        box-shadow: 0 24px 60px rgba(0, 0, 0, 0.18);
      }

      h1 {
        margin: 0 0 12px;
        font-family: Georgia, "Times New Roman", serif;
      }

      p {
        margin: 0;
        line-height: 1.6;
        color: rgba(255, 250, 242, 0.88);
      }
    </style>
  </head>
  <body>
    <div class="loader-shell">
      <h1>Готовим PDF-отчёт</h1>
      <p>LLM собирает итог по сценарию, ответам и баллам. Окно автоматически откроет print-preview, как только отчёт будет готов.</p>
    </div>
  </body>
</html>`);
  targetWindow.document.close();
}

export function renderInterviewReportErrorWindow(targetWindow: Window, message: string): void {
  targetWindow.document.open();
  targetWindow.document.write(`<!doctype html>
<html lang="ru">
  <head>
    <meta charset="utf-8" />
    <title>Ошибка отчёта</title>
    <style>
      body {
        margin: 0;
        min-height: 100vh;
        display: grid;
        place-items: center;
        background: #fff5f4;
        color: #3d2321;
        font-family: "Aptos", "Trebuchet MS", "Segoe UI", sans-serif;
      }

      .error-shell {
        max-width: 720px;
        padding: 28px;
        border-radius: 22px;
        background: #ffffff;
        border: 1px solid rgba(140, 42, 35, 0.12);
        box-shadow: 0 18px 44px rgba(61, 35, 33, 0.12);
      }

      h1 {
        margin: 0 0 12px;
        color: #8c2a23;
      }

      p {
        margin: 0;
        line-height: 1.6;
      }
    </style>
  </head>
  <body>
    <div class="error-shell">
      <h1>Не удалось подготовить отчёт</h1>
      <p>${escapeHtml(message)}</p>
    </div>
  </body>
</html>`);
  targetWindow.document.close();
}

export function renderInterviewReportWindow(targetWindow: Window, report: InterviewReport): void {
  targetWindow.document.open();
  targetWindow.document.write(buildReportHtml(report));
  targetWindow.document.close();
  targetWindow.focus();
  targetWindow.setTimeout(() => {
    targetWindow.print();
  }, 250);
}
