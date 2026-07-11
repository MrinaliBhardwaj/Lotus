/** SSE over fetch (EventSource can't send an Authorization header). */

import { API_BASE, authHeaders } from "./api";

export interface SseOptions {
  method?: "GET" | "POST";
  body?: unknown;
  signal?: AbortSignal;
  onEvent: (event: string, data: Record<string, unknown>) => void;
}

export async function sseFetch(path: string, options: SseOptions): Promise<void> {
  const response = await fetch(`${API_BASE}${path}`, {
    method: options.method ?? "GET",
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    headers: { "Content-Type": "application/json", ...authHeaders() },
    signal: options.signal,
  });
  if (!response.ok || response.body === null) {
    const body = await response.json().catch(() => ({}) as { detail?: string });
    throw new Error(body.detail ?? `HTTP ${response.status}`);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let boundary: number;
    while ((boundary = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      let event = "message";
      let data: string | null = null;
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice("event: ".length);
        else if (line.startsWith("data: ")) data = line.slice("data: ".length);
      }
      if (data !== null) options.onEvent(event, JSON.parse(data));
    }
  }
}
