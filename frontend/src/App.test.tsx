import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";

const jsonResponse = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });

const toPath = (input: RequestInfo | URL): string => {
  const raw =
    typeof input === "string"
      ? input
      : input instanceof URL
        ? input.toString()
        : input.url;
  return raw.replace("http://127.0.0.1:8000", "");
};

const createFetchMock = () => {
  let corpora = [{ id: 10, name: "HTTP PDF", description: "material docs" }];
  const documentsByCorpus: Record<number, unknown[]> = { 10: [] };
  let scenarios = [
    {
      id: 11,
      role_id: 1,
      name: "Backend Theory",
      slug: "backend-theory",
      description: "Checks HTTP facts",
      difficulty: "middle",
      rag_corpus_id: null,
      tasks: [],
    },
  ];

  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = toPath(input);
    const method = (init?.method ?? "GET").toUpperCase();

    if (method === "GET" && path === "/roles") {
      return jsonResponse([{ id: 1, name: "Backend", slug: "backend", description: "APIs" }]);
    }
    if (method === "GET" && path === "/scenarios") {
      return jsonResponse(scenarios);
    }
    if (method === "GET" && path === "/sql-scenarios") {
      return jsonResponse([]);
    }
    if (method === "GET" && path === "/rag/corpora") {
      return jsonResponse(corpora);
    }

    const documentsMatch = path.match(/^\/rag\/corpora\/(\d+)\/documents$/);
    if (method === "GET" && documentsMatch) {
      return jsonResponse(documentsByCorpus[Number(documentsMatch[1])] ?? []);
    }

    if (method === "POST" && path === "/rag/corpora") {
      const createdCorpus = { id: 12, name: "API handbook", description: "pdf-backed material" };
      corpora = [...corpora, createdCorpus];
      documentsByCorpus[12] = [];
      return jsonResponse(createdCorpus, 201);
    }

    const uploadMatch = path.match(/^\/rag\/corpora\/(\d+)\/documents\/upload$/);
    if (method === "POST" && uploadMatch) {
      const corpusId = Number(uploadMatch[1]);
      const formData = init?.body as FormData;
      const file = formData.get("file") as File;
      const uploaded = {
        id: 21,
        rag_corpus_id: corpusId,
        filename: file.name,
        content: "HTTP status 200 means success",
        content_type: file.type || "application/pdf",
        storage_bucket: "rag-documents",
        object_key: `corpora/${corpusId}/handbook.pdf`,
        size_bytes: file.size,
        checksum_sha256: "abc",
        status: "ready",
        created_at: "2026-03-31T10:00:00Z",
        ingested_at: "2026-03-31T10:00:00Z",
        metadata: null,
      };
      documentsByCorpus[corpusId] = [uploaded, ...(documentsByCorpus[corpusId] ?? [])];
      return jsonResponse(uploaded, 201);
    }

    const scenarioUpdateMatch = path.match(/^\/scenarios\/(\d+)$/);
    if (method === "PUT" && scenarioUpdateMatch) {
      const scenarioId = Number(scenarioUpdateMatch[1]);
      const body = JSON.parse(String(init?.body ?? "{}")) as { rag_corpus_id?: number | null };
      const updatedScenario = scenarios.find((scenario) => scenario.id === scenarioId);
      if (!updatedScenario) {
        return new Response("Scenario not found", { status: 404 });
      }
      const nextScenario = { ...updatedScenario, rag_corpus_id: body.rag_corpus_id ?? null };
      scenarios = scenarios.map((scenario) => (scenario.id === scenarioId ? nextScenario : scenario));
      return jsonResponse(nextScenario);
    }

    return new Response(`Unhandled ${method} ${path}`, { status: 500 });
  });
};

describe("Admin materials UI", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = createFetchMock();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("creates a material and uploads a pdf into it", async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByTestId("admin-toggle"));
    await user.type(await screen.findByTestId("material-name-input"), "API handbook");
    await user.click(screen.getByTestId("create-material-button"));

    expect(await screen.findByTestId("material-card-12")).toBeInTheDocument();

    const file = new File(["%PDF-1.4"], "handbook.pdf", { type: "application/pdf" });
    await user.upload(screen.getByTestId("pdf-upload-input"), file);
    await user.click(screen.getByTestId("upload-pdf-button"));

    expect(await screen.findByText("handbook.pdf")).toBeInTheDocument();

    const uploadCall = fetchMock.mock.calls.find(
      ([url, init]) =>
        String(url).includes("/rag/corpora/12/documents/upload") &&
        (init?.method ?? "GET").toUpperCase() === "POST",
    );
    expect(uploadCall).toBeTruthy();
    const formData = uploadCall?.[1]?.body as FormData;
    expect(formData.get("file")).toBe(file);
  });

  it("attaches a material to an existing scenario", async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByTestId("admin-toggle"));
    await user.selectOptions(await screen.findByTestId("scenario-attachment-select-11"), "10");
    await user.click(screen.getByTestId("attach-material-button-11"));

    await waitFor(() =>
      expect(screen.getByText('Материал привязан к сценарию "Backend Theory"')).toBeInTheDocument(),
    );

    const updateCall = fetchMock.mock.calls.find(
      ([url, init]) =>
        String(url).includes("/scenarios/11") && (init?.method ?? "GET").toUpperCase() === "PUT",
    );
    expect(updateCall).toBeTruthy();
    expect(JSON.parse(String(updateCall?.[1]?.body))).toEqual({ rag_corpus_id: 10 });
  });
});
