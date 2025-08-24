# sharepoint_ai_agent.py
from dotenv import load_dotenv
import os
from typing import List, Dict, Tuple
import io
import base64
import re
import numpy as np
import faiss
from pypdf import PdfReader

# Device code auth helper (you created this)
from msal_device_auth import get_sharepoint_ctx_device  # requires msal_device_auth.py

# === Load .env ===
load_dotenv()

# --- OpenAI client (new SDK) ---
# pip install "openai>=1.30,<1.51" "httpx<0.28"
from openai import OpenAI, RateLimitError, APIError
openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
if not os.getenv("OPENAI_API_KEY"):
    raise RuntimeError("OPENAI_API_KEY not set. Put it in .env or environment variables.")

# --- Pinecone vector DB ---
# pip install pinecone
from pinecone import Pinecone, ServerlessSpec

# --- SharePoint client ---
# pip install office365-sharepoint
from office365.sharepoint.client_context import ClientContext
from office365.runtime.auth.user_credential import UserCredential

# --- PDF → images (multimodal) ---
# pip install pdf2image pillow
# Windows needs Poppler installed; set POPPLER_PATH in .env if not on PATH
from pdf2image import convert_from_bytes
from PIL import Image

# --- OCR (optional fallback for scanned PDFs) ---
# pip install pytesseract
from pytesseract import image_to_string, pytesseract

# --- LangExtract (optional structured extraction) ---
# pip install langextract
try:
    import langextract as lx
    _HAS_LANGEXTRACT = True
except Exception:
    _HAS_LANGEXTRACT = False

import time
import hashlib

# ------------------- Config -------------------
PINECONE_API_KEY   = os.getenv("PINECONE_API_KEY")
PINECONE_INDEX     = os.getenv("PINECONE_INDEX")  # e.g. "sharepoint-media"
PINECONE_NAMESPACE = os.getenv("PINECONE_NAMESPACE", "__default__")
PINECONE_CLOUD     = os.getenv("PINECONE_CLOUD", "aws")
PINECONE_REGION    = os.getenv("PINECONE_REGION", "us-east-1")

SUPPORTED_MEDIA_EXTS = (".pdf", ".png", ".jpg", ".jpeg")
MAX_PDF_PAGES = int(os.getenv("MAX_PDF_PAGES", "3"))  # limit for cost/speed
POPPLER_PATH = os.getenv("POPPLER_PATH")  # set this on Windows if poppler isn’t on PATH

EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-small")

HYBRID_TOPK = int(os.getenv("HYBRID_TOPK", "6"))
SCORE_MIN   = float(os.getenv("RETRIEVAL_SCORE_MIN", "0.22"))

# OCR config
TESSERACT_CMD = os.getenv("TESSERACT_CMD")  # full path to tesseract.exe on Windows
if TESSERACT_CMD:
    pytesseract.tesseract_cmd = TESSERACT_CMD
OCR_LANG = os.getenv("OCR_LANG", "eng")
USE_OCR_FALLBACK = os.getenv("USE_OCR_FALLBACK", "true").lower() in ("1", "true", "yes")
MAX_OCR_PAGES = int(os.getenv("MAX_OCR_PAGES", str(MAX_PDF_PAGES)))

# Globals for Pinecone
_pc = None
_pc_index = None

# ------------------- Backoff wrappers -------------------
def _retry_backoff(fn, *args, **kwargs):
    delay = 2
    for attempt in range(6):  # ~2+4+8+16+32+64s
        try:
            return fn(*args, **kwargs)
        except (RateLimitError, APIError):
            if attempt == 5:
                raise
            time.sleep(delay)
            delay *= 2

# ------------------- Auth helpers -------------------
def connect_to_sharepoint_device(site_url: str):
    """
    Device-code OAuth (MFA-friendly). Requires:
      TENANT_ID and SHAREPOINT_CLIENT_ID in environment.
    """
    tenant_id = os.getenv("TENANT_ID")
    client_id = os.getenv("SHAREPOINT_CLIENT_ID")
    if not tenant_id or not client_id:
        raise RuntimeError("Device code auth needs TENANT_ID and SHAREPOINT_CLIENT_ID in .env")
    return get_sharepoint_ctx_device(site_url, tenant_id, client_id)

