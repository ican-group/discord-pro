"""Knowledge ingestion, storage, retrieval and evidence scoring (Phase 4K).

Standard library only (no new production dependency or hosted service), on the
existing standalone MongoDB through the ``Store`` protocol.

**Ownership.** ``knowledge_sources`` belong to an Enterprise (``organisation_id``
None) or to one Organisation; ancestry is server-set from the route and the
database, never from a client. ``knowledge_documents`` and
``knowledge_chunks`` copy the source's ``enterprise_id``/``organisation_id``.
Which sources a request may use is decided by ``core.knowledge``
(``plan_for`` + ``authorised_sources``): Enterprise approval
(``knowledge.allowed_sources`` policy, deny by default, X13) ∩ Bot/Instance/
Integration selection (``knowledge.approved_sources``, narrowing only) ∩
access scope ∩ source status ∩ requester groups.

**Document access.** A document may be restricted to some Organisations
and/or Bots inside its source's scope; every chunk carries ``acl_scopes``
(``*`` or ``org:<id>`` / ``bot:<id>``).

**Retrieval enforces access in the query itself:** one ``find`` on
``knowledge_chunks`` with ``enterprise_id`` = the requester's Enterprise,
``source_id`` ∈ the authorised set, ``acl_scopes`` ∈ {``*``, its Organisation,
its Bot} and ``terms`` ∈ the query terms. Nothing outside that filter is ever
read. Candidates are then re-checked (tenant, source, ACL) and their
documents' *current* state is verified (status ``ready``, same version, source
active), so revoked, deleted or superseded content is never returned, even
mid-update. There is no evidence cache.

**Scoring.** BM25 ranks candidates; the evidence score (0..1) is the share of
the query's IDF-weighted information the chunk contains, over the query terms
the authorised corpus knows, scaled by the share of query terms it knows
(``0.5 + 0.5·known/total``: a query about things the knowledge never mentions
cannot score high), compared against
the effective ``knowledge.min_evidence_score`` (which descendants cannot lower
below the Enterprise floor). Contradiction detection is a deterministic
heuristic: two strong chunks from different documents that share most of
their content words but state different numbers, or differ by a negation.

**Ingestion.** Size and type limits, safe text extraction (plain text,
Markdown, HTML without scripts, CSV, JSON, DOCX through a bounded ZIP read
with no XML entity expansion); PDF is refused. Nothing in a document is
executed. Ingestion is idempotent per (source, document name, content hash),
versioned (new chunks are written first, then the document's version flips,
then old chunks are removed) and retryable by the worker.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import zipfile
from datetime import datetime, timedelta
from html.parser import HTMLParser
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from core.hierarchy import Chain, ConcurrentModification, DuplicateKey, is_valid_id
from core.knowledge import (Evidence, KnowledgePlan, KnowledgeSource, RetrievalResult, RetrievalStatus, SourceKind,
                            SourceStatus, normalise_text, numbers, tokens)

SOURCES = "knowledge_sources"
DOCUMENTS = "knowledge_documents"
CHUNKS = "knowledge_chunks"
FILES = "knowledge_files"
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_TEXT_CHARS = 1_500_000
MAX_CHUNKS = 2000
CHUNK_CHARS = 900
CHUNK_OVERLAP = 150
MAX_QUERY_TERMS = 32
MAX_CANDIDATES = 400
TOP_K = 6
JOB_LEASE_SECONDS = 300
MAX_JOB_ATTEMPTS = 5
BM25_K1, BM25_B = 1.2, 0.75
CONTENT_TYPES = {
    "text/plain": "text", "text/markdown": "text", "text/html": "html", "text/csv": "csv",
    "application/json": "json",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/pdf": "pdf",                     # Phase 4F (K-E amendment): sandboxed, bounded, no OCR
}
PDF_MAX_PAGES = 300
PDF_CPU_SECONDS = 10
PDF_WALL_SECONDS = 20
PDF_MEMORY_BYTES = 512 * 1024 * 1024
ZIP_MAX_ENTRIES = 2000
ZIP_MAX_UNCOMPRESSED = 25 * 1024 * 1024


class IngestError(ValueError):
    """Safe category for admins (never document content)."""


def stable_id(name: str) -> str:
    h = hashlib.sha256(("bitbotpro:knowledge:" + name).encode()).hexdigest()
    return f"{h[0:8]}-{h[8:12]}-4{h[13:16]}-{'89ab'[int(h[16], 16) % 4]}{h[17:20]}-{h[20:32]}"


# ═════════════════════════════════ extraction ═════════════════════════════════

class _HTMLText(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "iframe", "object", "svg", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: List[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in ("p", "br", "div", "li", "h1", "h2", "h3", "h4", "tr", "section"):
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise IngestError("The document is not valid UTF-8 text.")


def _pdf_limits() -> None:                                   # runs in the child before exec
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (PDF_CPU_SECONDS, PDF_CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_AS, (PDF_MEMORY_BYTES, PDF_MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))


PDF_ERRORS = {"not_a_pdf": "The file is not a PDF.", "encrypted": "Encrypted PDFs are not supported.",
              "too_many_pages": f"PDFs are limited to {PDF_MAX_PAGES} pages.",
              "too_much_text": "The document contains too much text.",
              "no_text": "No text could be extracted: scanned/image-only PDFs need OCR, which is not supported.",
              "malformed": "The PDF could not be read.", "too_large": "The PDF is too complex to process.",
              "extractor_unavailable": "PDF extraction is not available on this server."}


async def extract_pdf(data: bytes) -> str:
    """Text of a PDF via an isolated child process with hard CPU/memory/file
    limits, a wall-clock timeout, no network and a minimal environment."""
    import asyncio
    import os
    import sys
    if len(data) > MAX_UPLOAD_BYTES:
        raise IngestError("The document is too large.")
    if not data.startswith(b"%PDF-"):
        raise IngestError(PDF_ERRORS["not_a_pdf"])
    worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pdf_extract_worker.py")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-I", worker, str(PDF_MAX_PAGES), str(MAX_TEXT_CHARS),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        preexec_fn=_pdf_limits, env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
        cwd="/")
    try:
        out, _ = await asyncio.wait_for(proc.communicate(data), timeout=PDF_WALL_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise IngestError("The PDF took too long to process.")
    if proc.returncode != 0 and not out:
        raise IngestError(PDF_ERRORS["too_large"])               # killed by a resource limit
    try:
        result = json.loads(out.decode("utf-8") or "{}")
    except ValueError:
        raise IngestError(PDF_ERRORS["malformed"])
    if not result.get("ok"):
        raise IngestError(PDF_ERRORS.get(result.get("error"), PDF_ERRORS["malformed"]))
    return _clean(str(result.get("text") or ""))


async def extract_any(data: bytes, content_type: str) -> str:
    kind = CONTENT_TYPES.get((content_type or "").split(";")[0].strip().lower())
    if kind == "pdf":
        return await extract_pdf(data)
    return extract_text(data, content_type)


def _clean(text: str) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    if not text:
        raise IngestError("No text could be extracted from the document.")
    if len(text) > MAX_TEXT_CHARS:
        raise IngestError("The document contains too much text.")
    return text


def extract_text(data: bytes, content_type: str) -> str:
    kind = CONTENT_TYPES.get((content_type or "").split(";")[0].strip().lower())
    if kind is None:
        raise IngestError("Unsupported document type (supported: text, Markdown, HTML, CSV, JSON, DOCX, PDF).")
    if kind == "pdf":
        raise IngestError("PDF extraction is asynchronous (use extract_any).")
    if len(data) > MAX_UPLOAD_BYTES:
        raise IngestError("The document is too large.")
    if not data:
        raise IngestError("The document is empty.")
    if kind == "text":
        text = _decode(data)
    elif kind == "html":
        p = _HTMLText()
        try:
            p.feed(_decode(data))
            p.close()
        except Exception:
            raise IngestError("The HTML document could not be read.")
        text = "".join(p.out)
    elif kind == "csv":
        try:
            rows = list(csv.reader(io.StringIO(_decode(data))))
        except csv.Error:
            raise IngestError("The CSV document could not be read.")
        text = "\n".join(", ".join(c.strip() for c in r if c.strip()) for r in rows[:50000])
    elif kind == "json":
        try:
            obj = json.loads(_decode(data))
        except ValueError:
            raise IngestError("The JSON document could not be read.")
        out: List[str] = []

        def walk(v, depth=0):
            if depth > 20 or len(out) > 100000:
                return
            if isinstance(v, str):
                out.append(v)
            elif isinstance(v, dict):
                for k, x in v.items():
                    if isinstance(x, (str, int, float)) and not isinstance(x, bool):
                        out.append(f"{k}: {x}")
                    else:
                        walk(x, depth + 1)
            elif isinstance(v, list):
                for x in v:
                    walk(x, depth + 1)
        walk(obj)
        text = "\n".join(out)
    else:
        text = _docx_text(data)
    return _clean(text)


def _docx_text(data: bytes) -> str:
    """DOCX = ZIP + XML. Bounded ZIP read (entry count, total and single-entry
    uncompressed size: zip-bomb safe), and text taken with a regex from
    ``word/document.xml``: no XML parser, so no entity expansion; DTDs refused."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        infos = zf.infolist()
    except (zipfile.BadZipFile, ValueError):
        raise IngestError("The DOCX document could not be read.")
    if len(infos) > ZIP_MAX_ENTRIES or sum(i.file_size for i in infos) > ZIP_MAX_UNCOMPRESSED:
        raise IngestError("The DOCX document is too large when unpacked.")
    try:
        info = zf.getinfo("word/document.xml")
    except KeyError:
        raise IngestError("The DOCX document has no text part.")
    if info.file_size > 10 * 1024 * 1024:
        raise IngestError("The DOCX document is too large when unpacked.")
    with zf.open(info) as fh:
        xml = fh.read(10 * 1024 * 1024 + 1).decode("utf-8", errors="replace")
    if "<!DOCTYPE" in xml or "<!ENTITY" in xml:
        raise IngestError("The DOCX document contains a DTD and was refused.")
    paragraphs = []
    for para in re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.S):
        runs = re.findall(r"<w:t(?: [^>]*)?>(.*?)</w:t>", para, flags=re.S)
        if runs:
            paragraphs.append("".join(runs))
    import html as _html
    return "\n\n".join(_html.unescape(p) for p in paragraphs)


