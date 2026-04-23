import { useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import { API_BASE, fetchJson } from "./api/client";
import { RunCodeSummary } from "./components/RunCodeSummary";
import { RunSqlSummary } from "./components/RunSqlSummary";
import { SqlSchemaPreview } from "./components/SqlSchemaPreview";
import { SectionHeader } from "./components/SectionHeader";
import type {
  InterviewReport,
  Message,
  PracticeAgentResponse,
  RagCorpus,
  RagDocument,
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
import {
  renderInterviewReportErrorWindow,
  renderInterviewReportLoadingWindow,
  renderInterviewReportWindow,
} from "./utils/reportExport";
import { formatCodeScoreComment, formatSqlScoreComment } from "./utils/scoreFormatting";
import "./App.css";

const defaultTasks: Task[] = [
  {
    id: "T1",
    type: "theory",
    title: "Основы регрессии",
    description: "Объяснить, что такое линейная регрессия, и перечислить метрики качества.",
    max_points: 10,
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
    max_points: 10,
    sql_scenario_id: "ecommerce_basic",
  },
];

type StartSessionResponse = {
  id: string;
};

type AdminCorpusDraft = {
  name: string;
  description: string;
};

type DocumentUploadDraft = {
  corpus_id: string;
  file: File | null;
};

type PracticeTaskState = {
  draft: string;
  runResult: SandboxRunResult | SqlRunResult | null;
  scoreResult: ScoreResultPayload | null;
  agentFeedback: string | null;
  replySource: "model" | "fallback" | null;
  executionLog: string | null;
  locked: boolean;
};

type PracticeTaskStateMap = Record<string, PracticeTaskState>;

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null;

const getErrorMessage = (error: unknown): string => {
  if (error instanceof Error) return error.message;
  return String(error);
};

const getInitialDraftForTask = (task: Task): string => {
  if (task.type === "coding") {
    return task.starter_code || "# Напишите решение здесь\n";
  }

  if (task.type === "sql") {
    return task.description_for_candidate ? "-- Напишите SQL здесь\n" : "select 1;";
  }

  return "";
};

const getToolResults = (resp: PracticeAgentResponse): ToolResultItem[] =>
  Array.isArray(resp.tool_results) ? resp.tool_results : [];

const getReplySource = (resp: PracticeAgentResponse): "model" | "fallback" =>
  resp.reply_source === "fallback" ? "fallback" : "model";

const findLatestToolResult = (
  toolResults: ToolResultItem[],
  toolName: string,
  options?: { preferSuccessful?: boolean },
): ToolResultItem | undefined => {
  const preferSuccessful = options?.preferSuccessful === true;
  const matches = toolResults.filter((item) => item?.name === toolName || item?.tool === toolName);
  if (!matches.length) return undefined;

  if (preferSuccessful) {
    const latestSuccessful = [...matches]
      .reverse()
      .find((item) => isRecord(item?.result) && item.result.ok === true);
    if (latestSuccessful) return latestSuccessful;
  }

  return matches[matches.length - 1];
};

const getScoreResultPayload = (rawResult: unknown): ScoreResultPayload | null => {
  if (!isRecord(rawResult) || !rawResult.ok) return null;
  return {
    points: rawResult.points as number,
    comment: rawResult.comment as string | undefined,
    is_final: rawResult.is_final as boolean | undefined,
    task_id: rawResult.task_id as string | undefined,
  };
};

const getSqlRunResultPayload = (rawResult: unknown): SqlRunResult | null => {
  if (!isRecord(rawResult)) return null;

  const directResult = rawResult.result;
  if (isRecord(directResult) && (isRecord(directResult.result) || "sql_scenario_id" in directResult || "ok" in directResult)) {
    return directResult as SqlRunResult;
  }

  if ("result" in rawResult || "sql_scenario_id" in rawResult || "ok" in rawResult) {
    return rawResult as SqlRunResult;
  }

  return null;
};

const buildScenarioMaterialDrafts = (items: Scenario[]): Record<number, string> =>
  items.reduce<Record<number, string>>((acc, scenario) => {
    acc[scenario.id] = scenario.rag_corpus_id ? String(scenario.rag_corpus_id) : "";
    return acc;
  }, {});

const formatBytes = (value?: number | null): string => {
  if (!value || value <= 0) return "0 B";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
};

const formatDateTime = (value?: string | null): string => {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "short",
    timeStyle: "short",
  }).format(parsed);
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
  const [practiceTaskState, setPracticeTaskState] = useState<PracticeTaskStateMap>({});
  const [sqlScenarios, setSqlScenarios] = useState<SqlScenario[]>([]);
  const [isRunningTests, setIsRunningTests] = useState(false);
  const [isScoring, setIsScoring] = useState(false);
  const [adminRoleDraft, setAdminRoleDraft] = useState({ name: "", slug: "", description: "" });
  const [adminCorpusDraft, setAdminCorpusDraft] = useState<AdminCorpusDraft>({ name: "", description: "" });
  const [adminScenarioDraft, setAdminScenarioDraft] = useState({
    role_id: "",
    name: "",
    slug: "",
    description: "",
    difficulty: "junior",
    rag_corpus_id: "",
    tasks: JSON.stringify(defaultTasks, null, 2),
  });
  const [corpora, setCorpora] = useState<RagCorpus[]>([]);
  const [documentsByCorpus, setDocumentsByCorpus] = useState<Record<number, RagDocument[]>>({});
  const [selectedCorpusId, setSelectedCorpusId] = useState<number | null>(null);
  const [documentUploadDraft, setDocumentUploadDraft] = useState<DocumentUploadDraft>({
    corpus_id: "",
    file: null,
  });
  const [uploadInputKey, setUploadInputKey] = useState(0);
  const [uploadingDocument, setUploadingDocument] = useState(false);
  const [updatingScenarioId, setUpdatingScenarioId] = useState<number | null>(null);
  const [scenarioMaterialDrafts, setScenarioMaterialDrafts] = useState<Record<number, string>>({});
  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [currentTaskIndex, setCurrentTaskIndex] = useState(0);
  const [sessionScores, setSessionScores] = useState<Record<string, number>>({});
  const [sessionMode, setSessionMode] = useState<"theory" | "practice">("theory");
  const [isExportingReport, setIsExportingReport] = useState(false);

  const selectedScenarioObj = useMemo(
    () => scenarios.find((s) => s.id === selectedScenario),
    [selectedScenario, scenarios],
  );
  const corporaById = useMemo(
    () => new Map(corpora.map((corpus) => [corpus.id, corpus])),
    [corpora],
  );
  const rolesById = useMemo(
    () => new Map(roles.map((role) => [role.id, role])),
    [roles],
  );
  const selectedCorpus = useMemo(
    () => corpora.find((corpus) => corpus.id === selectedCorpusId) ?? null,
    [corpora, selectedCorpusId],
  );
  const selectedCorpusDocuments = useMemo(
    () => (selectedCorpusId ? documentsByCorpus[selectedCorpusId] ?? [] : []),
    [documentsByCorpus, selectedCorpusId],
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

const getPracticeTaskState = (task?: Task | null): PracticeTaskState => {
  if (!task) {
      return {
        draft: "",
        runResult: null,
        scoreResult: null,
        agentFeedback: null,
        replySource: null,
        executionLog: null,
        locked: false,
      };
  }

  return (
    practiceTaskState[task.id] || {
      draft: getInitialDraftForTask(task),
      runResult: null,
      scoreResult: null,
      agentFeedback: null,
      replySource: null,
      executionLog: null,
      locked: false,
    }
  );
};

const updatePracticeTaskState = (
  taskId: string,
  updater: (prev: PracticeTaskState) => PracticeTaskState,
) => {
  setPracticeTaskState((prev) => {
    const current =
      prev[taskId] || {
        draft: "",
        runResult: null,
        scoreResult: null,
        agentFeedback: null,
        replySource: null,
        executionLog: null,
        locked: false,
      };

    return {
      ...prev,
      [taskId]: updater(current),
    };
  });
};

const currentPracticeState = useMemo(
  () => getPracticeTaskState(currentTask),
  [currentTask, practiceTaskState],
);

const currentDraft = currentPracticeState.draft;
const currentRunResult = currentPracticeState.runResult;
const currentScoreResult = currentPracticeState.scoreResult;
const currentAgentFeedback = currentPracticeState.agentFeedback;
const currentReplySource = currentPracticeState.replySource;
const currentExecutionLog = currentPracticeState.executionLog;
const currentTaskLocked = currentPracticeState.locked;

const theoryCompleted = useMemo(() => {
  if (!theoryTasks.length) return false;
  return theoryTasks.every((t) => sessionScores[t.id] !== undefined);
}, [theoryTasks, sessionScores]);

  const scoredTasksCount = Object.keys(sessionScores).length;
  const canExportReport = Boolean(sessionId) && (messages.length > 0 || scoredTasksCount > 0);

  const firstPracticeIndex = useMemo(() => {
  // индекс первого задания НЕ theory (coding/sql)
  return orderedTasks.findIndex((t) => t.type !== "theory");
  }, [orderedTasks]);

  const loadCorpusDocuments = async (corpusId: number): Promise<RagDocument[]> => {
    try {
      return await fetchJson<RagDocument[]>(`/rag/corpora/${corpusId}/documents`);
    } catch (err) {
      console.error(`Не удалось загрузить документы для корпуса ${corpusId}`, err);
      return [];
    }
  };

  useEffect(() => {
    const bootstrap = async () => {
      try {
        const [rolesResp, scenariosResp, sqlScenariosResp, corporaResp] = await Promise.all([
          fetchJson<Role[]>("/roles").catch(() => [] as Role[]),
          fetchJson<Scenario[]>("/scenarios").catch(() => [] as Scenario[]),
          fetchJson<SqlScenario[]>("/sql-scenarios").catch(() => [] as SqlScenario[]),
          fetchJson<RagCorpus[]>("/rag/corpora").catch(() => [] as RagCorpus[]),
        ]);

        const nextRoles = rolesResp.length ? rolesResp : sampleRoles();
        const nextScenarios = scenariosResp.length ? scenariosResp : sampleScenarios();

        setRoles(nextRoles);
        setScenarios(nextScenarios);
        setSqlScenarios(sqlScenariosResp);
        setCorpora(corporaResp);
        setScenarioMaterialDrafts(buildScenarioMaterialDrafts(nextScenarios));

        if (corporaResp.length) {
          const corpusDocuments = await Promise.all(
            corporaResp.map(async (corpus) => [corpus.id, await loadCorpusDocuments(corpus.id)] as const),
          );
          setDocumentsByCorpus(Object.fromEntries(corpusDocuments));
          setSelectedCorpusId(corporaResp[0].id);
          setDocumentUploadDraft({ corpus_id: String(corporaResp[0].id), file: null });
        } else {
          setDocumentsByCorpus({});
          setSelectedCorpusId(null);
          setDocumentUploadDraft({ corpus_id: "", file: null });
        }
      } catch (err) {
        console.error(err);
        setRoles(sampleRoles());
        setScenarios(sampleScenarios());
        setSqlScenarios([]);
        setCorpora([]);
        setDocumentsByCorpus({});
        setSelectedCorpusId(null);
        setDocumentUploadDraft({ corpus_id: "", file: null });
        setScenarioMaterialDrafts(buildScenarioMaterialDrafts(sampleScenarios()));
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
    if (!corpora.length) {
      if (selectedCorpusId !== null) {
        setSelectedCorpusId(null);
      }
      return;
    }

    if (!selectedCorpusId || !corpora.some((corpus) => corpus.id === selectedCorpusId)) {
      setSelectedCorpusId(corpora[0].id);
    }
  }, [corpora, selectedCorpusId]);

  useEffect(() => {
    if (selectedCorpusId && !documentUploadDraft.corpus_id) {
      setDocumentUploadDraft((prev) => ({ ...prev, corpus_id: String(selectedCorpusId) }));
    }
  }, [documentUploadDraft.corpus_id, selectedCorpusId]);

  useEffect(() => {
    if (!currentTask || currentTask.type === "theory") return;

    setPracticeTaskState((prev) => {
      if (prev[currentTask.id]) return prev;

      return {
        ...prev,
        [currentTask.id]: {
          draft: getInitialDraftForTask(currentTask),
          runResult: null,
          scoreResult: null,
          agentFeedback: null,
          replySource: null,
          executionLog: null,
          locked: false,
        },
      };
    });
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
        { id: "T-metrics", type: "theory", title: "Метрики A/B", max_points: 10, hints_allowed: false },
        {
          id: "SQL-ab",
          type: "sql",
          title: "Конверсия по когорте",
          description_for_candidate: "Напишите запрос конверсии по дню регистрации.",
          sql_scenario_id: "ab_product",
          max_points: 10,
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
        { id: "T-REST", type: "theory", title: "PUT vs PATCH идемпотентность", max_points: 10 },
        {
          id: "C-BE",
          type: "coding",
          language: "python",
          title: "Очередь задач",
          description_for_candidate: "Реализуйте очередь с ack/nack.",
          tests_id: "queue_basic",
          max_points: 10,
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
        { id: "T-circuit", type: "theory", title: "Circuit breaker", max_points: 10, hints_allowed: false },
        {
          id: "C-rate",
          type: "coding",
          language: "python",
          title: "Rate limiter",
          description_for_candidate: "Сделайте токен-бакет.",
          tests_id: "rate_limiter",
          max_points: 10,
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
        { id: "T-de-incr", type: "theory", title: "Инкрементальные загрузки", max_points: 10, hints_allowed: false },
        {
          id: "SQL-de-agg",
          type: "sql",
          title: "Агрегация событий",
          description_for_candidate: "Посчитайте DAU по регионам из таблицы events.",
          sql_scenario_id: "events_basic",
          max_points: 10,
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
        { id: "T-scd", type: "theory", title: "SCD типы", max_points: 10 },
        {
          id: "SQL-scd",
          type: "sql",
          title: "SCD Type 2 обновление",
          description_for_candidate: "Напишите SQL, который добавляет новую версию записи клиента.",
          sql_scenario_id: "scd_customers",
          max_points: 10,
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
    setPracticeTaskState({});

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

  const reviewCodeWithModel = async (task?: Task) => {
    if (!sessionId || !task) {
      if (task?.id) {
        updatePracticeTaskState(task.id, (prev) => ({
          ...prev,
          executionLog: "Нет активной сессии или задания",
        }));
      }
      return;
    }

    const taskState = getPracticeTaskState(task);

    updatePracticeTaskState(task.id, (prev) => ({
      ...prev,
      agentFeedback: null,
      scoreResult: null,
      replySource: null,
      runResult: null,
      executionLog: null,
    }));
    setIsScoring(false);
    setIsRunningTests(true);

    try {
      const resp = await fetchJson<PracticeAgentResponse>(`/sessions/${sessionId}/practice/code`, {
        method: "POST",
        body: JSON.stringify({
          task_id: task.id,
          language: task.language || "python",
          code: taskState.draft,
        }),
      });

      const toolResults = getToolResults(resp);
      const runCodeTool = findLatestToolResult(toolResults, "run_code");
      const scoreTool = findLatestToolResult(toolResults, "score_task", { preferSuccessful: true });
      const runCodeToolResult = isRecord(runCodeTool?.result) ? runCodeTool.result : null;
      const nestedRunCodeResult = runCodeToolResult?.result;
      const scorePayload = getScoreResultPayload(scoreTool?.result);

      let nextRunResult: SandboxRunResult | null = null;
      let nextExecutionLog: string | null = null;

      if (nestedRunCodeResult) {
        nextRunResult = nestedRunCodeResult as SandboxRunResult;
        nextExecutionLog = JSON.stringify(nestedRunCodeResult, null, 2);
      } else if (runCodeTool?.result) {
        nextExecutionLog = JSON.stringify(runCodeTool.result, null, 2);
      } else {
        nextExecutionLog = JSON.stringify(resp, null, 2);
      }

      setIsRunningTests(false);
      setIsScoring(true);

      updatePracticeTaskState(task.id, (prev) => ({
        ...prev,
        runResult: nextRunResult,
        executionLog: nextExecutionLog,
        scoreResult: scorePayload,
        agentFeedback: normalizePracticeReply(resp.reply || "Нет ответа модели"),
        replySource: getReplySource(resp),
        locked: true,
      }));
    } catch (err: unknown) {
      updatePracticeTaskState(task.id, (prev) => ({
        ...prev,
        agentFeedback: `Ошибка проверки: ${getErrorMessage(err)}`,
        replySource: null,
      }));
    } finally {
      setIsRunningTests(false);
      setIsScoring(false);
    }
  };

  const reviewSqlWithModel = async (task?: Task) => {
    if (!sessionId || !task) {
      if (task?.id) {
        updatePracticeTaskState(task.id, (prev) => ({
          ...prev,
          executionLog: "Нет активной сессии или задания",
        }));
      }
      return;
    }

    const taskState = getPracticeTaskState(task);

    updatePracticeTaskState(task.id, (prev) => ({
      ...prev,
      agentFeedback: null,
      scoreResult: null,
      replySource: null,
      runResult: null,
      executionLog: null,
    }));
    setIsScoring(false);
    setIsRunningTests(true);

    try {
      const resp = await fetchJson<PracticeAgentResponse>(`/sessions/${sessionId}/practice/sql`, {
        method: "POST",
        body: JSON.stringify({
          task_id: task.id,
          sql_scenario_id: task.sql_scenario_id || "",
          query: taskState.draft,
        }),
      });

      const toolResults = getToolResults(resp);
      const runSqlTool = findLatestToolResult(toolResults, "run_sql");
      const scoreTool = findLatestToolResult(toolResults, "score_task", { preferSuccessful: true });

      const normalizedSqlResult = getSqlRunResultPayload(runSqlTool?.result);
      const scorePayload = getScoreResultPayload(scoreTool?.result);

      setIsRunningTests(false);
      setIsScoring(true);

      updatePracticeTaskState(task.id, (prev) => ({
        ...prev,
        runResult: normalizedSqlResult ?? null,
        executionLog: normalizedSqlResult
          ? null
          : runSqlTool?.result
            ? JSON.stringify(runSqlTool.result, null, 2)
            : JSON.stringify(resp, null, 2),
        scoreResult: scorePayload,
        agentFeedback: normalizePracticeReply(resp.reply || "Нет ответа модели"),
        replySource: getReplySource(resp),
        locked: true,
      }));
    } catch (err: unknown) {
      updatePracticeTaskState(task.id, (prev) => ({
        ...prev,
        runResult: null,
        scoreResult: null,
        agentFeedback: `Ошибка SQL-проверки: ${getErrorMessage(err)}`,
        replySource: null,
      }));
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

  const submitCorpus = async () => {
    if (!adminCorpusDraft.name.trim()) {
      setStatus("Укажите название материала");
      return;
    }

    try {
      const resp = await fetchJson<RagCorpus>("/rag/corpora", {
        method: "POST",
        body: JSON.stringify(adminCorpusDraft),
      });
      setCorpora((prev) => [...prev, resp]);
      setDocumentsByCorpus((prev) => ({ ...prev, [resp.id]: [] }));
      setSelectedCorpusId(resp.id);
      setDocumentUploadDraft({ corpus_id: String(resp.id), file: null });
      setAdminCorpusDraft({ name: "", description: "" });
      setStatus("Материал создан");
    } catch (err: unknown) {
      setStatus(`Ошибка материала: ${getErrorMessage(err)}`);
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
      setScenarioMaterialDrafts((prev) => ({
        ...prev,
        [resp.id]: resp.rag_corpus_id ? String(resp.rag_corpus_id) : "",
      }));
      setAdminScenarioDraft((prev) => ({
        ...prev,
        name: "",
        slug: "",
        description: "",
        difficulty: "junior",
        rag_corpus_id: "",
        tasks: JSON.stringify(defaultTasks, null, 2),
      }));
      setStatus("Сценарий сохранен");
    } catch (err: unknown) {
      setStatus(`Ошибка сценария: ${getErrorMessage(err)}`);
    }
  };

  const uploadDocumentToCorpus = async () => {
    const targetCorpusId = Number(documentUploadDraft.corpus_id || selectedCorpusId);
    if (!targetCorpusId) {
      setStatus("Сначала выберите материал");
      return;
    }
    if (!documentUploadDraft.file) {
      setStatus("Выберите PDF-файл для загрузки");
      return;
    }

    const file = documentUploadDraft.file;
    const isPdf = file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf");
    if (!isPdf) {
      setStatus("Интерфейс принимает только PDF-файлы");
      return;
    }

    setUploadingDocument(true);
    try {
      const formData = new FormData();
      formData.append("file", file);

      const resp = await fetchJson<RagDocument>(`/rag/corpora/${targetCorpusId}/documents/upload`, {
        method: "POST",
        body: formData,
      });

      setDocumentsByCorpus((prev) => ({
        ...prev,
        [targetCorpusId]: [resp, ...(prev[targetCorpusId] ?? [])],
      }));
      setSelectedCorpusId(targetCorpusId);
      setDocumentUploadDraft({ corpus_id: String(targetCorpusId), file: null });
      setUploadInputKey((prev) => prev + 1);
      setStatus("PDF загружен в материал");
    } catch (err: unknown) {
      setStatus(`Ошибка загрузки PDF: ${getErrorMessage(err)}`);
    } finally {
      setUploadingDocument(false);
    }
  };

  const attachCorpusToScenario = async (scenario: Scenario) => {
    const nextCorpusId = scenarioMaterialDrafts[scenario.id]
      ? Number(scenarioMaterialDrafts[scenario.id])
      : null;

    setUpdatingScenarioId(scenario.id);
    try {
      const resp = await fetchJson<Scenario>(`/scenarios/${scenario.id}`, {
        method: "PUT",
        body: JSON.stringify({ rag_corpus_id: nextCorpusId }),
      });
      setScenarios((prev) => prev.map((item) => (item.id === resp.id ? resp : item)));
      setScenarioMaterialDrafts((prev) => ({
        ...prev,
        [resp.id]: resp.rag_corpus_id ? String(resp.rag_corpus_id) : "",
      }));
      setStatus(`Материал привязан к сценарию "${resp.name}"`);
    } catch (err: unknown) {
      setStatus(`Ошибка привязки материала: ${getErrorMessage(err)}`);
    } finally {
      setUpdatingScenarioId(null);
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
    if (task.type === "theory") {
      return "Ответьте в чате. В теоретическом блоке модель работает как интервьюер и не подсказывает правильный ответ.";
    }
    if (task.type === "coding") {
      return "Напишите код ниже. После проверки моделью ответ будет зафиксирован и редактор заблокируется.";
    }
    if (task.type === "sql") {
      return "Напишите SQL ниже. После проверки моделью ответ будет зафиксирован и редактор заблокируется.";
    }
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

  const exportInterviewReport = async () => {
    if (!sessionId) {
      setStatus("Сначала запустите интервью");
      return;
    }

    const reportWindow = window.open("", "_blank");
    if (!reportWindow) {
      setStatus("Разрешите всплывающее окно для выгрузки PDF-отчёта");
      return;
    }

    renderInterviewReportLoadingWindow(reportWindow);
    setIsExportingReport(true);

    try {
      const report = await fetchJson<InterviewReport>(`/sessions/${sessionId}/report`, {
        method: "POST",
      });
      renderInterviewReportWindow(reportWindow, report);
      setStatus("Отчёт подготовлен");
    } catch (err: unknown) {
      const message = getErrorMessage(err);
      renderInterviewReportErrorWindow(reportWindow, message);
      setStatus(`Не удалось собрать отчёт: ${message}`);
    } finally {
      setIsExportingReport(false);
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
            <button data-testid="start-session-button" className="primary" disabled={loading} onClick={startSession}>
              {sessionId ? "Начать" : "Создать сессию и начать"}
            </button>
            )}
            <button
              className="ghost"
              data-testid="admin-toggle"
              onClick={() => setView(view === "admin" ? "landing" : "admin")}
            >
              {view === "admin" ? "Вернуться" : "Добавить документ"}
            </button>
            {status && <span className="pill">{status}</span>}
          </div>
        </div>
        <div className="status-card">
          <p className="muted">Сессия</p>
          <h3>{sessionId || "не создана"}</h3>
          <p className="muted">{selectedScenarioObj?.name || "Выберите сценарий"}</p>
          {view === "session" && (
            <div className="status-card-actions">
              <p className="muted">Оценено блоков: {scoredTasksCount}/{orderedTasks.length || 0}</p>
              <button
                className="primary status-card-button"
                onClick={exportInterviewReport}
                disabled={!canExportReport || isExportingReport}
              >
                {isExportingReport ? "Готовим PDF..." : "Выгрузить PDF-отчёт"}
              </button>
            </div>
          )}
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
                        {scenario.rag_corpus_id && (
                          <span className="chip chip-secondary">
                            RAG · {corporaById.get(scenario.rag_corpus_id)?.name || `Материал #${scenario.rag_corpus_id}`}
                          </span>
                        )}
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
              data-testid="practice-mode-toggle"
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
                        data-testid="review-code-button"
                        className="primary"
                        onClick={() => reviewCodeWithModel(currentTask)}
                        disabled={!sessionId || isRunningTests || isScoring || currentTaskLocked}
                      >
                        Проверить моделью
                      </button>
                    </div>
                  </div>

                  <textarea
                    data-testid="coding-draft-input"
                    value={currentDraft}
                    onChange={(e) => {
                      if (!currentTask || currentTask.type !== "coding") return;
                      updatePracticeTaskState(currentTask.id, (prev) => ({
                        ...prev,
                        draft: e.target.value,
                      }));
                    }}
                    className="code tall"
                    disabled={currentTaskLocked}
                  />

                  {currentTaskLocked && (
                    <p className="muted">
                      Ответ зафиксирован после проверки моделью и больше не может быть изменён.
                    </p>
                  )}
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
                    </div>
                    <div style={{ display: "flex", gap: 8 }}>
                      <button
                        data-testid="review-sql-button"
                        className="primary"
                        onClick={() => reviewSqlWithModel(currentTask)}
                        disabled={!sessionId || isRunningTests || isScoring || currentTaskLocked}
                      >
                        Проверить моделью
                      </button>
                    </div>
                  </div>

                  <textarea
                    data-testid="sql-draft-input"
                    value={currentDraft}
                    onChange={(e) => {
                      if (!currentTask || currentTask.type !== "sql") return;
                      updatePracticeTaskState(currentTask.id, (prev) => ({
                        ...prev,
                        draft: e.target.value,
                      }));
                    }}
                    className="code tall"
                    disabled={currentTaskLocked}
                  />

                  {currentTaskLocked && (
                    <p className="muted">
                      Ответ зафиксирован после проверки моделью и больше не может быть изменён.
                    </p>
                  )}
                </div>
              )}
              {isRunningTests && (
                <div className="log">
                  <p className="label">Статус</p>
                  <p>Идёт запуск тестов в песочнице...</p>
                </div>
              )}

              {currentTask?.type === "coding" && (
                <>
                  <RunCodeSummary
                    runCodeResult={currentRunResult as SandboxRunResult | null}
                  />

                  {currentExecutionLog && !(currentRunResult as SandboxRunResult | null) && (
                    <div className="log">
                      <p className="label">Raw результат</p>
                      <pre>{currentExecutionLog}</pre>
                    </div>
                  )}

                  {isScoring && (
                    <div className="log">
                      <p className="label">Статус</p>
                      <p>Формируется оценка модели...</p>
                    </div>
                  )}

                  {(currentScoreResult || currentAgentFeedback) && (
                    <div className="log">
                      <p className="label">Комментарий модели</p>

                      {currentScoreResult ? (
                        <>
                          <p><strong>Оценка:</strong> {currentScoreResult.points}/{currentTask?.max_points ?? 10}</p>
                          {currentReplySource !== "fallback" && currentAgentFeedback ? (
                            <ReactMarkdown>{currentAgentFeedback}</ReactMarkdown>
                          ) : currentScoreResult.comment ? (
                            <ReactMarkdown>{formatCodeScoreComment(currentScoreResult.comment)}</ReactMarkdown>
                          ) : currentAgentFeedback ? (
                            <ReactMarkdown>{currentAgentFeedback}</ReactMarkdown>
                          ) : null}
                        </>
                      ) : (
                        <ReactMarkdown>{currentAgentFeedback || ""}</ReactMarkdown>
                      )}
                    </div>
                  )}
                </>
              )}

              {currentTask?.type === "sql" && (
                <>
                  <RunSqlSummary
                    sqlRunResult={currentRunResult as SqlRunResult | null}
                  />

                  {currentExecutionLog && !(currentRunResult as SqlRunResult | null) && (
                    <div className="log">
                      <p className="label">Raw результат</p>
                      <pre>{currentExecutionLog}</pre>
                    </div>
                  )}

                  {isScoring && (
                    <div className="log">
                      <p className="label">Статус</p>
                      <p>Формируется оценка модели...</p>
                    </div>
                  )}

                  {(currentScoreResult || currentAgentFeedback) && (
                    <div className="log">
                      <p className="label">Комментарий модели</p>

                      {currentScoreResult ? (
                        <>
                          <p><strong>Оценка:</strong> {currentScoreResult.points}/{currentTask?.max_points ?? 10}</p>
                          {currentReplySource !== "fallback" && currentAgentFeedback ? (
                            <ReactMarkdown>{currentAgentFeedback}</ReactMarkdown>
                          ) : currentScoreResult.comment ? (
                            <ReactMarkdown>{formatSqlScoreComment(currentScoreResult.comment)}</ReactMarkdown>
                          ) : currentAgentFeedback ? (
                            <ReactMarkdown>{formatSqlScoreComment(currentAgentFeedback)}</ReactMarkdown>
                          ) : null}
                        </>
                      ) : (
                        <ReactMarkdown>{formatSqlScoreComment(currentAgentFeedback || "")}</ReactMarkdown>
                      )}
                    </div>
                  )}
                </>
              )}

            </div>
          </section>
          )}
        </>
      )}

      {view === "admin" && (
        <section className="panel">
          <SectionHeader title="Админка" subtitle="Материалы, PDF-документы и привязка к сценариям" />
          <div className="admin-grid">
            <div className="admin-column">
              <div className="panel-block form">
                <h4>Новая роль</h4>
                <label className="field">
                  <span className="field-label">Название роли</span>
                  <input
                    data-testid="admin-role-name"
                    placeholder="Например, Backend"
                    value={adminRoleDraft.name}
                    onChange={(e) => setAdminRoleDraft({ ...adminRoleDraft, name: e.target.value })}
                  />
                </label>
                <label className="field">
                  <span className="field-label">Slug</span>
                  <input
                    placeholder="backend"
                    value={adminRoleDraft.slug}
                    onChange={(e) => setAdminRoleDraft({ ...adminRoleDraft, slug: e.target.value })}
                  />
                </label>
                <label className="field">
                  <span className="field-label">Описание</span>
                  <textarea
                    placeholder="Коротко опишите область интервью"
                    value={adminRoleDraft.description}
                    onChange={(e) => setAdminRoleDraft({ ...adminRoleDraft, description: e.target.value })}
                  />
                </label>
                <button type="button" onClick={submitRole}>Сохранить роль</button>
              </div>

              <div className="panel-block form">
                <h4>Новый материал</h4>
                <label className="field">
                  <span className="field-label">Название материала</span>
                  <input
                    data-testid="material-name-input"
                    placeholder="Например, HTTP handbook"
                    value={adminCorpusDraft.name}
                    onChange={(e) => setAdminCorpusDraft({ ...adminCorpusDraft, name: e.target.value })}
                  />
                </label>
                <label className="field">
                  <span className="field-label">Описание</span>
                  <textarea
                    placeholder="Какие знания лежат внутри материала"
                    value={adminCorpusDraft.description}
                    onChange={(e) => setAdminCorpusDraft({ ...adminCorpusDraft, description: e.target.value })}
                  />
                </label>
                <button type="button" data-testid="create-material-button" onClick={submitCorpus}>
                  Создать материал
                </button>
              </div>

              <div className="panel-block">
                <div className="panel-block-head">
                  <div>
                    <p className="label">Материалы</p>
                    <h4>{corpora.length ? `Доступно ${corpora.length}` : "Материалов пока нет"}</h4>
                    <p className="muted">Выберите материал, чтобы загрузить в него PDF и посмотреть состав.</p>
                  </div>
                </div>
                <div className="resource-list">
                  {corpora.length ? (
                    corpora.map((corpus) => (
                      <button
                        type="button"
                        key={corpus.id}
                        data-testid={`material-card-${corpus.id}`}
                        className={`resource-card ${selectedCorpusId === corpus.id ? "active" : ""}`}
                        onClick={() => {
                          setSelectedCorpusId(corpus.id);
                          setDocumentUploadDraft((prev) => ({ ...prev, corpus_id: String(corpus.id) }));
                        }}
                      >
                        <div className="resource-card-head">
                          <strong>{corpus.name}</strong>
                          <span className="pill small">{documentsByCorpus[corpus.id]?.length ?? 0} док.</span>
                        </div>
                        <p className="muted">{corpus.description || "Без описания"}</p>
                        <p className="muted">ID {corpus.id}</p>
                      </button>
                    ))
                  ) : (
                    <p className="muted">
                      Создайте первый материал, затем загрузите в него PDF и прикрепите материал к сценарию.
                    </p>
                  )}
                </div>
              </div>
            </div>

            <div className="admin-column">
              <div className="panel-block">
                <div className="panel-block-head">
                  <div>
                    <p className="label">PDF</p>
                    <h4>{selectedCorpus ? `Документы материала "${selectedCorpus.name}"` : "Выберите материал"}</h4>
                    <p className="muted">
                      {selectedCorpus?.description || "После выбора материала здесь появится загрузка PDF и список файлов."}
                    </p>
                  </div>
                </div>

                {selectedCorpus ? (
                  <>
                    <div className="form compact-form">
                      <label className="field">
                        <span className="field-label">Куда загружать</span>
                        <select
                          data-testid="material-select-for-upload"
                          value={documentUploadDraft.corpus_id || String(selectedCorpus.id)}
                          onChange={(e) =>
                            setDocumentUploadDraft((prev) => ({ ...prev, corpus_id: e.target.value }))
                          }
                        >
                          {corpora.map((corpus) => (
                            <option key={corpus.id} value={corpus.id}>
                              {corpus.name}
                            </option>
                          ))}
                        </select>
                      </label>
                      <label className="field">
                        <span className="field-label">PDF-файл</span>
                        <input
                          key={uploadInputKey}
                          data-testid="pdf-upload-input"
                          type="file"
                          accept=".pdf,application/pdf"
                          onChange={(e) =>
                            setDocumentUploadDraft((prev) => ({
                              ...prev,
                              file: e.target.files?.[0] ?? null,
                            }))
                          }
                        />
                      </label>
                      <button
                        type="button"
                        className="primary"
                        data-testid="upload-pdf-button"
                        onClick={uploadDocumentToCorpus}
                        disabled={uploadingDocument}
                      >
                        {uploadingDocument ? "Загрузка..." : "Загрузить PDF"}
                      </button>
                    </div>

                    <div className="document-list">
                      {selectedCorpusDocuments.length ? (
                        selectedCorpusDocuments.map((document) => (
                          <div key={document.id} className="document-card">
                            <div className="resource-card-head">
                              <strong>{document.filename}</strong>
                              <span className="pill small">{document.status}</span>
                            </div>
                            <p className="muted">
                              {document.content_type || "application/pdf"} · {formatBytes(document.size_bytes)}
                            </p>
                            <p className="muted">
                              Загружен: {formatDateTime(document.ingested_at || document.created_at)}
                            </p>
                          </div>
                        ))
                      ) : (
                        <p className="muted">В этом материале пока нет документов.</p>
                      )}
                    </div>
                  </>
                ) : (
                  <p className="muted">Слева пока нет выбранного материала.</p>
                )}
              </div>

              <div className="panel-block form">
                <h4>Новый сценарий</h4>
                <label className="field">
                  <span className="field-label">Роль</span>
                  <select
                    data-testid="scenario-role-select"
                    value={adminScenarioDraft.role_id}
                    onChange={(e) => setAdminScenarioDraft({ ...adminScenarioDraft, role_id: e.target.value })}
                  >
                    <option value="">Выберите роль</option>
                    {roles.map((role) => (
                      <option key={role.id} value={role.id}>
                        {role.name}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field">
                  <span className="field-label">Название сценария</span>
                  <input
                    placeholder="Например, Backend HTTP theory"
                    value={adminScenarioDraft.name}
                    onChange={(e) => setAdminScenarioDraft({ ...adminScenarioDraft, name: e.target.value })}
                  />
                </label>
                <label className="field">
                  <span className="field-label">Slug</span>
                  <input
                    placeholder="backend-http-theory"
                    value={adminScenarioDraft.slug}
                    onChange={(e) => setAdminScenarioDraft({ ...adminScenarioDraft, slug: e.target.value })}
                  />
                </label>
                <label className="field">
                  <span className="field-label">Описание</span>
                  <textarea
                    placeholder="Что проверяет сценарий"
                    value={adminScenarioDraft.description}
                    onChange={(e) => setAdminScenarioDraft({ ...adminScenarioDraft, description: e.target.value })}
                  />
                </label>
                <label className="field">
                  <span className="field-label">Сложность</span>
                  <select
                    value={adminScenarioDraft.difficulty}
                    onChange={(e) => setAdminScenarioDraft({ ...adminScenarioDraft, difficulty: e.target.value })}
                  >
                    <option value="junior">junior</option>
                    <option value="middle">middle</option>
                    <option value="senior">senior</option>
                  </select>
                </label>
                <label className="field">
                  <span className="field-label">Материал</span>
                  <select
                    data-testid="scenario-material-select"
                    value={adminScenarioDraft.rag_corpus_id}
                    onChange={(e) => setAdminScenarioDraft({ ...adminScenarioDraft, rag_corpus_id: e.target.value })}
                  >
                    <option value="">Без материалов</option>
                    {corpora.map((corpus) => (
                      <option key={corpus.id} value={corpus.id}>
                        {corpus.name}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field">
                  <span className="field-label">Tasks JSON</span>
                  <textarea
                    placeholder="tasks в стандартизированном формате"
                    value={adminScenarioDraft.tasks}
                    onChange={(e) => setAdminScenarioDraft({ ...adminScenarioDraft, tasks: e.target.value })}
                    className="code"
                  />
                </label>
                <button type="button" data-testid="create-scenario-button" onClick={submitScenario}>
                  Сохранить сценарий
                </button>
              </div>

              <div className="panel-block">
                <div className="panel-block-head">
                  <div>
                    <p className="label">Привязка</p>
                    <h4>Существующие сценарии</h4>
                    <p className="muted">Меняйте связанный материал без ручного ввода corpus ID.</p>
                  </div>
                </div>

                <div className="scenario-attachment-list">
                  {scenarios.length ? (
                    scenarios.map((scenario) => (
                      <div key={scenario.id} className="scenario-attachment">
                        <div>
                          <strong>{scenario.name}</strong>
                          <p className="muted">
                            {rolesById.get(scenario.role_id)?.name || `Role #${scenario.role_id}`} · {scenario.slug}
                          </p>
                        </div>
                        <div className="scenario-attachment-controls">
                          <select
                            data-testid={`scenario-attachment-select-${scenario.id}`}
                            value={scenarioMaterialDrafts[scenario.id] ?? ""}
                            onChange={(e) =>
                              setScenarioMaterialDrafts((prev) => ({
                                ...prev,
                                [scenario.id]: e.target.value,
                              }))
                            }
                          >
                            <option value="">Без материалов</option>
                            {corpora.map((corpus) => (
                              <option key={corpus.id} value={corpus.id}>
                                {corpus.name}
                              </option>
                            ))}
                          </select>
                          <button
                            type="button"
                            className="ghost"
                            data-testid={`attach-material-button-${scenario.id}`}
                            onClick={() => attachCorpusToScenario(scenario)}
                            disabled={updatingScenarioId === scenario.id}
                          >
                            {updatingScenarioId === scenario.id ? "Сохраняю..." : "Привязать"}
                          </button>
                        </div>
                      </div>
                    ))
                  ) : (
                    <p className="muted">Сценариев пока нет.</p>
                  )}
                </div>
              </div>
            </div>
          </div>
        </section>
      )}
    </div>
  );
}

export default App;
