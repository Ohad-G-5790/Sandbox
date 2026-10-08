//! Vote Tally: keeps an upvote and downvote count in an account.
//!
//! Accounts:
//!   0. `[writable]` Tally account, owned by this program, at least 16 bytes.
//!
//! Instruction data:
//!   byte 0  0 = upvote, 1 = downvote
//!
//! Account layout: `up: u64` followed by `down: u64`, both little-endian.

use solana_program::{
    account_info::{next_account_info, AccountInfo},
    entrypoint,
    entrypoint::ProgramResult,
    msg,
    program_error::ProgramError,
    pubkey::Pubkey,
};

entrypoint!(process_instruction);

pub const TALLY_SIZE: usize = 16;

#[derive(Debug, Default, PartialEq, Eq, Clone, Copy)]
pub struct Tally {
    pub up: u64,
    pub down: u64,
}

impl Tally {
    pub fn unpack(bytes: &[u8]) -> Result<Self, ProgramError> {
        let slice = bytes
            .get(..TALLY_SIZE)
            .ok_or(ProgramError::AccountDataTooSmall)?;
        let up = u64::from_le_bytes(slice[..8].try_into().unwrap());
        let down = u64::from_le_bytes(slice[8..16].try_into().unwrap());
        Ok(Tally { up, down })
    }

    pub fn pack(&self, bytes: &mut [u8]) -> Result<(), ProgramError> {
        let slice = bytes
            .get_mut(..TALLY_SIZE)
            .ok_or(ProgramError::AccountDataTooSmall)?;
        slice[..8].copy_from_slice(&self.up.to_le_bytes());
        slice[8..16].copy_from_slice(&self.down.to_le_bytes());
        Ok(())
    }

    pub fn upvote(self) -> Result<Self, ProgramError> {
        Ok(Tally {
            up: self
                .up
                .checked_add(1)
                .ok_or(ProgramError::ArithmeticOverflow)?,
            ..self
        })
    }

    pub fn downvote(self) -> Result<Self, ProgramError> {
        Ok(Tally {
            down: self
                .down
                .checked_add(1)
                .ok_or(ProgramError::ArithmeticOverflow)?,
            ..self
        })
    }
}

pub fn process_instruction(
    program_id: &Pubkey,
    accounts: &[AccountInfo],
    instruction_data: &[u8],
) -> ProgramResult {
    let account_iter = &mut accounts.iter();
    let tally_account = next_account_info(account_iter)?;

    if tally_account.owner != program_id {
        return Err(ProgramError::IncorrectProgramId);
    }

    let mut data = tally_account.try_borrow_mut_data()?;
    let tally = Tally::unpack(&data)?;
    let updated = match instruction_data.first() {
        Some(0) => tally.upvote()?,
        Some(1) => tally.downvote()?,
        _ => return Err(ProgramError::InvalidInstructionData),
    };
    updated.pack(&mut data)?;

    msg!("Tally: {} up, {} down", updated.up, updated.down);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trips_through_bytes() {
        let tally = Tally { up: 3, down: 1 };
        let mut buf = [0u8; TALLY_SIZE];
        tally.pack(&mut buf).unwrap();
        assert_eq!(Tally::unpack(&buf).unwrap(), tally);
    }

    #[test]
    fn counts_votes() {
        let tally = Tally::default()
            .upvote()
            .unwrap()
            .upvote()
            .unwrap()
            .downvote()
            .unwrap();
        assert_eq!(tally, Tally { up: 2, down: 1 });
    }

    #[test]
    fn rejects_small_buffers() {
        assert_eq!(
            Tally::unpack(&[0; 8]),
            Err(ProgramError::AccountDataTooSmall)
        );
    }
}