def chunk_text(text: str) -> List[Tuple[int, int, str]]:
    """Paragraph-aware windows of ~900 characters with overlap; (start, end, text)."""
    out: List[Tuple[int, int, str]] = []
    pos = 0
    n = len(text)
    while pos < n and len(out) < MAX_CHUNKS:
        end = min(n, pos + CHUNK_CHARS)
        if end < n:
            window = text[pos:end]
            cut = max(window.rfind("\n\n"), window.rfind(". "), window.rfind("\n"))
            if cut > CHUNK_CHARS // 2:
                end = pos + cut + 1
        piece = text[pos:end].strip()
        if piece:
            out.append((pos, end, piece))
        if end >= n:
            break
        pos = max(end - CHUNK_OVERLAP, pos + 1)
    return out


# ═════════════════════════════════ catalog ═════════════════════════════════

def source_from_doc(doc: dict) -> KnowledgeSource:
    try:
        status = SourceStatus(doc.get("status", "active"))
    except ValueError:
        status = SourceStatus.DISABLED
    return KnowledgeSource(id=doc["id"], enterprise_id=doc["enterprise_id"], organisation_id=doc.get("organisation_id"),
                           bot_id=None, kind=SourceKind.DOCUMENT, status=status,
                           acl_groups=frozenset(doc.get("acl_groups") or ()))


