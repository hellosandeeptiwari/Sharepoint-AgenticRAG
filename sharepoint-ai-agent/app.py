# app.py
import os, io, re, base64
from typing import Optional, Tuple, List, Dict

import streamlit as st
import streamlit.components.v1 as components
from PIL import Image, ImageDraw
import fitz  # PyMuPDF

# ==== your helper module (unchanged logic) ====
from sharepoint_ai_agent import (
    SharePointAgent, LocalAgent, _download_file_bytes,
    build_index_for_pdf, retrieve_from_index, answer_from_text_ctx,
    pinecone_enabled, pinecone_retrieve, pinecone_count,
    MAX_PDF_PAGES,
    ask_about_media, pages_to_media,
    langextract_available, langextract_structured_from_pack, upsert_langextract_records_to_pinecone,
)

# ---------------- Basic setup ----------------
POPPLER_PATH = os.getenv("POPPLER_PATH")
if POPPLER_PATH and POPPLER_PATH not in os.environ.get("PATH", ""):
    os.environ["PATH"] += os.pathsep + POPPLER_PATH

st.set_page_config(page_title="SharePoint AI Agent", layout="wide")

# ---------------- Utilities ----------------
def safe_rerun():
    try: st.rerun()
    except Exception:
        try: getattr(st, "experimental_rerun")()
        except Exception: pass

def detect_file_type(data: bytes, name: str | None = None) -> str:
    n = (name or "").lower()
    if data.startswith(b"%PDF-") or n.endswith(".pdf"): return "pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n") or n.endswith(".png"): return "png"
    if data.startswith(b"\xff\xd8\xff") or n.endswith((".jpg",".jpeg")): return "jpeg"
    return "unknown"

def anchor_from_chunk(chunk: str) -> str:
    tokens = re.findall(r"[A-Za-z0-9]{5,}", chunk or "") or re.findall(r"[A-Za-z0-9]{3,}", chunk or "")
    tokens.sort(key=len, reverse=True)
    return tokens[0] if tokens else (chunk[:20] if chunk else "")

def page_png_with_highlight(pdf_bytes: bytes, page: int, phrase: str, scale: float = 1.8) -> Tuple[Optional[bytes], int]:
    if not pdf_bytes: return None, 0
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        page = max(1, min(page, doc.page_count))
        p = doc[page-1]
        anchor = anchor_from_chunk(phrase or "")
        quads = []
        if anchor:
            try: quads = p.search_for(anchor, quads=True)
            except TypeError: quads = p.search_for(anchor) or []
        m = fitz.Matrix(scale, scale)
        pix = p.get_pixmap(matrix=m, alpha=False)
        img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGBA")
        draw = ImageDraw.Draw(img, "RGBA")
        hits = 0
        if quads:
            for q in quads:
                try: pts = [q.ul, q.ur, q.lr, q.ll]
                except AttributeError: pts = [(q.x0,q.y0),(q.x1,q.y0),(q.x1,q.y1),(q.x0,q.y1)]
                poly = [(x*scale, y*scale) for (x,y) in pts]
                draw.polygon(poly, fill=(255,230,0,76), outline=(255,200,0,255))
                hits += 1
        buf = io.BytesIO(); img.save(buf, format="PNG")
        return buf.getvalue(), hits
    except Exception:
        return None, 0

def read_upload_with_progress(upfile, label="Uploading…", chunk_size=1<<20):
    buf = upfile.getbuffer(); total = len(buf); out = io.BytesIO()
    prog = st.progress(0, text=f"{label} 0%")
    for i in range(0, total, chunk_size):
        out.write(buf[i:i+chunk_size]); frac = min(1.0, (i+chunk_size)/total)
        prog.progress(frac, text=f"{label} {int(frac*100)}%")
    prog.progress(1.0, text=f"{label} 100% ✅"); return out.getvalue()

def unpack_retrieval(ret):
    if ret is None: return [], [], None
    if isinstance(ret, tuple) and len(ret)==3: return ret[0] or [], ret[1] or [], ret[2]
    if isinstance(ret, tuple) and len(ret)==2: return ret[0] or [], ret[1] or [], None
    return [], [], None

