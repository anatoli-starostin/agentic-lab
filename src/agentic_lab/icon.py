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


# ── drawn by Claude ──────────────────────────────────────────────────────────
# Claude can't output pixels, but it draws well in SVG: one request for an icon inspired by
# the agent's name and role, checked (self-contained, nothing external or active), then
# converted to a PNG with rsvg-convert. Any failure falls back to the pattern above.

PROMPT = """Draw an app icon as one self-contained SVG for an AI agent named "{name}", {role} \
in the scientific lab "{lab}" (let the lab's name and field inspire the icon too). \
viewBox 0 0 512 512; a solid rounded-square background in a colour \
close to {colour}; one bold, recognizable motif inspired by the name; flat modern style; \
no text or letters; no external references, images, scripts or fonts. Output ONLY the SVG \
code."""
UNSAFE = ("<script", "<image", "<foreignobject", "href=\"http", "href='http", "url(http",
          "javascript:", "@import", "<!entity", "<!doctype")
ROLES = {"manager": "a manager agent, a team-mate members talk to in Slack",
         "secretary": "the lab's secretary agent, who keeps the lab running",
         "worker": "a worker agent that runs experiments on a GPU",
         "approvals": "the lab's approvals app, which asks humans to approve actions"}


class IconError(RuntimeError):
    pass


def background_hint(name):
    """A background colour of the name's own, so icons don't all come out alike."""
    digest = hashlib.sha256(name.strip().lower().encode()).digest()
    hue = int.from_bytes(digest[9:13], "big") / 2**32
    r, g, b = colorsys.hls_to_rgb(hue, 0.38, 0.55)
    return "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))


def clean_svg(text):
    """The SVG in Claude's answer, if it's self-contained; else IconError."""
    start, end = text.find("<svg"), text.rfind("</svg>")
    if start < 0 or end < start:
        raise IconError("no SVG in the answer")
    svg = text[start:end + len("</svg>")]
    low = svg.lower()
    bad = [u for u in UNSAFE if u in low]
    if bad:
        raise IconError(f"the SVG refers to something outside itself ({bad[0]})")
    if len(svg) > 200_000:
        raise IconError("the SVG is too large")
    return svg


def rasterize(svg, size=SIZE):
    import shutil
    import subprocess
    exe = shutil.which("rsvg-convert")
    if exe is None:
        raise IconError("rsvg-convert isn't installed (apt install librsvg2-bin)")
    r = subprocess.run([exe, "-w", str(size), "-h", str(size), "-f", "png"],
                       input=svg.encode(), capture_output=True, timeout=30)
    if r.returncode or not r.stdout.startswith(b"\x89PNG"):
        raise IconError(f"couldn't render the SVG: {r.stderr.decode()[-200:]}")
    return r.stdout


async def claude_svg(name, role="", env=None, model="sonnet", cwd=None, lab=""):
    """Ask Claude for the SVG (one turn, no tools)."""
    from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ResultMessage,
                                  TextBlock, query)
    opts = ClaudeAgentOptions(
        allowed_tools=[], max_turns=1, setting_sources=[], model=model, env=env or {},
        cwd=cwd, system_prompt="You draw app icons. You answer with SVG code only.")
    prompt = PROMPT.format(name=name, role=ROLES.get(role, role or "an agent"),
                           lab=lab or "a research lab", colour=background_hint(name))
    text = ""
    async for msg in query(prompt=prompt, options=opts):
        if isinstance(msg, AssistantMessage):
            text += "".join(b.text for b in msg.content if isinstance(b, TextBlock))
        elif isinstance(msg, ResultMessage) and msg.result and "<svg" in msg.result:
            text = msg.result
    return clean_svg(text)


async def drawn(name, role="", env=None, draw=None, lab=""):
    """(PNG bytes, how it was made): drawn by Claude, or the pattern if that fails. `lab`:
    the lab's name (lab.yaml `lab`), so the icon fits the project too."""
    try:
        svg = await (draw or claude_svg)(name, role, env=env, lab=lab)
        return rasterize(svg), "drawn by Claude"
    except Exception as e:
        return png(name), f"a pattern (Claude's drawing failed: {str(e)[:120]})"
