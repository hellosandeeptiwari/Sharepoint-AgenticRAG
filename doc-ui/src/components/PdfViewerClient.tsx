// src/components/PdfViewerClient.tsx
"use client";

import React, { useEffect, useRef } from "react";

/**
 * PdfViewerClient
 * - Renders the whole PDF (all pages) into canvases inside a scrollable div
 * - Exposes two imperative helpers via refs:
 *    • refScrollTo.current(page:number)
 *    • refHighlight.current(page:number, rects:number[][])
 * - Emits onViewChange(page) as the user scrolls (based on viewport middle)
 */
export default function PdfViewerClient({
  file,
  onReady,
  onViewChange,
  refScrollTo,
  refHighlight,
  scale = 1.05,
}: {
  file?: File | Blob;
  onReady?: (pages: number) => void;
  onViewChange?: (page: number) => void;
  refScrollTo?: React.MutableRefObject<(page: number) => void>;
  refHighlight?: React.MutableRefObject<(page: number, rects: number[][]) => void>;
  scale?: number;
}) {
  const DEBUG = true;

  const pdfjsRef = React.useRef<any>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const pagesRef = useRef<number>(0);
  const rafToken = useRef<number | null>(null);
  const currentRef = useRef<number>(1);

  // Helper: element's Y relative to scroller content
  const yWithinScroller = (el: HTMLElement) => {
    const sc = containerRef.current!;
    const er = el.getBoundingClientRect();
    const sr = sc.getBoundingClientRect();
    return sc.scrollTop + (er.top - sr.top);
  };

  // Load pdfjs + worker (client-side only)
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const m = await import("pdfjs-dist/build/pdf");
      // @ts-ignore
      m.GlobalWorkerOptions.workerSrc =
        "https://unpkg.com/pdfjs-dist@3.11.174/build/pdf.worker.min.js";
      if (!cancelled) pdfjsRef.current = m;
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Compute page from scroll (use the middle of the scroller)
  const computeCurrentFromScroll = () => {
    const scroller = containerRef.current;
    if (!scroller) return;

    const middle = scroller.scrollTop + scroller.clientHeight / 2;
    const children = Array.from(
      scroller.querySelectorAll<HTMLElement>("[data-page-wrap='true']")
    );

    let newPage = currentRef.current;

    for (let i = 0; i < children.length; i++) {
      const el = children[i];
      const top = yWithinScroller(el);
      const bottom = top + el.getBoundingClientRect().height;
      if (middle >= top && middle < bottom) {
        newPage = i + 1;
        break;
      }
    }

    if (newPage !== currentRef.current) {
      currentRef.current = newPage;
      onViewChange?.(newPage);
      if (DEBUG) console.log("[VIEWER] onViewChange ->", newPage);
    }
  };

  // Render PDF when file/engine ready
  useEffect(() => {
    let destroyed = false;

    (async () => {
      const pdfjs = pdfjsRef.current;
      if (!pdfjs || !file || !containerRef.current) return;

      const buf = await file.arrayBuffer();
      const doc = await pdfjs.getDocument({ data: buf }).promise;

      const pages = doc.numPages;
      pagesRef.current = pages;
      onReady?.(pages);
      currentRef.current = 1;
      onViewChange?.(1);

      const host = containerRef.current;
      host.innerHTML = "";

      for (let p = 1; p <= pages; p++) {
        if (destroyed) break;

        const page = await doc.getPage(p);
        const viewport = page.getViewport({ scale });

        // wrapper
        const wrap = document.createElement("div");
        wrap.id = `pg-${p}`;
        wrap.dataset.pageWrap = "true";
        wrap.className = "mb-4 relative";
        wrap.style.background = "#fff";
        wrap.style.borderRadius = "12px";
        wrap.style.overflow = "hidden";
        wrap.style.boxShadow = "0 1px 8px rgba(16,24,40,0.06)";

        // canvas
        const canvas = document.createElement("canvas");
        canvas.style.width = "100%";
        canvas.style.height = "auto";
        canvas.style.display = "block";

        // HiDPI
        const dpr = window.devicePixelRatio || 1;
        canvas.width = Math.floor(viewport.width * dpr);
        canvas.height = Math.floor(viewport.height * dpr);
        (canvas.style as any).width = `${viewport.width}px`;
        (canvas.style as any).height = `${viewport.height}px`;

        wrap.appendChild(canvas);
        host.appendChild(wrap);

        const ctx = canvas.getContext("2d")!;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        await page.render({ canvasContext: ctx, viewport }).promise;
      }

      // initial compute after render
      computeCurrentFromScroll();

      // scroll listener (throttled by rAF)
      const onScroll = () => {
        if (rafToken.current != null) return;
        rafToken.current = requestAnimationFrame(() => {
          rafToken.current = null;
          computeCurrentFromScroll();
        });
      };
      host.addEventListener("scroll", onScroll);

      return () => {
        host.removeEventListener("scroll", onScroll);
      };
    })();

    return () => {
      destroyed = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [file, onReady, onViewChange, scale]);

  // Expose: scrollTo(page)
  useEffect(() => {
    if (!refScrollTo) return;
    refScrollTo.current = (rawPage: number) => {
      const scroller = containerRef.current;
      if (!scroller) return;
      const pages = pagesRef.current || 1;
      const page = Math.min(Math.max(1, Math.floor(rawPage)), pages);

      const target = scroller.querySelector<HTMLElement>(`#pg-${page}`);
      if (!target) {
        if (DEBUG) console.log("[VIEWER] refScrollTo -> missing pg-", page);
        return;
      }

      const top = Math.max(0, yWithinScroller(target) - 8);
      if (DEBUG) console.log("[VIEWER] refScrollTo ->", rawPage, "clamped:", page, "top:", top);
      scroller.scrollTo({ top, behavior: "smooth" });
    };
  }, [refScrollTo]);

  // Expose: highlight(page, rects)
  useEffect(() => {
    if (!refHighlight) return;
    refHighlight.current = (rawPage: number, rects: number[][]) => {
      const scroller = containerRef.current;
      if (!scroller) return;

      const pages = pagesRef.current || 1;
      const page = Math.min(Math.max(1, Math.floor(rawPage)), pages);
      const wrap = document.getElementById(`pg-${page}`) as HTMLDivElement | null;
      if (!wrap) return;

      const canvas = wrap.querySelector("canvas") as HTMLCanvasElement | null;
      if (!canvas) return;

      wrap.style.position = "relative";
      let overlay = wrap.querySelector<HTMLDivElement>(".hl-overlay");
      if (!overlay) {
        overlay = document.createElement("div");
        overlay.className = "hl-overlay";
        overlay.style.position = "absolute";
        overlay.style.left = "0";
        overlay.style.top = "0";
        overlay.style.right = "0";
        overlay.style.bottom = "0";
        overlay.style.pointerEvents = "none";
        wrap.appendChild(overlay);
      }

      overlay.innerHTML = "";

      const rect = canvas.getBoundingClientRect();
      const W = rect.width || canvas.clientWidth || canvas.width;
      const H = rect.height || canvas.clientHeight || canvas.height;

      // Recenter to first rect (quarter-down from top of viewport)
      if (rects && rects.length) {
        const [, y0n] = rects[0];
        const topBase = yWithinScroller(wrap);
        const targetTop = Math.max(0, topBase + y0n * H - scroller.clientHeight * 0.25);
        scroller.scrollTo({ top: targetTop, behavior: "smooth" });
      }

      // Draw after scroll settles
      window.setTimeout(() => {
        overlay!.innerHTML = "";
        rects.forEach((r) => {
          const [x0n, y0n, x1n, y1n] = r;
          const hl = document.createElement("div");
          hl.style.position = "absolute";
          hl.style.left = `${x0n * W}px`;
          hl.style.top = `${y0n * H}px`;
          hl.style.width = `${(x1n - x0n) * W}px`;
          hl.style.height = `${(y1n - y0n) * H}px`;
          hl.style.background = "rgba(255, 200, 0, 0.28)";
          hl.style.border = "2px solid rgba(255, 170, 0, 0.9)";
          hl.style.borderRadius = "6px";
          hl.style.boxShadow = "0 0 0 4px rgba(255,170,0,0.12)";
          overlay!.appendChild(hl);
        });

        window.setTimeout(() => {
          if (overlay) overlay.innerHTML = "";
        }, 3500);
      }, 450);
    };
  }, [refHighlight]);

  return (
    <div
      ref={containerRef}
      className="h-[78vh] overflow-auto rounded-2xl border border-slate-200 shadow-sm bg-white p-3"
      style={{ WebkitOverflowScrolling: "touch" }}
    />
  );
}