def with_device_ctx(agent):
    agent.ctx = connect_to_sharepoint_device(agent.site_url)
    return agent

# ------------------- FS helpers -------------------
def _download_file_bytes_local(abs_path: str) -> bytes:
    with open(abs_path, "rb") as f:
        return f.read()

# ------------------- OpenAI helpers -------------------
def safe_embed_texts(texts: List[str]) -> np.ndarray:
    try:
        embs = _retry_backoff(openai_client.embeddings.create, model=EMBED_MODEL, input=texts).data
    except Exception as e:
        msg = str(e)
        if "insufficient_quota" in msg or "You exceeded your current quota" in msg:
            raise RuntimeError(
                "OpenAI quota exceeded for embeddings. "
                "Add billing or increase limits: https://platform.openai.com/account/billing"
            )
        raise
    X = np.array([e.embedding for e in embs], dtype="float32")
    faiss.normalize_L2(X)
    return X

def safe_chat(model: str, messages: List[Dict], **kwargs):
    try:
        return _retry_backoff(openai_client.chat.completions.create, model=model, messages=messages, **kwargs)
    except Exception as e:
        msg = str(e)
        if "insufficient_quota" in msg or "You exceeded your current quota" in msg:
            raise RuntimeError(
                "OpenAI quota exceeded for chat. "
                "Add billing or increase limits: https://platform.openai.com/account/billing"
            )
        raise

# ------------------- SharePoint helpers -------------------
def connect_to_sharepoint(site_url: str, username: str, password: str) -> ClientContext:
    """Connect to SharePoint using username/password credentials."""
    return ClientContext(site_url).with_credentials(UserCredential(username, password))

def _download_file_bytes(ctx: ClientContext, server_relative_url: str) -> bytes:
    """Download bytes from SharePoint server-relative URL OR an absolute local path."""
    if os.path.isabs(server_relative_url) and os.path.exists(server_relative_url):
        return _download_file_bytes_local(server_relative_url)
    buf = io.BytesIO()
    file = ctx.web.get_file_by_server_relative_url(server_relative_url)
    file.download(buf).execute_query()
    return buf.getvalue()

def list_files_in_folder(ctx: ClientContext, site_relative_folder: str) -> List[Dict]:
    """List files & folders in a folder by server-relative path (must start with '/')."""
    if not site_relative_folder.startswith("/"):
        site_relative_folder = "/" + site_relative_folder
    folder = ctx.web.get_folder_by_server_relative_url(site_relative_folder)
    folder.expand(["Files", "Folders"]).get().execute_query()

    results: List[Dict] = []
    for f in folder.files:  # type: ignore
        results.append({"name": f.name, "server_relative_url": f.serverRelativeUrl, "type": "file"})
    for d in folder.folders:  # type: ignore
        results.append({"name": d.name, "server_relative_url": d.serverRelativeUrl, "type": "folder"})
    return results

def list_files_recursive(ctx: ClientContext,
                         folder_sr: str,
                         max_items: int = 200,
                         depth_limit: int = 2,
                         timeout_s: int = 10) -> List[Dict]:
    """Recursively list files (bounded). Returns only file items."""
    import time as _t
    if not folder_sr.startswith("/"):
        folder_sr = "/" + folder_sr

    results: List[Dict] = []
    deadline = _t.monotonic() + timeout_s

    def _walk(path: str, depth: int):
        nonlocal results
        if _t.monotonic() > deadline or len(results) >= max_items or depth < 0:
            return
        folder = ctx.web.get_folder_by_server_relative_url(path)
        folder.expand(["Folders", "Files"]).get().execute_query()

        for f in folder.files:  # type: ignore
            results.append({"name": f.name, "server_relative_url": f.serverRelativeUrl, "type": "file"})
            if _t.monotonic() > deadline or len(results) >= max_items:
                return

        if depth > 0:
            for sub in folder.folders:  # type: ignore
                if _t.monotonic() > deadline or len(results) >= max_items:
                    return
                _walk(sub.serverRelativeUrl, depth - 1)

    _walk(folder_sr, depth_limit)
    return results

# ------------------- Media helpers -------------------
def filter_supported_media(items: List[Dict]) -> List[Dict]:
    return [
        it for it in items
        if it.get("type") == "file" and any(it["name"].lower().endswith(ext) for ext in SUPPORTED_MEDIA_EXTS)
    ]

