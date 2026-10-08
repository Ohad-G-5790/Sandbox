# Solana Account Inspector

A native Solana program that logs metadata about every account passed to it without
modifying anything: address, owner, lamport balance, data length, signer / writable /
executable flags, and whether the balance is rent exempt.

### Accounts

Any number of accounts, in any order.

### Instruction data

None.

## Build

Requires the [Solana CLI](https://docs.solana.com/cli/install-solana-cli-tools) and Rust.

```sh
cargo build-sbf
```

The compiled program lands in `target/deploy/solana_account_inspector.so`.

## Test

The pure helper functions have unit tests that run on the host without the SBF toolchain:

```sh
cargo test
```

## Deploy to devnet

```sh
solana config set --url devnet
solana program deploy target/deploy/solana_account_inspector.so
```

## License

MIT
