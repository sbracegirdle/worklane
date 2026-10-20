use crate::unicode::CaseFold;
use crate::{
    Result,
    board::{Board, Invocation, READY},
    controller::Controller,
    fail,
    harness::AgentHarness,
    process::ProcessSupervisor,
    state::StateStore,
    string,
    transport::GitHubTransport,
};
use serde_json::{Value, json};
use std::path::{Path, PathBuf};
#[derive(Clone)]
pub struct Actions {
    pub state: StateStore,
    pub root: PathBuf,
    pub agent: AgentHarness,
    pub transport: GitHubTransport,
    pub invocation: Invocation,
}
pub struct IssueDraft {
    pub board: Board,
    pub number: Option<i64>,
    pub item: Option<Value>,
    pub original: Value,
    pub title: String,
    pub body: String,
    pub status: String,
    pub statuses: Vec<String>,
}
impl Actions {
    pub fn security_board(&self) -> Result<Board> {
        self.security_board_at(&self.root)
    }
    fn security_board_at(&self, checkout: &Path) -> Result<Board> {
        let runtime = self.state.get_meta("runtime", json!({}))?;
        let config = crate::config::ConfigStore::resolve(checkout, &json!({}))?;
        // Project selection may persist; dangerous consent must come from this invocation.
        let project = self
            .invocation
            .project
            .as_deref()
            .filter(|s| !s.is_empty())
            .or_else(|| runtime["project"].as_str().filter(|s| !s.is_empty()))
            .or_else(|| config["project"].as_str().filter(|s| !s.is_empty()));
        Board::open_with_policy(
            &self.state.repo,
            self.transport.clone(),
            project,
            false,
            &[],
            self.invocation.clone(),
            false,
        )
    }
    pub fn pause(&self, value: bool) -> Result<()> {
        if !value {
            self.security_board()?;
        }
        if !value
            && self.state.running()?
            && self.state.get_meta("shutdown", json!({}))?["requested"] == true
        {
            return fail("Controller is stopping; wait for stopped status, then start.");
        }
        self.state.pause(value)
    }
    pub fn stop(&self, immediate: bool) -> Result<Value> {
        Controller::stop(&self.state, immediate)
    }
    pub fn start(&self, argv: &[String]) -> Result<i32> {
        let observed = self.state.get_meta("shutdown", json!({}))?;
        self.start_observed(argv, &observed)
    }
    /// Keep the invocation's original shutdown snapshot across every startup preflight.
    pub fn start_observed(&self, argv: &[String], observed: &Value) -> Result<i32> {
        self.security_board()?;
        let mut argv = argv
            .iter()
            .filter(|arg| arg.as_str() != "--dangerously-allow-public-board")
            .cloned()
            .collect::<Vec<_>>();
        if self.invocation.dangerously_allow_public_board {
            argv.push("--dangerously-allow-public-board".into());
        }
        Controller::start(&self.state, &argv, &self.root, observed)
    }
    pub fn retry(&self, n: i64) -> Result<()> {
        self.security_board()?;
        let _lock = self.state.controller_lock(false)?;
        let mut task = self.state.load(n)?;
        if task.as_object().is_none_or(|o| o.is_empty()) {
            return fail("No saved task for that issue.");
        }
        if !matches!(
            task["phase"].as_str(),
            Some("claiming" | "building" | "reviewing" | "finalizing" | "blocked" | "interrupted")
        ) {
            return fail("Only blocked/interrupted tasks need retry.");
        }
        let blocked = task["phase"] == "blocked";
        task["phase"] = json!("retry");
        if task.get("resume_stage").is_none() {
            task["resume_stage"] = json!(task["active_stage"].as_str().unwrap_or(
                match task["failed_phase"].as_str() {
                    Some("reviewing") => "review",
                    Some("finalizing") => "finalize",
                    _ => "build",
                }
            ));
        }
        let stage = string(&task, "resume_stage");
        task.as_object_mut().unwrap().remove("error");
        if blocked {
            if task["blocker"] == "board_status_drift" && task["build_result"]["verdict"] == "pass"
            {
                if task.get("completed_turns").is_none() {
                    task["completed_turns"] = json!({});
                }
                task["completed_turns"]["build"] = task["build_result"].clone();
            } else if let Some(o) = task["completed_turns"].as_object_mut() {
                o.remove(&stage);
            }
            task.as_object_mut().unwrap().remove("blocker");
        }
        self.state.save(&mut task)
    }
    pub fn queue_input(&self, id: &str, text: &str) -> Result<()> {
        self.security_board()?;
        self.state.queue_input(id, text)
    }
    pub fn resume_session(&self, n: i64, role: &str) -> Result<i32> {
        let lock = self.state.controller_lock(false)?;
        let session = self.state.session(n, role)?.ok_or_else(|| {
            crate::Error::new(
                "not_found",
                "No saved session yet. Enter follows live logs as soon as they arrive.",
            )
        })?;
        if !Path::new(&string(&session, "worktree")).is_dir() {
            return fail("Saved session worktree is missing; inspect it before resuming.");
        }
        for run in self.state.runs(None)? {
            if let (Some(pid), Some(birth)) = (run["pid"].as_i64(), run["identity"].as_str()) {
                if ProcessSupervisor::identity(pid as i32).as_deref() == Some(birth) {
                    return fail(
                        "An agent process is still alive. Stop Worklane and wait before resuming.",
                    );
                }
            }
        }
        self.security_board_at(Path::new(&string(&session, "worktree")))?;
        let result = self.agent.resume_locked(&session, &lock)?;
        if result != 0 {
            return fail(format!(
                "{} resume exited {result}; saved session retained.",
                session["backend"].as_str().unwrap_or("codex")
            ));
        }
        Ok(result)
    }
    pub fn open_pr(&self, task: &Value) -> Result<()> {
        let (pr, error) = crate::read_model::ReadModel::new(self.state.clone())
            .pr_status(&self.transport, task)?;
        let url = string(&pr, "url");
        if url.is_empty() {
            return Err(crate::Error::new(
                if error.is_empty() {
                    "not_found"
                } else {
                    "github"
                },
                if error.is_empty() {
                    "No PR for the selected issue.".into()
                } else {
                    error
                },
            ));
        }
        self.transport.browse_pr(&self.state.repo, &url)
    }
    pub fn load_editor(&self, project: Option<&str>, number: Option<i64>) -> Result<IssueDraft> {
        let board = Board::open_with_policy(
            &self.state.repo,
            self.transport.clone(),
            project,
            true,
            &[],
            self.invocation.clone(),
            false,
        )?;
        let mut statuses = board.field["options"]
            .as_array()
            .unwrap()
            .iter()
            .map(|o| string(o, "name"))
            .collect::<Vec<_>>();
        if statuses.is_empty() {
            return fail("The board has no Status options.");
        }
        let item = if let Some(n) = number {
            Some(
                board
                    .items()?
                    .into_iter()
                    .find(|i| i["content"]["number"] == n)
                    .ok_or_else(|| {
                        crate::Error::new(
                            "not_found",
                            "This open issue is no longer on the selected board.",
                        )
                    })?,
            )
        } else {
            None
        };
        let original = item
            .as_ref()
            .map(|i| i["content"].clone())
            .unwrap_or(json!({"title":"","body":""}));
        let status = item
            .as_ref()
            .map(|i| string(i, "status"))
            .unwrap_or_else(|| {
                statuses
                    .iter()
                    .find(|s| s.casefold() == READY.casefold())
                    .unwrap_or(&statuses[0])
                    .clone()
            });
        if item.is_some() && status.is_empty() {
            statuses.insert(0, String::new());
        }
        Ok(IssueDraft {
            board,
            number,
            item,
            title: string(&original, "title"),
            body: string(&original, "body"),
            original,
            status,
            statuses,
        })
    }
    pub fn save_editor(&self, editor: &mut IssueDraft) -> Result<String> {
        editor.board.require_safe()?;
        let title = editor.title.trim();
        if title.is_empty() {
            return fail("Title is required.");
        }
        let Some(item) = &editor.item else {
            return editor.board.add_issue(title, &editor.body, &editor.status);
        };
        let n = editor.number.unwrap().to_string();
        let current = self.transport.json(&[
            "issue",
            "view",
            &n,
            "--repo",
            &self.state.repo,
            "--json",
            "title,body,state,url",
        ])?;
        if current["state"] != "OPEN"
            || ["title", "body"]
                .iter()
                .any(|k| current[*k] != editor.original[*k])
        {
            return fail("Issue changed on GitHub; cancel and reopen the editor before saving.");
        }
        if editor.board.item_status(item)?.as_deref() != Some(&string(item, "status").casefold()) {
            return fail("Board status changed; cancel and reopen the editor before saving.");
        }
        let changed = current["title"] != title || string(&current, "body") != editor.body;
        if changed {
            self.transport.text(
                &[
                    "issue",
                    "edit",
                    &n,
                    "--repo",
                    &self.state.repo,
                    "--title",
                    title,
                    "--body-file",
                    "-",
                ],
                Some(&editor.body),
            )?;
            editor.original["title"] = json!(title);
            editor.original["body"] = json!(editor.body);
        }
        if editor.status != string(item, "status") {
            if let Err(e) = editor
                .board
                .move_item(item, &editor.status, &string(item, "status"))
            {
                return fail(if changed {
                    format!("Issue text saved; board status was not confirmed: {e}")
                } else {
                    e.to_string()
                });
            }
        }
        Ok(string(&current, "url"))
    }
}
