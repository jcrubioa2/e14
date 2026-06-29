# splitwise-uploader

A tiny [Splitwise](https://www.splitwise.com/) helper for adding and splitting
expenses from the command line (or for Claude to drive on demand). One file does the
work: `splitwise/client.py`.

## 1. Get a Splitwise API key (one-time, manual)

Personal access uses a **personal API key as a Bearer token** — no OAuth flow.

1. Go to **https://secure.splitwise.com/apps** → *Register a new application*.
2. Fill the required fields with anything valid:
   - **Application name**: e.g. `Claude`
   - **Application description**: e.g. `Personal CLI to add/split expenses via the API`
   - **Homepage URL**: any valid URL — it's only format-checked, doesn't need to
     resolve (e.g. `https://github.com/<you>`)
   - Callback/Support URL: leave blank
3. Accept the Terms → **Register and get API key** → copy the key.

## 2. Make the key available

The client reads `SPLITWISE_API_KEY` from the environment.

- **For Claude Code web sessions**: set `SPLITWISE_API_KEY` as an environment
  variable in your environment config (Settings → Environment). Then any future
  session can use it with no setup.
- **For local use**: `cp .env.example .env`, paste your key, and export it
  (`set -a; . ./.env; set +a`) or otherwise put it in your shell environment.

The key is **never committed** (`.env` is gitignored).

## 3. Install

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

## 4. Use

```bash
# Confirm the key works — prints your account:
python -m splitwise.client whoami

# Find IDs to reference:
python -m splitwise.client groups
python -m splitwise.client friends

# Equal split within a group:
python -m splitwise.client add --cost 40.00 --desc "Dinner" --group 12345 --equal

# Custom split: user 1 paid, everyone owes a set amount:
python -m splitwise.client add --cost 30.00 --desc "Taxi" --group 12345 \
    --payer 1 --owed 1:10.00 --owed 2:10.00 --owed 3:10.00
```

`--owed` takes `USER_ID:AMOUNT` (repeatable) and requires `--payer`. The owed and
paid amounts must each sum to `--cost` (Splitwise's rule); the client validates this
before sending.

## How Claude uses it

When you message Claude with something like *"add a $40 dinner split with Alice and
Bob"*, it resolves the group/user IDs (via `groups`/`friends`) and runs
`add` for you. The API key comes from the environment, so nothing is re-entered.

## Test

```bash
pip install pytest
pytest          # fully mocked, no network
```

## API reference

- Base URL: `https://secure.splitwise.com/api/v3.0`
- Auth: `Authorization: Bearer <SPLITWISE_API_KEY>`
- Docs: https://dev.splitwise.com/
- Note: `create_expense` returns HTTP 200 even on validation failure, with the
  problem in a non-empty `errors` object — the client checks this and raises.
