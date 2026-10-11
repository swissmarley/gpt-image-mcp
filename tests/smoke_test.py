"""Smoke tests for the GPT Image MCP server.

Usage:
  uv run python tests/smoke_test.py offline              # in-memory, stubbed OpenAI: free
  uv run python tests/smoke_test.py http URL             # against a running server: free checks (no OpenAI calls)
  uv run python tests/smoke_test.py http URL --paid      # + ONE real generate_image(quality="low")
"""

from __future__ import annotations

import asyncio
import base64
import struct
import sys
import zlib
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp import ClientSession  # noqa: E402
from mcp.types import ImageContent, TextContent  # noqa: E402


def tiny_png(w: int = 16, h: int = 16) -> bytes:
    raw = b"".join(b"\x00" + b"\xff\x80\x00" * w for _ in range(h))

    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


PNG_B64 = base64.b64encode(tiny_png()).decode()
failures = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global failures
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f": {detail}" if detail and not cond else ""))
    if not cond:
        failures += 1


def text_of(res) -> str:
    return " ".join(c.text for c in res.content if isinstance(c, TextContent))


async def validation_checks(s: ClientSession) -> None:
    tools = {t.name for t in (await s.list_tools()).tools}
    check("tools listed", tools == {"generate_image", "edit_image"}, str(tools))

    cases = [
        ("generate: bad model", "generate_image", {"prompt": "x", "model": "dall-e-3"}, "model must be"),
        ("generate: bad quality", "generate_image", {"prompt": "x", "quality": "ultra"}, "quality must be"),
        ("generate: bad size", "generate_image", {"prompt": "x", "size": "big"}, "size must be"),
        ("generate: bad n", "generate_image", {"prompt": "x", "n": 9}, "n must be"),
        ("generate: empty prompt", "generate_image", {"prompt": "  "}, "prompt must be"),
        ("edit: bad b64", "edit_image", {"prompt": "x", "image_url_or_b64": "not-an-image!!"}, "neither"),
        ("edit: non-image b64", "edit_image", {"prompt": "x", "image_url_or_b64": base64.b64encode(b"hello").decode()}, "PNG, JPEG"),
        ("edit: SSRF localhost", "edit_image", {"prompt": "x", "image_url_or_b64": "http://127.0.0.1:8000/x.png"}, "public internet"),
        ("edit: file scheme", "edit_image", {"prompt": "x", "image_url_or_b64": "file:///etc/passwd"}, "neither"),
        ("edit: bad model", "edit_image", {"prompt": "x", "image_url_or_b64": PNG_B64, "model": "foo"}, "model must be"),
    ]
    for name, tool, args, expect in cases:
        r = await s.call_tool(tool, args)
        check(name, r.isError and expect in text_of(r), text_of(r)[:160])


# --------------------------------------------------------------------------- #
async def offline() -> None:
    import server
    from mcp.shared.memory import create_connected_server_and_client_session

    calls: list[tuple[str, dict]] = []

    def fake(kind):
        def _f(**kw):
            calls.append((kind, kw))
            n = kw.get("n", 1)
            return SimpleNamespace(data=[SimpleNamespace(b64_json=PNG_B64) for _ in range(n)])

        return _f

    fake_client = SimpleNamespace(images=SimpleNamespace(generate=fake("generate"), edit=fake("edit")))
    server._openai = lambda: fake_client  # stub: no network, no cost

    async with create_connected_server_and_client_session(server.mcp._mcp_server) as s:
        print("Offline validation checks:")
        await validation_checks(s)

        print("Offline happy-path checks (stubbed OpenAI):")
        r = await s.call_tool("generate_image", {"prompt": "a red square", "quality": "low", "n": 2})
        imgs = [c for c in r.content if isinstance(c, ImageContent)]
        check("generate returns 2 ImageContent", not r.isError and len(imgs) == 2, text_of(r))
        check("ImageContent is PNG", all(i.mimeType == "image/png" and base64.b64decode(i.data)[:4] == b"\x89PNG" for i in imgs))
        kw = calls[-1][1]
        check("generate args forwarded", kw["model"] == "gpt-image-2.5-flare" and kw["quality"] == "low" and kw["n"] == 2, str(kw))

        r = await s.call_tool("edit_image", {"prompt": "make it blue", "image_url_or_b64": "data:image/png;base64," + PNG_B64})
        imgs = [c for c in r.content if isinstance(c, ImageContent)]
        check("edit returns ImageContent", not r.isError and len(imgs) == 1, text_of(r))
        kind, kw = calls[-1]
        check(
            "edit args forwarded",
            kind == "edit" and kw["model"] == "gpt-image-2.5-sunburst" and kw["size"] == "auto"
            and kw["image"][0] == "input.png" and kw["image"][2] == "image/png",
            str({k: v for k, v in kw.items() if k != "image"}),
        )
        check("files saved to output/", "saved on server as output/" in text_of(r), text_of(r))

    # Key redaction
    server._api_key = lambda: "sk-proj-THISISAFAKEKEY1234567890"
    red = server._redact("Incorrect API key provided: sk-proj-THISISAFAKEKEY1234567890 and sk-abc***wxyz")
    check("key redaction", "FAKEKEY" not in red and "sk-abc" not in red, red)


# --------------------------------------------------------------------------- #
async def http(url: str, paid: bool) -> None:
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(url) as (r, w, _):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            check("initialize", init.serverInfo.name == "gpt-image", init.serverInfo.name)
            print("HTTP validation checks:")
            await validation_checks(s)

            if paid:
                print("PAID: one generate_image(quality='low') call...")
                r = await s.call_tool(
                    "generate_image",
                    {"prompt": "A small red apple on a white table, simple illustration", "quality": "low", "size": "1024x1024"},
                )
                imgs = [c for c in r.content if isinstance(c, ImageContent)]
                check("real generate returns ImageContent", not r.isError and len(imgs) == 1, text_of(r))
                print("  ", text_of(r)[:300])
            else:
                # Free mode must never reach OpenAI: only validation-failing calls are made above.
                print("  (free mode: no OpenAI call made; use --paid for one real low-quality generation)")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "offline"
    if mode == "offline":
        asyncio.run(offline())
    elif mode == "http":
        asyncio.run(http(sys.argv[2], "--paid" in sys.argv))
    else:
        sys.exit(__doc__)
    print("\nRESULT:", "ALL PASSED" if failures == 0 else f"{failures} FAILURE(S)")
    sys.exit(1 if failures else 0)
