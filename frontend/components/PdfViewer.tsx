"use client";

/** PDF.js page viewer (Phase 1: page rendering + jump-to-page; the Phase 2
 * bbox highlight overlay mounts on top of these same page containers).
 * Pages render lazily via IntersectionObserver so 1000-page documents don't
 * rasterize up front. */

import { useEffect, useRef, useState } from "react";

import { authHeaders } from "@/lib/api";

interface Props {
  url: string; // absolute (S3 presigned) or API-relative (local dev route)
  jumpToPage: number | null; // 1-based; changes trigger a scroll
}

interface PdfjsModule {
  GlobalWorkerOptions: { workerSrc: string };
  getDocument: (options: object) => { promise: Promise<PdfDocument> };
}

interface PdfDocument {
  numPages: number;
  getPage: (page: number) => Promise<PdfPage>;
}

interface PdfPage {
  getViewport: (options: { scale: number }) => { width: number; height: number };
  render: (options: object) => { promise: Promise<void> };
}

export default function PdfViewer({ url, jumpToPage }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const pageRefs = useRef(new Map<number, HTMLDivElement>());
  const [pdf, setPdf] = useState<PdfDocument | null>(null);
  const [pageCount, setPageCount] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [flashPage, setFlashPage] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const pdfjs = (await import("pdfjs-dist")) as unknown as PdfjsModule;
        pdfjs.GlobalWorkerOptions.workerSrc = new URL(
          "pdfjs-dist/build/pdf.worker.min.mjs",
          import.meta.url,
        ).toString();
        const target = url.startsWith("http")
          ? url
          : `${process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000"}${url}`;
        const needsAuth = !url.startsWith("http") || target.includes("/local-uploads/");
        const document = await pdfjs.getDocument({
          url: target,
          httpHeaders: needsAuth ? authHeaders() : {},
        }).promise;
        if (cancelled) return;
        setPdf(document);
        setPageCount(document.numPages);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : "failed to load PDF");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [url]);

  // lazy page rasterization
  useEffect(() => {
    if (!pdf || pageCount === 0) return;
    const rendered = new Set<number>();
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          const pageNumber = Number((entry.target as HTMLElement).dataset.page);
          if (rendered.has(pageNumber)) continue;
          rendered.add(pageNumber);
          void renderPage(pdf, pageNumber, entry.target as HTMLDivElement);
        }
      },
      { root: containerRef.current, rootMargin: "600px" },
    );
    for (const element of pageRefs.current.values()) observer.observe(element);
    return () => observer.disconnect();
  }, [pdf, pageCount]);

  useEffect(() => {
    if (jumpToPage === null) return;
    const element = pageRefs.current.get(jumpToPage);
    if (element) {
      element.scrollIntoView({ behavior: "smooth", block: "start" });
      setFlashPage(jumpToPage);
      const timer = setTimeout(() => setFlashPage(null), 1600);
      return () => clearTimeout(timer);
    }
  }, [jumpToPage]);

  if (error) {
    return <p className="p-6 text-sm text-red-600">{error}</p>;
  }

  return (
    <div ref={containerRef} data-testid="pdf-viewer" className="h-full overflow-y-auto p-4">
      {pageCount === 0 && <p className="p-6 text-sm text-slate-400">Loading document…</p>}
      <div className="mx-auto max-w-[820px] space-y-4">
        {Array.from({ length: pageCount }, (_, index) => index + 1).map((pageNumber) => (
          <div
            key={pageNumber}
            data-page={pageNumber}
            data-testid={`pdf-page-${pageNumber}`}
            ref={(element) => {
              if (element) pageRefs.current.set(pageNumber, element);
            }}
            className={`relative min-h-[400px] rounded-lg bg-white shadow transition-shadow ${
              flashPage === pageNumber ? "ring-4 ring-amber-400" : ""
            }`}
          >
            <span className="absolute left-2 top-2 z-10 rounded bg-slate-900/70 px-1.5 py-0.5 text-[10px] text-white">
              p. {pageNumber}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

async function renderPage(
  pdf: PdfDocument,
  pageNumber: number,
  container: HTMLDivElement,
): Promise<void> {
  const page = await pdf.getPage(pageNumber);
  const viewport = page.getViewport({ scale: 1.3 });
  const canvas = document.createElement("canvas");
  canvas.width = viewport.width;
  canvas.height = viewport.height;
  canvas.className = "w-full h-auto rounded-lg";
  const context = canvas.getContext("2d");
  if (!context) return;
  await page.render({ canvasContext: context, viewport }).promise;
  container.style.minHeight = "0";
  container.appendChild(canvas);
}
