"""Remote MCP server: OpenAI GPT Image 2.5 generate/edit over Streamable HTTP.

Endpoint: http://<HOST>:<PORT>/<MCP_SECRET_PATH>/mcp

Local (run.sh):  secrets from .env; MCP_SECRET_PATH is generated on first run.
Cloud Run:       secrets from environment variables (Secret Manager); nothing is written
                 to disk unless SAVE_OUTPUT=1. Cloud Run is detected via $K_SERVICE.
The API key is never printed or logged.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import logging
import os
import re
import secrets
import socket
import string
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import anyio
from dotenv import dotenv_values, load_dotenv
from mcp.server.fastmcp import FastMCP, Image
from mcp.server.transport_security import TransportSecuritySettings

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR") or BASE_DIR / "output")


def _env_flag(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    return default if val is None else val.strip().lower() in ("1", "true", "yes", "on")


IN_CLOUD = bool(os.environ.get("K_SERVICE"))  # set automatically by Cloud Run
HOST = os.environ.get("HOST") or ("0.0.0.0" if IN_CLOUD else "127.0.0.1")
PORT = int(os.environ.get("PORT") or 8000)
# Stateless mode: no server-side sessions, so restarts / multiple instances / scale-to-zero are safe.
STATELESS = _env_flag("STATELESS_HTTP", IN_CLOUD)
# Cloud Run's filesystem is in-memory and wiped on restart, so saving is off there by default.
SAVE_OUTPUT = _env_flag("SAVE_OUTPUT", not IN_CLOUD)

ALLOWED_MODELS = ("gpt-image-2.5-flare", "gpt-image-2.5-sunburst")
ALLOWED_QUALITY = ("auto", "low", "medium", "high")
SIZE_RE = re.compile(r"^(\d{3,4})x(\d{3,4})$")
MAX_N = 4
MAX_PROMPT_CHARS = 32000
MAX_INPUT_IMAGE_BYTES = 25 * 1024 * 1024  # OpenAI edit limit is in this range
URL_TIMEOUT_S = 20

log = logging.getLogger("gpt-image-mcp")


# --------------------------------------------------------------------------- #
# Config / secrets
# --------------------------------------------------------------------------- #
def _ensure_secret_path() -> str:
    """Return MCP_SECRET_PATH from the environment or .env, generating it locally if absent."""
    values = dotenv_values(ENV_FILE) if ENV_FILE.exists() else {}
    # .env wins locally; in the cloud there is no .env (excluded from the image), so the env var is used.
    current = (values.get("MCP_SECRET_PATH") or os.environ.get("MCP_SECRET_PATH") or "").strip()
    if current:
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", current):
            sys.exit("MCP_SECRET_PATH must be 16-128 chars of [A-Za-z0-9_-].")
        return current
    if IN_CLOUD:
        # A random per-instance path would change on every cold start and break the connector.
        sys.exit("MCP_SECRET_PATH must be set as an environment variable/secret in the cloud.")

    alphabet = string.ascii_letters + string.digits
    new = "".join(secrets.choice(alphabet) for _ in range(32))

    if ENV_FILE.exists():
        text = ENV_FILE.read_text()
        if re.search(r"^MCP_SECRET_PATH=.*$", text, flags=re.M):
            text = re.sub(r"^MCP_SECRET_PATH=.*$", f"MCP_SECRET_PATH={new}", text, flags=re.M)
        else:
            text = text.rstrip("\n") + f"\nMCP_SECRET_PATH={new}\n"
    else:
        text = f"OPENAI_API_KEY=\nMCP_SECRET_PATH={new}\n"
    ENV_FILE.write_text(text)
    os.chmod(ENV_FILE, 0o600)
    return new


SECRET_PATH = _ensure_secret_path()
# Locally .env is authoritative. On Cloud Run there is no .env, so secrets come from env vars.
load_dotenv(ENV_FILE, override=True)


def _api_key() -> str | None:
    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    return key or None


_client = None


def _openai():
    global _client
    key = _api_key()
    if not key:
        raise ToolError("OPENAI_API_KEY is not configured on the server.")
    if _client is None:
        from openai import OpenAI

        _client = OpenAI(api_key=key, timeout=300, max_retries=1)
    return _client


# --------------------------------------------------------------------------- #
# Errors / sanitising
# --------------------------------------------------------------------------- #
class ToolError(Exception):
    """User-facing error with a message that is safe to return."""


_KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-*]{6,}")


def _redact(msg: str) -> str:
    key = _api_key()
    if key:
        msg = msg.replace(key, "[REDACTED]")
    msg = _KEY_PATTERN.sub("[REDACTED]", msg)
    return msg[:800]


def _openai_error(exc: Exception) -> ToolError:
    import openai

    if isinstance(exc, openai.AuthenticationError):
        return ToolError("OpenAI rejected the API key (401). Check the server's OPENAI_API_KEY.")
    if isinstance(exc, openai.PermissionDeniedError):
        return ToolError(
            "OpenAI denied access (403). Your org may need verification for this image model. "
            + _redact(getattr(exc, "message", ""))
        )
    if isinstance(exc, openai.RateLimitError):
        return ToolError("OpenAI rate limit or quota exceeded (429). " + _redact(getattr(exc, "message", "")))
    if isinstance(exc, openai.BadRequestError):
        return ToolError("OpenAI rejected the request (400): " + _redact(getattr(exc, "message", "")))
    if isinstance(exc, openai.NotFoundError):
        return ToolError("OpenAI returned 404 (model not available to this key?): " + _redact(getattr(exc, "message", "")))
    if isinstance(exc, openai.APITimeoutError):
        return ToolError("OpenAI request timed out. Try again or use quality='low'.")
    if isinstance(exc, openai.APIConnectionError):
        return ToolError("Could not connect to OpenAI. Check the server's network connection.")
    if isinstance(exc, openai.APIStatusError):
        return ToolError(f"OpenAI error ({exc.status_code}): " + _redact(getattr(exc, "message", "")))
    return ToolError("Unexpected error calling OpenAI: " + _redact(type(exc).__name__))


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def _check_prompt(prompt: str) -> str:
    if not isinstance(prompt, str) or not prompt.strip():
        raise ToolError("prompt must be a non-empty string.")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ToolError(f"prompt is too long (max {MAX_PROMPT_CHARS} characters).")
    return prompt.strip()


def _check_model(model: str) -> str:
    if model not in ALLOWED_MODELS:
        raise ToolError(f"model must be one of: {', '.join(ALLOWED_MODELS)}.")
    return model


def _check_quality(quality: str) -> str:
    if quality not in ALLOWED_QUALITY:
        raise ToolError(f"quality must be one of: {', '.join(ALLOWED_QUALITY)}.")
    return quality


def _check_size(size: str) -> str:
    if size == "auto":
        return size
    m = SIZE_RE.match(size or "")
    if not m:
        raise ToolError("size must be 'auto' or WIDTHxHEIGHT, e.g. 1024x1024, 1536x1024, 1024x1536.")
    w, h = int(m.group(1)), int(m.group(2))
    if not (256 <= w <= 4096 and 256 <= h <= 4096) or w % 16 or h % 16:
        raise ToolError("size dimensions must be multiples of 16 between 256 and 4096.")
    return size


def _check_n(n: int) -> int:
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= MAX_N:
        raise ToolError(f"n must be an integer from 1 to {MAX_N}.")
    return n


# --------------------------------------------------------------------------- #
# Image input handling
# --------------------------------------------------------------------------- #
def _sniff_mime(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _assert_public_host(host: str) -> None:
    """Block URLs that resolve to loopback/private/link-local addresses (SSRF guard)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise ToolError(f"Could not resolve host '{host}'.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ToolError("Image URL must point to a public internet host.")


def _fetch_url(url: str) -> bytes:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ToolError("image URL must be http(s).")
    _assert_public_host(parsed.hostname)

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            new = urllib.parse.urlparse(newurl)
            if new.scheme not in ("http", "https") or not new.hostname:
                raise ToolError("Image URL redirected to a non-http(s) location.")
            _assert_public_host(new.hostname)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": "gpt-image-mcp/0.1"})
    try:
        with opener.open(req, timeout=URL_TIMEOUT_S) as resp:
            data = resp.read(MAX_INPUT_IMAGE_BYTES + 1)
    except ToolError:
        raise
    except Exception as exc:  # network errors, HTTP errors
        raise ToolError(f"Could not download image: {type(exc).__name__}: {str(exc)[:200]}")
    if len(data) > MAX_INPUT_IMAGE_BYTES:
        raise ToolError("Input image is larger than 25 MB.")
    return data


def _load_input_image(src: str) -> tuple[str, bytes, str]:
    if not isinstance(src, str) or not src.strip():
        raise ToolError("image_url_or_b64 must be a non-empty string.")
    src = src.strip()

    if src.startswith(("http://", "https://")):
        data = _fetch_url(src)
    else:
        if src.startswith("data:"):
            header, _, src = src.partition(",")
            if ";base64" not in header:
                raise ToolError("data: URI must be base64-encoded.")
        try:
            data = base64.b64decode(re.sub(r"\s+", "", src), validate=True)
        except (binascii.Error, ValueError):
            raise ToolError("image_url_or_b64 is neither an http(s) URL nor valid base64 image data.")
        if len(data) > MAX_INPUT_IMAGE_BYTES:
            raise ToolError("Input image is larger than 25 MB.")

    mime = _sniff_mime(data)
    if not mime:
        raise ToolError("Input image must be PNG, JPEG, or WebP.")
    ext = mime.split("/")[1].replace("jpeg", "jpg")
    return (f"input.{ext}", data, mime)


# --------------------------------------------------------------------------- #
# Output handling
# --------------------------------------------------------------------------- #
def _save_and_wrap(resp, label: str) -> list:
    items = getattr(resp, "data", None) or []
    if not items:
        raise ToolError("OpenAI returned no images.")
    if SAVE_OUTPUT:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    content: list = []
    saved: list[str] = []
    for i, item in enumerate(items):
        if not item.b64_json:
            raise ToolError("OpenAI response did not include base64 image data.")
        raw = base64.b64decode(item.b64_json)
        if SAVE_OUTPUT:
            name = f"{stamp}.png" if len(items) == 1 else f"{stamp}-{i + 1}.png"
            (OUTPUT_DIR / name).write_bytes(raw)
            saved.append(f"output/{name}")
        content.append(Image(data=raw, format="png"))
    if saved:
        log.info("%s: saved %s", label, ", ".join(saved))
        summary = f"{label}: {len(items)} image(s), saved on server as {', '.join(saved)}"
    else:
        log.info("%s: returned %d image(s)", label, len(items))
        summary = f"{label}: {len(items)} image(s)"
    return [summary] + content


# --------------------------------------------------------------------------- #
# MCP server
# --------------------------------------------------------------------------- #
mcp = FastMCP(
    "gpt-image",
    instructions="Generate and edit images with OpenAI GPT Image 2.5.",
    host=HOST,
    port=PORT,
    streamable_http_path=f"/{SECRET_PATH}/mcp",
    stateless_http=STATELESS,
    # Behind a Cloudflare tunnel (random hostname) or Cloud Run's proxy, Host-header
    # allowlisting can't be pinned reliably. Access is gated by the secret path.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    max_request_body_size=40 * 1024 * 1024,  # allow base64 input images for edit_image
)


# The OpenAI SDK calls block for 10-60 s, so tools run them in a worker thread to keep the
# event loop free for other requests (important with concurrent requests on Cloud Run).
def _generate_sync(prompt: str, size: str, quality: str, model: str, n: int) -> list:
    try:
        args = dict(
            prompt=_check_prompt(prompt),
            size=_check_size(size),
            quality=_check_quality(quality),
            model=_check_model(model),
            n=_check_n(n),
            output_format="png",
        )
        client = _openai()
        try:
            resp = client.images.generate(**args)
        except Exception as exc:
            raise _openai_error(exc)
        return _save_and_wrap(resp, f"generate_image ({args['model']}, {args['size']}, {args['quality']})")
    except ToolError as exc:
        raise ValueError(str(exc)) from None


def _edit_sync(prompt: str, image_url_or_b64: str, model: str, size: str) -> list:
    try:
        prompt = _check_prompt(prompt)
        model = _check_model(model)
        size = _check_size(size)
        image = _load_input_image(image_url_or_b64)
        client = _openai()
        try:
            resp = client.images.edit(image=image, prompt=prompt, model=model, size=size, output_format="png")
        except Exception as exc:
            raise _openai_error(exc)
        return _save_and_wrap(resp, f"edit_image ({model}, {size})")
    except ToolError as exc:
        raise ValueError(str(exc)) from None


@mcp.tool(name="generate_image", structured_output=False)
async def generate_image(
    prompt: str,
    size: str = "1024x1024",
    quality: str = "auto",
    model: str = "gpt-image-2.5-flare",
    n: int = 1,
) -> list:
    """Generate image(s) from a text prompt with OpenAI GPT Image 2.5.

    Args:
        prompt: Description of the image to create.
        size: "auto" or WIDTHxHEIGHT, e.g. "1024x1024", "1536x1024" (landscape), "1024x1536" (portrait).
        quality: One of "auto", "low", "medium", "high".
        model: "gpt-image-2.5-flare" (default, faster) or "gpt-image-2.5-sunburst" (highest fidelity).
        n: Number of images, 1-4.
    """
    return await anyio.to_thread.run_sync(_generate_sync, prompt, size, quality, model, n)


@mcp.tool(name="edit_image", structured_output=False)
async def edit_image(
    prompt: str,
    image_url_or_b64: str,
    model: str = "gpt-image-2.5-sunburst",
    size: str = "auto",
) -> list:
    """Edit a reference image according to a text prompt with OpenAI GPT Image 2.5.

    Args:
        prompt: What to change in the image.
        image_url_or_b64: Public http(s) URL of a PNG/JPEG/WebP image, or base64 data (raw or data: URI).
        model: "gpt-image-2.5-sunburst" (default, best for precise edits) or "gpt-image-2.5-flare".
        size: "auto" or WIDTHxHEIGHT, e.g. "1024x1024".
    """
    return await anyio.to_thread.run_sync(_edit_sync, prompt, image_url_or_b64, model, size)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Keep httpx/openai request logging quiet (never log headers).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    if not _api_key():
        log.warning("OPENAI_API_KEY is not set: tools will return an error until it is added.")
    if IN_CLOUD:
        # Don't put the secret path into Cloud Logging.
        print(f"MCP server listening on {HOST}:{PORT} (stateless={STATELESS}, save_output={SAVE_OUTPUT})", flush=True)
    else:
        print(f"MCP endpoint: http://{HOST}:{PORT}/{SECRET_PATH}/mcp", flush=True)

    import uvicorn

    # Same as mcp.run(transport="streamable-http"), but with the access log disabled in the
    # cloud so the secret path isn't written to Cloud Logging on every request.
    uvicorn.run(mcp.streamable_http_app(), host=HOST, port=PORT, log_level="info", access_log=not IN_CLOUD)


if __name__ == "__main__":
    main()
