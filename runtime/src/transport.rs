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
    let token = uuid::Uuid::new_v4().simple().to_string();
    command.env(crate::process::OWNER_ENV, &token);
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
    let request = CommandRequest {
        argv: argv.iter().map(|s| s.to_string()).collect(),
        cwd: cwd.into(),
        stdin: stdin.map(str::to_owned),
        timeout: Duration::from_secs(120),
        environment: BTreeMap::new(),
    };
    let r = execute(&request)?;
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
}
impl GitHubTransport {
    pub fn new(state: &StateStore, cwd: &Path) -> Result<Self> {
        Ok(Self {
            cwd: cwd.into(),
            executable: "gh".into(),
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
    fn timestamp(&self) -> Result<f64> {
        let value = chrono::DateTime::parse_from_rfc3339(&self.budget.deps.clock.now())
            .map_err(|e| Error::new("validation", e.to_string()))?;
        Ok(value.timestamp_micros() as f64 / 1_000_000.)
    }
    pub fn check(&self) -> Result<()> {
        let s = self.budget.get_meta("github_backoff", json!({}))?;
        if self.timestamp()? < s["until"].as_f64().unwrap_or(0.) {
            return Err(Error::new(
                "quota",
                s["error"].as_str().unwrap_or("GitHub cooldown active."),
            ));
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
    pub fn call(&self, request: &CommandRequest) -> Result<CommandResult> {
        let _l = ProcessSupervisor::lock(&self.budget.path.join("budget.lock"), true)?;
        self.check()?;
        let response = execute(request)?;
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
                "gh",
                "api",
                "-i",
                "graphql",
                "-f",
                "query=query { viewer { login } }",
            ]
            .map(str::to_owned)
            .into();
            probe.stdin = None;
            let p = execute(&probe)?;
            let d = format!("{}\n{}", p.stdout, p.stderr);
            if let Some(r) = rate_limit(&d, self.timestamp()?) {
                self.record(&d, r)?;
                return Err(Error::new("quota", d));
            }
            if limited.is_none() && p.exit != 0 {
                return Err(Error::new("github", format!("gh failed: {}", d.trim())));
            }
        }
        if response
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
            self.record(rate_detail, retry)?;
            return Err(Error::new("quota", rate_detail));
        }
        if response.exit != 0 {
            return Err(Error::new("github", format!("gh failed: {detail}")));
        }
        Ok(response)
    }
    pub fn text(&self, args: &[&str], stdin: Option<&str>) -> Result<String> {
        let mut argv = vec![self.executable.to_string_lossy().into_owned()];
        argv.extend(args.iter().map(|s| s.to_string()));
        Ok(self
            .call(&CommandRequest {
                argv,
                cwd: self.cwd.clone(),
                stdin: stdin.map(str::to_owned),
                timeout: Duration::from_secs(120),
                environment: BTreeMap::from([(
                    crate::process::CONTROLLER_ENV.into(),
                    self.controller_token.clone(),
                )]),
            })?
            .stdout
            .trim()
            .into())
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
fn rate_limit(s: &str, now: f64) -> Option<f64> {
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
        retry = if let Ok(n) = v.parse::<f64>() {
            now + n
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