def _image_to_bytes(img: Image.Image, fmt: str = "PNG") -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()

def _pdf_bytes_to_images(pdf_bytes: bytes, max_pages: int = MAX_PDF_PAGES) -> List[bytes]:
    images = convert_from_bytes(pdf_bytes, first_page=1, last_page=max_pages, poppler_path=POPPLER_PATH)
    return [_image_to_bytes(im, fmt="PNG") for im in images]

def _to_data_url(img_bytes: bytes, mime: str) -> str:
    b64 = base64.b64encode(img_bytes).decode("utf-8")
    return f"data:{mime};base64,{b64}"

def ask_about_media(question: str, images: List[Tuple[bytes, str]]) -> str:
    """Ask a question over one or more images. `images` is a list of (bytes, mime) tuples."""
    content = [{"type": "text", "text": question}]
    for img_bytes, mime in images:
        content.append({"type": "image_url", "image_url": {"url": _to_data_url(img_bytes, mime)}})
    resp = safe_chat(
        "gpt-4.1-mini",
        messages=[
            {"role": "system", "content": "You answer strictly from the provided images. If unknown, say you don't know."},
            {"role": "user", "content": content},
        ],
        temperature=0.1,
    )
    return resp.choices[0].message.content.strip()

def prepare_media_for_file(ctx: ClientContext, server_relative_url: str, filename: str) -> List[Tuple[bytes, str]]:
    """Download a file and return [(bytes, mime)] ready for vision. Supports SharePoint or absolute local paths."""
    if os.path.isabs(server_relative_url) and os.path.exists(server_relative_url):
        data = _download_file_bytes_local(server_relative_url)
    else:
        data = _download_file_bytes(ctx, server_relative_url)

    lname = filename.lower()
    if lname.endswith(".pdf"):
        pages = _pdf_bytes_to_images(data, max_pages=MAX_PDF_PAGES)
        return [(p, "image/png") for p in pages]
    if lname.endswith(".png"):
        return [(data, "image/png")]
    if lname.endswith(".jpg") or lname.endswith(".jpeg"):
        return [(data, "image/jpeg")]
    return []

# ------------------- LocalAgent -------------------
class LocalAgent:
    def __init__(self, local_dir: str):
        self.local_dir = local_dir
        self.ctx = None  # not used; kept for API parity

    def server_relative_folder(self) -> str:
        return self.local_dir  # absolute path

    def list_available_files(self, recursive: bool = True, max_items: int = 200,
                             depth_limit: int = 10, timeout_s: int = 10) -> List[Dict]:
        results: List[Dict] = []
        root = self.local_dir
        if not recursive:
            for name in os.listdir(root):
                p = os.path.join(root, name)
                if os.path.isfile(p) and any(name.lower().endswith(ext) for ext in SUPPORTED_MEDIA_EXTS):
                    results.append({"name": name, "server_relative_url": p, "type": "file"})
            return results[:max_items]

        for dirpath, _, filenames in os.walk(root):
            for name in filenames:
                if any(name.lower().endswith(ext) for ext in SUPPORTED_MEDIA_EXTS):
                    p = os.path.join(dirpath, name)
                    results.append({"name": name, "server_relative_url": p, "type": "file"})
                    if len(results) >= max_items:
                        return results
        return results

