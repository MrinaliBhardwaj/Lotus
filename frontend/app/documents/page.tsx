"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  api,
  clearToken,
  getToken,
  uploadToPresignedUrl,
  type DocumentCreateResponse,
  type DocumentOut,
} from "@/lib/api";
import { sseFetch } from "@/lib/sse";

const STAGE_LABELS: Record<string, string> = {
  UPLOADED: "waiting for upload",
  VALIDATING: "validating",
  PARSING: "parsing pages",
  STRUCTURING: "detecting sections",
  CHUNKING: "chunking",
  EMBEDDING: "embedding",
  INDEXING: "indexing",
  READY: "ready",
  FAILED: "failed",
  DEGRADED: "degraded",
};

function StatusBadge({ status }: { status: string }) {
  const color =
    status === "READY"
      ? "bg-emerald-100 text-emerald-800"
      : status === "FAILED"
        ? "bg-red-100 text-red-800"
        : "bg-amber-100 text-amber-800";
  return (
    <span
      data-testid={`status-${status}`}
      className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${color}`}
    >
      {STAGE_LABELS[status] ?? status.toLowerCase()}
    </span>
  );
}

export default function DocumentsPage() {
  const router = useRouter();
  const [documents, setDocuments] = useState<DocumentOut[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const watched = useRef(new Set<string>());

  const refresh = useCallback(async () => {
    try {
      setDocuments(await api<DocumentOut[]>("/documents"));
    } catch {
      router.push("/login");
    }
  }, [router]);

  useEffect(() => {
    if (!getToken()) {
      router.push("/login");
      return;
    }
    void refresh();
  }, [refresh, router]);

  const watchProgress = useCallback(
    (documentId: string) => {
      if (watched.current.has(documentId)) return;
      watched.current.add(documentId);
      void sseFetch(`/documents/${documentId}/progress`, {
        onEvent: (_event, data) => {
          const status = data.status as string;
          setDocuments((current) =>
            current.map((d) => (d.id === documentId ? { ...d, status } : d)),
          );
          if (status === "READY" || status === "FAILED") void refresh();
        },
      }).finally(() => watched.current.delete(documentId));
    },
    [refresh],
  );

  // resume watching in-flight ingestions after a reload
  useEffect(() => {
    for (const document of documents) {
      if (!["READY", "FAILED", "DEGRADED", "UPLOADED"].includes(document.status)) {
        watchProgress(document.id);
      }
    }
  }, [documents, watchProgress]);

  async function upload(file: File) {
    setUploading(true);
    setError(null);
    try {
      const created = await api<DocumentCreateResponse>("/documents", {
        method: "POST",
        body: JSON.stringify({ title: file.name }),
      });
      await uploadToPresignedUrl(created.upload_url, file);
      await api(`/documents/${created.id}/complete`, { method: "POST" });
      await refresh();
      watchProgress(created.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "upload failed");
    } finally {
      setUploading(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  return (
    <main className="mx-auto max-w-3xl p-6">
      <header className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Your documents</h1>
        <button
          className="text-sm text-slate-500 hover:text-slate-900"
          onClick={() => {
            clearToken();
            router.push("/login");
          }}
        >
          Log out
        </button>
      </header>

      <div className="mt-6 rounded-2xl border-2 border-dashed border-slate-300 bg-white p-8 text-center">
        <p className="text-sm text-slate-500">
          Upload a PDF — it is validated, parsed, chunked and indexed before chat unlocks.
        </p>
        <input
          ref={fileInput}
          data-testid="file-input"
          type="file"
          accept="application/pdf"
          className="hidden"
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) void upload(file);
          }}
        />
        <button
          data-testid="upload-button"
          disabled={uploading}
          onClick={() => fileInput.current?.click()}
          className="mt-3 rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50"
        >
          {uploading ? "Uploading…" : "Choose a PDF"}
        </button>
        {error && <p className="mt-3 text-sm text-red-600">{error}</p>}
      </div>

      <ul className="mt-6 space-y-2">
        {documents.map((document) => (
          <li
            key={document.id}
            data-testid="document-row"
            className="flex items-center justify-between rounded-xl border border-slate-200 bg-white px-4 py-3"
          >
            <div className="min-w-0">
              <p className="truncate text-sm font-medium">{document.title}</p>
              <p className="text-xs text-slate-500">
                {document.page_count ? `${document.page_count} pages` : "—"}
              </p>
            </div>
            <div className="flex items-center gap-3">
              <StatusBadge status={document.status} />
              {document.status === "READY" && (
                <Link
                  data-testid="open-document"
                  href={`/documents/${document.id}`}
                  className="rounded-lg bg-slate-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-700"
                >
                  Open
                </Link>
              )}
            </div>
          </li>
        ))}
        {documents.length === 0 && (
          <li className="py-8 text-center text-sm text-slate-400">No documents yet.</li>
        )}
      </ul>
    </main>
  );
}