def ensure_state():
    ss = st.session_state
    ss.setdefault("connected_ok", False)
    ss.setdefault("agent", None)
    ss.setdefault("sp_items", [])
    ss.setdefault("current_doc_id", None)
    ss.setdefault("current_doc_name", None)
    ss.setdefault("current_doc_bytes", None)
    ss.setdefault("viewer_page", 1)
    ss.setdefault("num_pages", 1)
    ss.setdefault("highlight_phrase", None)
    ss.setdefault("chat_history", [])
    ss.setdefault("message_votes", [])
    ss.setdefault("last_answer", None)
    ss.setdefault("local_dir", os.getenv("LOCAL_DIR",""))
    ss.setdefault("mode", "Hybrid (RAG→Vision)")
    ss.setdefault("auto_ingest_on_load", True)
    ss.setdefault("temperature", 0.2)

def set_current_doc(doc_id: str, name: str, data: bytes):
    st.session_state.current_doc_id = doc_id
    st.session_state.current_doc_name = name
    st.session_state.current_doc_bytes = data
    if detect_file_type(data, name) == "pdf":
        try:
            doc = fitz.open(stream=data, filetype="pdf")
            st.session_state.num_pages = max(1, doc.page_count)
        except Exception:
            st.session_state.num_pages = 1
    else:
        st.session_state.num_pages = 1
    st.session_state.viewer_page = 1
    st.session_state.highlight_phrase = None

def push_chat(role: str, content: str, cites: Optional[List[Dict]] = None):
    st.session_state.chat_history.append({"role": role, "content": content, "cites": cites or []})
    st.session_state.message_votes.append({"up": 0, "down": 0})

def doc_loaded() -> bool:
    return bool(st.session_state.get("current_doc_bytes") and st.session_state.get("current_doc_name"))

# ---------- PDF.js continuous viewer with visible scrollbar ----------
def pdfjs_continuous_viewer(pdf_bytes: bytes, height_px: int = 980, scale: float = 1.25):
    """
    Renders ALL pages using PDF.js 3.x into canvases stacked vertically with its own scrollbar.
    Adds page anchors 'pg-<n>' for citation jumps from the References section.
    """
    b64 = base64.b64encode(pdf_bytes).decode()
    components.html(f"""
    <div id="pdf_scroll" style="height:{height_px}px; overflow:auto; border:1px solid #e6e9ee; border-radius:12px; box-shadow:0 1px 10px rgba(16,24,40,0.04);">
      <div id="pdf_container" style="padding:8px 12px;"></div>
    </div>
    <script>
    (async function() {{
      const container = window.parent.document.querySelector('#pdf_container');
      const scroller  = window.parent.document.querySelector('#pdf_scroll');
      if (!container || !scroller) return;

      const lib = document.createElement('script');
      lib.src = "https://unpkg.com/pdfjs-dist@3.11.174/build/pdf.min.js";
      document.body.appendChild(lib);
      await new Promise(r => lib.onload = r);
      pdfjsLib.GlobalWorkerOptions.workerSrc = "https://unpkg.com/pdfjs-dist@3.11.174/build/pdf.worker.min.js";

      const b64 = "{b64}";
      const byteChars = atob(b64);
      const byteNumbers = new Array(byteChars.length);
      for (let i = 0; i < byteChars.length; i++) byteNumbers[i] = byteChars.charCodeAt(i);
      const bytes = new Uint8Array(byteNumbers);

      const doc = await pdfjsLib.getDocument({{data: bytes}}).promise;
      const pages = doc.numPages;

      for (let p = 1; p <= pages; p++) {{
        const page = await doc.getPage(p);
        const viewport = page.getViewport({{ scale: {scale} }});
        const wrapper = document.createElement('div');
        wrapper.style.margin = '0 0 16px 0';
        wrapper.id = 'pg-' + p;
        const canvas = document.createElement('canvas');
        canvas.width  = viewport.width;
        canvas.height = viewport.height;
        canvas.style.width = '100%';
        canvas.style.height = 'auto';
        wrapper.appendChild(canvas);
        const ctx = canvas.getContext('2d');
        container.appendChild(wrapper);
        await page.render({{ canvasContext: ctx, viewport }}).promise;
      }}

      window.addEventListener('message', (ev) => {{
        const msg = ev?.data || {{}};
        if (msg.type !== 'scroll_to_page') return;
        const t = window.parent.document.querySelector('#pg-' + msg.page);
        if (t) scroller.scrollTo({{ top: t.offsetTop - 8, behavior: 'smooth' }});
      }});
    }})();
    </script>
    """, height=height_px+10)

