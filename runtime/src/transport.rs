use crate::unicode::CaseFold;
use crate::{
    Error, Result, fail,
    process::{OwnedChild, ProcessSupervisor},
    state::StateStore,
};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    io::{Read, Write},
    os::{
        fd::{AsRawFd, RawFd},
        unix::process::ExitStatusExt,
    },
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::Arc,
    time::{Duration, Instant},
};
#[derive(Clone, Debug)]
pub struct CommandRequest {
    pub argv: Vec<String>,
    pub cwd: PathBuf,
    pub stdin: Option<String>,
    pub timeout: Duration,
    pub environment: BTreeMap<String, String>,
}
#[derive(Clone, Debug)]
pub struct CommandResult {
    pub stdout: String,
    pub stderr: String,
    pub exit: i32,
}
/// A command boundary shared by real subprocesses and deterministic acceptance drivers.
pub type CommandExecutor = Arc<dyn Fn(&CommandRequest) -> Result<CommandResult> + Send + Sync>;
pub fn nonblocking(fd: RawFd) -> Result<()> {
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    Ok(())
}
pub fn readable(fd: RawFd, deadline: Instant) -> Result<()> {
    loop {
        let left = deadline.saturating_duration_since(Instant::now());
        if left.is_zero() {
            return Err(Error::new("busy", "Subprocess timed out."));
        }
        let mut p = libc::pollfd {
            fd,
            events: libc::POLLIN,
            revents: 0,
        };
        let n = unsafe { libc::poll(&mut p, 1, left.as_millis().clamp(1, 1000) as i32) };
        if n > 0 {
            return Ok(());
        }
        if n < 0 && std::io::Error::last_os_error().kind() != std::io::ErrorKind::Interrupted {
            return Err(std::io::Error::last_os_error().into());
        }
    }
}
/// Bounded capture with simultaneous stdin/stdout/stderr servicing, deadline and descendant cleanup.
pub fn execute(request: &CommandRequest) -> Result<CommandResult> {
    capture(request, true)
}
fn execute_browser(request: &CommandRequest) -> Result<CommandResult> {
    capture(request, false)
}
fn capture(request: &CommandRequest, managed: bool) -> Result<CommandResult> {
    let argv = &request.argv;
    let exe = argv
        .first()
        .ok_or_else(|| Error::new("validation", "Empty command."))?;
    let mut command = Command::new(exe);
    command
        .args(&argv[1..])
        .current_dir(&request.cwd)
        .envs(&request.environment)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    ProcessSupervisor::prepare(&mut command, &[]);
    let token = if managed {
        uuid::Uuid::new_v4().simple().to_string()
    } else {
        String::new()
    };
    if managed {
        command.env(crate::process::OWNER_ENV, &token);
    } else {
        command
            .env_remove(crate::process::OWNER_ENV)
            .env_remove(crate::process::CONTROLLER_ENV);
    }
    let mut child = OwnedChild::new(command.spawn()?, token);
    let mut input = child.child.stdin.take();
    let mut out = child.child.stdout.take().unwrap();
    let mut err = child.child.stderr.take().unwrap();
    for fd in [
        input.as_ref().unwrap().as_raw_fd(),
        out.as_raw_fd(),
        err.as_raw_fd(),
    ] {
        nonblocking(fd)?;
    }
    let bytes = request.stdin.as_deref().unwrap_or("").as_bytes();
    let mut sent = 0;
    let mut stdout = Vec::new();
    let mut stderr = Vec::new();
    let mut closed = [false, false];
    let deadline = Instant::now() + request.timeout;
    while !closed.iter().all(|v| *v) || input.is_some() {
        if Instant::now() > deadline {
            return Err(Error::new("busy", format!("{exe} timed out.")));
        }
        if let Some(pipe) = &mut input {
            if sent < bytes.len() {
                match pipe.write(&bytes[sent..]) {
                    Ok(n) => sent += n,
                    Err(e)
                        if matches!(
                            e.kind(),
                            std::io::ErrorKind::WouldBlock | std::io::ErrorKind::Interrupted
                        ) => {}
                    Err(e) if e.kind() == std::io::ErrorKind::BrokenPipe => sent = bytes.len(),
                    Err(e) => return Err(e.into()),
                }
            }
            if sent == bytes.len() {
                input = None;
            }
        }
        for (i, pipe, dst) in [
            (0, &mut out as &mut dyn Read, &mut stdout),
            (1, &mut err as &mut dyn Read, &mut stderr),
        ] {
            if closed[i] {
                continue;
            }
            let mut buf = [0; 65536];
            match pipe.read(&mut buf) {
                Ok(0) => closed[i] = true,
                Ok(n) => {
                    if dst.len() + n > 16 * 1024 * 1024 {
                        return fail("Subprocess output exceeded 16 MiB.");
                    }
                    dst.extend_from_slice(&buf[..n]);
                }
                Err(e)
                    if matches!(
                        e.kind(),
                        std::io::ErrorKind::WouldBlock | std::io::ErrorKind::Interrupted
                    ) => {}
                Err(e) => return Err(e.into()),
            }
        }
        if !closed.iter().all(|v| *v) {
            std::thread::sleep(Duration::from_millis(5));
        }
    }
    let status = loop {
        if let Some(s) = child.child.try_wait()? {
            break s;
        }
        if Instant::now() > deadline {
            return fail(format!("{exe} timed out while exiting."));
        }
        std::thread::sleep(Duration::from_millis(5));
    };
    Ok(CommandResult {
        stdout: String::from_utf8_lossy(&stdout).into_owned(),
        stderr: String::from_utf8_lossy(&stderr).into_owned(),
        exit: status
            .code()
            .unwrap_or_else(|| -status.signal().unwrap_or(1)),
    })
}
pub fn command(argv: &[&str], cwd: &Path, stdin: Option<&str>) -> Result<String> {
    command_with(&execute, argv, cwd, stdin)
}
pub fn command_with(
    executor: &dyn Fn(&CommandRequest) -> Result<CommandResult>,
    argv: &[&str],
    cwd: &Path,
    stdin: Option<&str>,
) -> Result<String> {
    let request = CommandRequest {
        argv: argv.iter().map(|s| s.to_string()).collect(),
        cwd: cwd.into(),
        stdin: stdin.map(str::to_owned),
        timeout: Duration::from_secs(120),
        environment: BTreeMap::new(),
    };
    let r = executor(&request)?;
    if r.exit != 0 {
        return Err(Error::new(
            "io",
            format!(
                "{} failed: {}",
                argv[0],
                [r.stdout.trim(), r.stderr.trim()]
                    .into_iter()
                    .filter(|s| !s.is_empty())
                    .collect::<Vec<_>>()
                    .join("\n")
            ),
        ));
    }
    Ok(r.stdout.trim().into())
}
#[derive(Clone)]
pub struct Repository {
    pub repo: String,
    pub root: PathBuf,
    pub default_branch: Option<String>,
}
pub struct RepositoryLocator;
impl RepositoryLocator {
    pub fn project(
        repository: &Repository,
        transport: GitHubTransport,
        explicit: Option<&str>,
        required_statuses: &[&str],
        load_fields: bool,
    ) -> Result<crate::board::Board> {
        crate::board::Board::open(
            &repository.repo,
            transport,
            explicit,
            load_fields,
            required_statuses,
        )
    }
    pub fn locate(path: &Path) -> Result<Repository> {
        let root = PathBuf::from(command(
            &["git", "rev-parse", "--show-toplevel"],
            path,
            None,
        )?);
        let remote = command(&["git", "remote", "get-url", "origin"], &root, None)?;
        let re=regex::Regex::new(r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+/[^/]+?)(?:\.git)?/?$").unwrap();
        let repo = re.captures(&remote).ok_or_else(|| {
            Error::new(
                "validation",
                "origin must point to a github.com repository.",
            )
        })?[1]
            .to_string();
        let default_branch = command(
            &["git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
            &root,
            None,
        )
        .ok()
        .map(|s| s.trim_start_matches("refs/remotes/origin/").into());
        Ok(Repository {
            repo,
            root,
            default_branch,
        })
    }
}
pub fn next_cursor(page: &Value, seen: &mut BTreeSet<String>) -> Result<Option<String>> {
    match page["hasNextPage"].as_bool() {
        Some(false) => Ok(None),
        Some(true) => {
            let c = page["endCursor"]
                .as_str()
                .filter(|s| !s.is_empty())
                .ok_or_else(|| {
                    Error::new("github", "GitHub returned incomplete or cyclic pagination.")
                })?;
            if !seen.insert(c.into()) {
                return fail("GitHub returned incomplete or cyclic pagination.");
            }
            Ok(Some(c.into()))
        }
        _ => fail("GitHub returned incomplete or cyclic pagination."),
    }
}
#[derive(Clone)]
pub struct GitHubTransport {
    pub cwd: PathBuf,
    pub executable: PathBuf,
    pub budget: StateStore,
    controller_token: String,
    executor: CommandExecutor,
    browser_executor: CommandExecutor,
}
impl GitHubTransport {
    /// Browser and issue commands share the cooldown without creating repository runtime state.
    pub fn standalone(cwd: &Path) -> Result<Self> {
        let budget = StateStore::open("_github", None)?;
        Ok(Self {
            cwd: cwd.into(),
            executable: "gh".into(),
            budget,
            controller_token: String::new(),
            executor: Arc::new(execute),
            browser_executor: Arc::new(execute_browser),
        })
    }

