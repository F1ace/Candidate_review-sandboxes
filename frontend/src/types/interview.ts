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
  tasks?: Task[];
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

export type ScoreResultPayload = {
  points: number;
  comment?: string;
  is_final?: boolean;
  task_id?: string;
};

export type ToolResultItem = {
  name?: string;
  result?: unknown;
};

export type PracticeAgentResponse = {
  reply?: string;
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
