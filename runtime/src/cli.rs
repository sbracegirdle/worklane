//! Native command input/output; domain mutations stay in the shared runtime.
use crate::{
    Error, Result,
    actions::Actions,
    board::{Board, HUMAN, Invocation, PROGRESS, READY, REVIEW},
    config::ConfigStore,
    controller::Controller,
    harness::AgentHarness,
    read_model::ReadModel,
    state::StateStore,
    string,
    transport::{GitHubTransport, RepositoryLocator, command},
    workflow::Workflow,
};
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    io::{self, BufRead, Write},
    path::{Path, PathBuf},
    time::Duration,
};

pub const HELP: &str =
    "Worklane: coding workers and one independent reviewer for a GitHub project board.
Usage: worklane [COMMAND] [NUMBER] [OPTIONS]
Commands: run start check board status logs config pause resume stop retry add stack tui
Default: tui (native dashboard).
  run / start          Foreground / detached controller
  check                Read-only board, dependency and stack readiness
  board                Open project board with gh
  status / stack       Offline saved state / pinned ancestry JSON
  logs [NUMBER]        Complete readable history (--raw includes protocol events)
  config               Offline settings walkthrough (EOF or Esc cancels)
  config --show        Print effective settings without saving
  config --save        Atomically save effective settings, including explicit overrides
  pause / resume       Pause / resume intake; resume does not launch a controller
  stop [--now]         Drain / interrupt owned processes; retain sessions and worktrees
  retry NUMBER         Enable exact saved-session retry after inspection
  add -t TITLE [-d BODY] [--draft | --shaping]
Options:
  --repo PATH          Checkout to manage (default .)
  --project OWNER/N    Explicit GitHub board; otherwise discover automatically
  --harness NAME       codex or opencode
  --model MODEL        Default model (empty inherits provider default)
  --worker-harness NAME / --worker-model MODEL
  --reviewer-harness NAME / --reviewer-model MODEL
  --finalizer-harness NAME / --finalizer-model MODEL
  --workers N          1..16 (default 3), plus one independent reviewer
  --effort LEVEL       low, medium, high, xhigh (default high)
  --interval N         Poll seconds, at least 10 (default 60)
  --once               Dispatch one batch, wait, then exit
  --title / -t TITLE   Issue title for add
  --description / --body / -d BODY   Literal issue body
  --dangerously-allow-public-board  Invocation-only public-board consent
  --refresh SECONDS / --no-color    Dashboard presentation options
  -h, --help / --version