    pub fn browse_board(&self, owner: &str, number: i64) -> Result<()> {
        command_with(
            &*self.browser_executor,
            &[
                "gh",
                "project",
                "view",
                &number.to_string(),
                "--owner",
                owner,
                "--web",
            ],
            &self.cwd,
            None,
        )?;
        Ok(())
    }

    pub fn new(state: &StateStore, cwd: &Path) -> Result<Self> {
        Ok(Self {
            cwd: cwd.into(),
            executable: "gh".into(),
            executor: Arc::new(execute),
            browser_executor: Arc::new(execute_browser),
            controller_token: crate::string(
                &state.get_meta("runtime", json!({}))?,
                "ownership_token",
            ),
            budget: StateStore::with_dependencies(
                "_github",
                state.path.parent().and_then(Path::parent),
                state.deps.clone(),
            )?,
        })
    }
    pub fn with_executor(mut self, executor: CommandExecutor) -> Self {
        self.executor = executor.clone();
        self.browser_executor = executor;
        self
    }
    fn timestamp(&self) -> Result<f64> {
        let value = chrono::DateTime::parse_from_rfc3339(&self.budget.deps.clock.now())
            .map_err(|e| Error::new("validation", e.to_string()))?;
        Ok(value.timestamp_micros() as f64 / 1_000_000.)
    }
    pub fn check(&self) -> Result<()> {
        let s = self.budget.get_meta("github_backoff", json!({}))?;
        if self.timestamp()? < s["until"].as_f64().unwrap_or(0.) {
            let mut error = Error::quota(
                s["error"].as_str().unwrap_or("GitHub cooldown active."),
                s["until"].as_f64().unwrap_or(0.),
            );
            error.deferred = true;
            return Err(error);
        }
        Ok(())
    }
    pub fn snapshot(&self) -> Result<Value> {
        self.budget.get_meta("github_backoff", json!({}))
    }
    pub fn clear(&self, previous: &Value) -> Result<()> {
        let _l = ProcessSupervisor::lock(&self.budget.path.join("budget.lock"), true)?;
        if self.snapshot()? == *previous {
            self.budget.set_meta("github_backoff", &json!({}))?;
        }
        Ok(())
    }
    fn record(&self, message: &str, retry: f64) -> Result<()> {
        let old = self.snapshot()?;
        let delay = (old["delay"].as_f64().unwrap_or(30.) * 2.).clamp(60., 3600.);
        self.budget.set_meta("github_backoff",&json!({"delay":delay as i64,"until":old["until"].as_f64().unwrap_or(0.).max(self.timestamp()?+delay).max(retry),"error":message,"generation":self.budget.deps.ids.next().replace('-',"")}))
    }
    /// Persist each quota failure once, including failures raised by injected transports.
    pub fn record_quota(&self, error: &mut Error) -> Result<()> {
        if error.kind != "quota" || error.deferred {
            return Ok(());
        }
        let _lock = ProcessSupervisor::lock(&self.budget.path.join("budget.lock"), true)?;
        self.record_error(error)
    }
    fn record_error(&self, error: &mut Error) -> Result<()> {
        if error.kind == "quota" && !error.deferred {
            self.record(&error.message, error.retry_at.unwrap_or(0.))?;
            error.deferred = true;
        }
        Ok(())
    }
    pub fn call(&self, request: &CommandRequest) -> Result<CommandResult> {
        let _l = ProcessSupervisor::lock(&self.budget.path.join("budget.lock"), true)?;
        self.check()?;
        let mut result = self.raw_call(request, true);
        if let Err(error) = &mut result {
            self.record_error(error)?;
        }
        result
    }
    fn raw_call(&self, request: &CommandRequest, budgeted: bool) -> Result<CommandResult> {
        let executor = if budgeted {
            &self.executor
        } else {
            &self.browser_executor
        };
        let response = executor(request)?;
        let detail = [response.stdout.trim(), response.stderr.trim()]
            .into_iter()
            .filter(|s| !s.is_empty())
            .collect::<Vec<_>>()
            .join("\n");
        let now = self.timestamp()?;
        let graphql_errors = request.argv.iter().any(|s| s == "graphql")
            && graphql_body(&response.stdout)
                .ok()
                .is_some_and(|v| has_errors(&v));
        let rate_detail = if response.exit == 0 {
            response.stdout.trim()
        } else {
            &detail
        };
        let limited = if response.exit != 0 || graphql_errors {
            rate_limit(rate_detail, now)
        } else {
            None
        };
        if response.exit != 0
            && !request.argv.iter().any(|s| s == "graphql")
            && (limited == Some(0.) || detail.casefold().contains("unknown owner type"))
        {
            let mut probe = request.clone();
            probe.argv = [
                request.argv[0].as_str(),
                "api",
                "-i",
                "graphql",
                "-f",
                "query=query { viewer { login } }",
            ]
            .map(str::to_owned)
            .into();
            probe.stdin = None;
            match executor(&probe) {
                Ok(p) => {
                    let d = [p.stdout.trim(), p.stderr.trim()]
                        .into_iter()
                        .filter(|s| !s.is_empty())
                        .collect::<Vec<_>>()
                        .join("\n");
                    let failed =
                        p.exit != 0 || graphql_body(&p.stdout).ok().is_some_and(|v| has_errors(&v));
                    let probe_rate = if failed {
                        rate_limit(&d, self.timestamp()?)
                    } else {
                        None
                    };
                    if let Some(r) = probe_rate {
                        return Err(Error::quota(d, r));
                    }
                    if limited.is_none() && p.exit != 0 {
                        return Err(Error::new("github", format!("gh failed: {d}")));
                    }
                }
                Err(error) if limited.is_none() => return Err(error),
                Err(_) => {} // Preserve the authoritative quota failure when its probe fails.
            }
        }
        if budgeted
            && response
                .stdout
                .casefold()
                .lines()
                .any(|s| s.trim() == "x-ratelimit-remaining: 0")
            && response.exit == 0
            && !graphql_errors
        {
            let normalized = response.stdout.replace("\r\n", "\n");
            let headers = normalized
                .split_once("\n\n")
                .map(|(h, _)| h)
                .unwrap_or(&normalized);
            let message = format!("API rate limit exhausted\n{headers}");
            self.record(
                &message,
                rate_limit(&message, self.timestamp()?).unwrap_or(0.),
            )?;
        }
        if let Some(retry) = limited {
            return Err(Error::quota(rate_detail, retry));
        }
        if response.exit != 0 {
            return Err(Error::new("github", format!("gh failed: {detail}")));
        }
        Ok(response)
    }
    pub fn text(&self, args: &[&str], stdin: Option<&str>) -> Result<String> {
        Ok(self.call(&self.request(args, stdin))?.stdout.trim().into())
    }
    /// A user browser action remains available from cached details during API cooldown.
    pub fn browse_pr(&self, repo: &str, url: &str) -> Result<()> {
        self.raw_call(
            &self.request(&["pr", "view", url, "--repo", repo, "--web"], None),
            false,
        )?;
        Ok(())
    }
    fn request(&self, args: &[&str], stdin: Option<&str>) -> CommandRequest {
        let mut argv = vec![self.executable.to_string_lossy().into_owned()];
        argv.extend(args.iter().map(|s| s.to_string()));
        CommandRequest {
            argv,
            cwd: self.cwd.clone(),
            stdin: stdin.map(str::to_owned),
            timeout: Duration::from_secs(120),
            environment: BTreeMap::from([(
                crate::process::CONTROLLER_ENV.into(),
                self.controller_token.clone(),
            )]),
        }
    }
    pub fn json(&self, args: &[&str]) -> Result<Value> {
        Ok(serde_json::from_str(&self.text(args, None)?)?)
    }
    pub fn graphql(&self, query: &str, variables: &Value) -> Result<Value> {
        let s = self.text(
            &["api", "-i", "graphql", "--input", "-"],
            Some(&crate::harness::python_ascii_json(
                &json!({"query":query,"variables":variables}),
            )),
        )?;
        let v = graphql_body(&s)?;
        if has_errors(&v) {
            return Err(Error::new("github", v["errors"].to_string()));
        }
        v.get("data")
            .cloned()
            .ok_or_else(|| Error::new("github", "Missing GraphQL data."))
    }
    pub fn pages(&self, query: &str, mut variables: Value, path: &[&str]) -> Result<Vec<Value>> {
        let mut out = Vec::new();
        let mut seen = BTreeSet::new();
        loop {
            let v = self.graphql(query, &variables)?;
            let mut c = &v;
            for key in path {
                c = c
                    .get(*key)
                    .ok_or_else(|| Error::new("github", "Missing connection."))?;
            }
            let a = c["nodes"]
                .as_array()
                .ok_or_else(|| Error::new("github", "Missing connection nodes."))?;
            out.extend(a.iter().cloned());
            if out.len() > 10000 {
                return fail("GitHub connection exceeds 10,000 items.");
            }
            let next = next_cursor(&c["pageInfo"], &mut seen)?;
            if let Some(next) = next {
                variables["cursor"] = json!(next);
            } else {
                if c["totalCount"]
                    .as_u64()
                    .is_some_and(|n| n != out.len() as u64)
                {
                    return fail("GitHub returned incomplete pagination.");
                }
                return Ok(out);
            }
        }
    }
}
fn has_errors(value: &Value) -> bool {
    value.get("errors").is_some_and(|e| match e {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
        Value::String(s) => !s.is_empty(),
        Value::Number(n) => n.as_f64() != Some(0.),
    })
}
fn graphql_body(s: &str) -> Result<Value> {
    let normalized = s.replace("\r\n", "\n");
    Ok(serde_json::from_str(
        normalized
            .split_once("\n\n")
            .map(|(_, b)| b)
            .unwrap_or(&normalized),
    )?)
}
pub fn rate_limit(s: &str, now: f64) -> Option<f64> {
    let lower = s.casefold();
    if !["rate limit", "rate_limit", "secondary rate"]
        .iter()
        .any(|t| lower.contains(t))
    {
        return None;
    }
    let mut headers = BTreeMap::new();
    for line in lower.lines() {
        if let Some((k, v)) = line.split_once(':') {
            headers.insert(k.trim(), v.trim());
        }
    }
    let mut retry = 0.;
    if let Some(v) = headers.get("retry-after") {
        retry = if let Some(digits) = crate::unicode::decimal_text(v) {
            now + digits.parse::<f64>().unwrap_or(0.)
        } else {
            chrono::DateTime::parse_from_rfc2822(v)
                .map(|d| d.timestamp() as f64)
                .unwrap_or(0.)
        };
    }
    if headers.get("x-ratelimit-remaining") == Some(&"0") {
        retry = retry.max(
            headers
                .get("x-ratelimit-reset")
                .and_then(|s| s.parse::<f64>().ok())
                .unwrap_or(0.)
                + 1.,
        );
    }
    Some(retry)
}
