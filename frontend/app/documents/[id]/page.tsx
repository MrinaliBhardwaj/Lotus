"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { use, useEffect, useState } from "react";

import ChatPane from "@/components/ChatPane";
import PdfViewer from "@/components/PdfViewer";
import { api, getToken, type ChatOut, type DocumentOut } from "@/lib/api";

export default function WorkspacePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const router = useRouter();
  const [document, setDocument] = useState<DocumentOut | null>(null);
  const [pdfUrl, setPdfUrl] = useState<string | null>(null);
  const [chatId, setChatId] = useState<string | null>(null);
  const [jumpToPage, setJumpToPage] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!getToken()) {
      router.push("/login");
      return;
    }
    void (async () => {
      try {
        setDocument(await api<DocumentOut>(`/documents/${id}`));
        const { url } = await api<{ url: string }>(`/documents/${id}/download-url`);
        setPdfUrl(url);
        // reuse this document's chat if one exists, otherwise start one
        const chats = await api<ChatOut[]>("/chats");
        const existing = chats.find((chat) => chat.document_id === id);
        if (existing) {
          setChatId(existing.id);
        } else {
          const chat = await api<ChatOut>("/chats", {
            method: "POST",
            body: JSON.stringify({ document_id: id }),
          });
          setChatId(chat.id);
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : "failed to load document");
      }
    })();
  }, [id, router]);

  if (error) {
    return (
      <main className="p-6">
        <p className="text-sm text-red-600">{error}</p>
        <Link href="/documents" className="text-sm text-slate-500 underline">
          Back to documents
        </Link>
      </main>
    );
  }

  return (
    <main className="flex h-screen flex-col">
      <header className="flex items-center justify-between border-b border-slate-200 bg-white px-4 py-2">
        <div className="flex items-center gap-3">
          <Link href="/documents" className="text-sm text-slate-500 hover:text-slate-900">
            ← Documents
          </Link>
          <h1 className="truncate text-sm font-semibold">{document?.title ?? "…"}</h1>
        </div>
        <span className="text-xs text-slate-400">
          {document?.page_count ? `${document.page_count} pages` : ""}
        </span>
      </header>
      <div className="grid min-h-0 flex-1 grid-cols-2">
        <section className="min-h-0 border-r border-slate-200 bg-slate-100">
          {pdfUrl && <PdfViewer url={pdfUrl} jumpToPage={jumpToPage} />}
        </section>
        <section className="min-h-0">
          {chatId && (
            <ChatPane
              chatId={chatId}
              onCite={(page) => {
                // re-trigger even for the same page
                setJumpToPage(null);
                requestAnimationFrame(() => setJumpToPage(page));
              }}
            />
          )}
        </section>
      </div>
    </main>
  );
}
