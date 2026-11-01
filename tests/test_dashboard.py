"""Checks on the single-file dashboard that need no browser."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = (Path(__file__).resolve().parents[1] / "openring" / "static" / "index.html").read_text(encoding="utf-8")


def run_js(code: str):
    """Run a script with Node.js and return what it prints as JSON (skips without Node.js)."""
    node = shutil.which("node")
    if not node:
        pytest.skip("needs Node.js")
    out = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=60, check=True).stdout
    return json.loads(out)


def test_campaigns_warn_about_calls_outside_8am_to_9pm_where_the_person_is():
    start = PAGE.index("const US_ZONES")
    code = PAGE[start:PAGE.index("\n}\n", PAGE.index("function callingHours")) + 2]
    checks = """
    const at = (numbers, start, end) => callingHours(numbers, new Date(start), end && new Date(end), "Europe/Lisbon")
      .map(x => x.zone + " " + x.at.toISOString());
    console.log(JSON.stringify({
      // 8 AM in New York on 2 Nov 2026 is 5 AM in Los Angeles, and 6 AM in Denver, 7 AM in Chicago.
      us: at(["+12125550100"], "2026-11-02T13:00:00Z"),
      hawaii: at(["+18085550100"], "2026-11-02T17:00:00Z"),
      saoPaulo: at(["+5511912345678"], "2026-11-02T12:00:00Z"),
      acre: at(["+5568912345678"], "2026-11-02T12:00:00Z"),
      lateWindow: at(["+5511912345678"], "2026-11-02T23:30:00Z", "2026-11-03T01:00:00Z"),
      unknownCode: at(["+999123456789"], "2026-11-02T06:00:00Z"),
      london: at(["+447700900123"], "2026-11-02T10:00:00Z"),
    }));
    """
    got = run_js(code + checks)
    assert got["us"] == ["America/Chicago 2026-11-02T13:00:00.000Z", "America/Denver 2026-11-02T13:00:00.000Z",
                         "America/Los_Angeles 2026-11-02T13:00:00.000Z"]
    # 7 AM in Honolulu, past 9 AM on the whole mainland: the area code says Hawaii.
    assert got["hawaii"] == ["Pacific/Honolulu 2026-11-02T17:00:00.000Z"]
    assert got["saoPaulo"] == []  # 9 AM
    assert got["acre"] == ["America/Rio_Branco 2026-11-02T12:00:00.000Z"]  # 7 AM: Acre is two hours behind Brasília
    # 8:30 PM to 10 PM in São Paulo: the first check past 9 PM is at 9:15 PM.
    assert got["lateWindow"] == ["America/Sao_Paulo 2026-11-03T00:15:00.000Z"]
    assert got["unknownCode"] == ["Europe/Lisbon 2026-11-02T06:00:00.000Z"]  # this computer's zone: 6 AM
    assert got["london"] == []


def test_dates_use_the_interface_language():
    # The page is in English; dates in the browser's language read "30 de out." among English words.
    assert not re.search(r"toLocale(Date|Time)?String\(\s*(\[\]|undefined)?\s*[,)]", PAGE)
    assert 'const LOCALE = "en-US"' in PAGE
