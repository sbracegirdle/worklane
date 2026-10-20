//! Controlled guard inputs for the unchanged portable failure fixtures.
use serde_json::{Value, json};
use std::{os::fd::AsRawFd, sync::Arc};
use worklane_runtime::{
    Error, Result,
    harness::AgentHarness,
    transport::{CommandExecutor, CommandResult, GitHubTransport},
    workflow::Workflow,
};

const BUILD: &[&str] = &[
    "build.blocked-result",
    "build.pass-with-findings",
    "build.question-created-pr",
    "build.invalid-sha",
    "build.wrong-head",
    "build.dirty-worktree",
];

pub fn invoke(request: &Value) -> Result<(Value, i32)> {
    let name = request["case"].as_str().unwrap_or_default();
    if !BUILD.contains(&name) {
        return Err(Error::new(
            "validation",
            "Native guard fixture is not implemented.",
        ));
    }
    let state = super::state(request)?;
    let root = std::env::current_dir()?;
    let head = "a".repeat(40);
    let mut result = json!({"verdict":"pass","pr_url":"https://github.com/owner/repo/pull/9",
        "head_sha":head,"summary":"Validated acceptance criteria","findings":[]});
    match name {
        "build.blocked-result" => result["verdict"] = json!("blocked"),
        "build.pass-with-findings" => result["findings"] = json!(["Unresolved"]),
        "build.invalid-sha" => result["head_sha"] = json!("short"),
        _ => (),
    }
    let mut task = json!({"number":1,"phase":"building","round":1,
        "item":{"id":"item-1","status":"In Progress","content":{"number":1,"type":"Issue","repository":"owner/repo"}},
        "question":name=="build.question-created-pr","head_sha":head,"branch":"codex/1-worklane","base":"main",
        "pr_url":"https://github.com/owner/repo/pull/9","worker_worktree":root,
        "completed_turns":{"build":result}});
    state.save(&mut task)?;
    let case = name.to_owned();
    let executor: CommandExecutor = Arc::new(move |request| {
        let args = request.argv.iter().map(String::as_str).collect::<Vec<_>>();
        let stdout = if args.first() == Some(&"git") {
            match args.get(1).copied() {
                Some("branch") => "codex/1-worklane".into(),
                Some("rev-parse") => {
                    if case == "build.wrong-head" {
                        "b".repeat(40)
                    } else {
                        "a".repeat(40)
                    }
                }
                Some("status") => {
                    if case == "build.dirty-worktree" {
                        " M tracked".into()
                    } else {
                        String::new()
                    }
                }
                _ => return Err(Error::new("validation", "Unexpected guard Git command.")),
            }
        } else if args.contains(&"graphql") {
            let body: Value = serde_json::from_str(request.stdin.as_deref().unwrap_or("{}"))?;
            let query = body["query"].as_str().unwrap_or_default();
            if query.contains("viewerCanUpdate") {
                json!({"data":{"node":{"public":false,"viewerCanUpdate":true},"repository":{"viewerPermission":"ADMIN"}}}).to_string()
            } else if query.contains("reviewThreads") {
                json!({"data":{"repository":{"pullRequest":{"reviewThreads":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}}}}}).to_string()
            } else {
                return Err(Error::new(
                    "validation",
                    "Unexpected guard GraphQL command.",
                ));
            }
        } else if args.get(1..3) == Some(&["pr", "view"]) {
            json!({"url":"https://github.com/owner/repo/pull/9","state":"OPEN","headRefName":"codex/1-worklane",
                "baseRefName":"main","headRefOid":"a".repeat(40),"isDraft":false,"isCrossRepository":false,"mergeable":"MERGEABLE"}).to_string()
        } else {
            return Err(Error::new("validation", "Unexpected guard GitHub command."));
        };
        Ok(CommandResult {
            stdout,
            stderr: String::new(),
            exit: 0,
        })
    });
    let mut board = super::selected_board(request, &state, &root)?;
    board.transport = GitHubTransport::new(&state, &root)?.with_executor(executor.clone());
    let mut agent = AgentHarness::new(&root, &state, &json!({}))?;
    agent.required_backend = Some("guard-fixture-must-not-launch".into());
    let flow =
        Workflow::new(state.clone(), board, agent, root, "main".into()).with_commands(executor);
    let lock = state.controller_lock(true)?;
    let effects = json!({"board_moves":[],"posts":0});
    Ok(match flow.build(&mut task, lock.as_raw_fd()) {
        Ok(()) => (json!({"ok":true,"effects":effects}), 0),
        Err(error) => (
            json!({"error":error.message,"type":"RuntimeError","effects":effects}),
            1,
        ),
    })
}
