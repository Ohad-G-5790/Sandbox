# Running the bot in the cloud

The bot is one always-on container (`Dockerfile` in this folder). By default it runs the **swing
trader in paper mode**: SOL, BTC, ETH and JUP against USDC, simulated money, no wallet, no real swaps.

On its first start it:
1. downloads about five years of hourly prices (a few minutes),
2. runs the out-of-sample test and prints the report in the logs (also saved as `/data/report.txt`),
3. paper trades the best strategy that beat holding, or stays in USDC if none did,

then wakes up once every 4 hours. Paper fills use real Jupiter quotes when Jupiter is reachable.
Everything (prices, state, journal) lives in `/data`, so attach a volume there.

It is not a website: hosts that only serve web pages or serverless functions (Vercel, Netlify) cannot
run it. It needs a host that keeps a container running.

**Region and price source.** Binance's price API refuses US addresses. In Europe (you are in Germany:
pick an EU region) the default works. On a US region, set the variable `HFTBOT_SWING_SOURCE=coinbase`.

## Option A: Railway (no server to manage)

1. Sign in at railway.com with GitHub, then **New Project → Deploy from GitHub repo → Ohad-G-5790/Sandbox**.
2. In the service's **Settings**: branch `claude/kind-hopper-yoaxlg`, **Root Directory** `trading-bot`.
   Railway finds the Dockerfile and builds it. Pick an EU region.
3. **Add a Volume** mounted at `/data`.
4. Deploy, then open **Logs**. You should see `PAPER TRADING SOL, BTC, ETH, JUP ...`, the download, the
   test report, and then one line per 4-hour bar.

Render (Background Worker with a disk at `/data`) and Fly.io (a machine with a volume at `/data`) work the
same way: build the Dockerfile in `trading-bot`, attach a volume. Menu names change over time; the
ingredients do not.

## Option B: a small Linux server ($4–6 a month at Hetzner, DigitalOcean, Vultr)

```sh
ssh root@YOUR_SERVER
curl -fsSL https://get.docker.com | sh
git clone -b claude/kind-hopper-yoaxlg https://github.com/Ohad-G-5790/Sandbox.git
cd Sandbox/trading-bot && docker build -t hftbot .
docker run -d --name hftbot --restart unless-stopped -v hftbot-data:/data hftbot
docker logs -f hftbot                     # watch it; Ctrl+C leaves it running
docker exec hftbot hftbot swing status --data /data
```

Update to new code:

```sh
cd ~/Sandbox && git pull && cd trading-bot && docker build -t hftbot .
docker stop hftbot && docker rm hftbot   # stop first, so it finishes cleanly
docker run -d --name hftbot --restart unless-stopped -v hftbot-data:/data hftbot
```

## What the container does on its own

- **Stopped or redeployed:** the host sends SIGTERM; the bot finishes cleanly between bars.
- **Crashed:** the host restarts it; state and the kill switch survive in `/data`.
- **A bar fails** (network, data source down): logged, retried in 5 minutes.
- **Prices stale:** it does not trade until fresh prices arrive.
- **Drawdown over the limit** (25% by default): sells everything to USDC and stays out, across restarts,
  until a human runs `swing reset-kill`.

## Changing what it runs

Override the start command (Railway: Settings → Custom Start Command; server: arguments after `hftbot` in `docker run`):

| Goal | Command |
|---|---|
| Swing trader, paper (default) | `swing run --config swing.toml --data /data` |
| Just the test report | `swing test --config swing.toml --data /data` (after a first run or `swing download`) |
| Current state and report | `swing status --data /data` |
| Swing trader, live | `swing run --live --config swing.toml --data /data` |
| Phoenix market maker, paper | `run --config config.solana.toml --journal /data/paper.jsonl` (needs `HFTBOT_RPC_URL`) |

Live swing trading also needs secret variables on the host:

- `HFTBOT_KEYPAIR_JSON`: the contents of the bot wallet's keypair file (the JSON array from `solana-keygen new`).
- `HFTBOT_CONFIRM_WALLET`: that wallet's public key, confirming which money is at risk.
- `HFTBOT_RPC_URL`: a private Solana RPC URL (Helius or QuickNode free tiers are fine at this pace).

Fund that wallet with USDC plus about 0.1 SOL for network fees (0.05 SOL is never traded). The host can
technically read its secrets: use a wallet made only for this bot, funded only with what you can lose,
and never put the key in the repository.
