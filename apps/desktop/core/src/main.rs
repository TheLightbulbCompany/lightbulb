//! `lightbulb`, the standalone binary. For now it answers only the internal subcommands
//! (`holder`, `print-env`); the CLI words move here with `cli.rs`.

fn main() {
    let argv: Vec<String> = std::env::args().collect();
    std::process::exit(lightbulb_core::entry::dispatch(&argv).unwrap_or_else(|| {
        eprintln!("Usage: lightbulb holder <socket> [--size=<cols>x<rows>] -- <command…> | print-env");
        2
    }));
}
