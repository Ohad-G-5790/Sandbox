//! Counter: increments a `u64` stored in the first 8 bytes of an account.
//!
//! Accounts:
//!   0. `[writable]` Counter account, owned by this program, at least 8 bytes.
//!
//! Instruction data: ignored.

use solana_program::{
    account_info::{next_account_info, AccountInfo},
    entrypoint,
    entrypoint::ProgramResult,
    msg,
    program_error::ProgramError,
    pubkey::Pubkey,
};

entrypoint!(process_instruction);

/// Size of the counter state in bytes.
pub const COUNTER_SIZE: usize = 8;

pub fn process_instruction(
    program_id: &Pubkey,
    accounts: &[AccountInfo],
    _instruction_data: &[u8],
) -> ProgramResult {
    let account_iter = &mut accounts.iter();
    let counter_account = next_account_info(account_iter)?;

    if counter_account.owner != program_id {
        msg!("Counter account is not owned by this program");
        return Err(ProgramError::IncorrectProgramId);
    }
    if !counter_account.is_writable {
        msg!("Counter account must be writable");
        return Err(ProgramError::InvalidAccountData);
    }

    let mut data = counter_account.try_borrow_mut_data()?;
    let current = read_counter(&data)?;
    let next = increment(current)?;
    data[..COUNTER_SIZE].copy_from_slice(&next.to_le_bytes());

    msg!("Counter: {} -> {}", current, next);
    Ok(())
}

/// Reads the little-endian `u64` counter from the start of `bytes`.
pub fn read_counter(bytes: &[u8]) -> Result<u64, ProgramError> {
    let slice = bytes
        .get(..COUNTER_SIZE)
        .ok_or(ProgramError::AccountDataTooSmall)?;
    let array: [u8; COUNTER_SIZE] = slice
        .try_into()
        .map_err(|_| ProgramError::AccountDataTooSmall)?;
    Ok(u64::from_le_bytes(array))
}

/// Adds one to the counter, failing on overflow instead of wrapping.
pub fn increment(value: u64) -> Result<u64, ProgramError> {
    value.checked_add(1).ok_or(ProgramError::ArithmeticOverflow)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_little_endian_counter() {
        let bytes = 42u64.to_le_bytes();
        assert_eq!(read_counter(&bytes).unwrap(), 42);
    }

    #[test]
    fn rejects_short_buffers() {
        assert_eq!(
            read_counter(&[1, 2, 3]),
            Err(ProgramError::AccountDataTooSmall)
        );
    }

    #[test]
    fn increment_fails_on_overflow() {
        assert_eq!(increment(5).unwrap(), 6);
        assert_eq!(increment(u64::MAX), Err(ProgramError::ArithmeticOverflow));
    }
}