CLI overrides > checkout .worklane.toml > defaults. GitHub and git authentication use gh.
";
const COMMANDS: &[&str] = &[
    "run", "start", "check", "board", "status", "logs", "config", "pause", "resume", "stop",
    "retry", "add", "stack", "tui",
];
#[derive(Default, Debug)]
pub struct Options {
    pub action: String,
    pub number: Option<i64>,
    pub repo: PathBuf,
    pub overrides: Value,
    pub public: bool,
    pub once: bool,
    pub now: bool,
    pub raw: bool,
    pub background: bool,
    pub generation: Option<String>,
    pub title: Option<String>,
    pub body: Option<String>,
    pub draft: bool,
    pub shaping: bool,
    pub show: bool,
    pub save: bool,
    pub help: bool,
    pub version: bool,
    pub refresh: Option<Duration>,
    pub no_color: bool,
}
fn usage(message: impl Into<String>) -> Error {
    Error::new("usage", message)
}
impl Options {
    pub fn parse(args: Vec<String>) -> Result<Self> {
        let mut options = Self {
            action: "tui".into(),
            repo: ".".into(),
            overrides: json!({}),
            ..Self::default()
        };
        let mut positional = Vec::new();
        let mut args = args.into_iter();
        let mut literal = false;
        while let Some(arg) = args.next() {
            if literal {
                positional.push(arg);
                continue;
            }
            if arg == "--" {
                literal = true;
                continue;
            }
            let (flag, inline) = arg
                .split_once('=')
                .map_or((arg.as_str(), None), |(k, v)| (k, Some(v.to_owned())));
            let boolean = matches!(
                flag,
                "-h" | "--help"
                    | "--version"
                    | "--dangerously-allow-public-board"
                    | "--once"
                    | "--now"
                    | "--raw"
                    | "--background"
                    | "--draft"
                    | "--shaping"
                    | "--show"
                    | "--save"
                    | "--no-color"
            );
            if boolean && inline.is_some() {
                return Err(usage(format!("{flag} does not take a value.")));
            }
            match flag {
                "-h" | "--help" => options.help = true,
                "--version" => options.version = true,
                "--dangerously-allow-public-board" => options.public = true,
                "--once" => options.once = true,
                "--now" => options.now = true,
                "--raw" => options.raw = true,
                "--background" => options.background = true,
                "--draft" => options.draft = true,
                "--shaping" => options.shaping = true,
                "--show" => options.show = true,
                "--save" => options.save = true,
                "--no-color" => options.no_color = true,
                _ if flag.starts_with('-') => {
                    let key = flag.trim_start_matches('-').replace('-', "_");
                    let known = ConfigStore::defaults().get(&key).is_some()
                        || matches!(
                            flag,
                            "--repo"
                                | "--generation"
                                | "-t"
                                | "--title"
                                | "-d"
                                | "--description"
                                | "--body"
                                | "--refresh"
                        );
                    if !known {
                        return Err(usage(format!("unrecognized arguments: {arg}")));
                    }
                    let value = inline
                        .or_else(|| args.next())
                        .ok_or_else(|| usage(format!("{flag} requires a value.")))?;
                    if (key == "harness" && !matches!(value.as_str(), "codex" | "opencode"))
                        || (key.ends_with("_harness")
                            && !matches!(value.as_str(), "" | "codex" | "opencode"))
                        || (key == "effort"
                            && !matches!(value.as_str(), "low" | "medium" | "high" | "xhigh"))
                    {
                        return Err(usage(format!("{flag}: invalid choice: {value}")));
                    }
                    match flag {
                        "--repo" => options.repo = expand_home(&value),
                        "--generation" => options.generation = Some(value),
                        "--title" | "-t" => options.title = Some(value),
                        "--description" | "--body" | "-d" => options.body = Some(value),
                        "--refresh" => {
                            let n = value.parse::<f64>().map_err(|_| {
                                Error::new("validation", "--refresh must be 0.1 to 60 seconds.")
                            })?;
                            if !n.is_finite() || !(0.1..=60.).contains(&n) {
                                return Err(Error::new(
                                    "validation",
                                    "--refresh must be 0.1 to 60 seconds.",
                                ));
                            }
                            options.refresh = Some(Duration::from_secs_f64(n));
                        }
                        _ => {
                            options.overrides[&key] =
                                if matches!(key.as_str(), "workers" | "interval") {
                                    json!(value.parse::<i64>().map_err(|_| usage(format!(
                                        "{flag}: invalid int value: {value}"
                                    )))?)
                                } else if matches!(
                                    key.as_str(),
                                    "stack_dependencies" | "completion_gates"
                                ) {
                                    serde_json::from_str(&value).map_err(|_| {
                                        usage(format!("{flag} requires a JSON list."))
                                    })?
                                } else {
                                    json!(value)
                                };
                        }
                    }
                }
                _ => positional.push(arg),
            }
        }
        if let Some(action) = positional.first() {
            if !COMMANDS.contains(&action.as_str()) {
                return Err(usage(format!("Command: invalid choice: {action}")));
            }
            options.action = action.clone();
        }
        if positional.len() > 2 {
            return Err(usage("Too many positional arguments."));
        }
        if let Some(number) = positional.get(1) {
            options.number = Some(ConfigStore::issue_number(number).map_err(|_| {
                usage("Issue number: invalid int value; expected a positive integer.")
            })?);
        }
        if options.number.is_some_and(|n| n <= 0) {
            return Err(usage(
                "Issue number: invalid int value; expected a positive integer.",
            ));
        }
        if options.help || options.version {
            return Ok(options);
        }
        if options.now && options.action != "stop" {
            return Err(Error::new("validation", "--now is only valid with stop."));
        }
        if options.action != "add"
            && (options.title.is_some()
                || options.body.is_some()
                || options.draft
                || options.shaping)
        {
            return Err(Error::new(
                "validation",
                "--title, --description/--body, --draft and --shaping are only valid with add.",
            ));
        }
        if options.draft && options.shaping {
            return Err(usage("--draft is not allowed with --shaping."));
        }
        if options.action == "add"
            && (options.title.as_ref().is_none_or(|s| s.trim().is_empty())
                || options.number.is_some())
        {
            return Err(Error::new(
                "validation",
                "Use worklane add --title TITLE [--description DESCRIPTION] [--draft | --shaping].",
            ));
        }
        if options.number.is_some() && !matches!(options.action.as_str(), "logs" | "retry") {
            return Err(usage("NUMBER is only valid with logs or retry."));
        }
        if options.action == "retry" && options.number.is_none() {
            return Err(Error::new(
                "validation",
                "Use worklane retry ISSUE_NUMBER after fixing the blocker.",
            ));
        }
        if (options.show || options.save) && options.action != "config"
            || options.show && options.save
        {
            return Err(usage("Use config with either --show or --save."));
        }
        if (options.background || options.generation.is_some()) && options.action != "run" {
            return Err(usage("Internal controller options require run."));
        }
        Ok(options)
    }
}
fn expand_home(value: &str) -> PathBuf {
    if value == "~" || value.starts_with("~/") {
        PathBuf::from(std::env::var_os("HOME").unwrap_or_default())
            .join(value.strip_prefix("~/").unwrap_or(""))
    } else {
        value.into()
    }
}
pub fn installation_info() -> Value {
    let nullable = |s: &str| if s.is_empty() { Value::Null } else { json!(s) };
    json!({"version":env!("CARGO_PKG_VERSION"),"executable":std::env::current_exe().ok(),
        "source":env!("WORKLANE_SOURCE"),"revision":nullable(env!("WORKLANE_REVISION")),
        "branch":nullable(env!("WORKLANE_BRANCH")),"dirty":env!("WORKLANE_DIRTY").parse::<bool>().ok(),"runtime":"native"})
}
fn print_json(value: &Value) -> Result<()> {
    let mut out = io::stdout().lock();
    serde_json::to_writer_pretty(&mut out, value)?;
    writeln!(out)?;
    Ok(())
}
fn config(options: &Options, root: &Path, mut values: Value) -> Result<()> {
    if options.show {
        return print_json(&values);
    }
    if !options.save {
        println!("WORKLANE SETUP — Enter keeps a value; use TOML values; Esc or EOF cancels.");
        let stdin = io::stdin();
        let mut input = stdin.lock();
        for key in ConfigStore::defaults().as_object().unwrap().keys() {
            loop {
                print!("{key} [{}]: ", values[key]);
                io::stdout().flush()?;
                let mut line = String::new();
                if input.read_line(&mut line)? == 0 || line.contains('\x1b') {
                    println!("Setup cancelled; configuration unchanged.");
                    return Ok(());
                }
                let line = line.trim();
                if line.is_empty() {
                    break;
                }
                let candidate =
                    if values[key].is_string() && !line.starts_with('"') && !line.starts_with('\'')
                    {
                        json!(line)
                    } else {
                        match toml::from_str::<toml::Value>(&format!("value = {line}")) {
                            Ok(v) => serde_json::to_value(&v["value"])?,
                            Err(e) => {
                                println!("Invalid value: {e}");
                                continue;
                            }
                        }
                    };
                let mut updated = values.clone();
                updated[key] = candidate;
                match ConfigStore::validate(&updated) {
                    Ok(v) => {
                        values = v;
                        break;
                    }
                    Err(e) => println!("{e}"),
                }
            }
        }
        print!("Save .worklane.toml? [y/N]: ");
        io::stdout().flush()?;
        let mut answer = String::new();
        input.read_line(&mut answer)?;
        if !matches!(answer.trim(), "y" | "Y" | "yes") {
            println!("Setup cancelled; configuration unchanged.");
            return Ok(());
        }
    }
    ConfigStore::save(root, &values)?;
    println!("Saved {}", root.join(".worklane.toml").display());
    Ok(())
}
fn actions(
    state: &StateStore,
    root: &Path,
    config: &Value,
    invocation: Invocation,
) -> Result<Actions> {
    Ok(Actions {
        state: state.clone(),
        root: root.into(),
        agent: AgentHarness::new(root, state, config)?,
        transport: GitHubTransport::new(state, root)?,
        invocation,
    })
}
fn harnesses(agent: &AgentHarness) -> Result<BTreeSet<String>> {
    let mut names = BTreeSet::new();
    for role in ["worker", "reviewer", "finalizer"] {
        names.insert(agent.selection(role, None).0);
    }
    for task in agent.state.all()? {
        if matches!(task["phase"].as_str(), Some("done" | "completed")) {
            continue;
        }
        if let Some(sessions) = task["sessions"].as_object() {
            for session in sessions.values() {
                names.insert(session["backend"].as_str().unwrap_or("codex").to_owned());
            }
        }
    }
    Ok(names)
}
fn required(binary: &str) -> Result<()> {
    let exists = std::env::var_os("PATH").is_some_and(|path| {
        std::env::split_paths(&path).any(|p| {
            use std::os::unix::fs::PermissionsExt;
            std::fs::metadata(p.join(binary))
                .is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
        })
    });
    if exists {
        Ok(())
    } else {
        Err(Error::new(
            "not_found",
            format!("Required command missing: {binary}"),
        ))
    }
}
fn run(options: &Options) -> Result<i32> {
    if options.help {
        print!("{HELP}");
        return Ok(0);
    }
    if options.version {
        print_json(&installation_info())?;
        return Ok(0);
    }
    if options.action == "tui" {
        let root = std::fs::canonicalize(&options.repo)?;
        let mut dashboard = crate::tui::Options::new(root.clone());
        dashboard.invocation = Invocation {
            project: options.overrides["project"]
                .as_str()
                .filter(|s| !s.is_empty())
                .map(str::to_owned),
            dangerously_allow_public_board: options.public,
        };
        dashboard.overrides = options.overrides.clone();
        dashboard.no_color = options.no_color || std::env::var_os("NO_COLOR").is_some();
        if let Some(refresh) = options.refresh {
            dashboard.refresh = refresh;
        }
        dashboard.controller = vec![
            std::env::current_exe()?.to_string_lossy().into_owned(),
            "run".into(),
            "--repo".into(),
            root.to_string_lossy().into_owned(),
            "--background".into(),
        ];
        for (key, value) in options.overrides.as_object().unwrap() {
            dashboard.controller.extend([
                format!("--{}", key.replace('_', "-")),
                value
                    .as_str()
                    .map(str::to_owned)
                    .unwrap_or_else(|| value.to_string()),
            ]);
        }
        if options.once {
            dashboard.controller.push("--once".into());
        }
        crate::tui::run(dashboard)?;
        return Ok(0);
    }
    let repository = RepositoryLocator::locate(&options.repo)?;
    let root = &repository.root;
    let action = options.action.as_str();
    // Offline emergency controls must not parse broken settings or touch GitHub.
    let settings = if matches!(action, "status" | "logs" | "pause" | "stop") {
        ConfigStore::defaults()
    } else {
        ConfigStore::resolve(root, &options.overrides)?
    };
    if action == "config" {
        config(options, root, settings)?;
        return Ok(0);
    }
    let invocation = Invocation {
        project: settings["project"]
            .as_str()
            .filter(|s| !s.is_empty())
            .map(str::to_owned),
        dangerously_allow_public_board: options.public,
    };
    if matches!(action, "board" | "add") {
        required("gh")?;
        let transport = GitHubTransport::standalone(root)?;
        let status = if options.draft {
            "Draft"
        } else if options.shaping {
            "Shaping"
        } else {
            READY
        };
        let board = Board::open_with_policy(
            &repository.repo,
            transport,
            invocation.project.as_deref(),
            action == "add",
            if action == "add" {
                std::slice::from_ref(&status)
            } else {
                &[]
            },
            invocation.clone(),
            false,
        )?;
        if action == "board" {
            board.transport.browse_board(&board.owner, board.number)?;
            println!("Opened project board: {}", string(&board.project, "url"));
        } else {
            let url = board.add_issue(
                options.title.as_deref().unwrap(),
                options.body.as_deref().unwrap_or(""),
                status,
            )?;
            println!(
                "{url}\nAdded to {} with initial status {status}.",
                string(&board.project, "url")
            );
        }
        return Ok(0);
    }
    let state = StateStore::open(&repository.repo, None)?;
    if action == "status" {
        print_json(
            &json!({"repository":repository.repo,"running":state.running()?,"paused":state.paused()?,
            "dependency_blocks":state.dependency_blocks()?,
            "shutdown":state.get_meta("shutdown",json!({}))?,"stop_result":state.get_meta("stop_result",json!({}))?,
            "runtime":state.get_meta("runtime",json!({}))?,"tasks":state.all()?,"runs":state.runs(None)?,
            "database":state.database,"state_dir":state.path}),
        )?;
        return Ok(0);
    }
    if action == "logs" {
        ReadModel::new(state).logs(options.number, options.raw, |row| {
            writeln!(
                io::stdout().lock(),
                "{} [{}] {}",
                string(row, "created_at"),
                string(row, "stream"),
                string(row, "content")
            )?;
            Ok(())
        })?;
        return Ok(0);
    }
    if action == "stack" {
        let mut tasks = state.all()?;
        tasks.sort_by_key(|t| {
            (
                t["stack"].as_array().map_or(0, Vec::len),
                t["number"].as_i64().unwrap_or(0),
            )
        });
        let rows = tasks
            .iter()
            .map(|t| {
                let mut row = json!({});
                for key in [
                    "number",
                    "phase",
                    "pr_url",
                    "branch",
                    "base",
                    "base_sha",
                    "head_sha",
                    "stack",
                    "stack_blocked",
                ] {
                    row[key] = t[key].clone();
                }
                row
            })
            .collect::<Vec<_>>();
        print_json(
            &json!({"policy":{"stack_dependencies":settings["stack_dependencies"],"completion_gates":settings["completion_gates"]},"stack":rows}),
        )?;
        return Ok(0);
    }
    match action {
        "pause" => {
            state.pause(true)?;
            println!("Paused: active jobs finish; no new jobs start.");
            return Ok(0);
        }
        "resume" => {
            actions(&state, root, &settings, invocation.clone())?.pause(false)?;
            println!("Resumed intake. Run worklane start if the controller is stopped.");
            return Ok(0);
        }
        "stop" => {
            Controller::stop(&state, options.now)?;
            println!(
                "{}",
                if state.get_meta("shutdown", json!({}))?["immediate"] == true {
                    "Stopped: no owned processes remain; saved sessions and worktrees retained."
                } else {
                    "Stop requested: intake paused; active jobs are draining. Use stop --now to interrupt; status shows progress."
                }
            );
            return Ok(0);
        }
        "retry" => {
            actions(&state, root, &settings, invocation.clone())?.retry(options.number.unwrap())?;
            println!(
                "Retry enabled using the saved session. Start Worklane; existing In Progress/Agent review assignments can continue without moving tickets."
            );
            return Ok(0);
        }
        _ => {}
    }
    // Acquire before network/authentication, so stop and duplicate-start protection work during startup.
    let observed = state.get_meta("shutdown", json!({}))?;
    let mut runtime = settings.clone();
    runtime["cli"] = installation_info();
    runtime["once"] = json!(options.once);
    runtime["dangerously_allow_public_board"] = json!(options.public);
    let controller = if action == "run" {
        Some(Controller::acquire(
            &state,
            root,
            &runtime,
            &observed,
            options.generation.as_deref(),
        )?)
    } else {
        None
    };
    let mut workflow_to_run = None;
    let mut execute = || -> Result<i32> {
        required("gh")?;
        let agent = AgentHarness::new(root, &state, &settings)?;
        let harnesses = harnesses(&agent)?;
        for name in &harnesses {
            required(name)?;
        }
        let transport = GitHubTransport::new(&state, root)?;
        let base = match &repository.default_branch {
            Some(b) => b.clone(),
            None => transport.json(&[
                "repo",
                "view",
                &repository.repo,
                "--json",
                "defaultBranchRef",
            ])?["defaultBranchRef"]["name"]
                .as_str()
                .filter(|s| !s.is_empty())
                .ok_or_else(|| Error::new("github", "Missing default branch."))?
                .to_owned(),
        };
        let mut board = Board::open_with_policy(
            &repository.repo,
            transport,
            invocation.project.as_deref(),
            true,
            &[READY, PROGRESS, HUMAN],
            invocation.clone(),
            action == "check",
        )?;
        if action == "check" {
            if board.security["safe"] != true {
                print_json(
                    &json!({"repository":repository.repo,"board":board.project["url"],"security":board.security}),
                )?;
                return Ok(1);
            }
            let mut items = board.items()?;
            let mut workflow =
                Workflow::new(state.clone(), board.clone(), agent, root.into(), base);
            workflow.stack_items = items.clone();
            let mut ready = Vec::new();
            let mut stack_ready = json!({});
            let mut reasons = json!({});
            let mut blocked = json!({});
            let mut review = Vec::new();
            for item in &mut items {
                let number = item["content"]["number"]
                    .as_i64()
                    .ok_or_else(|| Error::new("github", "Missing issue number."))?;
                if string(item, "status").eq_ignore_ascii_case(READY) {
                    if workflow.dependencies_ready(item, None)? {
                        ready.push(number);
                        if item.get("stack_plan").is_some() {
                            stack_ready[number.to_string()] = item["stack_plan"].clone();
                        }
                    } else if let Some(reason) = item.get("dependency_reason") {
                        reasons[number.to_string()] = reason.clone();
                    }
                }
                if string(item, "status").eq_ignore_ascii_case(REVIEW)
                    && item["blocked_by_count"] == 0
                {
                    review.push(number);
                }
                if item["blocked_by_count"].as_i64().unwrap_or(0) > 0 {
                    blocked[number.to_string()] = item["blocked_by_count"].clone();
                }
            }
            print_json(
                &json!({"repository":repository.repo,"board":board.project["url"],"security":board.security,
                "cli":installation_info(),"settings":settings,"harness":settings["harness"],"model":settings["model"],"workers":settings["workers"],"effort":settings["effort"],"reviewers":1,
                "will_add_agent_review":!board.options.keys().any(|k|k.eq_ignore_ascii_case(REVIEW)),"ready":ready,"stack_ready":stack_ready,"dependency_reasons":reasons,"dependency_blocked":blocked,"agent_review":review}),
            )?;
            return Ok(0);
        }
        if action == "start" {
            let mut argv = vec![
                std::env::current_exe()?.to_string_lossy().into_owned(),
                "run".into(),
                "--repo".into(),
                root.to_string_lossy().into_owned(),
                "--background".into(),
            ];
            let mut effective = settings.clone();
            effective["project"] = json!(format!("{}/{}", board.owner, board.number));
            // Serialize every resolved setting, preserving precedence even if the file changes during launch.
            for (key, value) in effective.as_object().unwrap() {
                argv.push(format!("--{}", key.replace('_', "-")));
                argv.push(
                    value
                        .as_str()
                        .map(str::to_owned)
                        .unwrap_or_else(|| value.to_string()),
                );
            }
            if options.once {
                argv.push("--once".into());
            }
            let pid = actions(&state, root, &settings, invocation.clone())?
                .start_observed(&argv, &observed)?;
            println!(
                "Started {} (PID {pid}); use worklane logs to inspect SQLite logs.",
                repository.repo
            );
            return Ok(0);
        }
        board
            .transport
            .text(&["auth", "setup-git", "--hostname", "github.com"], None)?;
        board.ensure_review_status()?;
        for name in harnesses {
            if name == "codex" {
                command(&["codex", "login", "status"], root, None)?;
            } else {
                command(&["opencode", "auth", "list"], root, None)?;
            }
        }
        if controller.as_ref().unwrap().stop.requested()
            || state.get_meta("shutdown", json!({}))?["requested"] == true
        {
            return Ok(0);
        }
        let message = format!(
            "Worklane: {}; {} workers, 1 reviewer; {} {}; board {}",
            repository.repo,
            settings["workers"],
            string(&settings, "harness"),
            string(&settings, "model"),
            string(&board.project, "url")
        );
        state.append_log(None, "controller", &message)?;
        if !options.background {
            println!("{message}");
        }
        workflow_to_run = Some(Workflow::new(
            state.clone(),
            board,
            agent,
            root.into(),
            base,
        ));
        Ok(0)
    };
    // Startup failures must survive detached stdout/stderr redirection.
    let outcome = execute();
    if let Err(error) = &outcome {
        if action == "run" {
            state.append_log(None, "controller-error", &error.to_string())?;
        }
    }
    let code = outcome?;
    if let (Some(controller), Some(workflow)) = (controller, workflow_to_run) {
        let result = controller.run(
            workflow,
            settings["workers"].as_u64().unwrap() as usize,
            Duration::from_secs(settings["interval"].as_u64().unwrap()),
            options.once,
        );
        if let Err(error) = &result {
            state.append_log(None, "controller-error", &error.to_string())?;
        }
        result?;
    }
    Ok(code)
}
pub fn main(args: Vec<String>) -> i32 {
    let result = Options::parse(args).and_then(|options| run(&options));
    match result {
        Ok(code) => code,
        Err(error) => {
            eprintln!("worklane: {error}");
            if error.kind == "usage" { 2 } else { 1 }
        }
    }
}
