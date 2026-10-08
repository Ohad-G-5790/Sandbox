//! Calculator: applies an arithmetic operation to a `u64` stored on chain.
//!
//! Accounts:
//!   0. `[writable]` State account, owned by this program, at least 8 bytes.
//!
//! Instruction data:
//!   byte 0      operation: 0 = add, 1 = subtract, 2 = multiply, 3 = reset
//!   bytes 1..9  little-endian u64 operand (omitted for reset)

use solana_program::{
    account_info::{next_account_info, AccountInfo},
    entrypoint,
    entrypoint::ProgramResult,
    msg,
    program_error::ProgramError,
    pubkey::Pubkey,
};

entrypoint!(process_instruction);

pub const STATE_SIZE: usize = 8;

#[derive(Debug, PartialEq, Eq)]
pub enum Operation {
    Add(u64),
    Subtract(u64),
    Multiply(u64),
    Reset,
}

impl Operation {
    pub fn unpack(data: &[u8]) -> Result<Self, ProgramError> {
        let (&tag, rest) = data
            .split_first()
            .ok_or(ProgramError::InvalidInstructionData)?;
        match tag {
            0 => Ok(Operation::Add(read_operand(rest)?)),
            1 => Ok(Operation::Subtract(read_operand(rest)?)),
            2 => Ok(Operation::Multiply(read_operand(rest)?)),
            3 => Ok(Operation::Reset),
            _ => Err(ProgramError::InvalidInstructionData),
        }
    }
}

fn read_operand(bytes: &[u8]) -> Result<u64, ProgramError> {
    let array: [u8; 8] = bytes
        .get(..8)
        .and_then(|s| s.try_into().ok())
        .ok_or(ProgramError::InvalidInstructionData)?;
    Ok(u64::from_le_bytes(array))
}

/// Applies `op` to `current`, failing on overflow or underflow.
pub fn apply(current: u64, op: &Operation) -> Result<u64, ProgramError> {
    let result = match op {
        Operation::Add(n) => current.checked_add(*n),
        Operation::Subtract(n) => current.checked_sub(*n),
        Operation::Multiply(n) => current.checked_mul(*n),
        Operation::Reset => Some(0),
    };
    result.ok_or(ProgramError::ArithmeticOverflow)
}

pub fn process_instruction(
    program_id: &Pubkey,
    accounts: &[AccountInfo],
    instruction_data: &[u8],
) -> ProgramResult {
    let account_iter = &mut accounts.iter();
    let state_account = next_account_info(account_iter)?;

    if state_account.owner != program_id {
        return Err(ProgramError::IncorrectProgramId);
    }

    let op = Operation::unpack(instruction_data)?;

    let mut data = state_account.try_borrow_mut_data()?;
    let current_bytes: [u8; STATE_SIZE] = data
        .get(..STATE_SIZE)
        .and_then(|s| s.try_into().ok())
        .ok_or(ProgramError::AccountDataTooSmall)?;
    let current = u64::from_le_bytes(current_bytes);
    let next = apply(current, &op)?;
    data[..STATE_SIZE].copy_from_slice(&next.to_le_bytes());

    msg!("{:?}: {} -> {}", op, current, next);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn with_operand(tag: u8, n: u64) -> Vec<u8> {
        let mut v = vec![tag];
        v.extend_from_slice(&n.to_le_bytes());
        v
    }

    #[test]
    fn unpacks_operations() {
        assert_eq!(Operation::unpack(&with_operand(0, 5)).unwrap(), Operation::Add(5));
        assert_eq!(Operation::unpack(&with_operand(1, 5)).unwrap(), Operation::Subtract(5));
        assert_eq!(Operation::unpack(&with_operand(2, 5)).unwrap(), Operation::Multiply(5));
        assert_eq!(Operation::unpack(&[3]).unwrap(), Operation::Reset);
        assert_eq!(Operation::unpack(&[]), Err(ProgramError::InvalidInstructionData));
        assert_eq!(Operation::unpack(&[9]), Err(ProgramError::InvalidInstructionData));
        assert_eq!(Operation::unpack(&[0, 1]), Err(ProgramError::InvalidInstructionData));
    }

    #[test]
    fn applies_checked_arithmetic() {
        assert_eq!(apply(2, &Operation::Add(3)).unwrap(), 5);
        assert_eq!(apply(5, &Operation::Subtract(3)).unwrap(), 2);
        assert_eq!(apply(4, &Operation::Multiply(3)).unwrap(), 12);
        assert_eq!(apply(99, &Operation::Reset).unwrap(), 0);
        assert_eq!(apply(1, &Operation::Subtract(2)), Err(ProgramError::ArithmeticOverflow));
        assert_eq!(apply(u64::MAX, &Operation::Add(1)), Err(ProgramError::ArithmeticOverflow));
    }
}
