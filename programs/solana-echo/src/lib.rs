//! Echo: logs whatever instruction data it receives.
//!
//! Useful as a first test of sending custom instruction data to a program.
//! The bytes are logged as hex and, when valid, as a UTF-8 string.

use solana_program::{
    account_info::AccountInfo, entrypoint, entrypoint::ProgramResult, msg, pubkey::Pubkey,
};

entrypoint!(process_instruction);

pub fn process_instruction(
    _program_id: &Pubkey,
    _accounts: &[AccountInfo],
    instruction_data: &[u8],
) -> ProgramResult {
    msg!("Received {} bytes", instruction_data.len());
    msg!("Hex: {}", to_hex(instruction_data));
    match core::str::from_utf8(instruction_data) {
        Ok(text) => msg!("UTF-8: {}", text),
        Err(_) => msg!("Instruction data is not valid UTF-8"),
    }
    Ok(())
}

/// Encodes `bytes` as a lowercase hex string.
pub fn to_hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{:02x}", b)).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encodes_hex() {
        assert_eq!(to_hex(&[0x00, 0xab, 0xff]), "00abff");
        assert_eq!(to_hex(&[]), "");
    }
}