# ---------------- Styles (refined, consistent) ----------------
st.markdown("""
<style>
:root{
  --accent:#0b5fff;
  --card-bg:#ffffff;
  --border:#e7e9ee;
  --radius:14px;
  --shadow:0 1px 10px rgba(16,24,40,0.04);
}
.block-container { padding-top: .5rem; padding-bottom: .25rem; }
h2,h3,h4 { margin-top:.25rem; }

/* Toolbar */
.toolbar { position: sticky; top: 0; z-index: 5; background: #fff; padding: .55rem .25rem;
           border-bottom: 1px solid #eef0f3; }

/* Cards */
.card { background:var(--card-bg); border:1px solid var(--border); border-radius:var(--radius);
        padding:.9rem 1rem; margin-bottom:.85rem; box-shadow:var(--shadow); }
.card h4 { margin:0 0 .6rem 0; }

/* Chat bubbles */
.chat-bubble-user, .chat-bubble-assistant {
  padding: .7rem .95rem; border-radius: var(--radius); margin: .25rem 0; line-height: 1.45;
  border: 1px solid rgba(0,0,0,0.05); box-shadow:0 1px 4px rgba(16,24,40,.03);
}
.chat-bubble-user { background: #eef2ff; }
.chat-bubble-assistant { background: #f8fafc; }

/* References list: simple links only */
.refs-list { display:flex; flex-wrap:wrap; gap:.6rem 1rem; }
.refs-list a { color:var(--accent); text-decoration:underline; font-weight:500; }

/* Chat input (multiline) */
#chat_form [data-testid="stTextArea"] textarea {
  min-height: 120px !important; height: 120px !important; border-radius: var(--radius) !important;
}
#chat_form .send-row { display:flex; gap:.5rem; align-items:center; margin-top:.5rem; }
#chat_form .send-row .spacer { flex: 1 1 auto; }
#chat_form [data-testid="baseButton-secondary"] button,
#chat_form [data-testid="baseButton-primary"] button {
  border-radius: 12px !important; padding: .45rem .9rem !important;
}

/* Scrollbar in PDF */
#pdf_scroll { scrollbar-width:thin; }
#pdf_scroll::-webkit-scrollbar { width: 10px; }
#pdf_scroll::-webkit-scrollbar-thumb { background:#cfd4db; border-radius:8px; }
</style>
""", unsafe_allow_html=True)

# ---------------- Sidebar (untouched) ----------------
ensure_state()
with st.sidebar:
    st.header("🔗 Connection")
    st.caption("Leave blank to use values from `.env` set inside sharepoint_ai_agent.py")
    site_url  = st.text_input("Site URL", value=os.getenv("SHAREPOINT_SITE","https://cnxsi.sharepoint.com"))
    library   = st.text_input("Library", value=os.getenv("SHAREPOINT_LIBRARY","Documents"))
    folder    = st.text_input("Folder (optional)", value=os.getenv("SHAREPOINT_FOLDER",""))
    sp_user   = st.text_input("Username", value=os.getenv("SHAREPOINT_USERNAME",""), autocomplete="username")
    sp_pass   = st.text_input("Password", type="password", value=os.getenv("SHAREPOINT_PASSWORD",""), autocomplete="current-password")

    st.divider()
    st.subheader("Agent")
    agent_type = st.radio("Choose agent:", ["SharePoint","Local"], index=0)
    if agent_type == "Local":
        st.session_state.local_dir = st.text_input("Local folder (absolute path)", value=st.session_state.local_dir)

    label = "Connect" if agent_type == "Local" else "Connect to SharePoint"
    if st.button(label, type="primary"):
        try:
            if agent_type == "SharePoint":
                st.session_state.agent = SharePointAgent(
                    site_url=site_url or None, library=library or None, folder=folder or None,
                    username=sp_user or None, password=sp_pass or None
                )
                st.session_state.connected_ok = True; st.success("Connected.")
            else:
                if not st.session_state.local_dir or not os.path.isdir(st.session_state.local_dir):
                    raise RuntimeError("Provide a valid local folder.")
                st.session_state.agent = LocalAgent(st.session_state.local_dir)
                st.session_state.connected_ok = True; st.success("Using Local Agent.")
        except Exception as e:
            st.session_state.connected_ok = False; st.error(f"Connection error: {e}")