class StoreCatalog:
    """Current source records of one Enterprise (no cache: read per request)."""

    def __init__(self, store):
        self.store = store

    async def sources(self, enterprise_id: str) -> List[KnowledgeSource]:
        if not is_valid_id(enterprise_id):
            return []
        return [source_from_doc(d) for d in await self.store.find(SOURCES, {"enterprise_id": enterprise_id})
                if d.get("enterprise_id") == enterprise_id]


# ═════════════════════════════════ retrieval ═════════════════════════════════

def acl_scopes_for(chain: Chain) -> List[str]:
    out = ["*"]
    if chain.organisation is not None:
        out.append(f"org:{chain.organisation.id}")
    if chain.bot is not None:
        out.append(f"bot:{chain.bot.id}")
    return out


def _conflicting(evidence: Sequence[Evidence]) -> FrozenSet[str]:
    """Deterministic contradiction heuristic (documented limits): two chunks of
    *different documents* about the same thing (≥ 60% shared content words)
    that state different numbers, or where only one is negated."""
    out = set()
    neg = {"no", "not", "never", "cannot", "can't", "none"}
    for i, a in enumerate(evidence):
        ta, na = set(tokens(a.text)) - set(numbers(a.text)), numbers(a.text)
        wa = set(normalise_text(a.text).split())
        for b in evidence[i + 1:]:
            if a.document_id == b.document_id:
                continue
            tb, nb = set(tokens(b.text)) - set(numbers(b.text)), numbers(b.text)
            if not ta or not tb:
                continue
            overlap = len(ta & tb) / min(len(ta), len(tb))
            if overlap < 0.6:
                continue
            wb = set(normalise_text(b.text).split())
            if (na and nb and na != nb) or (bool(wa & neg) != bool(wb & neg)):
                out.update({a.evidence_id, b.evidence_id})
    return frozenset(out)


