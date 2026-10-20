use std::{path::PathBuf, process::Command};
fn git(args: &[&str]) -> Option<String> {
    let result = Command::new("git").args(args).output().ok()?;
    result
        .status
        .success()
        .then(|| String::from_utf8_lossy(&result.stdout).trim().to_owned())
}
fn main() {
    // Worktree refs live outside the source directory; track them explicitly for installed identity.
    for name in ["HEAD", "index", "packed-refs"] {
        if let Some(path) = git(&["rev-parse", "--path-format=absolute", "--git-path", name]) {
            println!("cargo:rerun-if-changed={path}");
        }
    }
    if let Some(reference) = git(&["symbolic-ref", "-q", "HEAD"]) {
        if let Some(path) = git(&[
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            &reference,
        ]) {
            println!("cargo:rerun-if-changed={path}");
        }
    }
    println!("cargo:rerun-if-changed=src");
    println!("cargo:rerun-if-changed=prompts");
    println!("cargo:rerun-if-changed=Cargo.toml");
    let root = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
    // Packaged sources without Git metadata report unknown rather than an unrelated enclosing checkout.
    let tracked = git(&["ls-files", "--error-unmatch", "src/bin/worklane.rs"]).is_some();
    for (key, value) in [
        (
            "WORKLANE_REVISION",
            tracked.then(|| git(&["rev-parse", "HEAD"])).flatten(),
        ),
        (
            "WORKLANE_BRANCH",
            tracked
                .then(|| git(&["branch", "--show-current"]))
                .flatten(),
        ),
        (
            "WORKLANE_DIRTY",
            tracked
                .then(|| {
                    git(&["status", "--porcelain", "--untracked-files=no"])
                        .map(|s| (!s.is_empty()).to_string())
                })
                .flatten(),
        ),
    ] {
        println!("cargo:rustc-env={key}={}", value.unwrap_or_default());
    }
    println!("cargo:rustc-env=WORKLANE_SOURCE={}", root.display());
}
