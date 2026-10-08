# Solana PDA Storage

A native Solana program that creates one program derived address (PDA) account per user
and stores a `u64` in it. The PDA is derived from the seeds `["storage", user_pubkey]`,
and the program signs the account creation with `invoke_signed`.

### Instruction 0: Initialize

| # | Flags              | Description          |
|---|--------------------|----------------------|
| 0 | `signer, writable` | User and fee payer.  |
| 1 | `writable`         | PDA storage account. |
| 2 |                    | System Program.      |

Instruction data: `[0]`

### Instruction 1: Set

| # | Flags      | Description          |
|---|------------|----------------------|
| 0 | `signer`   | User.                |
| 1 | `writable` | PDA storage account. |

Instruction data: `[1]` followed by a little-endian `u64` value.

## Build

Requires the [Solana CLI](https://docs.solana.com/cli/install-solana-cli-tools) and Rust.

```sh
cargo build-sbf
```

The compiled program lands in `target/deploy/solana_pda_storage.so`.

## Test

The pure helper functions have unit tests that run on the host without the SBF toolchain:

```sh
cargo test
```

## Deploy to devnet

```sh
solana config set --url devnet
solana program deploy target/deploy/solana_pda_storage.so
```

## License

MIT
