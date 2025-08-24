"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import dynamic from "next/dynamic";
import {
  Copy,
  ThumbsUp,
  ThumbsDown,
  Paperclip,
  Send,
  Info,
  Loader2,
  FileText,
  ChevronLeft,
  ChevronRight,
} from "lucide-react";

import ReactMarkdown, { Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeExternalLinks from "rehype-external-links";

// Toggle this to silence logs once things are stable
const DEBUG = true;

// ========= local types =========
type Hilite = { page: any; phrase: string; rects: number[][] };

type ChatMsg = {
  role: "user" | "assistant";
  text: string;
  cites?: any[]; // can be number or string like "[2]" / "p. 2"
  bestPage?: number | null;
  hilites?: Hilite[];
};

type AskResponse = {
  text: string;
  cites: any[];
  best_page?: number | null;
  hilites?: Hilite[];
};

type SPItem = { name: string; server_relative_url: string };

// ========= PDF viewer (client-only) =========
const PdfViewer = dynamic(() => import("../components/PdfViewerClient"), {
  ssr: false,
});

// ========= helpers =========
async function jsonSafe(r: Response) {
  const text = await r.text();
  try {
    return JSON.parse(text);
  } catch {
    return { __raw: text };
  }
}

function stripRefs(s: string) {
  // remove any trailing "References: ..." line (we already show pills)
  return s.replace(/^\s*references:.*$/gim, "").trim();
}

// Normalizes page refs like: 2, "2", "[2]", "p. 2", "page 12" -> 2 / 12
function normalizePageRef(v: unknown): number {
  if (typeof v === "number" && Number.isFinite(v)) return v;
  const m = String(v ?? "").match(/\d+/);
  const n = m ? parseInt(m[0], 10) : NaN;
  return Number.isFinite(n) && n > 0 ? n : 1;
}

// Pull first markdown heading to show a topic pill
function getFirstHeading(md: string) {
  const m = md.match(/^\s*#{1,6}\s+(.+)$/m);
  return m?.[1]?.trim();
}

// ========= Markdown renderers (hydration-safe) =========
const mdComponents: Components = {
  h1: (p) => <h1 className="mt-4 mb-2 text-xl font-semibold" {...p} />,
  h2: (p) => <h2 className="mt-3 mb-1.5 text-lg font-semibold" {...p} />,
  h3: (p) => <h3 className="mt-3 mb-1 text-base font-semibold" {...p} />,
  // Use <div> so <pre> can be nested (avoids hydration warnings)
  p: ({ children }) => <div className="mb-2 leading-relaxed">{children}</div>,
  ul: (p) => <ul className="mb-2 ml-5 list-disc space-y-1" {...p} />,
  ol: (p) => <ol className="mb-2 ml-5 list-decimal space-y-1" {...p} />,
  li: (p) => <li className="leading-relaxed" {...p} />,
  a: (p) => (
    <a
      className="text-blue-700 underline hover:no-underline"
      {...p}
      target="_blank"
      rel="noopener noreferrer"
    />
  ),
  blockquote: (p) => (
    <blockquote className="my-2 border-l-4 border-slate-300 pl-3 italic text-slate-700" {...p} />
  ),
  table: (p) => (
    <div className="my-2 overflow-x-auto">
      <table className="min-w-full text-sm" {...p} />
    </div>
  ),
  th: (p) => <th className="border-b px-2 py-1 text-left font-semibold" {...p} />,
  td: (p) => <td className="border-b px-2 py-1 align-top" {...p} />,
  // Make <pre> explicit to avoid being nested under a <p>
  pre: ({ children }) => (
    <pre className="mb-2 overflow-x-auto rounded-lg bg-slate-900 p-3 text-slate-100">
      {children}
    </pre>
  ),
  // For block code, render only <code>; <pre> is above
  code({ inline, children, ...props }) {
    return inline ? (
      <code className="rounded bg-slate-100 px-1 py-0.5 text-[0.9em]" {...props}>
        {children}
      </code>
    ) : (
      <code className="text-[0.9em]">{children}</code>
    );
  },
};

// ========= API calls =========
async function apiIngest(file: File, onProgress: (pct: number, label?: string) => void) {
  onProgress(5, "Uploading…");
  const fd = new FormData();
  fd.append("file", file);
  const r = await fetch("/api/ingest", { method: "POST", body: fd });
  onProgress(100, "Completed ✅");
  const payload = await jsonSafe(r);
  if (!r.ok) throw new Error(payload?.detail || payload?.error || payload?.__raw || "Ingest failed");
  return payload as { ok: boolean; doc_id: string; pages: number };
}

async function apiQuickAsk(file: File, onStatus: (label: string) => void) {
  onStatus("Uploading…");
  const fd = new FormData();
  fd.append("file", file);
  const r = await fetch("/api/quick-ask", { method: "POST", body: fd });
  const payload = await jsonSafe(r);
  if (!r.ok) throw new Error(payload?.detail || payload?.error || payload?.__raw || "Quick Ask failed");
  return payload as { text: string; cites: any[] };
}

async function apiAsk(prompt: string, onSteps: (label: string) => void) {
  onSteps("Retrieving…");
  const r = await fetch("/api/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt, mode: "hybrid", topk: 6 }),
  });
  const payload = (await jsonSafe(r)) as AskResponse | any;
  if (!r.ok) {
    const msg = payload?.detail || payload?.error || payload?.__raw || "Ask failed";
    return { text: `❗ ${msg}`, cites: [], best_page: null, hilites: [] } as AskResponse;
  }
  return payload as AskResponse;
}

// ========= SharePoint helpers =========
async function spGetDefaults(): Promise<any> {
  const r = await fetch("/api/sp/defaults");
  const j = await jsonSafe(r);
  return j || {};
}

async function spConnect(body: any) {
  const r = await fetch("/api/sp/connect", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const j = await jsonSafe(r);
  if (!r.ok) throw new Error(j.detail || j.error || "Connect failed");
}

async function spList(): Promise<SPItem[]> {
  const r = await fetch("/api/sp/list");
  const j = await jsonSafe(r);
  if (!r.ok) throw new Error(j.detail || j.error || "List failed");
  return j.items || [];
}

async function spIngest(sr: string): Promise<{ data_b64?: string; mime?: string; doc_id?: string }> {
  const r = await fetch("/api/sp/ingest", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ server_relative_url: sr }),
  });
  const j = await jsonSafe(r);
  if (!r.ok) throw new Error(j.detail || j.error || "Ingest failed");
  return j;
}