class StoreRetriever:
    """The real retrieval engine (replaces the Phase 4A test double)."""

    def __init__(self, store, *, top_k: int = TOP_K, clock=None):
        self.store = store
        self.top_k = top_k
        self.clock = clock

    def _now(self) -> datetime:
        from datetime import timezone
        return datetime.fromisoformat(self.clock()) if self.clock else datetime.now(timezone.utc)

    @staticmethod
    def _in_validity(doc: dict, now: datetime) -> bool:
        """Expired or not-yet-effective documents are never evidence (Phase 4F)."""
        for key, ok in (("valid_from", lambda t: now >= t), ("valid_until", lambda t: now < t)):
            value = doc.get(key)
            if value:
                try:
                    if not ok(datetime.fromisoformat(value)):
                        return False
                except ValueError:
                    return False                                  # unreadable window: fail closed
        return True

    async def retrieve(self, query: str, *, chain: Chain, plan: KnowledgePlan,
                       authorised: FrozenSet[str]) -> RetrievalResult:
        if not authorised:
            return RetrievalResult(RetrievalStatus.OK, ())
        q_terms = list(dict.fromkeys(tokens(query)))[:MAX_QUERY_TERMS]
        if not q_terms:
            return RetrievalResult(RetrievalStatus.OK, ())
        ent = chain.enterprise_id
        sources = sorted(authorised)
        scopes = acl_scopes_for(chain)
        base = {"enterprise_id": ent, "source_id": sources, "acl_scopes": scopes}
        # access control is part of the query: nothing outside it is ever read
        candidates = await self.store.find(CHUNKS, {**base, "terms": q_terms}, limit=MAX_CANDIDATES)
        candidates = [c for c in candidates if c.get("enterprise_id") == ent and c.get("source_id") in authorised
                      and set(c.get("acl_scopes") or ()) & set(scopes)]           # defence in depth
        if not candidates:
            return RetrievalResult(RetrievalStatus.OK, ())
        total = max(1, await self.store.count(CHUNKS, base))
        idf: Dict[str, float] = {}
        known: List[str] = []
        for t in q_terms:
            df = await self.store.count(CHUNKS, {**base, "terms": t})
            idf[t] = math.log(1 + (total - df + 0.5) / (df + 0.5))
            if df:
                known.append(t)
        # evidence score: share of the query's information (IDF mass) over the terms the
        # authorised corpus knows, scaled by how much of the query it knows at all
        info = sum(idf[t] for t in known) or 1.0
        known_share = len(known) / len(q_terms)
        avg_len = sum(int(c.get("length") or 1) for c in candidates) / len(candidates)
        scored = []
        for c in candidates:
            tf = c.get("tf") or {}
            present = [t for t in q_terms if t in tf]
            coverage = (sum(idf[t] for t in present) / info) * (0.5 + 0.5 * known_share)
            bm25 = 0.0
            for t in present:
                f = tf[t]
                bm25 += idf[t] * f * (BM25_K1 + 1) / (f + BM25_K1 * (1 - BM25_B + BM25_B * c.get("length", 1) / avg_len))
            scored.append((bm25, coverage, c))
        scored.sort(key=lambda x: (-x[0], x[2]["id"]))
        top = scored[: self.top_k * 3]
        # current-state check: document ready, same version, source still active (fail closed)
        docs = {d["id"]: d for d in await self.store.find(DOCUMENTS, {"enterprise_id": ent,
                                                                      "id": sorted({c["document_id"] for _, _, c in top})})}
        live_sources = {d["id"]: d for d in await self.store.find(SOURCES, {"enterprise_id": ent, "id": sources})}
        evidence: List[Evidence] = []
        now_dt = self._now()
        for bm25, coverage, c in top:
            d = docs.get(c["document_id"])
            src = live_sources.get(c["source_id"])
            if not d or not self._in_validity(d, now_dt) or d.get("status") != "ready" or \
                    d.get("content_version") != c.get("doc_version") or \
                    not src or src.get("status") != "active" or d.get("source_id") != c["source_id"]:
                continue
            evidence.append(Evidence(evidence_id=c["id"], source_id=c["source_id"], enterprise_id=ent,
                                     score=round(min(1.0, coverage), 4), text=c["text"], document_id=c["document_id"],
                                     document_version=int(c.get("doc_version") or 0),
                                     span=(int(c.get("start") or 0), int(c.get("end") or 0))))
            if len(evidence) >= self.top_k:
                break
        strong = [e for e in evidence if e.score >= plan.min_evidence_score]
        return RetrievalResult(RetrievalStatus.OK, tuple(evidence), conflicting=_conflicting(strong))

    async def verify(self, enterprise_id: str, evidence: Sequence[Evidence]) -> bool:
        """Re-check, just before delivery, that every cited evidence item is still
        live (Phase 4F: a source revoked or a document deleted/replaced/expired
        *during generation* must not be delivered)."""
        if not evidence:
            return True
        now_dt = self._now()
        docs = {d["id"]: d for d in await self.store.find(DOCUMENTS, {"enterprise_id": enterprise_id,
                                                                      "id": sorted({e.document_id for e in evidence})})}
        srcs = {d["id"]: d for d in await self.store.find(SOURCES, {"enterprise_id": enterprise_id,
                                                                    "id": sorted({e.source_id for e in evidence})})}
        for e in evidence:
            d, s = docs.get(e.document_id), srcs.get(e.source_id)
            if not d or not s or s.get("status") != "active" or d.get("status") != "ready" or \
                    d.get("content_version") != e.document_version or not self._in_validity(d, now_dt):
                return False
            if not await self.store.get(CHUNKS, e.evidence_id):
                return False
        return True


