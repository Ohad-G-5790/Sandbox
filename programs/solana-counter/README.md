# Solana Counter

A native Solana program that increments a `u64` counter stored in the first 8 bytes of
an account.

### Accounts

| # | Flags      | Description                                                   |
|---|------------|---------------------------------------------------------------|
| 0 | `writable` | Counter account owned by this program, at least 8 bytes long. |

### Instruction data

None. Every call adds one to the counter and fails on overflow.

## Build

Requires the [Solana CLI](https://docs.solana.com/cli/install-solana-cli-tools) and Rust.

```sh
cargo build-sbf
```

The compiled program lands in `target/deploy/solana_counter.so`.

## Test

The pure helper functions have unit tests that run on the host without the SBF toolchain:

```sh
cargo test
```

## Deploy to devnet

```sh
solana config set --url devnet
solana program deploy target/deploy/solana_counter.so
```

## License

MIT