# ---------------- Header ----------------
st.markdown("## 📄 Document viewer")

# ---------------- Top row: picker + doc controls + status ----------------
def load_items():
    with st.spinner("Listing files..."):
        return st.session_state.agent.list_available_files(recursive=True, max_items=200, depth_limit=2)

if st.session_state.get("connected_ok"):
    c1, c2, c3 = st.columns([2,5,3], gap="large")
    with c1:
        st.subheader("📂 Files", divider=True)
        if st.button("🔄 Refresh"): st.session_state.sp_items = load_items()
        if not st.session_state.get("sp_items"): st.session_state.sp_items = load_items()
        items = st.session_state.sp_items
        selected = None
        if items:
            options = {f"{it['name']}  —  {it['server_relative_url']}": it for it in items}
            choice = st.selectbox("Select a file", list(options.keys()))
            selected = options.get(choice)

    with c2:
        st.subheader("Document controls", divider=True)
        if selected:
            st.write(f"**Selected:** {selected['name']}")
            cc1, cc2 = st.columns([1,1])
            with cc1:
                if st.button("Ingest & Load", type="primary", use_container_width=True):
                    sr, fname = selected["server_relative_url"], selected["name"]
                    try: b = _download_file_bytes(getattr(st.session_state.agent, "ctx", None), sr)
                    except Exception as e: st.error(f"Download failed: {e}"); b = None
                    if b:
                        ftype = detect_file_type(b, fname)
                        if ftype == "pdf":
                            p = st.progress(0, text="Starting…")
                            def _cb(done:int, total:int, stage:str="Embedding"):
                                frac = (done/total) if total else 0.0
                                p.progress(min(max(frac,0.01),0.99), text=f"{stage}: {done}/{total}")
                            try: build_index_for_pdf(b, doc_id=sr, progress_callback=_cb)  # type: ignore
                            except TypeError:
                                p.progress(0.25, text="Parsing…")
                                build_index_for_pdf(b, doc_id=sr)
                                p.progress(0.75, text="Saving…")
                            set_current_doc(sr, fname, b)
                            if pinecone_enabled():
                                p.progress(0.9, text="Finalizing…")
                                cnt = pinecone_count(sr); p.progress(1.0, text="Completed ✅")
                                st.caption(f"Pinecone has ≥{cnt} vectors for this doc.")
                            else:
                                p.progress(1.0, text="Completed (local) ✅")
                            st.success("Loaded in viewer.")
                        elif ftype in ("png","jpeg"):
                            set_current_doc(sr, fname, b); st.warning("Image loaded (Vision only).")
                        else:
                            st.error("Unsupported/corrupted file.")
            with cc2:
                st.toggle("Auto-ingest on load (PDF)", value=st.session_state.auto_ingest_on_load, key="auto_ingest_on_load")

    with c3:
        st.subheader("Status", divider=True)
        if st.session_state.get("current_doc_id"):
            st.success(f"Active: `{os.path.basename(st.session_state.current_doc_id)}`")
        else:
            st.info("No active document.")
else:
    st.info("Use the sidebar to connect, then pick a file or upload on the right.")

st.markdown("---")

# ---------------- Main split ----------------
left, right = st.columns([2.15, 1.35], gap="large")

