use crate::{
    Error, Result,
    config::ConfigStore,
    fail,
    process::{OWNER_ENV, OwnedChild, ProcessSupervisor},
    role,
    state::StateStore,
    string,
    transport::{nonblocking, readable},
};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::File,
    io::{Read, Seek, SeekFrom, Write},
    os::{
        fd::{AsRawFd, FromRawFd, RawFd},
        unix::process::ExitStatusExt,
    },
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::{Mutex, OnceLock},
    time::{Duration, Instant},
};
pub const MODEL: &str = "gpt-6.1-sol";
pub type ResumeCommand = (Vec<String>, Option<BTreeMap<String, String>>);
pub const RESULT_SCHEMA: &str = r#"{"type":"object","additionalProperties":false,"properties":{"verdict":{"type":"string","enum":["pass","changes_requested","blocked"]},"pr_url":{"type":"string"},"head_sha":{"type":"string"},"summary":{"type":"string"},"findings":{"type":"array","items":{"type":"string"}}},"required":["verdict","pr_url","head_sha","summary","findings"]}"#;
pub fn validate_session_id(backend: &str, id: &str) -> Result<String> {
    match backend {
        "codex" => uuid::Uuid::parse_str(id)
            .map(|u| u.to_string())
            .map_err(|_| Error::new("validation", "badly formed hexadecimal UUID string")),
        "opencode"
            if regex::Regex::new(r"^ses_[A-Za-z0-9_-]+$")
                .unwrap()
                .is_match(id) =>
        {
            Ok(id.into())
        }
        _ => Err(Error::new(
            "validation",
            format!("Invalid {backend} session ID."),
        )),
    }
}
pub fn session_name(repo: &str, n: i64, role: &str, task: &Value) -> String {
    let title = task["issue"]["title"]
        .as_str()
        .filter(|s| !s.is_empty())
        .or_else(|| task["item"]["content"]["title"].as_str())
        .unwrap_or("Issue")
        .split(crate::unicode::is_space)
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
        .chars()
        .take(120)
        .collect::<String>();
    format!("{repo} #{n} [{role}]: {title}")
}
pub fn validate_result(output: &Value) -> Result<()> {
    let Some(o) = output.as_object() else {
        return fail("Missing or invalid structured result.");
    };
    let keys = ["verdict", "pr_url", "head_sha", "summary", "findings"];
    if o.len() != keys.len() || keys.iter().any(|k| !o.contains_key(*k)) {
        return fail("Missing or invalid structured result.");
    }
    if !matches!(
        output["verdict"].as_str(),
        Some("pass" | "changes_requested" | "blocked")
    ) {
        return fail("Unknown agent verdict.");
    }
    if ["pr_url", "head_sha", "summary"]
        .iter()
        .any(|k| !output[*k].is_string())
    {
        return fail("Invalid result field types.");
    }
    if !output["findings"]
        .as_array()
        .is_some_and(|a| a.iter().all(Value::is_string))
    {
        return fail("Invalid review findings.");
    }
    Ok(())
}
pub fn event_text(e: &Value) -> String {
    let i = &e["item"];
    if let Some(t) = i["text"].as_str().filter(|s| !s.is_empty()) {
        return t.into();
    }
    if let Some(c) = i["command"].as_str().filter(|s| !s.is_empty()) {
        let o = i["aggregated_output"].as_str().unwrap_or_default();
        return format!(
            "$ {c}{}",
            if o.is_empty() {
                String::new()
            } else {
                format!("\n{o}")
            }
        );
    }
    if i["type"] == "file_change" {
        return format!(
            "Files: {}",
            i["changes"]
                .as_array()
                .unwrap_or(&vec![])
                .iter()
                .map(|v| v["path"].as_str().unwrap_or("?"))
                .collect::<Vec<_>>()
                .join(", ")
        );
    }
    e["message"]
        .as_str()
        .filter(|s| !s.is_empty())
        .or_else(|| e["error"].as_str())
        .or_else(|| e["error"]["message"].as_str())
        .or_else(|| e["type"].as_str())
        .unwrap_or("unknown")
        .into()
}
pub fn readable_log(stream: &str, content: &str) -> String {
    static ANSI: OnceLock<regex::Regex> = OnceLock::new();
    static DIAGNOSTIC: OnceLock<regex::Regex> = OnceLock::new();
    let ansi = ANSI.get_or_init(|| regex::Regex::new(r"\x1b\[[0-?]*[ -/]*[@-~]").unwrap());
    let diagnostic=DIAGNOSTIC.get_or_init(||regex::Regex::new(r"^\d{4}-\d{2}-\d{2}T\S+\s+(?:WARN|ERROR|INFO|DEBUG|TRACE)\s+(?:codex_mcp|codex_rmcp_client|rmcp)::").unwrap());
    let normalized = content.replace("\r\n", "\n");
    let result = normalized
        .split_terminator([
            '\n', '\r', '\u{b}', '\u{c}', '\u{1c}', '\u{1d}', '\u{1e}', '\u{85}', '\u{2028}',
            '\u{2029}',
        ])
        .map(|l| ansi.replace_all(l, "").into_owned())
        .filter(|l| {
            !([
                "thread.started",
                "turn.started",
                "turn.completed",
                "item.started",
                "item.completed",
                "item.updated",
            ]
            .contains(&l.trim())
                || (matches!(stream, "stderr" | "controller-error")
                    && diagnostic.is_match(l.trim())))
        })
        .collect::<Vec<_>>()
        .join("\n");
    if result.trim_start().starts_with('{') {
        if let Ok(v) = serde_json::from_str::<Value>(&result) {
            let verdict = match v["verdict"].as_str() {
                Some("pass") => "Passed",
                Some("changes_requested") => "Changes requested",
                Some("blocked") => "Blocked",
                _ => "",
            };
            if !verdict.is_empty()
                && v["summary"].is_string()
                && v.as_object().is_some_and(|o| {
                    o.keys().all(|k| {
                        ["verdict", "pr_url", "head_sha", "summary", "findings"]
                            .contains(&k.as_str())
                    })
                })
                && ["pr_url", "head_sha"]
                    .iter()
                    .all(|k| v.get(*k).is_none_or(Value::is_string))
                && v.get("findings")
                    .is_none_or(|a| a.as_array().is_some_and(|a| a.iter().all(Value::is_string)))
            {
                let mut lines = vec![
                    if v["summary"] == "" {
                        "No summary provided.".into()
                    } else {
                        string(&v, "summary")
                    },
                    "".into(),
                    verdict.into(),
                ];
                for (k, p) in [("pr_url", "PR"), ("head_sha", "Revision")] {
                    if let Some(s) = v[k].as_str().filter(|s| !s.is_empty()) {
                        lines.push(format!("{p}: {s}"));
                    }
                }
                if let Some(a) = v["findings"].as_array().filter(|a| !a.is_empty()) {
                    lines.extend(["".into(), "Findings:".into()]);
                    lines.extend(a.iter().map(|v| format!("- {}", v.as_str().unwrap())));
                }
                return lines.join("\n");
            }
        }
    }
    if result.trim().is_empty() {
        String::new()
    } else {
        result
    }
}
#[derive(Default)]
pub struct OpenCodeTurn {
    text: String,
}
impl OpenCodeTurn {
    pub fn events(&mut self, event: &Value) -> Result<Vec<Value>> {
        let mut out = Vec::new();
        if let Some(id) = event["sessionID"].as_str().filter(|s| !s.is_empty()) {
            out.push(
                json!({"type":"thread.started","thread_id":validate_session_id("opencode",id)?}),
            );
        }
        let p = &event["part"];
        match event["type"].as_str().unwrap_or_default() {
            "step_start" => { self.text.clear(); out.push(json!({"type":"turn.started"})); }
            "text" => {
                let t=p["text"].as_str().ok_or_else(||Error::new("validation","Missing OpenCode text."))?;
                if self.text.len()+t.len()>4*1024*1024 {return fail("Agent result exceeded 4 MiB.");}
                self.text.push_str(t);
                out.push(json!({"type":"item.completed","item":{"type":"agent_message","text":t}}));
            }
            "tool_use" => {
                let text=p["state"]["output"].as_str().filter(|s|!s.is_empty())
                    .or_else(||p["state"]["error"].as_str()).or_else(||p["tool"].as_str()).unwrap_or("tool");
                out.push(json!({"type":"item.completed","item":{"type":"tool","text":text}}));
            }
            "step_finish" => match p["reason"].as_str() {
                Some("stop")=>out.push(json!({"type":"turn.completed"})),
                Some("tool-calls")=>{},
                reason=>out.push(json!({"type":"turn.failed","message":format!("OpenCode finish reason: {}",reason.unwrap_or("None"))})),
            },
            "error"=>out.push(json!({"type":"error","error":event["error"]})),
            _=>{},
        }
        Ok(out)
    }
    pub fn result(&self) -> Result<Value> {
        Ok(serde_json::from_str(&self.text)?)
    }
}
pub fn opencode_environment() -> Result<BTreeMap<String, String>> {
    let mut env: BTreeMap<_, _> = std::env::vars().collect();
    env.remove("PWD");
    let mut config: Value = serde_json::from_str(
        env.get("OPENCODE_CONFIG_CONTENT")
            .filter(|s| !s.is_empty())
            .map(String::as_str)
            .unwrap_or("{}"),
    )?;
    if !config.is_object() {
        return fail("OpenCode configuration must be an object.");
    }
    if config.get("permission").is_none() {
        config["permission"] = json!({});
    }
    if config["permission"].is_string() {
        config["permission"] = json!({"*":config["permission"].clone()});
    }
    if !config["permission"].is_object() {
        return fail("OpenCode permissions must be an object.");
    }
    config["permission"]["task"] = json!("deny");
    env.insert("OPENCODE_CONFIG_CONTENT".into(), config.to_string());
    Ok(env)
}
fn check_opencode_session(session: &Value, tree: &Path, owner: &str) -> Result<()> {
    let id = validate_session_id("opencode", &string(session, "session_id"))?;
    let mut cmd = Command::new("opencode");
    cmd.args(["session", "export", "--standalone", &id])
        .current_dir(tree)
        .env_clear()
        .envs(opencode_environment()?)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    let token = uuid::Uuid::new_v4().simple().to_string();
    cmd.env(OWNER_ENV, &token)
        .env(crate::process::CONTROLLER_ENV, owner);
    ProcessSupervisor::prepare(&mut cmd, &[]);
    let mut child = OwnedChild::new(cmd.spawn()?, token);
    let deadline = Instant::now() + Duration::from_secs(30);
    loop {
        if let Some(status) = child.child.try_wait()? {
            if status.success() {
                return Ok(());
            }
            return fail(
                "OpenCode cannot read the saved session; refusing to create a replacement conversation.",
            );
        }
        if Instant::now() > deadline {
            return Err(Error::new("busy", "OpenCode session export timed out."));
        }
        std::thread::sleep(Duration::from_millis(5));
    }
}

