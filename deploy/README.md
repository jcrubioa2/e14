# Deploying the public E-14 community-poll report (Fly.io)

Single always-on machine, SQLite on the box, crops baked into the image. Whole
lifecycle driveable from the terminal via `flyctl`. Pilot scale (~33 MB of data).

## Architecture recap

- **Crowd flags → trigger only.** Crossing the vote threshold triggers a VLM second
  opinion via OpenRouter; the VLM (not the crowd) decides what is published as
  "strange". A "clean" verdict is **re-eligible** (re-adjudicated if votes keep
  climbing by `E14_POLL_RESCALE_STEP`), so one flaky verdict can't bury a real
  anomaly. "Strange" is terminal.
- **Read-only data** (`results.sqlite` + crops) is baked into the image.
- **Writable data** (`community.sqlite`: votes + adjudication state) lives on the Fly
  volume mounted at `/data` and survives every redeploy.
- **Abuse control:** per-voter token-bucket rate limit + Cloudflare Turnstile +
  per-image/per-field caching. Spend ceiling is managed in the OpenRouter dashboard.

## One-time prerequisites

1. **Cloudflare Turnstile** (only browser step): create a free Cloudflare account →
   Turnstile → add a widget. Note the **site key** (public) and **secret key**.
2. **OpenRouter**: create an API key at <https://openrouter.ai/keys> and set a spend
   limit on the dashboard. Pick a vision model (default
   `qwen/qwen-2.5-vl-7b-instruct`).
3. **flyctl**: install, then `flyctl auth login` (run interactively, e.g. via `!` in
   Claude Code, since it opens a browser).

## Deploy

```bash
# 1. Create the app (uses fly.toml; don't deploy yet).
fly launch --no-deploy --copy-config --name e14-poll --region mia

# 2. Persistent volume for the votes DB (mount source "data" -> /data).
fly volumes create data --size 1 --region mia

# 3. Secrets (never baked into the image).
fly secrets set \
  E14_OPENROUTER_API_KEY="sk-or-..." \
  E14_TURNSTILE_SECRET="0x...secret..." \
  E14_VOTER_SALT="$(openssl rand -hex 16)"

# 4. Public Turnstile site key (non-secret) — either edit fly.toml [env] or:
fly secrets set E14_TURNSTILE_SITEKEY="0x...sitekey..."   # secrets override [env]

# 5. Ship it.
fly deploy
```

Live at `https://e14-poll.fly.dev`. The public poll page is `/browse`; the existing
automatic-anomaly dashboard remains at `/`.

## Operate

```bash
fly logs           # watch flag POSTs + backgrounded VLM adjudications
fly status         # machine health (expect exactly 1, always running)
fly ssh console    # shell on the box; community.sqlite is at /data/community.sqlite
```

## Updating the baked data

Re-run the detector locally, then `fly deploy` — the new `data/detector` is shipped
in the image. Votes in `/data/community.sqlite` are untouched because they key off a
**stable** `document:page:row:section` field key, not the (re-assigned) row id.

## Backups (recommended once live)

Stream `community.sqlite` to object storage with Litestream (Fly **Tigris** is
built-in and S3-compatible: `fly storage create`). Deferred for the first deploy.

## Scaling to the full national dataset (later)

Don't bake ~17 GB of crops into the image. Push crops to S3/Tigris + a CDN, change
the template to emit `<img src="https://cdn/...">` (a deterministic `raw_crop_path →
cdn_url` mapping), and upload `results.sqlite` to the volume via `fly ssh sftp`.
`community.sqlite` stays on the volume unchanged.

## Local dev (WSL)

```bash
make detector-serve   # http://127.0.0.1:8001
```

With `E14_VLM_PROVIDER=mock` (or no OpenRouter key) adjudication uses the
deterministic stub, and with an empty `E14_TURNSTILE_SECRET` the captcha check is
skipped — so the flag flow works fully offline.