# ===== LEFT: PDF viewer =====
with left:
    st.markdown('<div class="toolbar">', unsafe_allow_html=True)
    lcol1, lcol2, lcol3, lcol4 = st.columns([1,1,3,3])
    cur_bytes = st.session_state.get("current_doc_bytes")
    cur_name  = st.session_state.get("current_doc_name")
    page      = st.session_state.get("viewer_page", 1)
    num_pages = st.session_state.get("num_pages", 1)
    with lcol1:
        if st.button("◀ Prev", key="nav_prev", use_container_width=True) and page>1:
            st.session_state.viewer_page = page-1; safe_rerun()
    with lcol2:
        if st.button("Next ▶", key="nav_next", use_container_width=True):
            st.session_state.viewer_page = min(num_pages, page+1); safe_rerun()
    with lcol3:
        jump = st.number_input("Go to page", min_value=1, max_value=max(1,num_pages),
                               value=min(page, num_pages), step=1, key="nav_go_num")
    with lcol4:
        if st.button("Go", key="nav_go", use_container_width=True):
            st.session_state.viewer_page = max(1, min(int(jump), num_pages)); safe_rerun()
    st.markdown('</div>', unsafe_allow_html=True)

    if not (cur_bytes and cur_name):
        st.info("No document loaded. Ingest or upload on the right.")
    else:
        ftype = detect_file_type(cur_bytes, cur_name)
        if ftype == "pdf":
            pdfjs_continuous_viewer(cur_bytes, height_px=980, scale=1.25)
        else:
            st.image(cur_bytes, use_container_width=True, caption=cur_name)

