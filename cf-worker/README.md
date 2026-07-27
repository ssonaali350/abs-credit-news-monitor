# Ask AI backend + ingest cron fallback (Cloudflare Worker)

Two jobs in one Worker:

1. Proxies the dashboard's "Ask AI" questions to Claude, holding the Anthropic
   API key as a Worker secret so it's never exposed to visitors' browsers.
   Includes a daily request cap (see `DAILY_LIMIT` in `src/index.js`) as a
   safety rail against unexpected traffic spikes/cost.
2. A Cron Trigger (`[triggers] crons` in `wrangler.toml`) that fires GitHub's
   `workflow_dispatch` API for the daily news-ingest workflow at 12:07 UTC.
   This exists because GitHub Actions' own `schedule` event missed two days
   running despite correct workflow config — a documented GitHub limitation
   (scheduled workflows are best-effort and get deprioritized under
   platform load), not something fixable from the workflow YAML alone.
   Cloudflare Cron Triggers are a more reliable scheduling primitive, so
   this Worker acts as an external, more dependable trigger for the same
   job GitHub's scheduler was supposed to run.

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

# 4. Store a GitHub fine-grained PAT (Actions: Read and write, scoped to
#    just this repo) so the Worker can fire workflow_dispatch on schedule
npx wrangler secret put GITHUB_PAT

# 5. Deploy
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