# ------------------- SharePointAgent -------------------
class SharePointAgent:
    def __init__(self, site_url=None, library=None, folder=None, username=None, password=None):
        # load from env if not passed
        self.site_url = site_url or os.getenv("SHAREPOINT_SITE")
        self.library = library or os.getenv("SHAREPOINT_LIBRARY", "Documents")
        self.folder = folder or os.getenv("SHAREPOINT_FOLDER", "")
        self.username = username or os.getenv("SHAREPOINT_USERNAME")
        self.password = password or os.getenv("SHAREPOINT_PASSWORD")

        missing = [n for n, v in {
            "SHAREPOINT_SITE": self.site_url,
            "SHAREPOINT_LIBRARY": self.library,
            "SHAREPOINT_USERNAME": self.username,
            "SHAREPOINT_PASSWORD": self.password
        }.items() if not v]
        if missing:
            raise RuntimeError(f"Missing config: {', '.join(missing)}. Check your .env or pass creds via UI.")

        self.ctx = connect_to_sharepoint(self.site_url, self.username, self.password)

    def server_relative_folder(self) -> str:
        """Build server-relative path safely and ALWAYS start with '/'."""
        base = "/" + self.site_url.split("/", 3)[-1]  # e.g. /personal/you or /sites/Team
        lib = (self.library or "").strip("/\\")
        sub = (self.folder or "").strip("/\\")
        parts = [base]
        if lib and not base.lower().endswith("/" + lib.lower()):
            parts.append(lib)
        if sub:
            parts.append(sub)
        return "/" + "/".join(p.strip("/\\") for p in parts if p)

    def list_available_files(self, recursive: bool = True, max_items: int = 200,
                             depth_limit: int = 2, timeout_s: int = 10) -> List[Dict]:
        """Return supported media files (pdf/images)."""
        folder_sr = self.server_relative_folder()
        if recursive:
            items = list_files_recursive(self.ctx, folder_sr, max_items=max_items,
                                         depth_limit=depth_limit, timeout_s=timeout_s)
        else:
            items = list_files_in_folder(self.ctx, folder_sr)
        return filter_supported_media(items)

# ------------------- RAG helpers -------------------
def chunk_text(text: str, size: int = 3500, overlap: int = 400):
    if not text:
        return []
    out, i = [], 0
    while i < len(text):
        out.append(text[i:i+size])
        i += max(1, size - overlap)
    return out

def pinecone_enabled() -> bool:
    return bool(PINECONE_API_KEY and PINECONE_INDEX)

def _get_pinecone():
    """Return (pc, index). Create index if missing."""
    global _pc, _pc_index
    if not pinecone_enabled():
        return None, None
    if _pc is None:
        _pc = Pinecone(api_key=PINECONE_API_KEY)
    existing = {ix.name for ix in _pc.list_indexes().indexes}
    if PINECONE_INDEX not in existing:
        _pc.create_index(
            name=PINECONE_INDEX,
            dimension=1536,   # matches text-embedding-3-small
            metric="cosine",
            spec=ServerlessSpec(cloud=PINECONE_CLOUD, region=PINECONE_REGION),
        )
    if _pc_index is None:
        _pc_index = _pc.Index(PINECONE_INDEX)
    return _pc, _pc_index

def _vector_ids(doc_id: str, meta: List[dict]) -> List[str]:
    ids, counter = [], {}
    for m in meta:
        p = m["page"]
        counter[p] = counter.get(p, 0) + 1
        ids.append(f"{doc_id}:p{p}:c{counter[p]}")
    return ids

def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def pinecone_has_doc(doc_id: str, checksum: str) -> bool:
    _, index = _get_pinecone()
    if index is None:
        return False
    res = index.query(
        vector=[0]*1536,   # dummy vector
        top_k=1,
        filter={"doc_id": {"$eq": doc_id}, "checksum": {"$eq": checksum}},
        include_metadata=False,
        namespace=PINECONE_NAMESPACE,
    )
    return bool(res.matches)

def pinecone_count(doc_id: str) -> int:
    _, index = _get_pinecone()
    if index is None:
        return 0
    res = index.query(
        vector=[0]*1536,
        top_k=1,
        include_metadata=False,
        namespace=PINECONE_NAMESPACE,
        filter={"doc_id": {"$eq": doc_id}},
    )
    return len(res.matches or [])

def upsert_chunks_to_pinecone(doc_id: str, chunks: List[str], meta: List[dict], X: np.ndarray,
                              batch: int = 100, checksum: str | None = None):
    _, index = _get_pinecone()
    if index is None:
        return 0
    ids = _vector_ids(doc_id, meta)
    total = 0
    for i in range(0, len(chunks), batch):
        sl = slice(i, i + batch)
        vecs = []
        for j, text in enumerate(chunks[sl], start=i):
            md = {
                "doc_id": doc_id,
                "page": int(meta[j]["page"]),
                "text": text[:4000],
            }
            if "ocr" in meta[j]:
                md["ocr"] = bool(meta[j]["ocr"])
            if "lang" in meta[j]:
                md["lang"] = meta[j]["lang"]
            if checksum:
                md["checksum"] = checksum
            vecs.append({"id": ids[j], "values": X[j].tolist(), "metadata": md})
        index.upsert(vectors=vecs, namespace=PINECONE_NAMESPACE)
        total += len(vecs)
    return total

