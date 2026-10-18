"""Slack app icons generated from agents' names."""
import struct
import zlib

from starlette.testclient import TestClient

from agentic_lab.icon import SIZE, pattern, png
from agentic_lab.secretary import web


def decode(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", data[16:24])
    i, idat = 8, b""
    while i < len(data):
        n = struct.unpack(">I", data[i:i + 4])[0]
        kind, body = data[i + 4:i + 8], data[i + 8:i + 8 + n]
        assert struct.unpack(">I", data[i + 8 + n:i + 12 + n])[0] == zlib.crc32(kind + body)
        if kind == b"IDAT":
            idat += body
        i += 12 + n
    return w, h, zlib.decompress(idat)


def test_icon_is_a_valid_png_and_stable():
    a = png("Flash")
    w, h, raw = decode(a)
    assert (w, h) == (SIZE, SIZE) and len(raw) == SIZE * (SIZE * 3 + 1)
    assert png("Flash") == a and png(" flash ") == a      # same name, same icon
    assert png("Iris") != a                               # another name, another icon
    for name in ("x", "Lab Approvals", "mgr-anatoli", ""):
        grid, colour = pattern(name)
        assert all(row == row[::-1] for row in grid)       # symmetric
        assert any(any(row) for row in grid)               # never blank


def test_web_serves_icons():
    state = {"lab": "t", "at": "2026-10-18T12:00:00+00:00", "accounts": {},
             "agents": {"mgr-flash": {"name": "Flash", "role": "manager"}}}
    client = TestClient(web.app(lambda: state))
    r = client.get("/icon/mgr-flash.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content == png("Flash")
    assert client.get("/icon/approvals.png").content == png("Lab Approvals")
    assert client.get("/icon/nobody.png").status_code == 404


# ── drawn by Claude, checked, with the pattern as fallback ─────────────────

def test_clean_svg_accepts_only_self_contained_svg():
    import pytest
    from agentic_lab.icon import IconError, clean_svg
    ok = 'Here:\n<svg viewBox="0 0 512 512"><rect width="512" height="512"/></svg>\nDone.'
    assert clean_svg(ok).startswith("<svg") and clean_svg(ok).endswith("</svg>")
    for bad in ('<svg><script>alert(1)</script></svg>', '<svg><image href="http://x"/></svg>',
                '<svg><rect style="fill:url(http://x)"/></svg>', 'no svg here',
                '<svg><foreignObject/></svg>'):
        with pytest.raises(IconError):
            clean_svg(bad)


def test_drawn_falls_back_to_the_pattern(monkeypatch):
    import asyncio
    from agentic_lab import icon as I

    async def broken(name, role, env=None):
        raise I.IconError("no network")
    data, how = asyncio.run(I.drawn("Flash", "manager", draw=broken))
    assert data == png("Flash") and "pattern" in how and "no network" in how

    async def good(name, role, env=None):
        assert name == "Flash" and role == "manager"
        return '<svg viewBox="0 0 512 512"><rect width="512" height="512"/></svg>'
    monkeypatch.setattr(I, "rasterize", lambda svg: b"\x89PNGfake")
    data, how = asyncio.run(I.drawn("Flash", "manager", draw=good))
    assert data == b"\x89PNGfake" and how == "drawn by Claude"
    assert I.background_hint("Flash") != I.background_hint("Iris")   # icons vary in colour
