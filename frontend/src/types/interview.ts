export type Task = {
  id: string;
  type: "theory" | "coding" | "sql";
  title: string;
  description?: string;
  description_for_candidate?: string;
  max_points?: number;
  tests_id?: string;
  sql_scenario_id?: string;
  language?: string;
  hints_allowed?: boolean;
  statement_md?: string;
  starter_code?: string;
};

export type Role = {
  id: number;
  name: string;
  slug: string;
  description?: string;
};

export type Scenario = {
  id: number;
  role_id: number;
  name: string;
  slug: string;
  description?: string;
  difficulty?: string;
  rag_corpus_id?: number | null;
  tasks?: Task[];
};

export type RagCorpus = {
  id: number;
  name: string;
  description?: string | null;
};

export type RagDocument = {
  id: number;
  rag_corpus_id: number;
  filename: string;
  content: string;
  content_type?: string | null;
  storage_bucket?: string | null;
  object_key?: string | null;
  size_bytes?: number | null;
  checksum_sha256?: string | null;
  status: string;
  created_at: string;
  ingested_at?: string | null;
  metadata?: Record<string, unknown> | null;
};

export type Message = {
  sender: "candidate" | "model" | "system";
  text: string;
  created_at?: string;
  task_id?: string | null;
};

export type View = "landing" | "session" | "admin";

export type SandboxValidationMode = "custom_checker" | "expected_error" | "exact" | string;

export type SandboxTestResult = {
  code?: string;
  name?: string;
  description?: string | null;
  passed?: boolean;
  error?: unknown;
  validation_mode?: SandboxValidationMode;
  expected?: unknown;
  actual?: unknown;
};

export type SandboxRunResult = {
  success?: boolean;
  stdout?: string;
  stderr?: string;
  exit_code?: number;
  details?: string | null;
  tests_total?: number;
  tests_passed?: number;
  test_results?: SandboxTestResult[];
};

export type SqlRunResultPayload = {
  success?: boolean;
  error?: string | null;
  columns?: string[];
  rows?: unknown[][];
};

export type SqlRunResult = {
  ok?: boolean;
  task_id?: string;
  sql_scenario_id?: string;
  result?: SqlRunResultPayload;
};

export type SqlScenario = {
  id: number;
  name: string;
  description?: string | null;
  db_schema?: string | null;
  reference_solutions?: Record<string, unknown> | null;
};

export type ScoreResultPayload = {
  points: number;
  comment?: string;
  is_final?: boolean;
  task_id?: string;
};

export type InterviewReportSection = {
  title: string;
  summary: string;
  highlights: string[];
};

export type InterviewReportTask = {
  task_id: string;
  title: string;
  task_type: string;
  score?: number | null;
  max_points: number;
  ratio?: number | null;
  summary: string;
  highlights: string[];
  score_comment?: string | null;
};

export type InterviewReport = {
  session_id: string;
  generated_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  duration_minutes: number;
  candidate_id?: string | null;
  role_name: string;
  role_slug: string;
  scenario_name: string;
  scenario_slug: string;
  difficulty?: string | null;
  headline: string;
  executive_summary: string;
  overall_assessment: string;
  closing_note: string;
  recommendation_label: string;
  recommendation_summary: string;
  generation_mode: string;
  overall_score: number;
  overall_max: number;
  overall_ratio?: number | null;
  scored_tasks: number;
  total_tasks: number;
  candidate_message_count: number;
  model_message_count: number;
  strengths: string[];
  growth_areas: string[];
  sections: InterviewReportSection[];
  task_breakdown: InterviewReportTask[];
};

export type ToolResultItem = {
  name?: string;
  tool?: string;
  result?: unknown;
};

export type PracticeAgentResponse = {
  reply?: string;
  reply_source?: "model" | "fallback";
  tool_results?: ToolResultItem[];
};

export type SessionStatePayload = {
  scores?: Record<string, number>;
};

export type StreamEventPayload = {
  type?: "token" | "error" | "done" | string;
  content?: string;
  detail?: string;
};
