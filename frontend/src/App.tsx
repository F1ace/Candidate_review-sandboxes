import { useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import { API_BASE, fetchJson } from "./api/client";
import { RunCodeSummary } from "./components/RunCodeSummary";
import { RunSqlSummary } from "./components/RunSqlSummary";
import { SqlSchemaPreview } from "./components/SqlSchemaPreview";
import { SectionHeader } from "./components/SectionHeader";
import type {
  Message,
  PracticeAgentResponse,
  Role,
  Scenario,
  SandboxRunResult,
  ScoreResultPayload,
  SessionStatePayload,
  StreamEventPayload,
  Task,
  ToolResultItem,
  View,
  SqlRunResult,
  SqlScenario,
} from "./types/interview";
import { formatCodeScoreComment, formatSqlScoreComment } from "./utils/scoreFormatting";
import "./App.css";

const defaultTasks: Task[] = [
  {
    id: "T1",
    type: "theory",
    title: "Основы регрессии",
    description: "Объяснить, что такое линейная регрессия, и перечислить метрики качества.",
    max_points: 5,
    hints_allowed: false,
  },
  {
    id: "C1",
    type: "coding",
    language: "python",
    title: "Логистическая регрессия",
    description_for_candidate: "Реализуйте логистическую регрессию без sklearn.",
    max_points: 10,
    tests_id: "logreg_basic",
  },
  {
    id: "SQL1",
    type: "sql",
    title: "Агрегация заказов",
    description_for_candidate: "По таблицам orders и customers посчитайте сумму заказов по городам.",
    max_points: 8,
    sql_scenario_id: "ecommerce_basic",
  },
];

type StartSessionResponse = {
  id: string;
};

type SubmitCodeResponse = {
  result?: SandboxRunResult;
};

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null;

const getErrorMessage = (error: unknown): string => {
  if (error instanceof Error) return error.message;
  return String(error);
};

const getToolResults = (resp: PracticeAgentResponse): ToolResultItem[] =>
  Array.isArray(resp.tool_results) ? resp.tool_results : [];

const findToolResult = (toolResults: ToolResultItem[], toolName: string): ToolResultItem | undefined =>
  toolResults.find((item) => item?.name === toolName);

const getScoreResultPayload = (rawResult: unknown): ScoreResultPayload | null => {
  if (!isRecord(rawResult) || !rawResult.ok) return null;
  return {
    points: rawResult.points as number,
    comment: rawResult.comment as string | undefined,
    is_final: rawResult.is_final as boolean | undefined,
    task_id: rawResult.task_id as string | undefined,
  };
};

function App() {
  const [view, setView] = useState<View>("landing");
  const [roles, setRoles] = useState<Role[]>([]);
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [selectedRole, setSelectedRole] = useState<number | null>(null);
  const [selectedScenario, setSelectedScenario] = useState<number | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [streamingReply, setStreamingReply] = useState<string>("");
  const [streaming, setStreaming] = useState(false);
  const [chatInput, setChatInput] = useState("");
  const [codeDraft, setCodeDraft] = useState("# Напишите решение здесь\n");
  const [sqlDraft, setSqlDraft] = useState("select * from orders limit 5;");
  const [agentFeedback, setAgentFeedback] = useState<string | null>(null);
  const [runCodeResult, setRunCodeResult] = useState<SandboxRunResult | null>(null);
  const [scoreResult, setScoreResult] = useState<ScoreResultPayload | null>(null);
  const [executionLog, setExecutionLog] = useState<string | null>(null);
  const [sqlRunResult, setSqlRunResult] = useState<SqlRunResult | null>(null);
  const [sqlScenarios, setSqlScenarios] = useState<SqlScenario[]>([]);
  const [isRunningTests, setIsRunningTests] = useState(false);
  const [isScoring, setIsScoring] = useState(false);
  const [adminRoleDraft, setAdminRoleDraft] = useState({ name: "", slug: "", description: "" });
  const [adminScenarioDraft, setAdminScenarioDraft] = useState({
    role_id: "",
    name: "",
    slug: "",
    description: "",
    difficulty: "junior",
    rag_corpus_id: "",
    tasks: JSON.stringify(defaultTasks, null, 2),
  });
  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [currentTaskIndex, setCurrentTaskIndex] = useState(0);
  const [sessionScores, setSessionScores] = useState<Record<string, number>>({});
  const [sessionMode, setSessionMode] = useState<"theory" | "practice">("theory");

  const selectedScenarioObj = useMemo(
    () => scenarios.find((s) => s.id === selectedScenario),
    [selectedScenario, scenarios],
  );

  const orderedTasks = useMemo(() => selectedScenarioObj?.tasks ?? [], [selectedScenarioObj]);
  const currentTask = orderedTasks[currentTaskIndex] || null;
  const activeSqlScenario = useMemo(() => {
  if (!currentTask || currentTask.type !== "sql" || !currentTask.sql_scenario_id) {
    return null;
  }

  return sqlScenarios.find((item) => item.name === currentTask.sql_scenario_id) || null;
}, [currentTask, sqlScenarios]);
  const theoryTasks = useMemo(
  () => orderedTasks.filter((t) => t.type === "theory"),
  [orderedTasks],
);

const theoryCompleted = useMemo(() => {
  if (!theoryTasks.length) return false;
  return theoryTasks.every((t) => sessionScores[t.id] !== undefined);
}, [theoryTasks, sessionScores]);

  const firstPracticeIndex = useMemo(() => {
  // индекс первого задания НЕ theory (coding/sql)
  return orderedTasks.findIndex((t) => t.type !== "theory");
  }, [orderedTasks]);

  useEffect(() => {
    const bootstrap = async () => {
      try {
        const [rolesResp, scenariosResp, sqlScenariosResp] = await Promise.all([
          fetchJson<Role[]>("/roles").catch(() => [] as Role[]),
          fetchJson<Scenario[]>("/scenarios").catch(() => [] as Scenario[]),
          fetchJson<SqlScenario[]>("/sql-scenarios").catch(() => [] as SqlScenario[]),
        ]);

        setRoles(rolesResp.length ? rolesResp : sampleRoles());
        setScenarios(scenariosResp.length ? scenariosResp : sampleScenarios());
        setSqlScenarios(sqlScenariosResp);
      } catch (err) {
        console.error(err);
        setRoles(sampleRoles());
        setScenarios(sampleScenarios());
        setSqlScenarios([]);
      }
    };

    bootstrap();
  }, []);

  useEffect(() => {
    if (selectedRole && !selectedScenario) {
      const first = scenarios.find((s) => s.role_id === selectedRole);
      if (first) setSelectedScenario(first.id);
    }
  }, [selectedRole, selectedScenario, scenarios]);

  useEffect(() => {
  if (!currentTask) return;

  if (currentTask.type === "coding") {
    setCodeDraft(currentTask.starter_code || "# Напишите решение здесь\n");
  }

  if (currentTask.type === "sql") {
    setSqlDraft(currentTask.description_for_candidate ? "-- Напишите SQL здесь\n" : "select 1;");
  }
}, [currentTask]);

  const sampleRoles = (): Role[] => [
    { id: 1, name: "Data Scientist", slug: "ds", description: "ML, эксперименты, метрики" },
    { id: 2, name: "Backend", slug: "backend", description: "API, очереди, устойчивость" },
    { id: 3, name: "Data Engineer", slug: "de", description: "ETL, SQL, пайплайны" },
  ];

  const sampleScenarios = (): Scenario[] => [
    {
      id: 101,
      role_id: 1,
      name: "DS — Junior ML",
      slug: "ds-junior-ml",
      description: "Регрессия, классификация, SQL основы",
      difficulty: "junior",
      tasks: defaultTasks,
    },
    {
      id: 102,
      role_id: 1,
      name: "DS — Product ML",
      slug: "ds-product-ml",
      description: "A/B, метрики продукта, рекомендации",
      difficulty: "middle",
      tasks: [
        { id: "T-metrics", type: "theory", title: "Метрики A/B", max_points: 5, hints_allowed: false },
        {
          id: "SQL-ab",
          type: "sql",
          title: "Конверсия по когорте",
          description_for_candidate: "Напишите запрос конверсии по дню регистрации.",
          sql_scenario_id: "ab_product",
          max_points: 8,
        },
      ],
    },
    {
      id: 201,
      role_id: 2,
      name: "Backend — REST",
      slug: "be-rest",
      description: "API дизайн, идемпотентность, очереди",
      difficulty: "middle",
      tasks: [
        { id: "T-REST", type: "theory", title: "PUT vs PATCH идемпотентность", max_points: 5 },
        {
          id: "C-BE",
          type: "coding",
          language: "python",
          title: "Очередь задач",
          description_for_candidate: "Реализуйте очередь с ack/nack.",
          tests_id: "queue_basic",
          max_points: 8,
        },
      ],
    },
    {
      id: 202,
      role_id: 2,
      name: "Backend — Resilience",
      slug: "be-resilience",
      description: "Ретраи, троттлинг, circuit breaker",
      difficulty: "senior",
      tasks: [
        { id: "T-circuit", type: "theory", title: "Circuit breaker", max_points: 6, hints_allowed: false },
        {
          id: "C-rate",
          type: "coding",
          language: "python",
          title: "Rate limiter",
          description_for_candidate: "Сделайте токен-бакет.",
          tests_id: "rate_limiter",
          max_points: 9,
        },
      ],
    },
    {
      id: 301,
      role_id: 3,
      name: "DE — Pipelines",
      slug: "de-pipelines",
      description: "Инкрементальные пайплайны, буферизация, SLA",
      difficulty: "middle",
      tasks: [
        { id: "T-de-incr", type: "theory", title: "Инкрементальные загрузки", max_points: 5, hints_allowed: false },
        {
          id: "SQL-de-agg",
          type: "sql",
          title: "Агрегация событий",
          description_for_candidate: "Посчитайте DAU по регионам из таблицы events.",
          sql_scenario_id: "events_basic",
          max_points: 8,
        },
      ],
    },
    {
      id: 302,
      role_id: 3,
      name: "DE — Warehousing",
      slug: "de-warehousing",
      description: "Моделирование данных, SCD, оркестрация",
      difficulty: "senior",
      tasks: [
        { id: "T-scd", type: "theory", title: "SCD типы", max_points: 6 },
        {
          id: "SQL-scd",
          type: "sql",
          title: "SCD Type 2 обновление",
          description_for_candidate: "Напишите SQL, который добавляет новую версию записи клиента.",
          sql_scenario_id: "scd_customers",
          max_points: 9,
        },
      ],
    },
  ];

  const pushMessage = (msg: Message) => setMessages((prev) => [...prev, msg]);

  const streamModel = async (sid?: string) => {
  const activeId = sid ?? sessionId;
    if (!activeId) return;
    setStreaming(true);
    setStreamingReply("");
    let accumulated = "";
    try {
      const resp = await fetch(`${API_BASE}/sessions/${activeId}/lm/chat-stream`, { method: "GET" });
      if (!resp.body) throw new Error("Нет body у ответа");
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split("\n\n");
        buffer = parts.pop() || "";
        for (const part of parts) {
          if (!part.trim().startsWith("data:")) continue;
          const raw = part.trim().slice(5).trim();
          if (!raw) continue;
          const data = JSON.parse(raw) as StreamEventPayload;
          if (data.type === "token") {
            accumulated += data.content;
            setStreamingReply((prev) => prev + data.content);
          }
          if (data.type === "error") {
            pushMessage({ sender: "system", text: `Ошибка модели: ${data.detail || data.content}` });
          }
          if (data.type === "done") {
            if (data.content) {
              accumulated = data.content;
              setStreamingReply(data.content);
            }
          }
        }
      }
    } catch (err: unknown) {
      pushMessage({ sender: "system", text: `Стрим не удался: ${getErrorMessage(err)}` });
    } finally {
      setStreaming(false);
      if (accumulated) {
        pushMessage({ sender: "model", text: accumulated });
        setStreamingReply("");
      }
      await refreshSessionState(activeId);
    }
  };

  const startSession = async () => {
  if (!selectedRole || !selectedScenario) {
    setStatus("Выберите роль и сценарий");
    return;
  }
  setLoading(true);
  try {
    const resp = await fetchJson<StartSessionResponse>("/sessions", {
      method: "POST",
      body: JSON.stringify({
        role_id: selectedRole,
        scenario_id: selectedScenario,
      }),
    });

    const newId = resp.id;
    setSessionId(newId);

    setMessages([
      {
        sender: "system",
        text: "Сессия создана. Модель сейчас представится и обозначит план.",
      },
    ]);
    setStatus("Сессия активна");
    setView("session");
    setCurrentTaskIndex(0);

    await streamModel(newId);
  } catch (err: unknown) {
    setStatus(getErrorMessage(err));
  } finally {
    setLoading(false);
  }
};

  const sendChatMessage = async () => {
    if (!chatInput.trim() || !sessionId) return;
    const outgoing: Message = { sender: "candidate", text: chatInput, task_id: currentTask?.id };
    pushMessage(outgoing);
    setChatInput("");
    try {
      await fetchJson(`/sessions/${sessionId}/messages`, {
        method: "POST",
        body: JSON.stringify(outgoing),
      });
      await streamModel();
    } catch (err: unknown) {
      pushMessage({ sender: "system", text: `Модель недоступна: ${getErrorMessage(err)}` });
    }
  };

  const submitCode = async (task?: Task) => {
    if (!sessionId || !task) {
      setExecutionLog("Нет активной сессии или кода");
      return;
    }

    setAgentFeedback(null);
    setScoreResult(null);
    setRunCodeResult(null);
    setExecutionLog(null);
    setSqlRunResult(null);
    setIsRunningTests(true);

    try {
      const resp = await fetchJson<SubmitCodeResponse>(`/sessions/${sessionId}/tasks/${task.id}/submit_code`, {
        method: "POST",
        body: JSON.stringify({
          code: codeDraft,
          language: task.language || "python",
        }),
      });

      const result = resp.result ?? null;
      setRunCodeResult(result);
      setExecutionLog(JSON.stringify(result, null, 2));
    } catch (err: unknown) {
      setExecutionLog(`Ошибка отправки: ${getErrorMessage(err)}`);
    } finally {
      setIsRunningTests(false);
    }
  };

  const reviewCodeWithModel = async (task?: Task) => {
    if (!sessionId || !task) {
      setExecutionLog("Нет активной сессии или задания");
      return;
    }

    setAgentFeedback(null);
    setScoreResult(null);
    setRunCodeResult(null);
    setSqlRunResult(null);
    setExecutionLog(null);
    setIsRunningTests(true);
    setIsScoring(false);

    try {
      const resp = await fetchJson<PracticeAgentResponse>(`/sessions/${sessionId}/practice/code`, {
        method: "POST",
        body: JSON.stringify({
          task_id: task.id,
          language: task.language || "python",
          code: codeDraft,
        }),
      });

      const toolResults = getToolResults(resp);
      const runCodeTool = findToolResult(toolResults, "run_code");
      const scoreTool = findToolResult(toolResults, "score_task");
      const runCodeToolResult = isRecord(runCodeTool?.result) ? runCodeTool.result : null;
      const nestedRunCodeResult = runCodeToolResult?.result;

      if (nestedRunCodeResult) {
        setRunCodeResult(nestedRunCodeResult as SandboxRunResult);
        setExecutionLog(JSON.stringify(nestedRunCodeResult, null, 2));
      } else if (runCodeTool?.result) {
        setExecutionLog(JSON.stringify(runCodeTool.result, null, 2));
      } else {
        setExecutionLog(JSON.stringify(resp, null, 2));
      }

      setIsRunningTests(false);
      setIsScoring(true);

      const scorePayload = getScoreResultPayload(scoreTool?.result);
      if (scorePayload) {
        setScoreResult(scorePayload);
      } else {
        setScoreResult(null);
      }

      setAgentFeedback(resp.reply || "Нет ответа модели");
    } catch (err: unknown) {
      setAgentFeedback(`Ошибка проверки: ${getErrorMessage(err)}`);
    } finally {
      setIsRunningTests(false);
      setIsScoring(false);
    }
  };

  const submitSql = async (task?: Task) => {
    if (!sessionId || !task) {
      setExecutionLog("Нет активной сессии или SQL");
      return;
    }

    setAgentFeedback(null);
    setScoreResult(null);
    setRunCodeResult(null);
    setSqlRunResult(null);
    setExecutionLog(null);
    setIsRunningTests(true);

    try {
      const resp = await fetchJson(`/sessions/${sessionId}/tasks/${task.id}/submit_sql`, {
        method: "POST",
        body: JSON.stringify({
          query: sqlDraft,
          sql_scenario_id: task.sql_scenario_id || "demo_sql",
        }),
      });

      if (resp && typeof resp === "object") {
        const payload = resp as Record<string, unknown>;
        const normalized =
          payload.result && typeof payload.result === "object"
            ? (payload.result as SqlRunResult)
            : null;

        if (normalized) {
          setSqlRunResult(normalized);
          setExecutionLog(null);
        } else {
          setSqlRunResult(null);
          setExecutionLog(JSON.stringify(resp, null, 2));
        }
      } else {
        setSqlRunResult(null);
        setExecutionLog(JSON.stringify(resp, null, 2));
      }
    } catch (err: unknown) {
      setSqlRunResult(null);
      setExecutionLog(`Ошибка SQL: ${getErrorMessage(err)}`);
    } finally {
      setIsRunningTests(false);
    }
  };

  const reviewSqlWithModel = async (task?: Task) => {
    if (!sessionId || !task) {
      setExecutionLog("Нет активной сессии или задания");
      return;
    }

    setAgentFeedback(null);
    setScoreResult(null);
    setRunCodeResult(null);
    setSqlRunResult(null);
    setExecutionLog(null);
    setIsRunningTests(true);
    setIsScoring(false);

    try {
      const resp = await fetchJson<PracticeAgentResponse>(`/sessions/${sessionId}/practice/sql`, {
        method: "POST",
        body: JSON.stringify({
          task_id: task.id,
          sql_scenario_id: task.sql_scenario_id || "",
          query: sqlDraft,
        }),
      });

      const toolResults = getToolResults(resp);
      const runSqlTool = findToolResult(toolResults, "run_sql");
      const scoreTool = findToolResult(toolResults, "score_task");

      if (runSqlTool?.result && typeof runSqlTool.result === "object") {
        setSqlRunResult(runSqlTool.result as SqlRunResult);
        setExecutionLog(null);
      } else if (runSqlTool?.result) {
        setSqlRunResult(null);
        setExecutionLog(JSON.stringify(runSqlTool.result, null, 2));
      } else {
        setSqlRunResult(null);
        setExecutionLog(null);
      }

      setIsRunningTests(false);
      setIsScoring(true);

      const scorePayload = getScoreResultPayload(scoreTool?.result);
      if (scorePayload) {
        setScoreResult(scorePayload);
      } else {
        setScoreResult(null);
      }

      setAgentFeedback(resp.reply || "Нет ответа модели");
    } catch (err: unknown) {
      setSqlRunResult(null);
      setScoreResult(null);
      setAgentFeedback(`Ошибка SQL-проверки: ${getErrorMessage(err)}`);
    } finally {
      setIsRunningTests(false);
      setIsScoring(false);
    }
  };

  const submitRole = async () => {
    try {
      const resp = await fetchJson<Role>("/roles", {
        method: "POST",
        body: JSON.stringify(adminRoleDraft),
      });
      setRoles((prev) => [...prev, resp]);
      setAdminRoleDraft({ name: "", slug: "", description: "" });
      setStatus("Роль сохранена");
    } catch (err: unknown) {
      setStatus(getErrorMessage(err));
    }
  };

  const submitScenario = async () => {
    try {
      const parsedTasks = JSON.parse(adminScenarioDraft.tasks);
      const payload = {
        role_id: Number(adminScenarioDraft.role_id),
        name: adminScenarioDraft.name,
        slug: adminScenarioDraft.slug,
        description: adminScenarioDraft.description,
        difficulty: adminScenarioDraft.difficulty,
        rag_corpus_id: adminScenarioDraft.rag_corpus_id ? Number(adminScenarioDraft.rag_corpus_id) : null,
        tasks: parsedTasks,
      };
      const resp = await fetchJson<Scenario>("/scenarios", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      setScenarios((prev) => [...prev, resp]);
      setStatus("Сценарий сохранен");
    } catch (err: unknown) {
      setStatus(`Ошибка сценария: ${getErrorMessage(err)}`);
    }
  };

const goNextTask = () => {
  if (currentTaskIndex < orderedTasks.length - 1) {
    setCurrentTaskIndex((i) => i + 1);
  }
};

  const goPrevTask = () => {
    if (currentTaskIndex > 0) {
      setCurrentTaskIndex((i) => i - 1);
    }
  };

  const taskHint = (task: Task | null) => {
    if (!task) return "";
    if (task.type === "theory") return "Ответьте в чате. В теоретическом блоке модель работает как интервьюер и не подсказывает правильный ответ.";
    if (task.type === "coding") return "Напишите код ниже и отправьте. После submit редактор блокируется в реальном UI.";
    if (task.type === "sql") return "Напишите SQL ниже и отправьте. После submit редактор блокируется в реальном UI.";
    return "";
  };

  const difficultyOrder: Record<string, number> = {
    junior: 0,
    middle: 1,
    senior: 2,
  };

  const refreshSessionState = async (sid?: string) => {
    const activeId = sid ?? sessionId;
    if (!activeId) return;

    try {
      const session = await fetchJson<SessionStatePayload>(`/sessions/${activeId}`);
      setSessionScores(session.scores || {});
    } catch (err) {
      console.error("Не удалось обновить состояние сессии", err);
    }
  };

  return (
    <div className="page">
      <header className="hero">
        <div>
          <p className="eyebrow">Chat-Review</p>
          <h1>
            Собеседование в чате с кодом и SQL <span className="accent">под контролем</span>
          </h1>
          <p className="muted">
            Выберите роль и сценарий, создайте сессию, а дальше — структурированный диалог с моделью-интервьюером.
            Песочницы для кода и SQL подключены, подсказки и оценки идут через tools.
          </p>
          <div className="hero-actions">
            {view !== "admin" && (
              <button className="primary" disabled={loading} onClick={startSession}>
                {sessionId ? "Начать" : "Создать сессию и начать"}
              </button>
            )}
            <button className="ghost" onClick={() => setView(view === "admin" ? "landing" : "admin")}>
              {view === "admin" ? "Вернуться" : "Открыть админку"}
            </button>
            {status && <span className="pill">{status}</span>}
          </div>
        </div>
        <div className="status-card">
          <p className="muted">Сессия</p>
          <h3>{sessionId || "не создана"}</h3>
          <p className="muted">{selectedScenarioObj?.name || "Выберите сценарий"}</p>
        </div>
      </header>

      {view === "landing" && (
        <>
          <div className="grid two-column">
            <section className="panel">
              <SectionHeader title="1. Роль" subtitle="Роли задают контекст промпта" />
              <div className="cards">
                {roles.map((role) => (
                  <button
                    key={role.id}
                    className={`card ${selectedRole === role.id ? "active" : ""}`}
                    onClick={() => {
                      setSelectedRole(role.id);
                      setSelectedScenario(null);
                    }}
                  >
                    <div className="card-title">
                      <span>{role.name}</span>
                      <span className="muted">{role.slug}</span>
                    </div>
                    <p className="muted">{role.description}</p>
                  </button>
                ))}
              </div>
            </section>

            <section className="panel">
              <SectionHeader title="2. Сценарий" subtitle="Стандартизированные задачи" />
              <div className="scenario-list">
                {(scenarios || [])
                  .filter((s) => !selectedRole || s.role_id === selectedRole)
                  .sort((a, b) => {
                    const da = difficultyOrder[(a.difficulty || "middle").toLowerCase()] ?? 99;
                    const db = difficultyOrder[(b.difficulty || "middle").toLowerCase()] ?? 99;
                    if (da !== db) return da - db;
                    return (a.name ?? a.slug).localeCompare(b.name ?? b.slug);
                  })
                  .map((scenario) => (
                    <div
                      key={scenario.id}
                      className={`scenario ${selectedScenario === scenario.id ? "active" : ""}`}
                      onClick={() => setSelectedScenario(scenario.id)}
                    >
                      <div className="scenario-head">
                        <div>
                          <p className="label">{scenario.difficulty}</p>
                          <h4>{scenario.name}</h4>
                        </div>
                        <p className="muted">{scenario.slug}</p>
                      </div>
                      <p className="muted">{scenario.description}</p>
                      <div className="chips">
                        {(scenario.tasks || []).map((task) => (
                          <span key={task.id} className="chip">
                            {task.type.toUpperCase()} · {task.id}
                          </span>
                        ))}
                      </div>
                    </div>
                  ))}
              </div>
            </section>
          </div>
          <div className="center">
            <button className="primary" onClick={startSession} disabled={loading}>
              Начать интервью
            </button>
          </div>
        </>
      )}

      {view === "session" && (
        <>
          <div className="mode-switch" style={{ display: "flex", gap: 8, marginBottom: 12 }}>
            <button
              className={sessionMode === "theory" ? "primary" : "ghost"}
              onClick={() => setSessionMode("theory")}
            >
              Теория
            </button>
            <button
              className={sessionMode === "practice" ? "primary" : "ghost"}
              onClick={() => {
                setSessionMode("practice");
                if (firstPracticeIndex >= 0) setCurrentTaskIndex(firstPracticeIndex);
              }}
            >
              Практика
            </button>
          </div>
            {sessionMode === "theory" && (
            <section className="panel">
              <SectionHeader title="Чат" subtitle="Приветствие сверху, затем решения заданий" />
              <div className="chat">
                <div className="messages">
                  {messages.map((msg, idx) => (
                    <div key={idx} className={`message ${msg.sender}`}>
                      <div className="message-meta">
                        <span>{msg.sender}</span>
                        {msg.task_id && <span className="pill small">task {msg.task_id}</span>}
                      </div>
                      <ReactMarkdown>{msg.text}</ReactMarkdown>
                    </div>
                  ))}
                  {streaming && (
                    <div className="message model">
                      <div className="message-meta">
                        <span>model • streaming</span>
                      </div>
                      <ReactMarkdown>{streamingReply || "…"}</ReactMarkdown>
                    </div>
                  )}
                </div>
                {theoryCompleted && (
                  <div className="message system">
                    <div className="message-meta">
                      <span>system</span>
                    </div>
                    <p>Теоретический этап завершён. Продолжайте во вкладке практического задания.</p>
                  </div>
                )}
                <div className="composer">
                  <textarea
                    placeholder={
                      theoryCompleted
                        ? "Теоретический этап завершён. Перейдите к практике."
                        : "Ваш ответ или вопрос..."
                    }
                    value={chatInput}
                    onChange={(e) => setChatInput(e.target.value)}
                    disabled={theoryCompleted}
                  />
                  <button
                    className="primary"
                    onClick={sendChatMessage}
                    disabled={!sessionId || streaming || theoryCompleted}
                  >
                    Отправить
                  </button>
                </div>
              </div>
            </section>
          )}

          
          {sessionMode === "practice" && (
          <section className="panel">
            <SectionHeader title="Задания" subtitle="Показываем по одному, сохраняем контекст" />
            <div className="task-nav">
              <div>
                <p className="label">Текущее задание</p>
                <h4>
                  {currentTaskIndex + 1}/{orderedTasks.length} · {currentTask?.id || "—"} · {currentTask?.title || "Нет"}
                </h4>
                <p className="muted">{taskHint(currentTask)}</p>
              </div>
              <div className="task-nav-buttons">
                <button className="ghost" onClick={goPrevTask} disabled={currentTaskIndex === 0}>
                  Назад
                </button>
                <button className="ghost" onClick={goNextTask} disabled={currentTaskIndex >= orderedTasks.length - 1}>
                  Следующее
                </button>
              </div>
            </div>

            <div className="task-details">
              <h4>{selectedScenarioObj?.name || "Задачи появятся после выбора"}</h4>
              <p className="muted">
                {(selectedScenarioObj?.tasks || []).length
                  ? `${(selectedScenarioObj?.tasks || []).length} заданий в сценарии`
                  : "Добавьте задачи в админке"}
              </p>

              {currentTask?.type === "theory" && (
                <div className="editor">
                  <div className="editor-head">
                    <div>
                      <p className="label">Theory</p>
                      <h5>{currentTask.title}</h5>
                      <p className="muted">{currentTask.description}</p>
                    </div>
                  </div>
                  <p className="muted">Ответ дайте в чате выше. Модель задаст уточнения и выставит баллы.</p>
                </div>
              )}

              {currentTask?.type === "coding" && (
                <div className="editor">
                  <div className="editor-head">
                    <div>
                      <p className="label">Coding</p>
                      <h5>{currentTask.title}</h5>
                      <div className="muted markdown">
                        <ReactMarkdown>
                          {currentTask.statement_md ||
                            currentTask.description_for_candidate ||
                            currentTask.description ||
                            "Напишите решение ниже"}
                        </ReactMarkdown>
                      </div>
                    </div>
                    <div style={{ display: "flex", gap: 8 }}>
                      <button
                        className="ghost"
                        onClick={() => submitCode(currentTask)}
                        disabled={!sessionId || isRunningTests || isScoring}
                      >
                        Submit code
                      </button>
                      <button
                        className="primary"
                        onClick={() => reviewCodeWithModel(currentTask)}
                        disabled={!sessionId || isRunningTests || isScoring}
                      >
                        Проверить моделью
                      </button>
                    </div>
                  </div>
                  <textarea value={codeDraft} onChange={(e) => setCodeDraft(e.target.value)} className="code tall" />
                </div>
              )}

              {currentTask?.type === "sql" && (
                <SqlSchemaPreview sqlScenario={activeSqlScenario} />
              )}
              {currentTask?.type === "sql" && (
                <div className="editor">
                  <div className="editor-head">
                    <div>
                      <p className="label">SQL</p>
                      <h5>{currentTask.title}</h5>
                      <p className="muted">
                        {currentTask.description_for_candidate || currentTask.description || "Напишите SQL-запрос"}
                      </p>
                    </div>
                      <div style={{ display: "flex", gap: 8 }}>
                      <button className="ghost" onClick={() => submitSql(currentTask)} disabled={!sessionId}>
                        Submit SQL
                      </button>
                      <button className="primary" onClick={() => reviewSqlWithModel(currentTask)} disabled={!sessionId}>
                        Проверить моделью
                      </button>
                    </div>
                  </div>
                  <textarea value={sqlDraft} onChange={(e) => setSqlDraft(e.target.value)} className="code tall" />
                </div>
              )}
              {isRunningTests && (
                <div className="log">
                  <p className="label">Статус</p>
                  <p>Идёт запуск тестов в песочнице...</p>
                </div>
              )}

              <RunCodeSummary runCodeResult={runCodeResult} />
              <RunSqlSummary sqlRunResult={sqlRunResult} />

              {executionLog && !runCodeResult && !sqlRunResult && (
                <div className="log">
                  <p className="label">Raw результат</p>
                  <pre>{executionLog}</pre>
                </div>
              )}

              {isScoring && (
                <div className="log">
                  <p className="label">Статус</p>
                  <p>Формируется оценка модели...</p>
                </div>
              )}
              {(scoreResult || agentFeedback) && (
                <div className="log">
                  <p className="label">Комментарий модели</p>

                  {scoreResult ? (
                    <>
                      <p><strong>Оценка:</strong> {scoreResult.points}/10</p>
                      {scoreResult.comment && (
                        <ReactMarkdown>
                          {currentTask?.type === "sql"
                            ? formatSqlScoreComment(scoreResult.comment)
                            : formatCodeScoreComment(scoreResult.comment)}
                        </ReactMarkdown>
                      )}
                    </>
                  ) : (
                    <ReactMarkdown>{agentFeedback || ""}</ReactMarkdown>
                  )}
                </div>
              )}
            </div>
          </section>
          )}
        </>
      )}

      {view === "admin" && (
        <section className="panel">
          <SectionHeader title="Админка" subtitle="Роли, сценарии, документы" />
          <div className="grid two-column">
            <div className="form">
              <h4>Новая роль</h4>
              <input
                placeholder="Название"
                value={adminRoleDraft.name}
                onChange={(e) => setAdminRoleDraft({ ...adminRoleDraft, name: e.target.value })}
              />
              <input
                placeholder="Slug"
                value={adminRoleDraft.slug}
                onChange={(e) => setAdminRoleDraft({ ...adminRoleDraft, slug: e.target.value })}
              />
              <textarea
                placeholder="Описание"
                value={adminRoleDraft.description}
                onChange={(e) => setAdminRoleDraft({ ...adminRoleDraft, description: e.target.value })}
              />
              <button onClick={submitRole}>Сохранить роль</button>
            </div>

            <div className="form">
              <h4>Новый сценарий</h4>
              <input
                placeholder="Role ID"
                value={adminScenarioDraft.role_id}
                onChange={(e) => setAdminScenarioDraft({ ...adminScenarioDraft, role_id: e.target.value })}
              />
              <input
                placeholder="Название"
                value={adminScenarioDraft.name}
                onChange={(e) => setAdminScenarioDraft({ ...adminScenarioDraft, name: e.target.value })}
              />
              <input
                placeholder="Slug"
                value={adminScenarioDraft.slug}
                onChange={(e) => setAdminScenarioDraft({ ...adminScenarioDraft, slug: e.target.value })}
              />
              <textarea
                placeholder="Описание"
                value={adminScenarioDraft.description}
                onChange={(e) => setAdminScenarioDraft({ ...adminScenarioDraft, description: e.target.value })}
              />
              <input
                placeholder="RAG Corpus ID"
                value={adminScenarioDraft.rag_corpus_id}
                onChange={(e) => setAdminScenarioDraft({ ...adminScenarioDraft, rag_corpus_id: e.target.value })}
              />
              <textarea
                placeholder="tasks в стандартизированном формате"
                value={adminScenarioDraft.tasks}
                onChange={(e) => setAdminScenarioDraft({ ...adminScenarioDraft, tasks: e.target.value })}
                className="code"
              />
              <button onClick={submitScenario}>Сохранить сценарий</button>
            </div>
          </div>
        </section>
      )}
    </div>
  );
}

export default App;
