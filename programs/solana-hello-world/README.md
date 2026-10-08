# Solana Hello World

The smallest useful native (non-Anchor) Solana program. It takes no accounts and no
instruction data and writes a greeting, the program id, the number of accounts and the
instruction data length to the program log.

Invoke it with an empty instruction and check the transaction logs with
`solana confirm -v <signature>`.

## Build

Requires the [Solana CLI](https://docs.solana.com/cli/install-solana-cli-tools) and Rust.

```sh
cargo build-sbf
```

The compiled program lands in `target/deploy/solana_hello_world.so`.

## Test

The pure helper functions have unit tests that run on the host without the SBF toolchain:

```sh
cargo test
```

## Deploy to devnet

```sh
solana config set --url devnet
solana program deploy target/deploy/solana_hello_world.so
```

## License

MIT
