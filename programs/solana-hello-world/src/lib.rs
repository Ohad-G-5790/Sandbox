//! Hello World: the smallest useful native Solana program.
//!
//! It takes no accounts and no instruction data. It simply writes a few
//! lines to the program log so you can confirm a deployment works.

use solana_program::{
    account_info::AccountInfo, entrypoint, entrypoint::ProgramResult, msg, pubkey::Pubkey,
};

entrypoint!(process_instruction);

pub fn process_instruction(
    program_id: &Pubkey,
    accounts: &[AccountInfo],
    instruction_data: &[u8],
) -> ProgramResult {
    msg!("Hello, world!");
    msg!("Program id: {}", program_id);
    msg!("Accounts passed: {}", accounts.len());
    msg!("Instruction data length: {}", instruction_data.len());
    Ok(())
}
