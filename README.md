# gpt-image-mcp

A remote MCP server that lets Claude.ai generate and edit images with OpenAI **GPT Image 2.5**,
exposed over HTTPS through a temporary Cloudflare Quick Tunnel.

- Transport: Streamable HTTP on `127.0.0.1:8000`, endpoint `/<MCP_SECRET_PATH>/mcp`
- Tools:
  - `generate_image(prompt, size="1024x1024", quality="auto", model="gpt-image-2.5-flare", n=1)`
  - `edit_image(prompt, image_url_or_b64, model="gpt-image-2.5-sunburst", size="auto")`
- Images come back as inline MCP `ImageContent` (base64 PNG). A copy is saved to `./output/<timestamp>.png`.

## Requirements

- macOS with [uv](https://docs.astral.sh/uv/) (Python 3.10+ is fetched automatically)
- `cloudflared` (`run.sh` installs it with `brew install cloudflared` if it's missing)
- An OpenAI API key with access to the GPT Image 2.5 models (your org may need to be verified)

## Setup

```bash
cd gpt-image-mcp
cp .env.example .env        # run.sh does this for you if .env is missing
# edit .env and set OPENAI_API_KEY=sk-...
uv sync
```

`MCP_SECRET_PATH` (32 random characters) is generated into `.env` on first start. Keep it private:
anyone with the full URL can use your API key. To rotate it, clear the value and restart.

## Run

```bash
./run.sh
```

The script starts the server, waits for port 8000, starts `cloudflared tunnel --url http://localhost:8000`,
and prints something like:

```
https://random-words-here.trycloudflare.com/<MCP_SECRET_PATH>/mcp
```

Press **Ctrl+C** to stop both the server and the tunnel. Logs are in `logs/`, and the last URL is in
`logs/connector_url.txt`.

## Add it to Claude.ai

1. Open Claude.ai → **Settings → Connectors**.
2. Click **Add custom connector**.
3. Give it a name (e.g. "GPT Image") and paste the URL that `run.sh` printed.
4. Leave the OAuth fields empty and click **Add**.
5. In a chat, enable the connector from the tools menu and ask e.g. *"Generate a low-quality image of a red apple"*.

> **The URL changes every time `run.sh` starts**, and it stops working as soon as cloudflared stops.
> After each restart, remove the old connector and add the new URL again.

## Deploy to Google Cloud Run (permanent URL)

Instead of a tunnel that changes every run, you can host the server on Cloud Run. You get a fixed
`https://gpt-image-mcp-xxxx.run.app/<MCP_SECRET_PATH>/mcp` URL, scale-to-zero, and 10-minute request timeouts.
For personal use it normally stays within Cloud Run's free tier. Check current limits at
https://cloud.google.com/run/pricing. A billing account (card) is required even for the free tier.

### One-time setup

```bash
# 1. Install the Google Cloud CLI and log in
brew install --cask google-cloud-sdk
gcloud auth login

# 2. Create a project and link billing (or do both in https://console.cloud.google.com)
gcloud projects create my-gpt-image-mcp --name="GPT Image MCP"   # project IDs are globally unique
gcloud config set project my-gpt-image-mcp
gcloud billing accounts list                                       # copy your ACCOUNT_ID
gcloud billing projects link my-gpt-image-mcp --billing-account=ACCOUNT_ID
```

Strongly recommended: create a **budget alert** (Console → Billing → Budgets & alerts, e.g. $5) and set a
**monthly spend limit in your OpenAI account**. Anyone with the connector URL can spend OpenAI credit.

### Deploy (and redeploy after code changes)

```bash
./deploy.sh                                   # region us-central1
REGION=europe-west1 ./deploy.sh               # or pick a region near you
```

`deploy.sh`:
1. Enables Cloud Run, Cloud Build, Artifact Registry and Secret Manager.
2. Stores `OPENAI_API_KEY` and `MCP_SECRET_PATH` from your `.env` in **Secret Manager**. They are never printed
   and never uploaded with the source (`.gcloudignore` excludes `.env`).
3. Creates a service account `gpt-image-mcp-sa` that can read only those two secrets.
4. Builds the `Dockerfile` with Cloud Build and deploys with: 512 MiB, 1 vCPU, 600 s timeout,
   0–2 instances, public (`--allow-unauthenticated`, protected by the secret path).
5. Checks the endpoint with a free MCP `initialize` and prints the connector URL (also saved to
   `logs/cloudrun_url.txt`).

Then add it in Claude.ai: **Settings → Connectors → Add custom connector → paste the URL**. Unlike the
tunnel, this URL stays the same across redeploys, so you only add it once.

### How cloud mode differs
`server.py` detects Cloud Run through `$K_SERVICE` and then:
- listens on `0.0.0.0:$PORT`
- runs **stateless** (no server-side sessions, so cold starts and multiple instances are safe)
- **doesn't save images to disk** (Cloud Run's disk lives in memory and is wiped on restart). Override with `SAVE_OUTPUT=1`.
- reads secrets from environment variables, and refuses to start if `MCP_SECRET_PATH` is missing
- disables uvicorn's access log, so the secret path isn't logged on every request

### Day-to-day commands

```bash
gcloud run services logs read gpt-image-mcp --region us-central1 --limit 50   # logs
./deploy.sh                                     # rotate key/path: edit .env, then redeploy
gcloud run services delete gpt-image-mcp --region us-central1                 # tear down
gcloud secrets delete gpt-image-mcp-openai-key; gcloud secrets delete gpt-image-mcp-secret-path
```

To rotate the URL, clear `MCP_SECRET_PATH` in `.env` (a new one is generated), run `./deploy.sh`, and
re-add the connector.

### Troubleshooting
- **Org policy blocks `--allow-unauthenticated`** (common on Workspace accounts): use a personal Google
  account project, or ask your admin to allow public Cloud Run services.
- **Cloud Build permission errors on first deploy**: newer projects build with the default compute
  service account. Grant it the *Cloud Run Builder* role (`roles/run.builder`) and retry.
- **First request is slow (~5–10 s)**: that's a cold start after the service scaled to zero.
  `--min-instances 1` removes it, but you pay for an always-on instance.
- Cloud Run's own request logs (Cloud Logging) still record full request URLs, including the secret path.
  Those logs are visible only to members of your project.

## Testing

```bash
uv run python tests/smoke_test.py offline                     # free, stubbed OpenAI
uv run python tests/smoke_test.py http "$(cat logs/connector_url.txt)"          # free checks via tunnel (no OpenAI calls)
uv run python tests/smoke_test.py http "$(cat logs/cloudrun_url.txt)"           # free checks against Cloud Run
uv run python tests/smoke_test.py http "$(cat logs/connector_url.txt)" --paid   # + 1 low-quality generation
```

You can also use the MCP Inspector (`uv run mcp dev server.py`), but note that it uses stdio by default.

## Parameters

| Param | Values |
|---|---|
| `model` | `gpt-image-2.5-flare` (fast, default for generate), `gpt-image-2.5-sunburst` (precise edits, default for edit) |
| `quality` | `auto`, `low`, `medium`, `high` (generate only) |
| `size` | `auto` or `WxH` with multiples of 16 from 256 to 4096 (e.g. `1024x1024`, `1536x1024`, `1024x1536`). OpenAI makes the final call on which sizes a model supports. |
| `n` | 1–4 |
| `image_url_or_b64` | public http(s) URL, raw base64, or `data:image/...;base64,...` (PNG/JPEG/WebP, ≤ 25 MB) |

## Security notes

- The key is read from `.env` only. It is never logged or returned, and OpenAI error messages are scrubbed of `sk-…` strings.
- `.env`, `output/` and `logs/` are git-ignored. `.env` is created with mode 600.
- The secret path is the only access control. There is no OAuth. Don't share the URL.
- Host-header (DNS-rebinding) protection is disabled because the tunnel hostname is random. The server only binds to 127.0.0.1.
- URL inputs to `edit_image` are blocked if they resolve to private, loopback or link-local addresses (SSRF guard).

## Known limits

- Quick tunnels have no uptime guarantee and are meant for testing. Use a named Cloudflare tunnel for anything long-lived.
- If `~/.cloudflared/config.yaml` exists, quick tunnels may not start (`run.sh` warns you).
- Claude.ai can't upload a file from the chat straight into `edit_image`. Pass a public image URL or base64 data.
- A new `*.trycloudflare.com` hostname can take up to a minute to resolve. If you curl it from your Mac
  too early, macOS may cache the "not found" answer for a few minutes. Claude.ai is not affected. To test
  locally anyway, use `curl --doh-url https://1.1.1.1/dns-query ...`.
- Large high-quality generations can take a minute or more.
