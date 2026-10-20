//! Controlled PTY acceptance helper: real ownership/generation, no providers or GitHub writes.
use serde_json::json;
use std::{thread, time::Duration};
use worklane_runtime::{
    config::ConfigStore, controller::Controller, state::StateStore, transport::RepositoryLocator,
};
fn main() {
    let args = std::env::args().collect::<Vec<_>>();
    let value = |flag: &str| {
        args.iter()
            .position(|s| s == flag)
            .and_then(|i| args.get(i + 1))
    };
    let root = std::path::PathBuf::from(value("--repo").unwrap());
    let repository = RepositoryLocator::locate(&root).unwrap();
    let state = StateStore::open(&repository.repo, None).unwrap();
    let observed = state.get_meta("shutdown", json!({})).unwrap();
    let controller = Controller::acquire(
        &state,
        &root,
        &ConfigStore::defaults(),
        &observed,
        value("--generation").map(String::as_str),
    )
    .unwrap();
    while state.get_meta("shutdown", json!({})).unwrap()["requested"] != true {
        thread::sleep(Duration::from_millis(25));
    }
    drop(controller);
}
