# STEYDA dashboard AI agent — backend proxy (FREE, no subscription)

The dashboard's chat agent talks to an LLM through this tiny Cloudflare Worker,
using **Cloudflare Workers AI** — a free, built-in model runner. **No API key, no
subscription, no credit card** (within Cloudflare's free daily allowance).

Flow: dashboard → `POST /` `{question, context, history}` → Worker → Workers AI → `{answer}`.

## Deploy (≈10 minutes, free)

### Cloudflare dashboard (no install)
1. Go to <https://dash.cloudflare.com/> (free account) → **Workers & Pages** → **Create** → **Create Worker**. Name it e.g. `steyda-agent`. **Deploy**, then **Edit code**.
2. Paste the contents of [`worker.js`](worker.js), **Save and deploy**.
3. Worker → **Settings** → **Bindings** → **Add** → **Workers AI**:
   - Variable name: **`AI`** (must be exactly `AI`). Save.
4. Worker → **Settings** → **Variables and Secrets** → add a **Variable**:
   - `ALLOW_ORIGIN` = `https://operations-steyda.github.io`
   - (optional) `MODEL` = a Workers AI model id, e.g. `@cf/meta/llama-3.1-8b-instruct` (default) or the larger `@cf/meta/llama-3.3-70b-instruct-fp8-fast` for better answers (uses more of the daily allowance).
5. Copy the Worker URL (looks like `https://steyda-agent.<your-subdomain>.workers.dev`).
6. **Send that URL to Claude Code** — it sets `AGENT_CONFIG.endpoint` in the dashboard and redeploys. The chat goes live.

### Wrangler CLI (alternative)
```bash
npm i -g wrangler
wrangler login
wrangler deploy agent/worker.js --name steyda-agent
# add to wrangler.toml:  [ai]\n  binding = "AI"   and the vars above
```

## Notes
- **Free allowance:** Workers AI includes a free daily quota (Neurons). Light use — a handful of people asking inventory/production questions — stays well within it. If you ever exceed it, requests pause until the next day (or you can enable paid usage).
- The Worker only answers requests whose `Origin` is `ALLOW_ORIGIN`, caps question/history size, and stores nothing.
- Quality: Llama 3.1 8B is good for reading the structured dashboard data. If answers feel thin, set `MODEL` to `@cf/meta/llama-3.3-70b-instruct-fp8-fast`.
- The agent is **read-only**: it explains the dashboard data, it cannot change the plan or place orders.
- No key to rotate — nothing secret lives in this Worker.
