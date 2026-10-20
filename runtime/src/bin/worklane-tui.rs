use std::{path::PathBuf, time::Duration};
use worklane_runtime::{
    Error, Result,
    tui::{Options, run},
};
fn main() {
    if let Err(e) = main_result() {
        eprintln!("worklane-tui: {e}");
        std::process::exit(1);
    }
}
fn main_result() -> Result<()> {
    let mut options = Options::new(std::env::current_dir()?);
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--help" | "-h" => {
                println!(
                    "Native Worklane TUI\nUsage: worklane-tui [--repo PATH] [--refresh SECONDS] [--project OWNER/NUMBER] [--no-color] [--controller PATH]\nNavigation: j/k arrows Enter, / search, i issue, B board, n/e editor, c config, p PR, o browser, a session, m input\nControls: s start, z pause, u resume, x drain, X stop now, t retry, q quit; F2/Ctrl-S save, Esc cancel"
                );
                return Ok(());
            }
            "--version" | "-V" => {
                println!("worklane-tui {}", env!("CARGO_PKG_VERSION"));
                return Ok(());
            }
            "--repo" => options.root = PathBuf::from(value(&mut args, &arg)?),
            "--project" => options.invocation.project = Some(value(&mut args, &arg)?),
            "--refresh" => {
                let value = value(&mut args, &arg)?
                    .parse::<f64>()
                    .map_err(|_| Error::new("validation", "Invalid refresh interval"))?;
                if !value.is_finite() || !(0.05..=3600.).contains(&value) {
                    return worklane_runtime::fail(
                        "Refresh must be between 0.05 and 3600 seconds.",
                    );
                }
                options.refresh = Duration::from_secs_f64(value);
            }
            "--controller" => {
                let path = value(&mut args, &arg)?;
                options.controller = vec![
                    path,
                    "run".into(),
                    "--repo".into(),
                    options.root.to_string_lossy().into_owned(),
                ];
            }
            "--no-color" => options.no_color = true,
            "--dangerously-allow-public-board" => {
                options.invocation.dangerously_allow_public_board = true
            }
            _ => return worklane_runtime::fail(format!("Unknown option: {arg}")),
        }
    }
    if !options.controller.is_empty() {
        options.controller[3] = options.root.to_string_lossy().into_owned();
    }
    options.no_color |= std::env::var_os("NO_COLOR").is_some();
    run(options)
}
fn value(args: &mut impl Iterator<Item = String>, flag: &str) -> Result<String> {
    args.next()
        .ok_or_else(|| Error::new("validation", format!("{flag} requires a value")))
}
