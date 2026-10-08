# Solana Calculator

A native Solana program that keeps a `u64` in an account and applies arithmetic
operations to it. All arithmetic is checked, so overflow and underflow fail the
transaction instead of wrapping.

### Accounts

| # | Flags      | Description                                                 |
|---|------------|-------------------------------------------------------------|
| 0 | `writable` | State account owned by this program, at least 8 bytes long. |

### Instruction data

| Bytes | Meaning                                                   |
|-------|-----------------------------------------------------------|
| 0     | Operation: `0` add, `1` subtract, `2` multiply, `3` reset |
| 1..9  | Little-endian `u64` operand (omitted for reset)           |

## Build

Requires the [Solana CLI](https://docs.solana.com/cli/install-solana-cli-tools) and Rust.

```sh
cargo build-sbf
```

The compiled program lands in `target/deploy/solana_calculator.so`.

## Test

The pure helper functions have unit tests that run on the host without the SBF toolchain:

```sh
cargo test
```

## Deploy to devnet

```sh
solana config set --url devnet
solana program deploy target/deploy/solana_calculator.so
```

## License

MIT
