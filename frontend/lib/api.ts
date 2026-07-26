/** Minimal typed API client. JWT lives in localStorage (documented MVP
 * tradeoff — an httpOnly-cookie session is the Phase 2 hardening). */

export const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

const TOKEN_KEY = "lexa_token";

export function getToken(): string | null {
  return typeof window === "undefined" ? null : window.localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  window.localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  window.localStorage.removeItem(TOKEN_KEY);
}

export function authHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...authHeaders(), ...(init.headers ?? {}) },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}) as { detail?: string });
    throw new ApiError(response.status, body.detail ?? `HTTP ${response.status}`);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/** Upload the file directly to storage. S3 returns a POST with policy fields
 * (which enforce the size ceiling server-side); the local dev route takes a
 * PUT. The Authorization header is attached only to the API's own local route
 * — a real S3 presigned upload must never receive it. */
export async function uploadToPresignedUrl(upload: DocumentCreateResponse, file: File): Promise<void> {
  const target = upload.upload_url.startsWith("http")
    ? upload.upload_url
    : `${API_BASE}${upload.upload_url}`;
  const isLocalRoute = target.startsWith(`${API_BASE}/local-uploads/`);

  let response: Response;
  if (upload.upload_method === "POST") {
    const form = new FormData();
    for (const [key, value] of Object.entries(upload.upload_fields)) form.append(key, value);
    form.append("file", file); // must be last for S3
    response = await fetch(target, { method: "POST", body: form });
  } else {
    response = await fetch(target, {
      method: "PUT",
      body: file,
      headers: isLocalRoute ? authHeaders() : {},
    });
  }
  if (!response.ok) throw new ApiError(response.status, "upload failed");
}

// --- API types (mirror app/schemas/) -----------------------------------------

export interface UserOut {
  id: string;
  email: string;
}

export interface TokenResponse {
  access_token: string;
  user: UserOut;
}

export interface DocumentOut {
  id: string;
  title: string;
  status: string;
  page_count: number | null;
  size_bytes: number | null;
  created_at: string;
}

export interface DocumentCreateResponse {
  id: string;
  title: string;
  upload_url: string;
  upload_method: string;
  upload_fields: Record<string, string>;
}

export interface ChatOut {
  id: string;
  document_id: string;
  title: string | null;
}

export interface Citation {
  sid: number;
  chunk_id: string;
  page_start: number;
  page_end: number;
  section_path: string;
  section_title: string | null;
}

export interface MessageOut {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations: Citation[] | null;
}
