"""File upload management — stores files, extracts content for LLM context."""

import base64
import html
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path

from core import BASE_DIR, SETTINGS

log = logging.getLogger(__name__)

UPLOAD_DIR = BASE_DIR / "data" / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# Max file sizes
MAX_IMAGE_MB = 10
MAX_DOC_MB = 25

# Supported types
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/heic", "image/heif"}
DOC_TYPES = {"application/pdf", "text/plain", "text/markdown", "text/html", "text/csv",
             "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
ALL_TYPES = IMAGE_TYPES | DOC_TYPES


def save_upload(filename: str, data: bytes, content_type: str) -> dict:
    """Save an uploaded file and return metadata + extracted content.

    Returns:
        {
            "file_id": str,
            "filename": str,
            "content_type": str,
            "size": int,
            "path": str,
            "extracted_text": str (for docs) or None,
            "base64": str (for images) or None,
        }
    """
    content_type = _normalize_content_type(filename, content_type)
    if content_type not in ALL_TYPES:
        return {"error": f"Unsupported file type: {content_type}"}

    max_bytes = MAX_IMAGE_MB * 1024 * 1024 if content_type in IMAGE_TYPES else MAX_DOC_MB * 1024 * 1024
    if len(data) > max_bytes:
        return {"error": f"File too large: {len(data) / 1024 / 1024:.1f}MB (max {max_bytes / 1024 / 1024}MB)"}

    # Generate file ID
    file_id = hashlib.sha256(f"{time.time()}{filename}".encode()).hexdigest()[:16]
    ext = Path(filename).suffix or _guess_ext(content_type)
    stored_name = f"{file_id}{ext}"
    stored_path = UPLOAD_DIR / stored_name

    # Save to disk
    stored_path.write_bytes(data)
    log.info(f"Saved upload: {filename} -> {stored_name} ({len(data)} bytes)")

    result = {
        "file_id": file_id,
        "filename": filename,
        "content_type": content_type,
        "size": len(data),
        "path": str(stored_path),
    }

    # Extract content based on type
    if content_type in IMAGE_TYPES:
        result["base64"] = base64.b64encode(data).decode("utf-8")
        result["extracted_text"] = None
    elif content_type == "text/plain" or content_type == "text/markdown" or content_type == "text/csv":
        result["extracted_text"] = data.decode("utf-8", errors="replace")
        result["base64"] = None
    elif content_type == "text/html":
        text = data.decode("utf-8", errors="replace")
        # Strip HTML tags for context
        import re
        clean = re.sub(r'<[^>]+>', ' ', text)
        clean = re.sub(r'\s+', ' ', clean).strip()
        result["extracted_text"] = clean
        result["base64"] = None
    elif content_type == "application/pdf":
        result["extracted_text"] = _extract_pdf_text(data)
        result["base64"] = None
    elif "wordprocessingml" in content_type:
        result["extracted_text"] = _extract_docx_text(stored_path)
        result["base64"] = None
    else:
        result["extracted_text"] = None
        result["base64"] = None

    editable_copy = _materialize_editable_copy(
        file_id=file_id,
        filename=filename,
        content_type=content_type,
        stored_path=stored_path,
        extracted_text=result.get("extracted_text"),
    )
    if editable_copy:
        result["editable_filename"] = editable_copy["filename"]
        result["editable_path"] = str(editable_copy["path"])

    # Save metadata
    meta_path = UPLOAD_DIR / f"{file_id}.json"
    meta_path.write_text(json.dumps({
        "file_id": file_id,
        "filename": filename,
        "content_type": content_type,
        "size": len(data),
        "uploaded_at": time.time(),
        "stored_name": stored_name,
        "editable_filename": result.get("editable_filename"),
    }, indent=2))

    return result


def get_upload(file_id: str) -> dict:
    """Retrieve upload metadata and content."""
    meta_path = UPLOAD_DIR / f"{file_id}.json"
    if not meta_path.exists():
        return {"error": "File not found"}
    meta = json.loads(meta_path.read_text())
    stored_path = UPLOAD_DIR / meta["stored_name"]
    if not stored_path.exists():
        return {"error": "File data missing"}
    meta["path"] = str(stored_path)
    return meta


def build_attachment_block(upload_result: dict) -> dict:
    """Convert upload result to Anthropic API content block."""
    if upload_result.get("base64"):
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": upload_result["content_type"],
                "data": upload_result["base64"],
            }
        }
    elif upload_result.get("extracted_text"):
        return {
            "type": "text",
            "text": f"[Uploaded file: {upload_result['filename']}]\n\n{upload_result['extracted_text'][:8000]}"
        }
    return {"type": "text", "text": f"[Uploaded file: {upload_result['filename']} — content could not be extracted]"}


