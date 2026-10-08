# Solana Vote Tally

A native Solana program that keeps an upvote and downvote count in an account.

### Accounts

| # | Flags      | Description                                                  |
|---|------------|--------------------------------------------------------------|
| 0 | `writable` | Tally account owned by this program, at least 16 bytes long. |

### Instruction data

| Byte | Meaning                  |
|------|--------------------------|
| 0    | `0` upvote, `1` downvote |

### Account layout

`up: u64` followed by `down: u64`, both little-endian.

## Build

Requires the [Solana CLI](https://docs.solana.com/cli/install-solana-cli-tools) and Rust.

```sh
cargo build-sbf
```

The compiled program lands in `target/deploy/solana_vote_tally.so`.

## Test

The pure helper functions have unit tests that run on the host without the SBF toolchain:

```sh
cargo test
```

## Deploy to devnet

```sh
solana config set --url devnet
solana program deploy target/deploy/solana_vote_tally.so
```

## License

MIT
