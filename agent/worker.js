/**
 * STEYDA Candy dashboard — AI agent proxy (Cloudflare Worker, FREE Workers AI).
 *
 * Runs an LLM on Cloudflare's built-in Workers AI — no API key, no subscription,
 * within the free daily allowance. The dashboard POSTs { question, context, history }
 * and gets back { answer }.
 *
 * Deploy: see agent/README.md. The Worker needs ONE binding and (optionally) one var:
 *   AI            (binding)  — Workers AI binding, variable name must be "AI"
 *   ALLOW_ORIGIN  (var)      — https://operations-steyda.github.io
 *   MODEL         (var,opt)  — e.g. @cf/meta/llama-3.1-8b-instruct
 */
const SYSTEM = `You are STEYDA's candy operations assistant, embedded in the Steyda FBA inventory dashboard for a Swedish-candy Amazon business.

Answer ONLY from the DASHBOARD DATA given in the user's message. It covers:
- inventory & replenishment per SKU (available, inbound, staged at the Stockholm warehouse, velocity, days of cover, reorder point, production quantity, status),
- candy production & ingredient consumption (per-candy recipes, current stock of each raw ingredient in cartons, and the expected stock after a planned production run).

Rules:
- Be concise and specific; always cite the actual numbers from the data.
- Units: finished candy is counted in POUCHES; warehouse ingredients in CARTONS.
- If the data does not contain the answer, say so plainly — never invent a SKU, ingredient, or number.
- When asked what to reorder or produce, use the provided production-quantity / reorder-point / cover figures; do not re-derive your own targets.
- You read and explain the data. You cannot change the dashboard or place orders.`;

export default {
  async fetch(request, env) {
    const allow = env.ALLOW_ORIGIN || "https://operations-steyda.github.io";
    const cors = {
      "Access-Control-Allow-Origin": allow,
      "Access-Control-Allow-Methods": "POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
      "Vary": "Origin",
    };
    if (request.method === "OPTIONS") return new Response(null, { headers: cors });
    if (request.method !== "POST") return json({ answer: "POST only." }, 405, cors);
    if ((request.headers.get("Origin") || "") !== allow)
      return json({ answer: "Forbidden origin." }, 403, cors);
    if (!env.AI) return json({ answer: "Workers AI binding 'AI' is not set up on this Worker (see agent/README.md)." }, 200, cors);

    let body;
    try { body = await request.json(); }
    catch { return json({ answer: "Bad request." }, 400, cors); }

    const question = String(body.question || "").slice(0, 4000);
    if (!question) return json({ answer: "Ask me something about the plan." }, 200, cors);
    const context = body.context || {};
    const history = Array.isArray(body.history) ? body.history.slice(-8) : [];

    const messages = [{ role: "system", content: SYSTEM }];
    for (const m of history) {
      if (m && (m.role === "user" || m.role === "assistant") && m.content)
        messages.push({ role: m.role, content: String(m.content).slice(0, 4000) });
    }
    messages.push({
      role: "user",
      content: "DASHBOARD DATA (JSON):\n" + JSON.stringify(context) + "\n\nQUESTION: " + question,
    });

    try {
      const out = await env.AI.run(env.MODEL || "@cf/meta/llama-3.1-8b-instruct", {
        messages,
        max_tokens: 1024,
      });
      const answer = (out && (out.response || out.result?.response)) || "No answer.";
      return json({ answer: String(answer).trim() }, 200, cors);
    } catch (e) {
      return json({ answer: "Workers AI error: " + (e.message || e) }, 200, cors);
    }
  },
};

function json(obj, status, cors) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { ...cors, "content-type": "application/json" },
  });
}