def _extract_pdf_text(data: bytes) -> str:
    """Extract text from PDF bytes."""
    try:
        import subprocess
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(data)
            f.flush()
            # Try pdftotext first (poppler)
            result = subprocess.run(
                ["pdftotext", f.name, "-"],
                capture_output=True, text=True, timeout=30
            )
            os.unlink(f.name)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()[:10000]
    except Exception:
        pass
    # Fallback: basic text extraction
    try:
        text = data.decode("latin-1", errors="replace")
        import re
        # Extract text between BT/ET blocks (very basic PDF text extraction)
        chunks = re.findall(r'\((.*?)\)', text)
        return " ".join(chunks)[:5000]
    except Exception:
        return "[PDF content could not be extracted]"


def _extract_docx_text(path: Path) -> str:
    """Extract text from .docx file."""
    try:
        import zipfile
        import xml.etree.ElementTree as ET
        with zipfile.ZipFile(path) as z:
            with z.open("word/document.xml") as f:
                tree = ET.parse(f)
                ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
                paragraphs = tree.findall(".//w:p", ns)
                texts = []
                for p in paragraphs:
                    runs = p.findall(".//w:t", ns)
                    texts.append("".join(r.text or "" for r in runs))
                return "\n".join(texts)[:10000]
    except Exception as e:
        return f"[DOCX extraction failed: {e}]"


def _guess_ext(content_type: str) -> str:
    """Guess file extension from content type."""
    mapping = {
        "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
        "image/webp": ".webp", "image/heic": ".heic", "image/heif": ".heif", "application/pdf": ".pdf",
        "text/plain": ".txt", "text/markdown": ".md", "text/html": ".html",
        "text/csv": ".csv",
    }
    return mapping.get(content_type, ".bin")


def _normalize_content_type(filename: str, content_type: str) -> str:
    """Browsers sometimes send useful files as application/octet-stream."""
    ct = str(content_type or "").split(";", 1)[0].strip().lower()
    suffix = Path(filename or "").suffix.lower()
    if ct and ct != "application/octet-stream":
        return ct
    by_ext = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".heic": "image/heic",
        ".heif": "image/heif",
        ".pdf": "application/pdf",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".html": "text/html",
        ".htm": "text/html",
        ".csv": "text/csv",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    return by_ext.get(suffix, ct or "application/octet-stream")


def _editable_output_dir() -> Path:
    output_dir = Path(
        os.path.expanduser(
            SETTINGS.get("outputs", {}).get("default_path", "~/Documents/Claude Outputs")
        )
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def _sanitize_upload_stem(filename: str) -> str:
    stem = Path(filename or "upload").stem.strip() or "upload"
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "", stem).strip()
    return cleaned or "upload"


def _wrap_text_as_editable_html(title: str, text: str) -> str:
    blocks = []
    for chunk in re.split(r"\n\s*\n", str(text or "").strip()):
        chunk = chunk.strip()
        if not chunk:
            continue
        blocks.append(f"<p>{html.escape(chunk).replace(chr(10), '<br>')}</p>")
    body = "\n".join(blocks) or "<p></p>"
    safe_title = html.escape(title or "Uploaded Document")
    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        f"<title>{safe_title}</title></head><body><h1>{safe_title}</h1>{body}</body></html>"
    )


def _materialize_editable_copy(
    file_id: str,
    filename: str,
    content_type: str,
    stored_path: Path,
    extracted_text: str | None,
) -> dict | None:
    editable_types = {
        "text/plain",
        "text/markdown",
        "text/html",
        "text/csv",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    if content_type not in editable_types:
        return None

    title = _sanitize_upload_stem(filename).replace("_", " ").strip() or "Uploaded Document"
    editable_name = f"{title} - uploaded-{file_id[:6]}.html"
    editable_path = _editable_output_dir() / editable_name

    if content_type == "text/html":
        html_content = stored_path.read_text(errors="replace")
        if "<html" not in html_content.lower() and "<body" not in html_content.lower():
            html_content = _wrap_text_as_editable_html(title, html_content)
    else:
        html_content = _wrap_text_as_editable_html(title, extracted_text or "")

    editable_path.write_text(html_content)
    return {"filename": editable_name, "path": editable_path}