# ===== RIGHT: Notes / References / Upload / Chat =====
with right:
    # Only show these if doc loaded or chat started
    if doc_loaded() or st.session_state.chat_history:
        st.markdown('<div class="card"><h4>Important Notes</h4><ul style="margin:0 0 .25rem 1rem; padding:0;">'
                    '<li>Use the links in <strong>References</strong> to jump to pages.</li>'
                    '<li>Responses are tuned to be deterministic at lower temperature.</li>'
                    '</ul></div>', unsafe_allow_html=True)

        # --- References (ONE place, links only) ---
        st.markdown('<div class="card"><h4>References</h4>', unsafe_allow_html=True)
        last_cites = []
        for msg in reversed(st.session_state.chat_history):
            if msg["role"] == "assistant" and msg.get("cites"):
                last_cites = msg["cites"]; break
        if last_cites:
            links = []
            for i, c in enumerate(last_cites):
                pg = c.get("page") or "?"
                links.append(f'<a href="#" onclick="window.parent.postMessage({{type:\'scroll_to_page\',page:{int(pg)}}},\'*\');return false;">p. {pg}</a>')
            st.markdown(f'<div class="refs-list">{"".join(f"<span>{x}</span>" for x in links)}</div>', unsafe_allow_html=True)
        else:
            st.caption("No citations yet.")
        st.markdown('</div>', unsafe_allow_html=True)

    # --- Upload / Tools ---
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.subheader("📤 Upload (PDF/PNG/JPG)")
    up = st.file_uploader("Drag & drop or browse", type=["pdf","png","jpg","jpeg"])
    if up is not None:
        b = read_upload_with_progress(up, label="Uploading file…")
        fname = up.name; ftype = detect_file_type(b, fname)
        set_current_doc(doc_id=fname, name=fname, data=b); st.success(f"Loaded: {fname}")

        u1, u2 = st.columns(2)
        with u1:
            if st.button("Ingest This PDF", disabled=(ftype!="pdf"), key="ingest_upload"):
                # progress already handled inside build_index_for_pdf -> _cb above
                p = st.progress(0, text="Starting…")
                def _cb(done:int, total:int, stage:str="Embedding"):
                    frac = (done/total) if total else 0.0
                    p.progress(min(max(frac,0.01),0.99), text=f"{stage}: {done}/{total}")
                try: pack = build_index_for_pdf(b, fname, progress_callback=_cb)  # type: ignore
                except TypeError:
                    p.progress(0.25, text="Parsing PDF…")
                    pack = build_index_for_pdf(b, fname)
                    p.progress(0.75, text="Saving to vector store…")

                if pack is None:
                    p.progress(1.0, text="No extractable text ❗")
                    st.warning("No extractable text. If this is a scan, enable OCR fallback in `.env`.")
                else:
                    if pinecone_enabled():
                        p.progress(0.9, text="Finalizing in Pinecone…")
                        cnt = pinecone_count(fname); p.progress(1.0, text="Completed ✅")
                        st.success("Chunks embedded."); st.caption(f"Pinecone has ≥{cnt} vectors for this doc.")
                    else:
                        p.progress(1.0, text="Completed (local index) ✅")
                        st.success("Chunks embedded (local index).")
        with u2:
            if st.button("Quick Ask About Upload", key="quick_ask_upload"):
                # NEW: explicit progress/status for Quick Ask
                with st.status("Processing Quick Ask…", expanded=True) as s:
                    s.update(label="Preparing…")
                    temp_hint = " Answer concisely and deterministically." if st.session_state.temperature <= 0.3 else ""
                    if ftype == "pdf":
                        s.update(label="Indexing (one-off)…")
                        pack = build_index_for_pdf(b, fname)
                        if pack is None:
                            s.update(label="No text layer; rasterizing first pages…")
                            from pdf2image import convert_from_bytes
                            imgs = convert_from_bytes(b, first_page=1, last_page=min(MAX_PDF_PAGES,4),
                                                     poppler_path=os.getenv("POPPLER_PATH"))
                            media = []
                            step = 0
                            for im in imgs:
                                step += 1
                                s.update(label=f"Converting page {step}/{len(imgs)}…")
                                buf = io.BytesIO(); im.save(buf, format="PNG"); media.append((buf.getvalue(),"image/png"))
                            s.update(label="Asking the model…")
                            v_ans = ask_about_media("Summarize briefly." + temp_hint, media)
                            push_chat("assistant", v_ans, []); s.update(label="Done.")
                        else:
                            s.update(label="Retrieving relevant chunks…")
                            ret = retrieve_from_index(pack, "Summarize briefly." + temp_hint, topk=6)
                            chunks, cites, _ = unpack_retrieval(ret)
                            s.update(label="Generating answer…")
                            ans = answer_from_text_ctx("Summarize briefly." + temp_hint, [f"(p. {c['page']}) " + ch for ch, c in zip(chunks, cites)])
                            st.session_state.last_answer = {"cites": cites, "chunks": chunks}
                            push_chat("assistant", ans, cites); s.update(label="Done.")
                    else:
                        s.update(label="Analyzing image…")
                        media = [(b, "image/png" if ftype=="png" else "image/jpeg")]
                        ans = ask_about_media("What does this image show?" + temp_hint, media)
                        push_chat("assistant", ans, []); s.update(label="Done.")
                safe_rerun()

    # LangExtract (unchanged)
    with st.expander("🔎 Structured Extraction (LangExtract)", expanded=False):
        if not langextract_available():
            st.info("LangExtract not installed. `pip install langextract`")
        else:
            task_text = st.text_area("What do you want to extract?",
                value="Extract key findings with fields: title, summary, date (if any).")
            if st.button("Run on Current Document", key="run_langextract"):
                db = st.session_state.get("current_doc_bytes"); did = st.session_state.get("current_doc_id")
                if not db or not did: st.warning("Load a PDF first.")
                else:
                    with st.spinner("Extracting structured data..."):
                        pack = build_index_for_pdf(db, did)
                    if pack is None:
                        st.warning("Could not build text index (no text / bad PDF).")
                    else:
                        structured = langextract_structured_from_pack(
                            pack, task_instructions=task_text, examples=[],
                            model_provider=("openai" if os.getenv("OPENAI_API_KEY") else "gemini"),
                            model_name="gpt-4o-mini"
                        )
                        st.success(f"Extracted {len(structured)} record(s).")
                        if structured:
                            st.json(structured[:min(10,len(structured))])
                            if pinecone_enabled():
                                n = upsert_langextract_records_to_pinecone(did, structured)
                                st.caption(f"Saved {n} record vectors to Pinecone.")
    st.markdown('</div>', unsafe_allow_html=True)  # /card

    # --- Chat card ---
    st.markdown('<div class="card"><h4>Assistant</h4>', unsafe_allow_html=True)

    st.selectbox("Answer mode", ["Hybrid (RAG→Vision)", "Pinecone Only", "Local Index (One-off)"],
                 index=["Hybrid (RAG→Vision)", "Pinecone Only", "Local Index (One-off)"].index(st.session_state.mode),
                 key="mode")

    # Show history (without duplicate citations; references live in the References card)
    for i, msg in enumerate(st.session_state.chat_history):
        with st.chat_message("user" if msg["role"]=="user" else "assistant"):
            st.markdown(
                f'<div class="{"chat-bubble-user" if msg["role"]=="user" else "chat-bubble-assistant"}">{msg["content"]}</div>',
                unsafe_allow_html=True,
            )

    # Multiline chat input (Ctrl/Cmd+Enter submit)
    with st.form("chat_form", clear_on_submit=True):
        prompt = st.text_area("", placeholder="Enter your message… (Shift+Enter newline, Ctrl/Cmd+Enter send)",
                              key="chat_textarea", label_visibility="collapsed")
        send_col1, send_col2 = st.columns([6,1])
        with send_col1: st.markdown('<div class="send-row"><div class="spacer"></div></div>', unsafe_allow_html=True)
        with send_col2: send = st.form_submit_button("Send", use_container_width=True, type="primary")

    if send and (prompt or "").strip():
        user_text = prompt.strip()
        temp_hint = " Answer concisely and deterministically." if st.session_state.temperature <= 0.3 else ""
        push_chat("user", user_text)
        did = st.session_state.get("current_doc_id")
        dname = st.session_state.get("current_doc_name")
        dbytes = st.session_state.get("current_doc_bytes")
        if not (did and dname and dbytes):
            push_chat("assistant","No active document. Load a PDF or image first.",[]); safe_rerun()

        ftype = detect_file_type(dbytes, dname)
        mode = st.session_state.mode
        ctx_chunks, cites, scores = [], [], None
        ans_text = None
        try:
            if mode=="Pinecone Only":
                if not pinecone_enabled(): ans_text = "Pinecone is not configured."
                else:
                    with st.status("Retrieving from Pinecone…", expanded=True):
                        ret = pinecone_retrieve(did, user_text + temp_hint, topk=6)
                        ctx_chunks, cites, scores = unpack_retrieval(ret)
                        if not ctx_chunks: ans_text = "No indexed chunks found. Click **Ingest & Load** first."
            elif mode=="Local Index (One-off)":
                with st.status("Indexing (one-off)…", expanded=True):
                    pack = build_index_for_pdf(dbytes, did or dname)
                if pack is not None:
                    with st.status("Retrieving relevant chunks…", expanded=True):
                        ret = retrieve_from_index(pack, user_text + temp_hint, topk=6)
                        ctx_chunks, cites, scores = unpack_retrieval(ret)
            else:  # Hybrid
                if ftype=="pdf":
                    if pinecone_enabled():
                        with st.status("Retrieving from Pinecone…", expanded=True):
                            ret = pinecone_retrieve(did, user_text + temp_hint, topk=6)
                            ctx_chunks, cites, scores = unpack_retrieval(ret)
                            weak = (len(ctx_chunks)==0)
                    else:
                        with st.status("Indexing (one-off)…", expanded=True):
                            pack = build_index_for_pdf(dbytes, did or dname)
                        if pack is not None:
                            with st.status("Retrieving relevant chunks…", expanded=True):
                                ret = retrieve_from_index(pack, user_text + temp_hint, topk=6)
                                ctx_chunks, cites, scores = unpack_retrieval(ret)
                        weak = (len(ctx_chunks)==0)
                    if weak:
                        with st.status("Falling back to vision (first pages)…", expanded=True):
                            media = pages_to_media(dbytes, max_pages=min(MAX_PDF_PAGES,4))
                            ans_text = ask_about_media(user_text + temp_hint, media)
                else:
                    with st.status("Analyzing image…", expanded=True):
                        media = [(dbytes, "image/png" if ftype=="png" else "image/jpeg")]
                        ans_text = ask_about_media(user_text + temp_hint, media)
        except Exception as e:
            ans_text = f"Error during retrieval: {e}"

        if ans_text is None and ctx_chunks:
            with st.status("Generating answer…", expanded=True):
                ans_text = answer_from_text_ctx(user_text + temp_hint, [f"(p. {c.get('page')}) " + ch for ch,c in zip(ctx_chunks, cites)])

        cites = cites or []
        push_chat("assistant", ans_text or "Sorry, I couldn't extract an answer.", cites)
        st.session_state.last_answer = {"cites": cites, "chunks": ctx_chunks}
        if cites and ctx_chunks:
            st.session_state.viewer_page = int(cites[0].get("page") or 1)
            st.session_state.highlight_phrase = anchor_from_chunk(ctx_chunks[0]) or None
        safe_rerun()

    st.markdown('</div>', unsafe_allow_html=True)  # /card
