"""Isolated PDF text extraction (Phase 4F, K-E amendment). Executed ONLY as a
child process by ``runtime.knowledge_store.extract_pdf``:

    python -I runtime/pdf_extract_worker.py <max_pages> <max_chars>   (PDF bytes on stdin)

The parent applies hard resource limits before this code runs (CPU seconds,
address space, open files, no core dumps) plus a wall-clock timeout, so a
malformed or hostile PDF (infinite loops, decompression bombs) can only kill
this child. Here, before ``pypdf`` is imported, networking is disabled; pypdf
itself never executes JavaScript, launches actions, opens attachments,
resolves external references or touches the filesystem. Output is one JSON
line: ``{"ok": true, "pages": n, "text": "..."}`` or ``{"ok": false,
"error": "<category>"}``. Nothing from the document is ever executed.
"""
import io
import json
import socket
import sys


def _no_network(*_a, **_k):
    raise OSError("network disabled in the PDF extractor")


def main() -> None:
    socket.socket = _no_network                      # type: ignore[assignment]
    socket.create_connection = _no_network           # type: ignore[assignment]
    max_pages, max_chars = int(sys.argv[1]), int(sys.argv[2])
    data = sys.stdin.buffer.read()

    def out(**kw):
        sys.stdout.write(json.dumps(kw))
        sys.stdout.flush()

    if not data.startswith(b"%PDF-"):
        return out(ok=False, error="not_a_pdf")
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except Exception:
        return out(ok=False, error="extractor_unavailable")
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            return out(ok=False, error="encrypted")
        n = len(reader.pages)
        if n > max_pages:
            return out(ok=False, error="too_many_pages")
        parts, total = [], 0
        for page in reader.pages:
            text = page.extract_text() or ""
            total += len(text)
            if total > max_chars:
                return out(ok=False, error="too_much_text")
            parts.append(text)
        text = "\n\n".join(p.strip() for p in parts if p and p.strip())
        if not text.strip():
            return out(ok=False, error="no_text", pages=n)      # image-only / scanned: OCR not supported
        return out(ok=True, pages=n, text=text)
    except PdfReadError:
        return out(ok=False, error="malformed")
    except MemoryError:
        return out(ok=False, error="too_large")
    except RecursionError:
        return out(ok=False, error="malformed")
    except Exception:
        return out(ok=False, error="malformed")


if __name__ == "__main__":
    main()