def pinecone_retrieve(doc_id: str, query: str, topk: int = 6):
    _, index = _get_pinecone()
    if index is None:
        return [], [], []
    qv = safe_embed_texts([query])
    res = index.query(
        vector=qv[0].tolist(),
        top_k=topk,
        include_metadata=True,
        namespace=PINECONE_NAMESPACE,
        filter={"doc_id": {"$eq": doc_id}},
    )
    ctx, cites, scores = [], [], []
    for m in res.matches or []:
        md = m.metadata or {}
        ctx.append(md.get("text", ""))
        cites.append({"doc_id": md.get("doc_id", doc_id), "page": md.get("page", None)})
        scores.append(float(getattr(m, "score", 0.0)))
    return ctx, cites, scores

def retrieve_from_index(index_pack, query: str, topk: int = 6):
    qv = safe_embed_texts([query])
    D, I = index_pack["index"].search(qv, topk)
    I = I[0].tolist(); D = D[0].tolist()
    ctx  = [index_pack["chunks"][i] for i in I]
    cites= [index_pack["meta"][i]   for i in I]
    scores = D
    return ctx, cites, scores

FIGURE_HINTS = re.compile(r"\b(figure|table|chart|diagram|screenshot|image|page\s*\d+)\b", re.I)

def retrieval_looks_weak(scores, ctx):
    if not scores:
        return True
    if max(scores) < SCORE_MIN:
        return True
    if sum(len(c) for c in ctx) < 600:
        return True
    return False

def pick_fallback_pages(cites, pad=1, max_pages=8):
    pages = sorted({c["page"] for c in cites if c.get("page")})
    padded = []
    for p in pages:
        for q in range(max(1, p-pad), p+pad+1):
            padded.append(q)
    uniq = []
    for p in padded:
        if p not in uniq:
            uniq.append(p)
        if len(uniq) >= max_pages:
            break
    return uniq or [1]

def answer_from_text_ctx(question: str, context_chunks: List[str]) -> str:
    messages = [
        {"role": "system", "content": "Answer ONLY from the context. If not present, say you don't know. Provide page citations like (p. X)."},
        {"role": "user", "content": f"Context:\n\n" + "\n\n---\n\n".join(context_chunks) + f"\n\nQuestion: {question}"}
    ]
    res = safe_chat("gpt-4.1-mini", messages, temperature=0.1)
    return res.choices[0].message.content.strip()

def pages_to_media(pdf_bytes: bytes, pages: List[int], max_pages_each_call: int = 8) -> List[Tuple[bytes,str]]:  # noqa: E231
    imgs = convert_from_bytes(
        pdf_bytes,
        first_page=min(pages),
        last_page=max(pages),
        poppler_path=POPPLER_PATH
    )
    first = min(pages)
    keep = [i for i, _ in enumerate(imgs, start=first) if i in set(pages)]
    media: List[Tuple[bytes,str]] = []
    for i, im in enumerate(imgs, start=first):
        if i in keep:
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            media.append((buf.getvalue(), "image/png"))
    return media[:max_pages_each_call]

# ------------------- OCR helpers -------------------
def _ocr_pdf_bytes(pdf_bytes: bytes, max_pages: int = MAX_OCR_PAGES, lang: str = OCR_LANG) -> List[Tuple[int, str, str]]:
    texts: List[Tuple[int, str, str]] = []
    try:
        images = convert_from_bytes(pdf_bytes, first_page=1, last_page=max_pages, poppler_path=POPPLER_PATH)
    except Exception as e:
        print(f"[warn] pdf2image failed during OCR render: {e}")
        return texts
    for i, im in enumerate(images, start=1):
        try:
            raw_txt = image_to_string(im, lang=lang) or ""
            detected = lang
            texts.append((i, raw_txt, detected))
        except Exception as e:
            print(f"[warn] OCR failed on page {i}: {e}")
    return texts

# ------------------- LangExtract helpers -------------------
def langextract_available() -> bool:
    return _HAS_LANGEXTRACT

