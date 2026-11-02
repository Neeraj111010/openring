"""Reads one PDF in a process of its own (see `knowledge.pdf_text`): the file on stdin, JSON on stdout."""

from __future__ import annotations

import io
import json
import os
import sys
import threading


def read(data: bytes, max_pages: int, max_chars: int) -> dict[str, str]:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            return {"error": "this PDF has a password: save a copy without one"}
        pages: list[str] = []
        size = 0
        for page in reader.pages[:max_pages]:
            pages.append(page.extract_text() or "")
            size += len(pages[-1])
            if size >= max_chars:
                break
    except Exception:  # whatever a broken or hostile file does to the reader
        return {"error": "this PDF can't be read"}
    return {"text": "\n\n".join(pages)}


def main() -> None:
    seconds, max_pages, max_chars = float(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
    # Killing a process can miss the real one behind a Windows launcher, so it also stops by itself.
    timer = threading.Timer(seconds + 1, os._exit, [3])
    timer.daemon = True
    timer.start()
    if hasattr(os, "nice"):  # what's left of the computer goes to the calls
        os.nice(10)
    sys.stdout.write(json.dumps(read(sys.stdin.buffer.read(), max_pages, max_chars)))
    sys.stdout.flush()
    os._exit(0)  # without waiting for a timer or a stuck thread


if __name__ == "__main__":
    main()
