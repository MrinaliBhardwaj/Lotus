"use client";

/** Chat pane: streamed answers with clickable [S#] citations that jump the
 * viewer to the cited page (resolved server-side — the model never emits
 * page numbers). */

import { useEffect, useRef, useState } from "react";

import { api, type Citation, type MessageOut } from "@/lib/api";
import { sseFetch } from "@/lib/sse";

interface Props {
  chatId: string;
  onCite: (page: number) => void;
}

interface DisplayMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations: Citation[];
  streaming?: boolean;
}

function CitedText({
  content,
  citations,
  onCite,
}: {
  content: string;
  citations: Citation[];
  onCite: (page: number) => void;
}) {
  const byId = new Map(citations.map((c) => [c.sid, c]));
  const parts = content.split(/(\[S\d+\])/g);
  return (
    <p className="whitespace-pre-wrap text-sm leading-relaxed">
      {parts.map((part, index) => {
        const match = /^\[S(\d+)\]$/.exec(part);
        if (!match) return <span key={index}>{part}</span>;
        const citation = byId.get(Number(match[1]));
        if (!citation) return <span key={index}>{part}</span>;
        return (
          <button
            key={index}
            data-testid="citation-chip"
            title={citation.section_path}
            onClick={() => onCite(citation.page_start)}
            className="mx-0.5 inline-flex items-center rounded-md bg-sky-100 px-1.5 py-0.5 text-xs font-medium text-sky-800 hover:bg-sky-200"
          >
            p. {citation.page_start}
          </button>
        );
      })}
    </p>
  );
}

export default function ChatPane({ chatId, onCite }: Props) {
  const [messages, setMessages] = useState<DisplayMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    void api<MessageOut[]>(`/chats/${chatId}/messages`).then((history) =>
      setMessages(
        history.map((message) => ({
          id: message.id,
          role: message.role,
          content: message.content,
          citations: message.citations ?? [],
        })),
      ),
    );
  }, [chatId]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function send() {
    const question = draft.trim();
    if (!question || busy) return;
    setBusy(true);
    setError(null);
    setDraft("");
    const streamingId = `streaming-${Date.now()}`;
    setMessages((current) => [
      ...current,
      { id: `user-${Date.now()}`, role: "user", content: question, citations: [] },
      { id: streamingId, role: "assistant", content: "", citations: [], streaming: true },
    ]);
    try {
      await sseFetch(`/chats/${chatId}/messages`, {
        method: "POST",
        body: { content: question },
        onEvent: (event, data) => {
          if (event === "delta") {
            setMessages((current) =>
              current.map((message) =>
                message.id === streamingId
                  ? { ...message, content: message.content + (data.text as string) }
                  : message,
              ),
            );
          } else if (event === "done") {
            setMessages((current) =>
              current.map((message) =>
                message.id === streamingId
                  ? {
                      ...message,
                      id: data.message_id as string,
                      citations: data.citations as Citation[],
                      streaming: false,
                    }
                  : message,
              ),
            );
          }
        },
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "request failed");
      setMessages((current) => current.filter((message) => message.id !== streamingId));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex-1 space-y-4 overflow-y-auto p-4">
        {messages.length === 0 && (
          <p className="pt-10 text-center text-sm text-slate-400">
            Ask anything about this document — answers cite the page they came from.
          </p>
        )}
        {messages.map((message) => (
          <div
            key={message.id}
            data-testid={`message-${message.role}`}
            className={`max-w-[85%] rounded-2xl px-4 py-3 ${
              message.role === "user"
                ? "ml-auto bg-slate-900 text-white"
                : "border border-slate-200 bg-white"
            }`}
          >
            {message.role === "assistant" ? (
              <>
                <CitedText
                  content={message.content}
                  citations={message.citations}
                  onCite={onCite}
                />
                {message.streaming && <span className="animate-pulse text-slate-400">▍</span>}
              </>
            ) : (
              <p className="whitespace-pre-wrap text-sm">{message.content}</p>
            )}
          </div>
        ))}
        <div ref={bottomRef} />
      </div>
      {error && <p className="px-4 pb-2 text-sm text-red-600">{error}</p>}
      <form
        className="flex gap-2 border-t border-slate-200 bg-white p-3"
        onSubmit={(event) => {
          event.preventDefault();
          void send();
        }}
      >
        <input
          data-testid="chat-input"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="Ask about this document…"
          className="flex-1 rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-500"
        />
        <button
          data-testid="chat-send"
          type="submit"
          disabled={busy || !draft.trim()}
          className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50"
        >
          Send
        </button>
      </form>
    </div>
  );
}