fn memory_file(name: &std::ffi::CStr) -> Result<File> {
    let fd = unsafe { libc::memfd_create(name.as_ptr(), libc::MFD_CLOEXEC) };
    if fd < 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    Ok(unsafe { File::from_raw_fd(fd) })
}

/// One bounded metadata-only connection; never invokes an agent turn.
pub struct CodexAppServer {
    child: OwnedChild,
    input: std::process::ChildStdin,
    output: std::process::ChildStdout,
    pending: Vec<u8>,
    scanned: usize,
    id: u64,
    deadline: Instant,
}
impl CodexAppServer {
    pub fn open(cwd: &Path, timeout: Duration) -> Result<Self> {
        Self::open_with_owner(cwd, timeout, "")
    }
    pub fn open_with_owner(cwd: &Path, timeout: Duration, owner: &str) -> Result<Self> {
        let mut cmd = Command::new("codex");
        let token = uuid::Uuid::new_v4().simple().to_string();
        cmd.env(OWNER_ENV, &token)
            .env(crate::process::CONTROLLER_ENV, owner);
        cmd.args(["app-server", "--listen", "stdio://"])
            .current_dir(cwd)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        ProcessSupervisor::prepare(&mut cmd, &[]);
        let mut child = OwnedChild::new(cmd.spawn()?, token);
        let input = child.child.stdin.take().unwrap();
        let output = child.child.stdout.take().unwrap();
        nonblocking(output.as_raw_fd())?;
        let mut s = Self {
            child,
            input,
            output,
            pending: Vec::new(),
            scanned: 0,
            id: 0,
            deadline: Instant::now() + timeout,
        };
        s.request("initialize",json!({"clientInfo":{"name":"worklane","version":"0.1.0"},"capabilities":{"experimentalApi":true}}))?;
        s.send(&json!({"method":"initialized","params":{}}))?;
        Ok(s)
    }
    fn send(&mut self, message: &Value) -> Result<()> {
        self.input.write_all(format!("{message}\n").as_bytes())?;
        self.input.flush()?;
        Ok(())
    }
    pub fn request(&mut self, method: &str, params: Value) -> Result<Value> {
        self.id += 1;
        self.send(&json!({"id":self.id,"method":method,"params":params}))?;
        loop {
            if let Some(offset) = self.pending[self.scanned..]
                .iter()
                .position(|b| *b == b'\n')
            {
                let n = self.scanned + offset;
                let line = self.pending.drain(..=n).collect::<Vec<_>>();
                self.scanned = 0;
                let message: Value = serde_json::from_slice(&line)?;
                if message["id"].as_u64() != Some(self.id) {
                    continue;
                }
                if let Some(error) = message.get("error") {
                    return fail(format!(
                        "Codex {method}: {}",
                        error["message"]
                            .as_str()
                            .map(str::to_owned)
                            .unwrap_or_else(|| error.to_string())
                    ));
                }
                return message
                    .get("result")
                    .cloned()
                    .ok_or_else(|| Error::new("validation", "Missing metadata result."));
            }
            self.scanned = self.pending.len();
            readable(self.output.as_raw_fd(), self.deadline).map_err(|_| {
                Error::new(
                    "busy",
                    format!("Codex metadata request timed out: {method}"),
                )
            })?;
            let mut chunk = [0; 65536];
            match self.output.read(&mut chunk) {
                Ok(0) => return fail(format!("Codex app-server closed during {method}")),
                Ok(n) => {
                    self.pending.extend_from_slice(&chunk[..n]);
                    if self.pending.len() > 4 * 1024 * 1024 {
                        return fail("Codex metadata response exceeded 4 MiB");
                    }
                }
                Err(e)
                    if matches!(
                        e.kind(),
                        std::io::ErrorKind::WouldBlock | std::io::ErrorKind::Interrupted
                    ) => {}
                Err(e) => return Err(e.into()),
            }
        }
    }
}
impl Drop for CodexAppServer {
    fn drop(&mut self) {
        let _ = self.child.terminate();
    }
}
static METADATA_LOCK: Mutex<()> = Mutex::new(());
pub struct PreparedTurn {
    pub argv: Vec<String>,
    pub prompt: String,
    pub environment: Option<BTreeMap<String, String>>,
    pub backend: String,
    pub model: String,
}
#[derive(Clone)]
pub struct AgentHarness {
    pub config: Value,
    pub root: PathBuf,
    pub state: StateStore,
    pub timeout: Duration,
    pub metadata_timeout: Duration,
    pub required_backend: Option<String>,
}
impl AgentHarness {
    pub fn new(root: &Path, state: &StateStore, config: &Value) -> Result<Self> {
        Ok(Self {
            config: ConfigStore::validate(config)?,
            root: root.into(),
            state: state.clone(),
            timeout: Duration::from_secs(8 * 3600),
            metadata_timeout: Duration::from_secs(15),
            required_backend: None,
        })
    }
    pub fn selection(&self, role: &str, session: Option<&Value>) -> (String, String) {
        if let Some(s) = session {
            let backend = s["backend"].as_str().unwrap_or("codex");
            let model = s["model"]
                .as_str()
                .filter(|s| !s.is_empty())
                .unwrap_or(if backend == "codex" { MODEL } else { "" });
            return (backend.into(), model.into());
        }
        let backend = self.config[format!("{role}_harness")]
            .as_str()
            .filter(|s| !s.is_empty())
            .unwrap_or(self.config["harness"].as_str().unwrap_or("codex"));
        let model = self.config[format!("{role}_model")]
            .as_str()
            .filter(|s| !s.is_empty())
            .unwrap_or(self.config["model"].as_str().unwrap_or_default());
        (
            backend.into(),
            if model.is_empty() && backend == "codex" {
                MODEL.into()
            } else {
                model.into()
            },
        )
    }
    pub fn metadata(&self, id: &str, session: &str, stage: &str, task: &Value) -> Result<()> {
        let result = (|| -> Result<()> {
            let _lock = METADATA_LOCK
                .lock()
                .map_err(|_| Error::new("busy", "Metadata lock poisoned."))?;
            let _shared = ProcessSupervisor::lock(&self.state.path.join("metadata.lock"), true)?;
            let root = self.root.canonicalize()?;
            let name = session_name(
                &self.state.repo,
                task["number"].as_i64().unwrap_or_default(),
                role(stage)?,
                task,
            );
            let owner = string(
                &self.state.get_meta("runtime", json!({}))?,
                "ownership_token",
            );
            let mut server = CodexAppServer::open_with_owner(&root, self.metadata_timeout, &owner)?;
            server.request("thread/name/set", json!({"threadId":session,"name":name}))?;
            let mut cursor = Value::Null;
            let mut seen = BTreeSet::new();
            let mut matches = Vec::new();
            let mut count = 0;
            loop {
                let page = server.request("project/list", json!({"cursor":cursor,"limit":100}))?;
                let data = page["data"]
                    .as_array()
                    .ok_or_else(|| Error::new("validation", "Missing Codex project list."))?;
                count += data.len();
                if count > 10000 {
                    return fail("Codex project/list exceeded 10,000 projects.");
                }
                for p in data {
                    if p["roots"].as_array().is_some_and(|a| {
                        a.iter().any(|r| {
                            r["path"].as_str().is_some_and(|p| {
                                Path::new(p).canonicalize().ok().as_ref() == Some(&root)
                            })
                        })
                    }) {
                        matches.push(p.clone());
                    }
                }
                cursor = page["nextCursor"].clone();
                if cursor.is_null() || cursor == "" {
                    break;
                }
                let c = cursor
                    .as_str()
                    .ok_or_else(|| Error::new("validation", "Invalid Codex pagination cursor."))?;
                if !seen.insert(c.to_string()) {
                    return fail("Codex project/list repeated a pagination cursor");
                }
            }
            if matches.len() > 1 {
                return fail(format!(
                    "Multiple Codex projects have root {}; inspect project membership",
                    root.display()
                ));
            }
            let project = if let Some(p) = matches.pop() {
                p
            } else {
                server.request("project/create",json!({"name":self.state.repo,"roots":[{"path":root}],"idempotencyKey":uuid::Uuid::new_v5(&uuid::Uuid::NAMESPACE_URL,format!("worklane-project:{}",root.display()).as_bytes()).to_string()}))?["project"].clone()
            };
            server.request(
                "thread/metadata/update",
                json!({"threadId":session,"projectId":project["id"]}),
            )?;
            self.state.append_log(
                Some(id),
                "session",
                &format!(
                    "Named {session}: {name}; Codex project {}",
                    string(&project, "id")
                ),
            )?;
            Ok(())
        })();
        if let Err(error) = result {
            self.state.append_log(Some(id),"warning",&format!("Codex session metadata could not be updated: {error}. Will retry on the next turn."))?;
        }
        Ok(())
    }
    pub fn resume_args(&self, session: &Value) -> Result<ResumeCommand> {
        let tree = PathBuf::from(string(session, "worktree"));
        if !tree.is_dir() {
            return fail(format!(
                "Session worktree is missing: {}. Restore it before resuming.",
                tree.display()
            ));
        }
        let backend = session["backend"].as_str().unwrap_or("codex");
        let id = validate_session_id(backend, &string(session, "session_id"))?;
        match backend {
            "codex" => Ok((
                vec![
                    "codex".into(),
                    "resume".into(),
                    id,
                    "--sandbox".into(),
                    "workspace-write".into(),
                    "--ask-for-approval".into(),
                    "on-request".into(),
                    "--disable".into(),
                    "multi_agent".into(),
                ],
                None,
            )),
            "opencode" => {
                check_opencode_session(
                    session,
                    &tree,
                    &string(
                        &self.state.get_meta("runtime", json!({}))?,
                        "ownership_token",
                    ),
                )?;
                Ok((
                    vec![
                        "opencode".into(),
                        tree.to_string_lossy().into_owned(),
                        "--session".into(),
                        id,
                    ],
                    Some(opencode_environment()?),
                ))
            }
            _ => fail("Unknown harness."),
        }
    }
    pub fn resume(&self, session: &Value) -> Result<i32> {
        crate::actions::Actions {
            state: self.state.clone(),
            root: self.root.clone(),
            agent: self.clone(),
            transport: crate::transport::GitHubTransport::new(&self.state, &self.root)?,
        }
        .resume_session(
            session["number"].as_i64().unwrap_or(0),
            session["role"].as_str().unwrap_or("worker"),
        )
    }
    pub fn resume_locked(&self, session: &Value, lock: &crate::process::LockGuard) -> Result<i32> {
        let (args, env) = self.resume_args(session)?;
        let mut cmd = Command::new(&args[0]);
        cmd.args(&args[1..])
            .current_dir(string(session, "worktree"));
        if let Some(env) = env {
            cmd.env_clear().envs(env);
        }
        let fd = lock.as_raw_fd();
        unsafe {
            use std::os::unix::process::CommandExt;
            cmd.pre_exec(move || {
                let flags = libc::fcntl(fd, libc::F_GETFD);
                if flags < 0 || libc::fcntl(fd, libc::F_SETFD, flags & !libc::FD_CLOEXEC) < 0 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        let status = cmd.status()?;
        Ok(status
            .code()
            .unwrap_or_else(|| -status.signal().unwrap_or(1)))
    }
    fn stopped(&self) -> Result<bool> {
        Ok(self.state.get_meta("shutdown", json!({}))?["immediate"]
            .as_bool()
            .unwrap_or(false))
    }
    #[allow(clippy::too_many_arguments)]
    pub fn prepare(
        &self,
        stage: &str,
        task: &Value,
        tree: &Path,
        session: Option<&Value>,
        mut turn_prompt: String,
        result_schema: &Value,
        schema: &mut File,
        result: &mut File,
    ) -> Result<PreparedTurn> {
        let role = role(stage)?;
        let number = task["number"]
            .as_i64()
            .ok_or_else(|| Error::new("validation", "Missing task number."))?;
        let (backend, model) = self.selection(role, session);
        if self
            .required_backend
            .as_ref()
            .is_some_and(|b| b != &backend)
        {
            return fail("Saved conversation belongs to another harness; refusing to replace it.");
        }
        if session.is_some_and(|s| s["backend"].as_str().unwrap_or("codex") != backend) {
            return fail("Saved conversation belongs to another harness; refusing to replace it.");
        }
        schema.set_len(0)?;
        schema.rewind()?;
        result.set_len(0)?;
        result.rewind()?;
        let mut args = Vec::new();
        let mut env = None;
        if backend == "codex" {
            schema.write_all(python_ascii_json(result_schema).as_bytes())?;
            schema.rewind()?;
            args.extend(["codex", "exec", "--approve-for-me", "--cd"].map(str::to_owned));
            args.push(tree.to_string_lossy().into());
            args.extend([
                "--config".into(),
                format!(
                    "model_reasoning_effort=\"{}\"",
                    string(&self.config, "effort")
                ),
                "--config".into(),
                "features.multi_agent=false".into(),
            ]);
            if stage == "finalize" {
                args.extend(["--add-dir".into(), self.root.to_string_lossy().into()]);
            }
            if session.is_some() {
                args.push("resume".into());
            }
            args.extend([
                "--model".into(),
                model.clone(),
                "--json".into(),
                "--output-schema".into(),
                format!("/proc/self/fd/{}", schema.as_raw_fd()),
                "--output-last-message".into(),
                format!("/proc/self/fd/{}", result.as_raw_fd()),
            ]);
            if let Some(s) = &session {
                args.push(validate_session_id("codex", &string(s, "session_id"))?);
            }
            args.push("-".into());
        } else if backend == "opencode" {
            if let Some(s) = &session {
                check_opencode_session(
                    s,
                    tree,
                    &string(
                        &self.state.get_meta("runtime", json!({}))?,
                        "ownership_token",
                    ),
                )?;
            }
            args.extend(
                [
                    "opencode",
                    "run",
                    "--standalone",
                    "--format",
                    "json",
                    "--auto",
                    "--agent",
                    "build",
                    "--title",
                ]
                .map(str::to_owned),
            );
            args.push(session_name(&self.state.repo, number, role, task));
            if !model.is_empty() {
                args.extend(["--model".into(), model.clone()]);
            }
            if let Some(s) = &session {
                args.extend(["--session".into(), string(s, "session_id")]);
            }
            env = Some(opencode_environment()?);
            turn_prompt.push_str(
                "\nReturn only one JSON object matching this schema, without Markdown fences:\n",
            );
            turn_prompt.push_str(&python_ascii_json(result_schema));
        } else {
            return fail("Unknown harness.");
        }

        Ok(PreparedTurn {
            argv: args,
            prompt: turn_prompt,
            environment: env,
            backend,
            model,
        })
    }
    pub fn run(
        &self,
        stage: &str,
        task: &mut Value,
        tree: &Path,
        extra: &str,
        runner_fd: RawFd,
    ) -> Result<Value> {
        if self.stopped()? {
            return Err(Error::new(
                "interrupted",
                "Immediate stop requested before the next agent turn.",
            ));
        }
        let role = role(stage)?;
        let number = task["number"]
            .as_i64()
            .ok_or_else(|| Error::new("validation", "Missing task number."))?;
        let mut session = self.state.session(number, role)?;
        let (backend, model) = self.selection(role, session.as_ref());
        if self
            .required_backend
            .as_ref()
            .is_some_and(|b| b != &backend)
        {
            return fail("Saved conversation belongs to another harness; refusing to replace it.");
        }
        if session
            .as_ref()
            .is_some_and(|s| s["backend"].as_str().unwrap_or("codex") != backend)
        {
            return fail("Saved conversation belongs to another harness; refusing to replace it.");
        }
        let result_key = match role {
            "worker" => "build_result",
            "reviewer" => "review_result",
            _ => "handoff_result",
        };
        if session.is_none()
            && (self
                .state
                .runs(Some(number))?
                .iter()
                .any(|r| r["role"] == role)
                || task.get(result_key).is_some_and(|v| !v.is_null())
                || task["legacy_session_roles"]
                    .as_array()
                    .is_some_and(|a| a.iter().any(|r| r == role)))
        {
            return fail(format!(
                "No saved {role} session ID; inspect worklane logs. Refusing to start a replacement conversation."
            ));
        }
        if let Some(s) = &session {
            if !tree.is_dir() {
                return fail(format!(
                    "Session worktree is missing: {}. Restore it before resuming.",
                    tree.display()
                ));
            }
            let saved = string(s, "worktree");
            if !saved.is_empty() && Path::new(&saved).canonicalize()? != tree.canonicalize()? {
                return fail(format!(
                    "The {role} session belongs to another worktree; refusing to resume in a different directory."
                ));
            }
        }
        let mut prompt = include_str!("../prompts/common_prompt.txt").to_owned()
            + match stage {
                "build" => include_str!("../prompts/build_prompt.txt"),
                "review" => include_str!("../prompts/review_prompt.txt"),
                _ => include_str!("../prompts/finalize_prompt.txt"),
            };
        if session.is_some() {
            prompt.push_str("\nContinue this existing conversation. Inspect the current files, PR and feedback before acting; a previous turn may have been interrupted. Do not discard existing work or duplicate completed external actions.\n");
        }
        prompt.push_str(&format!(
            "\nPrimary checkout: {}\nAssigned task (JSON):\n{}\n{extra}",
            self.root.display(),
            python_ascii_json(task)
        ));
        let mut schema = memory_file(c"worklane-schema")?;
        let mut result = memory_file(c"worklane-result")?;
        let id = self.state.begin_run(task, stage, tree)?;
        let outcome = (|| -> Result<Value> {
            let mut inputs = self.state.advance_run(&id, None)?;
            loop {
                if self.stopped()? {
                    return Err(Error::new(
                        "interrupted",
                        "Immediate stop requested before the next agent turn.",
                    ));
                }
                let mut turn_prompt = prompt.clone();
                if !inputs.is_empty() {
                    turn_prompt.push_str(&format!("\nAdditional user requests from the Worklane TUI (JSON):\n{}\nHandle these within the assigned task and existing safeguards. Inspect prior actions before retrying an interrupted request; return the required structured result.\n",python_json(&json!(inputs))));
                }
                let prepared = self.prepare(
                    stage,
                    task,
                    tree,
                    session.as_ref(),
                    turn_prompt,
                    &serde_json::from_str(RESULT_SCHEMA)?,
                    &mut schema,
                    &mut result,
                )?;
                let output = self.invoke(
                    &prepared.argv,
                    &prepared.prompt,
                    stage,
                    task,
                    tree,
                    &schema,
                    &mut result,
                    prepared.environment,
                    &id,
                    &backend,
                    &model,
                    runner_fd,
                )?;
                inputs = self.state.advance_run(&id, Some(&output))?;
                if inputs.is_empty() {
                    return Ok(output);
                }
                session = self.state.session(number, role)?;
            }
        })();
        if let Err(e) = &outcome {
            self.state.finish_run(
                &id,
                if e.kind == "interrupted" {
                    "interrupted"
                } else {
                    "failed"
                },
                Some(&e.to_string()),
                None,
            )?;
        }
        outcome
    }
    #[allow(clippy::too_many_arguments)]
    fn invoke(
        &self,
        args: &[String],
        prompt: &str,
        stage: &str,
        task: &mut Value,
        tree: &Path,
        schema: &File,
        result: &mut File,
        env: Option<BTreeMap<String, String>>,
        id: &str,
        backend: &str,
        model: &str,
        runner_fd: RawFd,
    ) -> Result<Value> {
        let mut command = Command::new(&args[0]);
        command
            .args(&args[1..])
            .current_dir(tree)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        if let Some(env) = env {
            command.env_clear().envs(env);
        }
        let token = self.state.deps.ids.next().replace('-', "");
        command.env(OWNER_ENV, &token);
        command.env(
            crate::process::CONTROLLER_ENV,
            string(
                &self.state.get_meta("runtime", json!({}))?,
                "ownership_token",
            ),
        );
        ProcessSupervisor::prepare(
            &mut command,
            &[runner_fd, schema.as_raw_fd(), result.as_raw_fd()],
        );
        let mut child = {
            let _lock = self.state.launch_lock()?;
            if self.stopped()? {
                return Err(Error::new(
                    "interrupted",
                    "Immediate stop requested before agent launch.",
                ));
            }
            let child = OwnedChild::new(command.spawn()?, token.clone());
            task["ownership_token"] = json!(token);
            self.state.attach_process(id, task, &child.root)?;
            child
        };
        let mut input = child.child.stdin.take();
        let mut stdout = child.child.stdout.take().unwrap();
        let mut stderr = child.child.stderr.take().unwrap();
        for fd in [
            input.as_ref().unwrap().as_raw_fd(),
            stdout.as_raw_fd(),
            stderr.as_raw_fd(),
        ] {
            nonblocking(fd)?;
        }
        let mut buffers = [Vec::new(), Vec::new()];
        let mut closed = [false, false];
        let mut sent = 0;
        let deadline = Instant::now() + self.timeout;
        let mut decoder = OpenCodeTurn::default();
        let mut observed: Option<String> = None;
        let mut completed = false;
        let mut failed = false;
        let mut received = |stream: usize, line: &[u8]| -> Result<()> {
            if stream == 1 {
                self.state
                    .append_log(Some(id), "stderr", &String::from_utf8_lossy(line))?;
                return Ok(());
            }
            let event: Value = match serde_json::from_slice(line) {
                Ok(v) => v,
                Err(_) => {
                    self.state
                        .append_log(Some(id), "stdout", &String::from_utf8_lossy(line))?;
                    return Ok(());
                }
            };
            if !event.is_object() {
                return fail(format!("{backend} emitted an unexpected event shape."));
            }
            let events = if backend == "opencode" {
                decoder.events(&event)?
            } else {
                vec![event.clone()]
            };
            self.state.record_events(
                id,
                &events,
                model,
                backend,
                if backend == "opencode" {
                    Some(&event)
                } else {
                    None
                },
            )?;
            for e in &events {
                match e["type"].as_str().unwrap_or_default() {
                    "thread.started" => {
                        let value = validate_session_id(backend, &string(e, "thread_id"))?;
                        if observed.as_ref().is_some_and(|s| s != &value) {
                            return fail("Agent emitted conflicting session IDs.");
                        }
                        if observed.is_none() && backend == "codex" {
                            self.metadata(id, &value, stage, task)?;
                        }
                        observed = Some(value);
                    }
                    "turn.started" => completed = false,
                    "turn.completed" => completed = true,
                    "turn.failed" | "error" => failed = true,
                    _ => (),
                }
            }
            Ok(())
        };
        while !closed.iter().all(|b| *b) {
            if self.stopped()? {
                return Err(Error::new(
                    "interrupted",
                    "Immediate stop requested; saved session retained.",
                ));
            }
            if Instant::now() > deadline {
                return Err(Error::new(
                    "interrupted",
                    format!("{backend} timed out; saved session can continue. Run ID: {id}."),
                ));
            }
            if let Some(pipe) = &mut input {
                match pipe.write(&prompt.as_bytes()[sent..]) {
                    Ok(n) => sent += n,
                    Err(e)
                        if matches!(
                            e.kind(),
                            std::io::ErrorKind::WouldBlock | std::io::ErrorKind::Interrupted
                        ) => {}
                    Err(e) if e.kind() == std::io::ErrorKind::BrokenPipe => sent = prompt.len(),
                    Err(e) => return Err(e.into()),
                }
                if sent == prompt.len() {
                    input = None;
                }
            }
            for (i, pipe) in [
                (0, &mut stdout as &mut dyn Read),
                (1, &mut stderr as &mut dyn Read),
            ] {
                if closed[i] {
                    continue;
                }
                let mut chunk = [0; 65536];
                match pipe.read(&mut chunk) {
                    Ok(0) => {
                        closed[i] = true;
                        if !buffers[i].iter().all(u8::is_ascii_whitespace) {
                            received(i, &buffers[i])?;
                        }
                        buffers[i].clear();
                    }
                    Ok(n) => {
                        buffers[i].extend_from_slice(&chunk[..n]);
                        while let Some(n) = buffers[i].iter().position(|b| *b == b'\n') {
                            let line = buffers[i].drain(..=n).collect::<Vec<_>>();
                            received(i, &line[..line.len() - 1])?;
                        }
                        if buffers[i].len() > 4 * 1024 * 1024 {
                            return fail("Agent event exceeded 4 MiB.");
                        }
                    }
                    Err(e)
                        if matches!(
                            e.kind(),
                            std::io::ErrorKind::WouldBlock | std::io::ErrorKind::Interrupted
                        ) => {}
                    Err(e) => return Err(e.into()),
                }
            }
            if !closed.iter().all(|b| *b) {
                std::thread::sleep(Duration::from_millis(5));
            }
        }
        let status = loop {
            if let Some(s) = child.child.try_wait()? {
                break s;
            }
            if Instant::now() > deadline {
                return Err(Error::new(
                    "interrupted",
                    format!("{backend} timed out while exiting; resume the saved session."),
                ));
            }
            if self.stopped()? {
                return Err(Error::new(
                    "interrupted",
                    "Immediate stop requested; saved session retained.",
                ));
            }
            std::thread::sleep(Duration::from_millis(5));
        };
        if let Some(signal) = status.signal() {
            return Err(Error::new(
                "interrupted",
                format!("{backend} interrupted by signal {signal}; session retained."),
            ));
        }
        if !status.success() {
            return fail(format!(
                "{} exited {}; see worklane logs {} (run {id}).",
                if backend == "codex" {
                    "Codex"
                } else {
                    "Opencode"
                },
                status.code().unwrap_or(1),
                task["number"]
            ));
        }
        if observed.is_none() || !completed || failed {
            return fail(format!(
                "{backend} did not report a successful turn with a session ID; see worklane logs {}.",
                task["number"]
            ));
        }
        let output = if backend == "opencode" {
            decoder.result()?
        } else {
            result.seek(SeekFrom::Start(0))?;
            if result.metadata()?.len() > 4 * 1024 * 1024 {
                return fail("Agent result exceeded 4 MiB.");
            }
            serde_json::from_reader(result)?
        };
        validate_result(&output)?;
        Ok(output)
    }
}
/// Python-compatible JSON separators for prompts and test protocol (not durable storage).
pub fn python_json(value: &Value) -> String {
    fn encode(v: &Value, out: &mut String) {
        match v {
            Value::Object(o) => {
                out.push('{');
                for (i, (k, v)) in o.iter().enumerate() {
                    if i > 0 {
                        out.push_str(", ");
                    }
                    out.push_str(&serde_json::to_string(k).unwrap());
                    out.push_str(": ");
                    encode(v, out);
                }
                out.push('}');
            }
            Value::Array(a) => {
                out.push('[');
                for (i, v) in a.iter().enumerate() {
                    if i > 0 {
                        out.push_str(", ");
                    }
                    encode(v, out);
                }
                out.push(']');
            }
            _ => out.push_str(&v.to_string()),
        }
    }
    let mut out = String::new();
    encode(value, &mut out);
    out
}

pub fn python_ascii_json(value: &Value) -> String {
    let mut out = String::new();
    for c in python_json(value).chars() {
        if c.is_ascii() {
            out.push(c);
        } else {
            let mut units = [0; 2];
            for u in c.encode_utf16(&mut units) {
                out.push_str(&format!("\\u{u:04x}"));
            }
        }
    }
    out
}
