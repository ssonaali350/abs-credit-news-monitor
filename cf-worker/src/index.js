// Ask AI backend for the ABS Credit News Monitor dashboard.
//
// Holds the Anthropic API key as a Worker secret (never exposed to visitors'
// browsers) and proxies question + dashboard-data context to Claude. The
// frontend still builds the compact news/holdings context client-side (it
// already has that data loaded) and just POSTs {question, newsLines,
// holdingsLines} here.
//
// Safety rail: a simple daily request cap stored in Workers KV, so a
// traffic spike (or a bot) can't run up unexpected Anthropic spend on the
// site owner's account. Counts only successful Anthropic calls, so a burst
// of malformed/failed requests doesn't itself exhaust the budget.

const DAILY_LIMIT = 100;
const ALLOWED_ORIGIN = "https://ssonaali350.github.io";
const MODEL = "claude-haiku-4-5-20251001";
const GITHUB_REPO = "ssonaali350/abs-credit-news-monitor";
const GITHUB_WORKFLOW = "ingest.yml";

function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
  };
}

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...corsHeaders() },
  });
}

async function getTodayCount(env) {
  const key = `count:${new Date().toISOString().slice(0, 10)}`;
  const raw = await env.RATE_LIMIT_KV.get(key);
  return { key, count: raw ? parseInt(raw, 10) : 0 };
}

// Reliable external trigger for the daily news-ingest workflow. GitHub's own
// `schedule` event missed two days running despite correct workflow config
// (active, correct branch, valid cron, no account/permission issue) — a
// known, documented GitHub limitation (scheduled workflows are explicitly
// best-effort and get deprioritized under platform load), not something
// further YAML changes can fix. Cloudflare Cron Triggers are a much more
// reliable primitive, so this Worker just fires the same workflow via the
// GitHub REST API on schedule instead of waiting on GitHub's own scheduler.
async function triggerGithubIngest(env) {
  const resp = await fetch(
    `https://api.github.com/repos/${GITHUB_REPO}/actions/workflows/${GITHUB_WORKFLOW}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_PAT}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "abs-ask-ai-cron-trigger",
      },
      body: JSON.stringify({ ref: "main" }),
    }
  );
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`GitHub workflow_dispatch failed: ${resp.status} ${text}`);
  }
}

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders() });
    }
    if (request.method !== "POST") {
      return jsonResponse({ error: "Method not allowed" }, 405);
    }

    const { key, count } = await getTodayCount(env);
    if (count >= DAILY_LIMIT) {
      return jsonResponse({ error: `Daily question limit (${DAILY_LIMIT}) reached across all visitors — try again tomorrow.` }, 429);
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return jsonResponse({ error: "Invalid request body" }, 400);
    }

    const { question, newsLines, holdingsLines } = body || {};
    if (!question || typeof question !== "string") {
      return jsonResponse({ error: "Missing question" }, 400);
    }

    const prompt = `You answer questions about an ABS credit news dashboard's own data. Use ONLY the data below; if the answer isn't in it, say so plainly.

NEWS ITEMS (date | sector | action_type | relevance 1-5 | issuer | title), most recent first:
${(newsLines || "").slice(0, 60000)}

SAMPLE PORTFOLIO HOLDINGS (illustrative demo data, not real fund positions):
${(holdingsLines || "").slice(0, 10000)}

Question: ${question}

Answer concisely, in a few sentences.`;

    try {
      const anthropicResp = await fetch("https://api.anthropic.com/v1/messages", {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-api-key": env.ANTHROPIC_API_KEY,
          "anthropic-version": "2023-06-01",
        },
        body: JSON.stringify({
          model: MODEL,
          max_tokens: 500,
          messages: [{ role: "user", content: prompt }],
        }),
      });
      const data = await anthropicResp.json();
      if (!anthropicResp.ok) {
        return jsonResponse({ error: (data.error && data.error.message) || "Anthropic API error" }, anthropicResp.status);
      }

      // Only count successful calls against the daily budget.
      await env.RATE_LIMIT_KV.put(key, String(count + 1), { expirationTtl: 172800 });

      return jsonResponse({ answer: data.content[0].text });
    } catch (e) {
      return jsonResponse({ error: "Request to Anthropic failed: " + e.message }, 502);
    }
  },

  async scheduled(event, env, ctx) {
    ctx.waitUntil(
      triggerGithubIngest(env).catch((err) => console.error(err.message))
    );
  },
};
