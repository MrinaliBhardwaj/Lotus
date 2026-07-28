"use client";

/** PDF.js page viewer with a citation highlight overlay. Pages render lazily
 * via IntersectionObserver so 1000-page documents don't rasterize up front;
 * highlight rects are drawn as absolutely-positioned divs using the normalized
 * (0–1) bboxes captured at parse time, so they scale with the rendered page
 * without any pixel math. */

import { useEffect, useMemo, useRef, useState } from "react";

import { authHeaders, type Highlight } from "@/lib/api";

interface Props {
  url: string; // absolute (S3 presigned) or API-relative (local dev route)
  highlight: Highlight | null; // changes trigger a scroll + overlay redraw
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

export default function PdfViewer({ url, highlight }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const pageRefs = useRef(new Map<number, HTMLDivElement>());
  const [pdf, setPdf] = useState<PdfDocument | null>(null);
  const [pageCount, setPageCount] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [flashPage, setFlashPage] = useState<number | null>(null);

  // group the citation's rects by page so each page container draws its own
  const rectsByPage = useMemo(() => {
    const map = new Map<number, [number, number, number, number][]>();
    for (const box of highlight?.bboxes ?? []) {
      (map.get(box.page) ?? map.set(box.page, []).get(box.page)!).push(box.rect);
    }
    return map;
  }, [highlight]);

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
    if (highlight === null) return;
    const element = pageRefs.current.get(highlight.page);
    if (element) {
      element.scrollIntoView({ behavior: "smooth", block: "start" });
      setFlashPage(highlight.page);
      const timer = setTimeout(() => setFlashPage(null), 1600);
      return () => clearTimeout(timer);
    }
  }, [highlight]);

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
            {/* citation highlight overlay — normalized rects → CSS percentages,
                so they scale with the rendered page automatically */}
            {(rectsByPage.get(pageNumber) ?? []).map(([x0, y0, x1, y1], index) => (
              <div
                key={index}
                data-testid={`highlight-${pageNumber}`}
                className="pointer-events-none absolute z-20 rounded-sm bg-amber-300/40 ring-1 ring-amber-500/70 mix-blend-multiply"
                style={{
                  left: `${x0 * 100}%`,
                  top: `${y0 * 100}%`,
                  width: `${(x1 - x0) * 100}%`,
                  height: `${(y1 - y0) * 100}%`,
                }}
              />
            ))}
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
