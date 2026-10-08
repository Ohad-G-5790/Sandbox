//! Lamport Transfer: moves lamports from a signer to a recipient through a
//! cross-program invocation (CPI) of the System Program.
//!
//! Accounts:
//!   0. `[signer, writable]` Sender (a System Program owned account)
//!   1. `[writable]`         Recipient
//!   2. `[]`                 System Program
//!
//! Instruction data: little-endian u64 amount in lamports.

use solana_program::{
    account_info::{next_account_info, AccountInfo},
    entrypoint,
    entrypoint::ProgramResult,
    msg,
    program::invoke,
    program_error::ProgramError,
    pubkey::Pubkey,
};
use solana_system_interface::{instruction as system_instruction, program as system_program};

entrypoint!(process_instruction);

/// Parses the amount from the instruction data.
pub fn parse_amount(data: &[u8]) -> Result<u64, ProgramError> {
    let array: [u8; 8] = data
        .get(..8)
        .and_then(|s| s.try_into().ok())
        .ok_or(ProgramError::InvalidInstructionData)?;
    let amount = u64::from_le_bytes(array);
    if amount == 0 {
        return Err(ProgramError::InvalidInstructionData);
    }
    Ok(amount)
}

pub fn process_instruction(
    _program_id: &Pubkey,
    accounts: &[AccountInfo],
    instruction_data: &[u8],
) -> ProgramResult {
    let account_iter = &mut accounts.iter();
    let sender = next_account_info(account_iter)?;
    let recipient = next_account_info(account_iter)?;
    let system_program_account = next_account_info(account_iter)?;

    if !sender.is_signer {
        msg!("Sender must sign the transaction");
        return Err(ProgramError::MissingRequiredSignature);
    }
    if !system_program::check_id(system_program_account.key) {
        msg!("Account 2 must be the System Program");
        return Err(ProgramError::IncorrectProgramId);
    }

    let amount = parse_amount(instruction_data)?;
    msg!(
        "Transferring {} lamports from {} to {}",
        amount,
        sender.key,
        recipient.key
    );

    invoke(
        &system_instruction::transfer(sender.key, recipient.key, amount),
        &[
            sender.clone(),
            recipient.clone(),
            system_program_account.clone(),
        ],
    )?;

    msg!("Transfer complete");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_amount() {
        assert_eq!(parse_amount(&1_000u64.to_le_bytes()).unwrap(), 1_000);
    }

    #[test]
    fn rejects_zero_and_short_data() {
        assert_eq!(
            parse_amount(&0u64.to_le_bytes()),
            Err(ProgramError::InvalidInstructionData)
        );
        assert_eq!(
            parse_amount(&[1, 2]),
            Err(ProgramError::InvalidInstructionData)
        );
    }
}
