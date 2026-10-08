# Solana Lamport Transfer

A native Solana program that moves lamports from a signer to a recipient by making a
cross-program invocation (CPI) into the System Program.

### Accounts

| # | Flags              | Description                             |
|---|--------------------|-----------------------------------------|
| 0 | `signer, writable` | Sender, a System Program owned account. |
| 1 | `writable`         | Recipient.                              |
| 2 |                    | System Program.                         |

### Instruction data

Little-endian `u64` amount in lamports. Zero is rejected.

## Build

Requires the [Solana CLI](https://docs.solana.com/cli/install-solana-cli-tools) and Rust.

```sh
cargo build-sbf
```

The compiled program lands in `target/deploy/solana_lamport_transfer.so`.

## Test

The pure helper functions have unit tests that run on the host without the SBF toolchain:

```sh
cargo test
```

## Deploy to devnet

```sh
solana config set --url devnet
solana program deploy target/deploy/solana_lamport_transfer.so
```

## License

MIT
