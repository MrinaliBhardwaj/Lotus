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

/** PUT the file to the presigned URL. The Authorization header is attached
 * only for the API's own local-dev upload route — a real S3 presigned URL
 * must never receive it. */
export async function uploadToPresignedUrl(url: string, file: File): Promise<void> {
  const target = url.startsWith("http") ? url : `${API_BASE}${url}`;
  const isLocalRoute = target.startsWith(`${API_BASE}/local-uploads/`);
  const response = await fetch(target, {
    method: "PUT",
    body: file,
    headers: isLocalRoute ? authHeaders() : {},
  });
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