# ═════════════════════════════════ ingestion ═════════════════════════════════

def document_id_for(source_id: str, name: str) -> str:
    return stable_id(f"document:{source_id}:{name}")


def _acl_scopes(acl: Optional[Dict[str, List[str]]]) -> List[str]:
    acl = acl or {}
    scopes = [f"org:{o}" for o in acl.get("organisations") or []] + [f"bot:{b}" for b in acl.get("bots") or []]
    return sorted(scopes) or ["*"]


class Ingestor:
    def __init__(self, store, *, clock):
        self.store = store
        self.clock = clock

    async def submit(self, source: dict, *, name: str, content_type: str, data: bytes, actor_id: str) -> dict:
        """Create or update a document (idempotent on identical content) and
        process it inline; failures stay retryable by the worker."""
        if not re.fullmatch(r"[A-Za-z0-9 ._\-()]{1,200}", name or ""):
            raise IngestError("Invalid document name.")
        if (content_type or "").split(";")[0].strip().lower() not in CONTENT_TYPES:
            raise IngestError("Unsupported document type (supported: text, Markdown, HTML, CSV, JSON, DOCX, PDF).")
        if len(data) > MAX_UPLOAD_BYTES:
            raise IngestError("The document is too large.")
        did = document_id_for(source["id"], name)
        digest = hashlib.sha256(data).hexdigest()
        now = self.clock()
        for _ in range(8):
            doc = await self.store.get(DOCUMENTS, did)
            if doc is not None and ((doc.get("sha256") == digest and doc.get("status") == "ready"
                                     and not doc.get("pending_version")) or doc.get("pending_sha256") == digest):
                return doc                                                # idempotent re-upload
            if doc is not None and doc.get("status") == "deleting":
                raise IngestError("The document is being deleted.")
            target_version = max(int((doc or {}).get("content_version") or 0),
                                 int((doc or {}).get("pending_version") or 0)) + 1
            file_doc = {"id": f"{did}:{target_version}", "enterprise_id": source["enterprise_id"],
                        "organisation_id": source.get("organisation_id"), "source_id": source["id"],
                        "document_id": did, "data": data, "version": 1}
            try:
                await self.store.insert(FILES, file_doc)
            except DuplicateKey:
                pass
            pending = {"pending_version": target_version, "pending_sha256": digest, "content_type": content_type,
                       "size": len(data), "job": {"state": "queued", "attempts": 0, "lease_until": None,
                                                  "error": None}, "updated_at": now, "updated_by": actor_id}
            try:
                if doc is None:
                    await self.store.insert(DOCUMENTS, {
                        "id": did, "enterprise_id": source["enterprise_id"],
                        "organisation_id": source.get("organisation_id"), "source_id": source["id"], "name": name,
                        "status": "processing", "content_version": None, "sha256": None,
                        "acl": {"organisations": [], "bots": []}, "chunk_count": 0, "created_at": now,
                        "created_by": actor_id, "version": 1, **pending})
                    # "version" is the compare-and-swap counter; "content_version" is the live content
                else:
                    await self.store.update(DOCUMENTS, did, {**pending, "status": doc["status"] if doc["status"]
                                                             == "ready" else "processing"},
                                            expect_version=doc["version"])
                break
            except (DuplicateKey, ConcurrentModification):
                continue
        await self.process(did)
        return await self.store.get(DOCUMENTS, did)

    async def process(self, did: str) -> str:
        now = self.clock()
        doc = await self.store.get(DOCUMENTS, did)
        if doc is None or not doc.get("pending_version") or doc.get("status") == "deleting":
            return "nothing"
        job = doc.get("job") or {}
        if job.get("state") == "running" and job.get("lease_until") and \
                datetime.fromisoformat(now) < datetime.fromisoformat(job["lease_until"]):
            return "busy"
        if job.get("state") == "failed" and int(job.get("attempts", 0)) >= MAX_JOB_ATTEMPTS:
            return "failed"
        lease = (datetime.fromisoformat(now) + timedelta(seconds=JOB_LEASE_SECONDS)).isoformat()
        try:
            await self.store.update(DOCUMENTS, did, {"job": {**job, "state": "running", "lease_until": lease,
                                                             "attempts": int(job.get("attempts", 0)) + 1}},
                                    expect_version=doc["version"])
        except ConcurrentModification:
            return "busy"
        doc = await self.store.get(DOCUMENTS, did)
        target = int(doc["pending_version"])
        file_doc = await self.store.get(FILES, f"{did}:{target}")
        try:
            if file_doc is None:
                raise IngestError("The uploaded file is missing; upload it again.")
            text = await extract_any(bytes(file_doc["data"]), doc.get("content_type") or "")
            pieces = chunk_text(text)
        except IngestError as exc:
            await self._fail(did, str(exc), permanent=True)
            return "failed"
        except Exception:
            await self._fail(did, "Processing failed; it will be retried.", permanent=False)
            return "retry"
        scopes = _acl_scopes(doc.get("acl"))
        for i, (start, end, piece) in enumerate(pieces):
            terms = tokens(piece)
            tf: Dict[str, int] = {}
            for t in terms:
                tf[t] = tf.get(t, 0) + 1
            chunk = {"id": stable_id(f"chunk:{did}:{target}:{i}"), "enterprise_id": doc["enterprise_id"],
                     "organisation_id": doc.get("organisation_id"), "source_id": doc["source_id"],
                     "document_id": did, "doc_version": target, "index": i, "start": start, "end": end,
                     "text": piece, "terms": sorted(tf), "tf": tf, "length": max(1, len(terms)),
                     "acl_scopes": scopes, "version": 1}
            try:
                await self.store.insert(CHUNKS, chunk)
            except DuplicateKey:
                pass                                                      # resumed after a crash
        # flip the live version (CAS) only after every new chunk exists
        for _ in range(8):
            doc = await self.store.get(DOCUMENTS, did)
            if doc is None or doc.get("status") == "deleting" or int(doc.get("pending_version") or 0) != target:
                await self.store.delete_many(CHUNKS, {"enterprise_id": doc["enterprise_id"] if doc else "",
                                                      "document_id": did, "doc_version": target})
                return "superseded"
            try:
                await self.store.update(DOCUMENTS, did, {
                    "status": "ready", "content_version": target, "sha256": doc.get("pending_sha256"),
                    "pending_version": None, "pending_sha256": None, "chunk_count": len(pieces),
                    "job": {"state": "done", "attempts": int((doc.get("job") or {}).get("attempts", 1)),
                            "lease_until": None, "error": None}, "updated_at": self.clock()},
                    expect_version=doc["version"])
                break
            except ConcurrentModification:
                continue
        await self._purge_old(doc["enterprise_id"], did, keep=target)
        return "ready"

    async def _purge_old(self, enterprise_id: str, did: str, *, keep: int) -> None:
        for c in await self.store.find(CHUNKS, {"enterprise_id": enterprise_id, "document_id": did}):
            if c.get("doc_version") != keep:
                await self.store.delete(CHUNKS, c["id"])
        for f in await self.store.find(FILES, {"enterprise_id": enterprise_id, "document_id": did}):
            if f["id"] != f"{did}:{keep}":
                await self.store.delete(FILES, f["id"])

    async def _fail(self, did: str, error: str, *, permanent: bool) -> None:
        for _ in range(8):
            doc = await self.store.get(DOCUMENTS, did)
            if doc is None:
                return
            job = dict(doc.get("job") or {})
            job.update(state="failed" if permanent or int(job.get("attempts", 0)) >= MAX_JOB_ATTEMPTS else "queued",
                       lease_until=None, error=error[:200])
            fields = {"job": job, "updated_at": self.clock()}
            if doc.get("content_version") is None and doc.get("status") != "deleting":
                fields["status"] = "failed"
            try:
                await self.store.update(DOCUMENTS, did, fields, expect_version=doc["version"])
                return
            except ConcurrentModification:
                continue

    async def set_acl(self, did: str, acl: Dict[str, List[str]]) -> dict:
        """Re-label every chunk first, then the document (retrieval filters on the
        chunk label, so a narrowed ACL takes effect immediately)."""
        doc = await self.store.get(DOCUMENTS, did)
        scopes = _acl_scopes(acl)
        for c in await self.store.find(CHUNKS, {"enterprise_id": doc["enterprise_id"], "document_id": did}):
            await self.store.update(CHUNKS, c["id"], {"acl_scopes": scopes})
        await self.store.update(DOCUMENTS, did, {"acl": acl, "updated_at": self.clock()}, expect_version=doc["version"])
        return await self.store.get(DOCUMENTS, did)

    async def delete_document(self, did: str) -> None:
        """Secure deletion: mark ``deleting`` first (retrieval fails closed on it
        immediately), then remove chunks, stored files and the record."""
        for _ in range(8):
            doc = await self.store.get(DOCUMENTS, did)
            if doc is None:
                return
            try:
                await self.store.update(DOCUMENTS, did, {"status": "deleting"}, expect_version=doc["version"])
                break
            except ConcurrentModification:
                continue
        flt = {"enterprise_id": doc["enterprise_id"], "document_id": did}
        await self.store.delete_many(CHUNKS, flt)
        await self.store.delete_many(FILES, flt)
        await self.store.delete_many(DOCUMENTS, {"enterprise_id": doc["enterprise_id"], "id": did})

    async def delete_source(self, source: dict) -> None:
        await self.store.update(SOURCES, source["id"], {"status": "revoked"})       # unusable at once
        for d in await self.store.find(DOCUMENTS, {"enterprise_id": source["enterprise_id"],
                                                   "source_id": source["id"]}):
            await self.delete_document(d["id"])
        await self.store.delete_many(CHUNKS, {"enterprise_id": source["enterprise_id"], "source_id": source["id"]})
        await self.store.delete_many(SOURCES, {"enterprise_id": source["enterprise_id"], "id": source["id"]})

    async def maintain(self, *, limit: int = 20) -> int:
        """Worker: retry queued/stale jobs (bounded)."""
        n = 0
        for doc in await self.store.find(DOCUMENTS, {"job.state": ["queued", "running"]}, limit=limit):
            if await self.process(doc["id"]) == "ready":
                n += 1
        return n


__all__ = ["SOURCES", "DOCUMENTS", "CHUNKS", "FILES", "StoreCatalog", "StoreRetriever", "Ingestor", "IngestError",
           "extract_text", "chunk_text", "document_id_for", "stable_id", "MAX_UPLOAD_BYTES", "CONTENT_TYPES",
           "acl_scopes_for"]
