//! PDA Storage: creates a program derived address (PDA) account per user and
//! stores a `u64` in it.
//!
//! The PDA is derived from the seeds `["storage", user_pubkey]`.
//!
//! Instruction 0, Initialize
//!   accounts: 0. `[signer, writable]` user / payer
//!             1. `[writable]`         PDA storage account
//!             2. `[]`                 System Program
//!   data:     [0]
//!
//! Instruction 1, Set
//!   accounts: 0. `[signer]`   user
//!             1. `[writable]` PDA storage account
//!   data:     [1, u64 little-endian value]

use solana_program::{
    account_info::{next_account_info, AccountInfo},
    entrypoint,
    entrypoint::ProgramResult,
    msg,
    program::invoke_signed,
    program_error::ProgramError,
    pubkey::Pubkey,
    rent::Rent,
    sysvar::Sysvar,
};
use solana_system_interface::{instruction as system_instruction, program as system_program};

entrypoint!(process_instruction);

pub const SEED_PREFIX: &[u8] = b"storage";
pub const STORAGE_SIZE: usize = 8;

#[derive(Debug, PartialEq, Eq)]
pub enum Instruction {
    Initialize,
    Set(u64),
}

impl Instruction {
    pub fn unpack(data: &[u8]) -> Result<Self, ProgramError> {
        match data.split_first() {
            Some((0, _)) => Ok(Instruction::Initialize),
            Some((1, rest)) => {
                let array: [u8; 8] = rest
                    .get(..8)
                    .and_then(|s| s.try_into().ok())
                    .ok_or(ProgramError::InvalidInstructionData)?;
                Ok(Instruction::Set(u64::from_le_bytes(array)))
            }
            _ => Err(ProgramError::InvalidInstructionData),
        }
    }
}

/// Derives the storage PDA for `user`.
pub fn storage_address(user: &Pubkey, program_id: &Pubkey) -> (Pubkey, u8) {
    Pubkey::find_program_address(&[SEED_PREFIX, user.as_ref()], program_id)
}

pub fn process_instruction(
    program_id: &Pubkey,
    accounts: &[AccountInfo],
    instruction_data: &[u8],
) -> ProgramResult {
    let account_iter = &mut accounts.iter();
    let user = next_account_info(account_iter)?;
    let storage = next_account_info(account_iter)?;

    if !user.is_signer {
        return Err(ProgramError::MissingRequiredSignature);
    }

    let (expected_address, bump) = storage_address(user.key, program_id);
    if expected_address != *storage.key {
        msg!("Storage account does not match the expected PDA");
        return Err(ProgramError::InvalidSeeds);
    }

    match Instruction::unpack(instruction_data)? {
        Instruction::Initialize => {
            let system_program_account = next_account_info(account_iter)?;
            if !system_program::check_id(system_program_account.key) {
                return Err(ProgramError::IncorrectProgramId);
            }

            let lamports = Rent::get()?.minimum_balance(STORAGE_SIZE);
            invoke_signed(
                &system_instruction::create_account(
                    user.key,
                    storage.key,
                    lamports,
                    STORAGE_SIZE as u64,
                    program_id,
                ),
                &[
                    user.clone(),
                    storage.clone(),
                    system_program_account.clone(),
                ],
                &[&[SEED_PREFIX, user.key.as_ref(), &[bump]]],
            )?;
            msg!("Initialized storage at {}", storage.key);
        }
        Instruction::Set(value) => {
            if storage.owner != program_id {
                return Err(ProgramError::IncorrectProgramId);
            }
            let mut data = storage.try_borrow_mut_data()?;
            if data.len() < STORAGE_SIZE {
                return Err(ProgramError::AccountDataTooSmall);
            }
            data[..STORAGE_SIZE].copy_from_slice(&value.to_le_bytes());
            msg!("Stored value {}", value);
        }
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unpacks_instructions() {
        assert_eq!(Instruction::unpack(&[0]).unwrap(), Instruction::Initialize);
        let mut set = vec![1];
        set.extend_from_slice(&7u64.to_le_bytes());
        assert_eq!(Instruction::unpack(&set).unwrap(), Instruction::Set(7));
        assert_eq!(
            Instruction::unpack(&[1, 0]),
            Err(ProgramError::InvalidInstructionData)
        );
        assert_eq!(
            Instruction::unpack(&[2]),
            Err(ProgramError::InvalidInstructionData)
        );
    }

    #[test]
    fn derives_a_stable_pda() {
        let program_id = Pubkey::new_unique();
        let user = Pubkey::new_unique();
        let (a, bump_a) = storage_address(&user, &program_id);
        let (b, bump_b) = storage_address(&user, &program_id);
        assert_eq!(a, b);
        assert_eq!(bump_a, bump_b);
        assert!(!a.is_on_curve());
    }
}
