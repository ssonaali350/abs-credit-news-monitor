# Ask AI backend (Cloudflare Worker)

Proxies the dashboard's "Ask AI" questions to Claude, holding the Anthropic
API key as a Worker secret so it's never exposed to visitors' browsers.
Includes a daily request cap (see `DAILY_LIMIT` in `src/index.js`) as a
safety rail against unexpected traffic spikes/cost.

## One-time setup

```bash
cd cf-worker

# 1. Log in to Cloudflare (opens a browser)
npx wrangler login

# 2. Create the KV namespace used for the daily rate-limit counter
npx wrangler kv namespace create RATE_LIMIT_KV
# Copy the returned "id" into wrangler.toml's [[kv_namespaces]] id field.

# 3. Store your Anthropic API key as a Worker secret (prompts for the value)
npx wrangler secret put ANTHROPIC_API_KEY

# 4. Deploy
npx wrangler deploy
```

`wrangler deploy` prints the Worker's URL
(`https://abs-ask-ai.<your-subdomain>.workers.dev`) — that's what the
dashboard's `index.html` calls.

## Re-deploying after code changes

```bash
cd cf-worker && npx wrangler deploy
```

## Adjusting the daily cap

Edit `DAILY_LIMIT` in `src/index.js`, then re-deploy.
