# Solana Clock Reader

A native Solana program that reads the `Clock` sysvar through the `Sysvar::get()`
syscall and logs the slot, epoch, epoch start timestamp, leader schedule epoch and
unix timestamp.

### Accounts

None required. The sysvar is read via syscall, so the Clock account does not need to be
passed in.

### Instruction data

None.

## Build

Requires the [Solana CLI](https://docs.solana.com/cli/install-solana-cli-tools) and Rust.

```sh
cargo build-sbf
```

The compiled program lands in `target/deploy/solana_clock_reader.so`.

## Test

The pure helper functions have unit tests that run on the host without the SBF toolchain:

```sh
cargo test
```

## Deploy to devnet

```sh
solana config set --url devnet
solana program deploy target/deploy/solana_clock_reader.so
```

## License

MIT
