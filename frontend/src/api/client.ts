const API_BASE =
  import.meta.env.VITE_API_URL ||
  (typeof window !== "undefined" && window.location.origin.includes("8000") ? "" : "http://127.0.0.1:8000");

async function fetchJson<T = unknown>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(text || "Request failed");
  }
  return (await resp.json()) as T;
}

export { API_BASE, fetchJson };
