"""Vapi's knowledge bases: files the assistant can look things up in during a call.

`POST /file` keeps a file's text, cut into passages, in the database; a `query` tool lists the files, and the model
searches them with the words it picks. The search runs on this computer with SQLite's full-text index: free, no
model to download, a few milliseconds per question. It matches words, not meanings. For meaning, point a knowledge
base at your own search with `provider: "custom-knowledge-base"`, as on Vapi.
"""

from __future__ import annotations

import html
import io
import re
import zipfile
from typing import Any

import httpx

from . import netguard
from .store import Store

MAX_FILE_BYTES = 20_000_000
MAX_TEXT_CHARS = 2_000_000  # what's kept of one file
MAX_PDF_PAGES = 2_000
CHUNK_CHARS = 900  # about a paragraph or two: enough to answer from, little enough to read on every turn after
OVERLAP_CHARS = 150  # so an answer on the edge of two passages is whole in one of them
MAX_PASSAGES = 4
MAX_ANSWER_CHARS = 4_000
MAX_QUERY_WORDS = 30
TEXT_TYPES = (".txt", ".md", ".csv", ".tsv", ".yaml", ".yml", ".json", ".xml", ".log")
CUSTOM = "custom-knowledge-base"
DEFAULT_TIMEOUT = 20
NOTHING = "The knowledge base has nothing on that. Say you don't know instead of guessing."


class KnowledgeError(ValueError):
    """A file that can't be read, or a knowledge base that can't answer."""


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16" if data[:2] in (b"\xff\xfe", b"\xfe\xff") else "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def _docx_text(data: bytes) -> str:
    """A .docx is a zip of XML. A regular expression instead of an XML parser: a file can't expand entities in it,
    and reading stops at the limit, so a small file can't unpack into a huge one."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as package, package.open("word/document.xml") as part:
            xml = part.read(MAX_TEXT_CHARS * 8).decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError, OSError, RuntimeError, NotImplementedError) as exc:
        raise KnowledgeError("this .docx can't be opened") from exc
    paragraphs = re.split(r"</w:p>", xml)
    words = ("".join(re.findall(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>", p)) for p in paragraphs)
    return "\n".join(html.unescape(line) for line in words if line.strip())


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise KnowledgeError("this PDF has a password: save a copy without one")
        pages = []
        size = 0
        for page in reader.pages[:MAX_PDF_PAGES]:
            pages.append(page.extract_text() or "")
            size += len(pages[-1])
            if size >= MAX_TEXT_CHARS:
                break
    except KnowledgeError:
        raise
    except (PyPdfError, ValueError, KeyError, TypeError, AttributeError, IndexError, RecursionError, OSError) as exc:
        raise KnowledgeError("this PDF can't be read") from exc
    return "\n\n".join(pages)


def extract_text(name: str, data: bytes) -> str:
    """A file's text; KnowledgeError when it has none OpenRing can read."""
    extension = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if extension in TEXT_TYPES or not extension:
        if b"\0" in data[:4096] and data[:2] not in (b"\xff\xfe", b"\xfe\xff"):
            raise KnowledgeError("this file isn't text")
        text = _decode(data)
    elif extension == ".docx":
        text = _docx_text(data)
    elif extension == ".pdf":
        text = _pdf_text(data)
    elif extension == ".doc":
        raise KnowledgeError("old .doc files can't be read: save it as .docx or .pdf")
    else:
        raise KnowledgeError(f"{extension} files can't be read: use .pdf, .docx, {', '.join(TEXT_TYPES)}")
    text = text.replace("\r\n", "\n").replace("\x00", "")[:MAX_TEXT_CHARS].strip()
    if not text:
        raise KnowledgeError("this file has no text to search (a scanned PDF, perhaps)")
    return text


def chunks(text: str) -> list[str]:
    """The text cut into passages, at line ends where it can be."""
    lines: list[str] = []
    for line in text.split("\n"):
        while len(line) > CHUNK_CHARS:  # one long line: cut at a space
            cut = line.rfind(" ", 0, CHUNK_CHARS)
            cut = cut if cut > 0 else CHUNK_CHARS
            lines.append(line[:cut])
            line = line[cut:].lstrip()
        lines.append(line)
    passages: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if current and size + len(line) > CHUNK_CHARS:
            passages.append("\n".join(current).strip())
            kept: list[str] = []
            size = 0
            for earlier in reversed(current):  # the end of this passage starts the next
                if size + len(earlier) > OVERLAP_CHARS:
                    break
                kept.insert(0, earlier)
                size += len(earlier) + 1
            current = kept
        current.append(line)
        size += len(line) + 1
    if "\n".join(current).strip():
        passages.append("\n".join(current).strip())
    return [p for p in passages if p]


