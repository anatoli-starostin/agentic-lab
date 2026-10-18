"""An icon for an agent's Slack app, generated from its name: the same name always gives
the same icon (a symmetric 5x5 pattern in a colour of its own), so the admin needn't design
one. A 512x512 PNG, the smallest size Slack accepts for an app icon; plain Python, no
image library.
"""
import colorsys
import hashlib
import struct
import zlib

SIZE = 512
GRID = 5
BACKGROUND = (242, 242, 242)


def _colour(digest):
    hue = digest[0] / 255
    r, g, b = colorsys.hls_to_rgb(hue, 0.47, 0.62)
    return int(r * 255), int(g * 255), int(b * 255)


def pattern(name):
    """The 5x5 grid of filled cells for `name` (mirrored left-right)."""
    digest = hashlib.sha256(name.strip().lower().encode()).digest()
    bits = int.from_bytes(digest[1:5], "big")
    grid = []
    for row in range(GRID):
        half = [(bits >> (row * 3 + col)) & 1 for col in range(3)]
        grid.append(half + half[1::-1])                # columns 0 1 2 1 0
    if not any(any(r) for r in grid):                   # never a blank icon
        grid[GRID // 2][GRID // 2] = 1
    return grid, _colour(digest)


def png(name, size=SIZE):
    """The icon for `name`, as PNG bytes."""
    grid, colour = pattern(name)
    cell = size // (GRID + 1)
    offset = (size - cell * GRID) // 2
    rows = []
    for y in range(size):
        gy = (y - offset) // cell
        line = bytearray(b"\x00")                        # filter: none
        for x in range(size):
            gx = (x - offset) // cell
            on = 0 <= gx < GRID and 0 <= gy < GRID and grid[gy][gx]
            line += bytes(colour if on else BACKGROUND)
        rows.append(bytes(line))

    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
            + chunk(b"IEND", b""))
