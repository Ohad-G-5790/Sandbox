//! Account Inspector: logs metadata about every account passed to it.
//!
//! Pass any accounts you like; the program reads them without modifying
//! anything. For each account it logs the address, owner, lamport balance,
//! data length, signer / writable / executable flags, and whether the balance
//! is rent exempt.

use solana_program::{
    account_info::AccountInfo, entrypoint, entrypoint::ProgramResult, msg, pubkey::Pubkey,
    rent::Rent, sysvar::Sysvar,
};

entrypoint!(process_instruction);

pub fn process_instruction(
    _program_id: &Pubkey,
    accounts: &[AccountInfo],
    _instruction_data: &[u8],
) -> ProgramResult {
    let rent = Rent::get()?;
    msg!("Inspecting {} account(s)", accounts.len());

    for (index, account) in accounts.iter().enumerate() {
        let lamports = account.lamports();
        let data_len = account.data_len();
        msg!("Account {}: {}", index, account.key);
        msg!("  owner: {}", account.owner);
        msg!("  lamports: {}", lamports);
        msg!("  data length: {}", data_len);
        msg!(
            "  signer: {}, writable: {}, executable: {}",
            account.is_signer,
            account.is_writable,
            account.executable
        );
        msg!("  rent exempt: {}", rent.is_exempt(lamports, data_len));
    }

    Ok(())
}
