//! Message Board: stores a short UTF-8 message in an account.
//!
//! Accounts:
//!   0. `[writable]` Message account, owned by this program.
//!
//! Instruction data: the raw UTF-8 bytes of the message.
//!
//! Account layout: `u32` little-endian length followed by the message bytes.
//! The maximum message length is the account size minus 4.

use solana_program::{
    account_info::{next_account_info, AccountInfo},
    entrypoint,
    entrypoint::ProgramResult,
    msg,
    program_error::ProgramError,
    pubkey::Pubkey,
};

entrypoint!(process_instruction);

const LENGTH_PREFIX: usize = 4;

/// Writes `message` into `buf` with a length prefix.
pub fn write_message(buf: &mut [u8], message: &str) -> Result<(), ProgramError> {
    let bytes = message.as_bytes();
    let required = LENGTH_PREFIX + bytes.len();
    if buf.len() < required {
        return Err(ProgramError::AccountDataTooSmall);
    }
    let len = u32::try_from(bytes.len()).map_err(|_| ProgramError::InvalidInstructionData)?;
    buf[..LENGTH_PREFIX].copy_from_slice(&len.to_le_bytes());
    buf[LENGTH_PREFIX..required].copy_from_slice(bytes);
    Ok(())
}

/// Reads the stored message from `buf`.
pub fn read_message(buf: &[u8]) -> Result<&str, ProgramError> {
    let prefix: [u8; LENGTH_PREFIX] = buf
        .get(..LENGTH_PREFIX)
        .and_then(|s| s.try_into().ok())
        .ok_or(ProgramError::AccountDataTooSmall)?;
    let len = u32::from_le_bytes(prefix) as usize;
    let bytes = buf
        .get(LENGTH_PREFIX..LENGTH_PREFIX + len)
        .ok_or(ProgramError::InvalidAccountData)?;
    core::str::from_utf8(bytes).map_err(|_| ProgramError::InvalidAccountData)
}

pub fn process_instruction(
    program_id: &Pubkey,
    accounts: &[AccountInfo],
    instruction_data: &[u8],
) -> ProgramResult {
    let account_iter = &mut accounts.iter();
    let message_account = next_account_info(account_iter)?;

    if message_account.owner != program_id {
        return Err(ProgramError::IncorrectProgramId);
    }

    let message =
        core::str::from_utf8(instruction_data).map_err(|_| ProgramError::InvalidInstructionData)?;

    let mut data = message_account.try_borrow_mut_data()?;
    write_message(&mut data, message)?;

    msg!("Stored message: {}", message);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trips_a_message() {
        let mut buf = [0u8; 32];
        write_message(&mut buf, "hello solana").unwrap();
        assert_eq!(read_message(&buf).unwrap(), "hello solana");
    }

    #[test]
    fn rejects_messages_that_do_not_fit() {
        let mut buf = [0u8; 8];
        assert_eq!(
            write_message(&mut buf, "too long!"),
            Err(ProgramError::AccountDataTooSmall)
        );
    }

    #[test]
    fn rejects_corrupt_length() {
        let mut buf = [0u8; 8];
        buf[..4].copy_from_slice(&100u32.to_le_bytes());
        assert_eq!(read_message(&buf), Err(ProgramError::InvalidAccountData));
    }
}