// ========= Chat component =========
const Chat: React.FC<{
  onSend: (t: string) => Promise<ChatMsg>;
  onJump: (page: number) => void;
  onHighlight: (page: number, rects: number[][]) => void;
  history: ChatMsg[];
  pushHistory: (m: ChatMsg) => void;
  onSettingsToggle: () => void;
}> = ({ onSend, onJump, onHighlight, history, pushHistory, onSettingsToggle }) => {
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);

  const handleSend = useCallback(async () => {
    const t = text.trim();
    if (!t) return;
    setSending(true);
    pushHistory({ role: "user", text: t });
    setText("");

    try {
      const reply = await onSend(t);
      pushHistory(reply);

      const best = reply.bestPage ?? null;
      const firstCite = (reply.cites && reply.cites.length > 0) ? reply.cites[0] : null;
      const pageToJump = best
        ? normalizePageRef(best)
        : (firstCite ? normalizePageRef(firstCite) : null);

      if (DEBUG) {
        console.log("[CHAT] computed pageToJump:", pageToJump, "best:", best, "firstCite:", firstCite);
      }

      if (pageToJump) {
        onJump(pageToJump);
        const h = reply.hilites?.find((x) => normalizePageRef(x.page) === pageToJump);
        if (h?.rects?.length) {
          // small delay so smooth scroll lands
          setTimeout(() => onHighlight(pageToJump, h.rects!), 600);
        }
      }
    } finally {
      setSending(false);
    }
  }, [text, onSend, pushHistory, onJump, onHighlight]);

  return (
    <div className="card flex h-full flex-col">
      {/* header */}
      <div className="flex items-center gap-2">
        <div className="font-semibold text-slate-800">Assistant</div>
        <div className="ml-auto flex gap-2">
          <button type="button" className="iconbtn" title="Settings" onClick={onSettingsToggle}>⚙️</button>
          <button className="iconbtn" title="TTS">🔊</button>
          <button className="iconbtn" title="Mic">🎤</button>
        </div>
      </div>

      {/* messages */}
      <div className="mt-2 flex-1 overflow-auto pr-1">
        {history.map((m, i) => (
          <div
            key={i}
            className="mt-3 rounded-2xl border border-slate-200 bg-white shadow-sm px-4 py-3"
          >
            {/* Optional topic pill for assistant */}
            {m.role === "assistant" && (() => {
              const tag = getFirstHeading(m.text);
              return tag ? (
                <div className="mb-2">
                  <span className="inline-flex items-center gap-2 text-xs rounded-full bg-slate-100 border border-slate-200 px-2.5 py-1">
                    <span className="inline-flex items-center justify-center h-5 w-5 rounded-full bg-emerald-600 text-white text-[11px] font-semibold">
                      DW
                    </span>
                    {tag}
                  </span>
                </div>
              ) : null;
            })()}

            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              rehypePlugins={[[rehypeExternalLinks, { target: "_blank", rel: ["noopener", "noreferrer"] }]]}
              components={mdComponents}
            >
              {stripRefs(m.text)}
            </ReactMarkdown>

            {m.role === "assistant" && (m.cites?.length ?? 0) > 0 && (
              <div className="mt-2 rounded-xl border border-slate-200 bg-white p-2.5">
                <div className="mb-1 flex items-center gap-2 text-sm font-semibold text-slate-700">
                  <Info className="h-4 w-4" /> References
                </div>
                <div className="flex flex-wrap gap-2">
                  {m.cites!.map((p, idx) => (
                    <button
                      key={idx}
                      type="button"
                      onClick={() => {
                        const pageNum = normalizePageRef(p);
                        if (DEBUG) console.log("[CITE] raw:", p, "normalized:", pageNum);
                        onJump(pageNum);
                        const h = (m.hilites || []).find((x) => normalizePageRef(x.page) === pageNum);
                        if (h?.rects?.length) {
                          setTimeout(() => onHighlight(pageNum, h.rects!), 600);
                        }
                      }}
                      className="inline-flex items-center gap-1 rounded-full border border-slate-200 bg-white px-2.5 py-1 text-sm text-blue-700 hover:underline"
                    >
                      <FileText className="h-4 w-4" />
                      {`p. ${normalizePageRef(p)}`}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {m.role === "assistant" && (
              <div className="mt-2 flex justify-end gap-2">
                <button
                  className="btn-outline"
                  onClick={() => navigator.clipboard.writeText(m.text)}
                  title="Copy"
                >
                  <Copy className="h-4 w-4" />
                </button>
                <button className="btn-outline" title="Good">
                  <ThumbsUp className="h-4 w-4" />
                </button>
                <button className="btn-outline" title="Bad">
                  <ThumbsDown className="h-4 w-4" />
                </button>
              </div>
            )}
          </div>
        ))}
      </div>

      {/* wide composer */}
      <div className="mt-2 rounded-2xl border border-slate-200 shadow-sm p-2">
        <div className="flex items-end gap-2">
          <button className="btn-outline h-10 w-10 flex items-center justify-center" title="Attach">
            <Paperclip className="h-4 w-4" />
          </button>
          <div className="flex-1 relative">
            <textarea
              className="w-full min-h-[44px] rounded-xl border border-slate-200 px-3 py-2 outline-none focus:ring-2 focus:ring-indigo-200"
              placeholder="Enter your message"
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
                  e.preventDefault();
                  handleSend();
                }
              }}
            />
            <button
              onClick={handleSend}
              disabled={sending}
              className="absolute right-2 bottom-2 flex h-9 w-9 items-center justify-center rounded-full bg-indigo-600 text-white shadow hover:bg-indigo-500 disabled:opacity-60"
              aria-label="Send"
            >
              {sending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};

// ========= page =========
export default function Page() {
  const [file, setFile] = useState<File | undefined>();
  const [ingesting, setIngesting] = useState(false);
  const [ingestPct, setIngestPct] = useState(0);
  const [ingestLabel, setIngestLabel] = useState("");
  const [quickLabel, setQuickLabel] = useState("");
  const [chat, setChat] = useState<ChatMsg[]>([]);
  const [pages, setPages] = useState<number | null>(null);
  const [currentPage, setCurrentPage] = useState<number>(1);
  const [showSettings, setShowSettings] = useState(false);

  const [spConn, setSpConn] = useState({
    site_url: "",
    library: "Documents",
    folder: "",
    username: "",
    password: "",
  });
  const [spItems, setSpItems] = useState<SPItem[]>([]);
  const [spLoading, setSpLoading] = useState(false);

  const scrollToRef = useRef<(p: number) => void>(() => {});
  const highlightRef = useRef<(p: number, rects: number[][]) => void>(() => {});

  const pushHistory = useCallback((m: ChatMsg) => setChat((prev) => [...prev, m]), []);

  const toggleSettings = useCallback(() => setShowSettings((v) => !v), []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setShowSettings(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Prefill SharePoint defaults from backend .env
  useEffect(() => {
    (async () => {
      const d = await spGetDefaults();
      setSpConn((prev) => ({ ...prev, ...d }));
    })();
  }, []);

  const handleSendToServer = useCallback(async (text: string) => {
    const res = await apiAsk(text, (label) => {
      if (DEBUG) console.log("[ASK] status:", label);
    });

    if (DEBUG) {
      console.log(
        "ASK cites payload:",
        res.cites,
        "best_page:",
        res.best_page,
        "types:",
        res.cites?.map((c: any) => typeof c),
        "hilites sample:",
        Array.isArray(res.hilites) ? res.hilites.slice(0, 1) : res.hilites
      );
    }

    return {
      role: "assistant",
      text: stripRefs(res.text),
      cites: res.cites,
      bestPage: res.best_page ?? null,
      hilites: res.hilites || [],
    } as ChatMsg;
  }, []);

  const onQuickAsk = useCallback(async () => {
    if (!file) return;
    setQuickLabel("Preparing…");
    try {
      const res = await apiQuickAsk(file, (label) => DEBUG && console.log("[QUICK] status:", label));
      pushHistory({ role: "assistant", text: res.text, cites: res.cites });
    } catch (e: any) {
      pushHistory({ role: "assistant", text: `❗ ${String(e.message || e)}` });
    } finally {
      setQuickLabel("");
    }
  }, [file, pushHistory]);

  const onIngest = useCallback(async () => {
    if (!file) return;
    setIngesting(true);
    setIngestPct(0);
    setIngestLabel("Starting…");
    try {
      await apiIngest(file, (pct, label) => {
        setIngestPct(pct);
        if (label) setIngestLabel(label);
        if (DEBUG) console.log("[INGEST] progress:", pct, label);
      });
    } catch (e: any) {
      alert(e.message || String(e));
    } finally {
      setIngesting(false);
    }
  }, [file]);

  // SharePoint actions
  const doSpConnect = async () => {
    try {
      setSpLoading(true);
      await spConnect(spConn);
      DEBUG && console.log("[SP] connected");
    } catch (e: any) {
      alert(e.message || String(e));
    } finally {
      setSpLoading(false);
    }
  };
  const doSpList = async () => {
    try {
      const items = await spList();
      setSpItems(items || []);
      DEBUG && console.log("[SP] list items:", items);
    } catch (e: any) {
      alert(e.message || String(e));
    }
  };
  const doSpIngest = async (sr: string) => {
    try {
      const j = await spIngest(sr);
      if (j?.data_b64 && j?.mime) {
        const bin = Uint8Array.from(atob(j.data_b64), (c) => c.charCodeAt(0));
        const f = new File([bin], j.doc_id || "sharepoint.pdf", { type: j.mime });
        setFile(f);
        setCurrentPage(1);
        DEBUG && console.log("[SP] ingested file:", f.name, f.type);
      }
    } catch (e: any) {
      alert(e.message || String(e));
    }
  };

  // Pager actions
  const goPrev = useCallback(() => {
    setCurrentPage((p) => {
      const next = Math.max(1, p - 1);
      DEBUG && console.log("[PAGER] goPrev to:", next);
      scrollToRef.current?.(next);
      return next;
    });
  }, []);
  const goNext = useCallback(() => {
    setCurrentPage((p) => {
      const max = pages ?? p;
      const next = Math.min(max, p + 1);
      DEBUG && console.log("[PAGER] goNext to:", next, "of", max);
      scrollToRef.current?.(next);
      return next;
    });
  }, [pages]);

  // Reset page number when file changes
  useEffect(() => {
    if (file) setCurrentPage(1);
  }, [file]);

  return (
    <>
      {/* Top bar with centered filename */}
      <header className="h-12 border-b border-slate-200 flex items-center px-4 gap-3 bg-white sticky top-0 z-30">
        <button className="rounded-lg p-1.5 hover:bg-slate-100" aria-label="Back">←</button>
        <div className="font-semibold text-slate-900">Insmed Wiki</div>

        <div className="mx-auto w-[420px] max-w-[48vw]">
          <div className="flex items-center h-8 rounded-xl border border-slate-200 bg-white px-3 text-sm text-slate-700 shadow-sm">
            <div className="truncate w-full">{file ? file.name : "—"}</div>
          </div>
        </div>

        <span className="mr-2 text-[11px] px-2 py-0.5 rounded-full border border-slate-200 text-slate-600">
          v2.3.0
        </span>
        <div className="text-sm text-slate-700">you@company.com</div>
        <button className="ml-1 rounded-lg p-1.5 hover:bg-slate-100" aria-label="Help">?</button>
        <button
          className="rounded-lg p-1.5 hover:bg-slate-100"
          aria-label="Settings"
          onClick={toggleSettings}
        >
          ⚙️
        </button>
      </header>

      <main className="mx-auto max-w-[1600px] min-w-[1200px] px-6 py-4 overflow-x-auto">
        <h1 className="sr-only">Document viewer</h1>

        {/* Two columns similar to the reference */}
        <div className="grid grid-cols-[minmax(740px,1.6fr)_minmax(420px,1fr)] gap-6 items-start min-w-[1180px]">
          {/* LEFT: viewer */}
          <section className="min-w-[720px]">
            {!file ? (
              <div className="rounded-2xl border border-blue-100 bg-blue-50 p-4 text-blue-900">
                No document loaded. Upload on the right.
              </div>
            ) : (
              <div className="rounded-2xl border border-slate-200 shadow-sm bg-white p-2">
                <PdfViewer
                  file={file}
                  onReady={(p) => { DEBUG && console.log("[PDF] ready pages:", p); setPages(p); setCurrentPage(1); }}
                  onViewChange={(p) => { DEBUG && console.log("[PDF] view change ->", p); setCurrentPage(p); }}
                  refScrollTo={scrollToRef}
                  refHighlight={highlightRef}
                />

                {/* Mini toolbar under PDF */}
                <div className="mt-2 flex items-center justify-between text-sm text-slate-600">
                  <button className="px-2 py-1 rounded-lg border border-slate-200 hover:bg-slate-50">
                    Show Contents
                  </button>

                  {/* Center pager */}
                  <div className="flex items-center gap-2">
                    <button
                      className="px-2 py-1 rounded-lg border border-slate-200 hover:bg-slate-50"
                      onClick={goPrev}
                      disabled={!pages || currentPage <= 1}
                      aria-label="Previous page"
                    >
                      <ChevronLeft className="h-4 w-4" />
                    </button>
                    <div className="min-w-[72px] text-center">
                      {pages ? `${currentPage} / ${pages}` : "\u00A0"}
                    </div>
                    <button
                      className="px-2 py-1 rounded-lg border border-slate-200 hover:bg-slate-50"
                      onClick={goNext}
                      disabled={!pages || currentPage >= (pages ?? 1)}
                      aria-label="Next page"
                    >
                      <ChevronRight className="h-4 w-4" />
                    </button>
                  </div>

                  <select className="px-2 py-1 rounded-lg border border-slate-200 bg-white">
                    <option>Auto Width</option>
                    <option>Fit Page</option>
                    <option>100%</option>
                    <option>125%</option>
                  </select>
                </div>
              </div>
            )}
          </section>

          {/* RIGHT: settings (toggle) + chat */}
          <aside className="min-w-[420px] h-[78vh] flex flex-col gap-4 border-l border-slate-200 pl-5">
            {showSettings && (
              <>
                {/* Connection */}
                <div className="card">
                  <h4 className="mb-2 text-lg font-semibold text-slate-900">Connection</h4>
                  <div className="grid grid-cols-2 gap-2">
                    <input
                      className="input"
                      placeholder="Site URL"
                      value={spConn.site_url}
                      onChange={(e) => setSpConn({ ...spConn, site_url: e.target.value })}
                    />
                    <input
                      className="input"
                      placeholder="Library (Documents)"
                      value={spConn.library}
                      onChange={(e) => setSpConn({ ...spConn, library: e.target.value })}
                    />
                    <input
                      className="input"
                      placeholder="Folder (optional)"
                      value={spConn.folder}
                      onChange={(e) => setSpConn({ ...spConn, folder: e.target.value })}
                    />
                    <input
                      className="input"
                      placeholder="Username"
                      value={spConn.username}
                      onChange={(e) => setSpConn({ ...spConn, username: e.target.value })}
                    />
                    <input
                      className="input"
                      placeholder="Password"
                      type="password"
                      value={spConn.password}
                      onChange={(e) => setSpConn({ ...spConn, password: e.target.value })}
                    />
                  </div>
                  <div className="mt-2 flex gap-2">
                    <button className="btn-outline" onClick={doSpConnect} disabled={spLoading}>
                      Connect
                    </button>
                    <button className="btn-outline" onClick={doSpList}>
                      List files
                    </button>
                  </div>
                  {spItems.length > 0 && (
                    <div className="mt-2 max-h-48 overflow-auto rounded-lg border">
                      {spItems.map((it) => (
                        <div
                          key={it.server_relative_url}
                          className="flex items-center justify-between border-b px-2 py-1 text-sm"
                        >
                          <span className="truncate">{it.name}</span>
                          <button className="btn-primary" onClick={() => doSpIngest(it.server_relative_url)}>
                            Ingest
                          </button>
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                {/* Upload */}
                <div className="card">
                  <h4 className="mb-2 text-lg font-semibold text-slate-900">Upload</h4>
                  <input
                    type="file"
                    accept="application/pdf,image/png,image/jpeg"
                    onChange={(e) => {
                      const f = e.target.files?.[0];
                      if (f) setFile(f);
                    }}
                  />
                  <div className="mt-3 grid grid-cols-2 gap-2">
                    <button className="btn-primary" disabled={!file || ingesting} onClick={onIngest}>
                      Ingest &amp; Load
                    </button>
                    <button className="btn-outline" disabled={!file} onClick={onQuickAsk}>
                      Quick Ask
                    </button>
                  </div>
                  {(ingesting || ingestPct > 0) && (
                    <div className="mt-3">
                      <div className="text-sm text-slate-600">{ingestLabel}</div>
                      <div className="mt-1 h-2 w-full overflow-hidden rounded-full bg-slate-200">
                        <div className="h-full bg-indigo-600 transition-all" style={{ width: `${ingestPct}%` }} />
                      </div>
                    </div>
                  )}
                  {quickLabel && (
                    <div className="mt-3 rounded-lg border border-slate-200 bg-slate-50 p-2 text-sm text-slate-700">
                      {quickLabel}
                    </div>
                  )}
                </div>
              </>
            )}

            {/* Chat stays visible */}
            <Chat
              onSend={handleSendToServer}
              onJump={(p: number) => scrollToRef.current?.(p)}
              onHighlight={(page: number, rects: number[][]) => highlightRef.current?.(page, rects)}
              history={chat}
              pushHistory={pushHistory}
              onSettingsToggle={toggleSettings}
            />
          </aside>
        </div>

        <style jsx>{`
          .card {
            background: #fff;
            border: 1px solid #e7e9ee;
            border-radius: 16px;
            box-shadow: 0 1px 10px rgba(16, 24, 40, 0.05);
            padding: 14px;
          }
          .btn-primary {
            background: #0b5fff;
            color: #fff;
            border: 1px solid #0b5fff;
            border-radius: 12px;
            padding: 0.55rem 0.85rem;
            font-weight: 600;
          }
          .btn-primary:disabled { opacity: 0.6; }
          .btn-outline {
            background: #fff;
            border: 1px solid #dfe3ea;
            border-radius: 12px;
            padding: 0.4rem 0.6rem;
            transition: background 0.15s ease;
          }
          .btn-outline:hover { background: #f8fafc; }
          .iconbtn {
            width: 30px;
            height: 30px;
            border: 1px solid #e5e7eb;
            border-radius: 8px;
            background: #fff;
          }
          .input {
            border: 1px solid #dfe3ea;
            border-radius: 10px;
            padding: 0.45rem 0.6rem;
          }
          ::-webkit-scrollbar { width: 10px; height: 10px; }
          ::-webkit-scrollbar-thumb { background: #e5e7eb; border-radius: 8px; }
        `}</style>
      </main>
    </>
  );
}
