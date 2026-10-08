# Solana Message Board

A native Solana program that stores a short UTF-8 message in an account. Each call
overwrites the previous message.

### Accounts

| # | Flags      | Description                             |
|---|------------|-----------------------------------------|
| 0 | `writable` | Message account owned by this program.  |

### Instruction data

The raw UTF-8 bytes of the message.

### Account layout

A little-endian `u32` length followed by the message bytes. The maximum message length is
the account size minus 4.

## Build

Requires the [Solana CLI](https://docs.solana.com/cli/install-solana-cli-tools) and Rust.

```sh
cargo build-sbf
```

The compiled program lands in `target/deploy/solana_message_board.so`.

## Test

The pure helper functions have unit tests that run on the host without the SBF toolchain:

```sh
cargo test
```

## Deploy to devnet

```sh
solana config set --url devnet
solana program deploy target/deploy/solana_message_board.so
```

## License

MIT
