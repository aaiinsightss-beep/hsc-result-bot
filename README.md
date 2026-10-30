# HSC Result Bot

Two shells, one brain:

| shell | where | notes |
|---|---|---|
| cloud brain | GitHub Actions (this repo) | cron `55 4 * * *` UTC = 10:55 Dhaka; holds inside the runner to 10:59:55, polls the queue top-down every 30s, delivers result cards to the SAME Telegram chat. `workflow_dispatch` mode `now` = one immediate pass + verdict. |
| local GUI | your PC (`main_gui.py`) | same engine, county-down UI, lives on config.json — untouched by the cloud. |

## cockpit (Telegram, same chat)

| command | does |
|---|---|
| /start · /help | button menu — Run now · Status · List students · Stop |
| /add name board year roll reg [exam] | add a student; list order = priority, index 1 tops (exam defaults ssc) |
| /list · /del N · /top N | manage the queue |
| /run | poll once right now, reply the verdict |
| /stop | cancel polling for this window (tomorrow's cron re-opens it) |
| /status | engine state, mirror health, next wake |

## state

The queue + engine state live in a secret **Gist** (the KV store):
`queue.json` students in priority order · `state.json` polling flags,
telegram offset, mirror health. Your row sits at index 1 — the brain
polls top-down, shouts when it finds yours, keeps grinding for the rest.

## secrets (Actions → Settings → Secrets → Actions)

- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — delivery + cockpit
- `GIST_TOKEN` — token with the `gist` scope
- `GIST_ID` — the state gist

Nothing personal ever lands in the repo: config.json is gitignored, the
queue lives in the secret gist.

## self-healing

403 tar-pit → cool-off + fresh session · 419 → full session rebuild ·
network dead → 10s cool + next mirror · repeated mirror fail → 60s
backoff · every mirror down → one Telegram one-liner so you know it's
the board, not you · crash / window deadline → workflow re-runs once.
Captcha misses never surface — fresh regrab x4 inside mirrors.py.