def match_query(text: str) -> str | None:
    """The question as a full-text query: any of its words, longer ones also as the start of a word ("horario"
    finds "horarios"). Only letters and digits get through, so nothing in it can act as query syntax."""
    words = list(dict.fromkeys(re.findall(r"[^\W_]+", text.lower())))[:MAX_QUERY_WORDS]
    return " OR ".join(f'"{w}"*' if len(w) >= 4 else f'"{w}"' for w in words) or None


def search(store: Store, file_ids: list[str], query: str) -> list[str]:
    match = match_query(query)
    return store.search_chunks(file_ids, match, MAX_PASSAGES) if match and file_ids else []


def _shorten(passages: list[str]) -> str:
    return "\n\n---\n\n".join(passages)[:MAX_ANSWER_CHARS]


async def ask_server(
    base: dict[str, Any], query: str, messages: list[dict[str, Any]], call: dict[str, Any] | None,
    *, client: httpx.AsyncClient | None = None,
) -> str:
    """Vapi's `knowledge-base-request` to your own search. It answers `documents`, or a `custom-message` to use."""
    server = base.get("server") or {}
    url = str(server.get("url") or "")
    headers = {str(k): str(v) for k, v in (server.get("headers") or {}).items()}
    if server.get("secret"):
        headers.setdefault("X-Vapi-Secret", str(server["secret"]))
    try:
        await netguard.check_url_async(url)
    except netguard.UnsafeURL as exc:
        raise KnowledgeError(f"the knowledge base's address is refused ({exc})") from exc
    payload = {"message": {"type": "knowledge-base-request", "query": query, "messages": messages, "call": call}}
    owned = client is None
    client = client or netguard.client()
    try:
        r = await client.post(url, json=payload, headers=headers,
                              timeout=float(server.get("timeoutSeconds") or DEFAULT_TIMEOUT))
    except httpx.HTTPError as exc:  # never the URL: the model reads this, and it often carries a secret
        raise KnowledgeError(f"could not reach the knowledge base ({exc.__class__.__name__})") from exc
    finally:
        if owned:
            await client.aclose()
    if r.status_code >= 400:
        raise KnowledgeError(f"the knowledge base answered HTTP {r.status_code}")
    try:
        body = r.json()
    except ValueError as exc:
        raise KnowledgeError("the knowledge base's answer is not JSON") from exc
    body = body if isinstance(body, dict) else {}
    message = body.get("message") if isinstance(body.get("message"), dict) else {}
    if message.get("content"):
        return str(message["content"])[:MAX_ANSWER_CHARS]
    documents = [d for d in body.get("documents") or [] if isinstance(d, dict) and d.get("content")]
    return _shorten([str(d["content"]) for d in documents]) or NOTHING


async def answer(
    tool: dict[str, Any], query: str, *, store: Store | None, messages: list[dict[str, Any]],
    call: dict[str, Any] | None,
) -> str:
    """What the model reads back from a `query` tool: passages from its files, and from any server of its own."""
    import asyncio

    bases = [b for b in tool.get("knowledgeBases") or [] if isinstance(b, dict)]
    found: list[str] = []
    for base in (b for b in bases if b.get("provider") == CUSTOM):
        found.append(await ask_server(base, query, messages, call))
    found = [text for text in found if text != NOTHING]
    file_ids = [str(i) for b in bases if b.get("provider") != CUSTOM for i in b.get("fileIds") or []]
    if store is not None:
        found += await asyncio.to_thread(search, store, file_ids, query)
    return _shorten(found) or NOTHING


def check(base: Any, where: str = "knowledgeBases") -> None:
    """Refuse a knowledge base that can't answer anything."""
    if not isinstance(base, dict):
        raise KnowledgeError(f"{where} must be objects with fileIds, or a custom-knowledge-base server")
    if base.get("provider") == CUSTOM:
        server = base.get("server") if isinstance(base.get("server"), dict) else {}
        if not str(server.get("url") or "").startswith(("https://", "http://")):
            raise KnowledgeError(f"a {CUSTOM} needs server.url, starting with https:// or http://")
        return
    ids = base.get("fileIds")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
        raise KnowledgeError(f"{where} needs fileIds: the ids POST /file answers")
