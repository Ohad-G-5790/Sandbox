//! Clock Reader: reads the `Clock` sysvar and logs its fields.
//!
//! It takes no accounts and no instruction data. The sysvar is read through
//! the `Sysvar::get()` syscall, so the Clock account does not need to be
//! passed in.

use solana_program::{
    account_info::AccountInfo, clock::Clock, entrypoint, entrypoint::ProgramResult, msg,
    pubkey::Pubkey, sysvar::Sysvar,
};

entrypoint!(process_instruction);

pub fn process_instruction(
    _program_id: &Pubkey,
    _accounts: &[AccountInfo],
    _instruction_data: &[u8],
) -> ProgramResult {
    let clock = Clock::get()?;
    msg!("Slot: {}", clock.slot);
    msg!("Epoch: {}", clock.epoch);
    msg!("Epoch start timestamp: {}", clock.epoch_start_timestamp);
    msg!("Leader schedule epoch: {}", clock.leader_schedule_epoch);
    msg!("Unix timestamp: {}", clock.unix_timestamp);
    Ok(())
}
