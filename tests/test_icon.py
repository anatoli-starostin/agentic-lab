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
