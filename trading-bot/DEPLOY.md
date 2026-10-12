# Running the bot in the cloud

The bot is one always-on container (`Dockerfile` in this folder). By default it **paper trades** on live
Phoenix SOL/USDC data: it reads the real market and simulates fills, with no wallet and no orders. Logs
go to the host's log viewer (a status line every 50 blocks); every block is journaled to `/data`.

It needs one setting: `HFTBOT_RPC_URL`, a private Solana RPC URL (free tiers at Helius or QuickNode are
enough to start). The public endpoint rate-limits a bot that reads the market every 400 ms.

It is not a website, so hosts that only serve web pages or serverless functions (Vercel, Netlify) cannot
run it. It needs a host that keeps a container running.

## Option A: Railway (no server to manage)

1. Sign in at railway.com with GitHub, then **New Project → Deploy from GitHub repo → Ohad-G-5790/Sandbox**.
2. In the service's **Settings**: branch `claude/kind-hopper-yoaxlg`, **Root Directory** `trading-bot`.
   Railway finds the Dockerfile and builds it.
3. **Variables**: add `HFTBOT_RPC_URL` = your RPC URL.
4. **Add a Volume** mounted at `/data` (so journals survive restarts).
5. Pick a region near your RPC provider (US East for most Solana RPCs) and deploy.
6. Open **Logs**. You should see `PAPER TRADING on live data ...`, then a line every 50 blocks.

Render (Background Worker with a disk at `/data`) and Fly.io (a machine with a volume at `/data`) work the
same way: build the Dockerfile in `trading-bot`, set the variable, attach a volume. Menu names change
over time; the three ingredients do not.

## Option B: a small Linux server ($4–6 a month at Hetzner, DigitalOcean, Vultr)

```sh
ssh root@YOUR_SERVER
curl -fsSL https://get.docker.com | sh
git clone -b claude/kind-hopper-yoaxlg https://github.com/Ohad-G-5790/Sandbox.git
cd Sandbox/trading-bot && docker build -t hftbot .
docker run -d --name hftbot --restart unless-stopped \
  -e HFTBOT_RPC_URL="https://..." -v hftbot-data:/data hftbot
docker logs -f hftbot                     # watch it; Ctrl+C leaves it running
```

Update to new code:

```sh
cd ~/Sandbox && git pull && cd trading-bot && docker build -t hftbot .
docker stop hftbot && docker rm hftbot   # stop first: it cancels resting orders on the way out
docker run -d --name hftbot --restart unless-stopped -e HFTBOT_RPC_URL="https://..." -v hftbot-data:/data hftbot
```

Copy a journal to your computer: `docker cp hftbot:/data/paper.jsonl .` on the server, then
`scp root@YOUR_SERVER:~/paper.jsonl .` from your computer.

## What the container does on its own

- **Stopped or redeployed:** the host sends SIGTERM; the bot cancels every resting order, then exits.
  (`docker rm -f` and "kill" skip this. Use stop.)
- **Crashed:** the host restarts it. On a live start, the bot first cancels any orders a previous run left behind.
- **Journals append** across restarts. Note that PnL and position are counted from each start.

## Changing what it runs

Override the start command (Railway: Settings → Custom Start Command; server: arguments after `hftbot` in `docker run`):

| Goal | Command |
|---|---|
| Check the market parses (once) | `market-info` |
| Record real data for backtests | `record /data/sol_usdc.jsonl` |
| Paper trading (default) | `run --config config.solana.toml --journal /data/paper.jsonl` |
| Live trading | `run --live --config config.solana.toml --journal /data/live.jsonl` |

Live trading also needs two **secret** variables on the host, and a seat claimed once (run the one-off
command `claim-seat` with the same variables set):

- `HFTBOT_KEYPAIR_JSON`: the contents of the bot wallet's keypair file (the JSON array from `solana-keygen new`).
- `HFTBOT_CONFIRM_WALLET`: that wallet's public key, confirming which money is at risk.

The host can technically read its secrets. Use a wallet made only for this bot, funded only with what
you can lose, and never put the key in the repository.
