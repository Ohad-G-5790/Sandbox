# Solana Echo

A native Solana program that logs whatever instruction data it receives, both as a hex
string and, when the bytes are valid, as UTF-8 text. Handy as a first test of passing
custom instruction data to a program.

### Accounts

None required.

### Instruction data

Any bytes.

## Build

Requires the [Solana CLI](https://docs.solana.com/cli/install-solana-cli-tools) and Rust.

```sh
cargo build-sbf
```

The compiled program lands in `target/deploy/solana_echo.so`.

## Test

The pure helper functions have unit tests that run on the host without the SBF toolchain:

```sh
cargo test
```

## Deploy to devnet

```sh
solana config set --url devnet
solana program deploy target/deploy/solana_echo.so
```

## License

MIT
