# Sandbox: basic Solana programs

Ten small native (non-Anchor) Solana programs, each a standalone Rust crate under
`programs/`. Every crate builds with `cargo build-sbf` and has unit tests for its pure
helper functions that run with plain `cargo test`.

| Program | What it does |
|---------|--------------|
| [solana-hello-world](programs/solana-hello-world) | Logs a greeting and basic call info |
| [solana-counter](programs/solana-counter) | Increments a `u64` stored in an account |
| [solana-echo](programs/solana-echo) | Logs the instruction data as hex and UTF-8 |
| [solana-clock-reader](programs/solana-clock-reader) | Reads and logs the Clock sysvar |
| [solana-calculator](programs/solana-calculator) | Add, subtract, multiply and reset an on-chain value |
| [solana-lamport-transfer](programs/solana-lamport-transfer) | Transfers lamports via a System Program CPI |
| [solana-pda-storage](programs/solana-pda-storage) | Creates a per-user PDA account and stores a `u64` |
| [solana-vote-tally](programs/solana-vote-tally) | Keeps an upvote and downvote tally |
| [solana-message-board](programs/solana-message-board) | Stores a short UTF-8 message in an account |
| [solana-account-inspector](programs/solana-account-inspector) | Logs metadata about every account passed in |

## Publishing each program as its own repository

`scripts/publish-repos.sh` creates one public GitHub repository per program and pushes
the program into it. It needs the [GitHub CLI](https://cli.github.com/) logged in:

```sh
gh auth login
gh auth setup-git
scripts/publish-repos.sh
```

Run it with `DRY_RUN=1` first to see what it would do without touching GitHub.

## Piano app

[`piano/`](piano) is a velocity-sensitive grand piano that runs in the browser: open
`piano/index.html` and play it with any MIDI keyboard (real key velocity and pedals),
your computer keyboard, the mouse, touch or a pen. See [piano/README.md](piano/README.md).