def build_concat_and_page_map(chunks: List[str], cites: List[dict]):
    doc_text = ""
    page_map = []
    cursor = 0
    for chunk, c in zip(chunks, cites):
        start = cursor
        end = cursor + len(chunk)
        page_map.append((start, end, c.get("page")))
        doc_text += chunk
        cursor = end
    return doc_text, page_map

def span_to_page(span_start: int, span_end: int, page_map: List[Tuple[int,int,int]]) -> int | None:
    for s, e, p in page_map:
        if span_start >= s and span_start < e:
            return p
    return None

def run_langextract_on_text(doc_text: str, task_instructions: str, examples: list[dict] | None = None,
                            model_provider: str = "openai", model_name: str = "gpt-4.1-mini"):
    if not _HAS_LANGEXTRACT:
        raise RuntimeError("LangExtract not installed. `pip install langextract`")
    if model_provider == "openai":
        provider = lx.providers.OpenAI(model=model_name)
    elif model_provider == "gemini":
        provider = lx.providers.Gemini(model="gemini-1.5-pro")
    elif model_provider == "ollama":
        provider = lx.providers.Ollama(model="llama3.1")
    else:
        raise ValueError("Unsupported provider")

    extractor = lx.Extractor(
        provider=provider,
        instructions=task_instructions,
        examples=examples or [],
        return_highlights=True,
    )
    result = extractor.extract(doc_text)
    return result.records, result.highlights

def langextract_structured_from_pack(pack, task_instructions: str, examples: list[dict] | None = None,
                                     model_provider: str = "openai", model_name: str = "gpt-4.1-mini"):
    chunks = pack["chunks"]
    cites = pack["meta"]
    doc_text, page_map = build_concat_and_page_map(chunks, cites)

    records, highlights = run_langextract_on_text(
        doc_text=doc_text,
        task_instructions=task_instructions,
        examples=examples or [],
        model_provider=model_provider,
        model_name=model_name
    )

    enriched = []
    for rec, hls in zip(records, highlights):
        top_span = hls[0] if hls else None
        page = span_to_page(top_span["start"], top_span["end"], page_map) if top_span else None
        enriched.append({"record": rec, "page": page, "spans": hls})
    return enriched

def upsert_langextract_records_to_pinecone(doc_id: str, records: List[dict]) -> int:
    _, index = _get_pinecone()
    if index is None or not records:
        return 0
    vecs = []
    for i, r in enumerate(records, start=1):
        rid = f"{doc_id}:lx:{i}"
        text_for_embed = str(r["record"])
        v = safe_embed_texts([text_for_embed])[0].tolist()
        md = {"doc_id": doc_id, "type": "langextract"}
        md.update(r)
        vecs.append({"id": rid, "values": v, "metadata": md})
    index.upsert(vectors=vecs, namespace=PINECONE_NAMESPACE)
    return len(vecs)

# ------------------- Indexer (with OCR fallback) -------------------
def build_index_for_pdf(pdf_bytes: bytes, doc_id: str):
    """Extract text, chunk, embed, build local FAISS, and upsert to Pinecone (optional).
       If no extractable text, optionally fall back to OCR.
    """
    reader = None
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as e:
        print(f"[warn] Not a valid PDF for {doc_id}: {e}")

    chunks, meta = [], []

    if reader is not None:
        for pno, page in enumerate(reader.pages, start=1):
            txt = page.extract_text() or ""
            for piece in chunk_text(txt, size=3500, overlap=400):
                if piece.strip():
                    chunks.append(piece)
                    meta.append({"doc_id": doc_id, "page": pno, "ocr": False})

    if not chunks and USE_OCR_FALLBACK:
        ocr_pages = _ocr_pdf_bytes(pdf_bytes, max_pages=MAX_OCR_PAGES, lang=OCR_LANG)
        for pno, raw_txt, detected_lang in ocr_pages:
            for piece in chunk_text(raw_txt, size=3500, overlap=400):
                if piece.strip():
                    chunks.append(piece)
                    meta.append({"doc_id": doc_id, "page": pno, "ocr": True, "lang": detected_lang})

    if not chunks:
        return None

    X = safe_embed_texts(chunks)
    index = faiss.IndexFlatIP(X.shape[1])
    index.add(X)

    if pinecone_enabled():
        checksum = _sha256(pdf_bytes)
        try:
            if not pinecone_has_doc(doc_id, checksum):
                upserted = upsert_chunks_to_pinecone(doc_id, chunks, meta, X, checksum=checksum)
                print(f"[pinecone] upserted {upserted} vectors for {doc_id}")
            else:
                print(f"[pinecone] already indexed (checksum match) for {doc_id}")
        except Exception as e:
            print(f"[warn] Pinecone upsert/check failed: {e}")

    return {"index": index, "vectors": X, "chunks": chunks, "meta": meta}

