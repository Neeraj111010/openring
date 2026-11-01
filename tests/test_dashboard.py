"""Checks on the single-file dashboard that need no browser."""

import re
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[1] / "openring" / "static" / "index.html").read_text(encoding="utf-8")


def test_dates_use_the_interface_language():
    # The page is in English; dates in the browser's language read "30 de out." among English words.
    assert not re.search(r"toLocale(Date|Time)?String\(\s*(\[\]|undefined)?\s*[,)]", PAGE)
    assert 'const LOCALE = "en-US"' in PAGE