# ------------------- Hybrid QA -------------------
def hybrid_answer_pdf(ctx, server_relative_url: str, filename: str, question: str, topk: int = 6) -> str:
    """
    1) RAG over extracted text (Pinecone if configured, else FAISS)
    2) If figure/table/page hinted or retrieval weak -> render ONLY cited pages and run multimodal once.
    """
    # Support absolute local paths too
    if os.path.isabs(server_relative_url) and os.path.exists(server_relative_url):
        pdf_bytes = _download_file_bytes_local(server_relative_url)
        doc_id = server_relative_url
    else:
        pdf_bytes = _download_file_bytes(ctx, server_relative_url)
        doc_id = server_relative_url

    pack = build_index_for_pdf(pdf_bytes, doc_id)
    if pack is None:
        media = prepare_media_for_file(ctx, server_relative_url, filename)  # uses MAX_PDF_PAGES
        return ask_about_media(question, media)

    if pinecone_enabled():
        ctx_chunks, cites, scores = pinecone_retrieve(doc_id, question, topk=topk)
        if not ctx_chunks:
            ctx_chunks, cites, scores = retrieve_from_index(pack, question, topk=topk)
    else:
        ctx_chunks, cites, scores = retrieve_from_index(pack, question, topk=topk)

    wants_figures = bool(FIGURE_HINTS.search(question))
    weak = retrieval_looks_weak(scores, ctx_chunks)

    text_answer = answer_from_text_ctx(
        question,
        [f"(p. {c['page']}) " + chunk for chunk, c in zip(ctx_chunks, cites)]
    )

    if not wants_figures and not weak:
        return text_answer

    cited_pages = pick_fallback_pages(cites, pad=1, max_pages=MAX_PDF_PAGES)
    media = pages_to_media(pdf_bytes, cited_pages, max_pages_each_call=MAX_PDF_PAGES)
    vision_answer = ask_about_media(question, media)

    return f"{vision_answer}\n\n— Retrieved pages: {', '.join(str(p) for p in cited_pages)}"

# ------------------- CLI test harness -------------------
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", help="SharePoint site override")
    parser.add_argument("--folder", help="Server-relative folder (under library)")
    parser.add_argument("--file", help="Server-relative file path or ABSOLUTE local path to index/ask")
    parser.add_argument("--question", help="Question to ask", default="Summarize the key findings.")
    parser.add_argument("--extract", help="Run LangExtract with these instructions (optional)")
    args = parser.parse_args()

    agent = SharePointAgent(site_url=args.site or None, folder=args.folder or None)

    if not args.file:
        print("List few files for reference:")
        for it in agent.list_available_files(recursive=True)[:10]:
            print(f"- {it['server_relative_url']} ({it['name']})")
        raise SystemExit("Pass --file to ingest/ask")

    sr = args.file
    fname = os.path.basename(sr)
    print(f"Asking about: {sr}\nQ: {args.question}\n")
    print(hybrid_answer_pdf(agent.ctx, sr, fname, args.question))

    if args.extract:
        print("\n[LangExtract] Running structured extraction...")
        pdf_bytes = _download_file_bytes(agent.ctx, sr)
        pack = build_index_for_pdf(pdf_bytes, doc_id=sr)
        if pack and langextract_available():
            structured = langextract_structured_from_pack(pack, task_instructions=args.extract)
            print(f"Extracted {len(structured)} record(s). Example:\n", structured[:1])
            if pinecone_enabled():
                n = upsert_langextract_records_to_pinecone(sr, structured)
                print(f"[pinecone] upserted {n} structured record vectors.")
        else:
            print("No text index available or LangExtract not installed.")
