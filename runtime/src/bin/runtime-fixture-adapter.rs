//! Test-only driver. Production frontends call the library directly.
use serde_json::{Value, json};
use std::{
    io::{self, Read},
    os::fd::AsRawFd,
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};
use worklane_runtime::{
    config::ConfigStore,
    harness::{self, AgentHarness, OpenCodeTurn},
    process::{ProcessRef, ProcessSupervisor, Processes},
    state::StateStore,
    *,
};
fn state(request: &Value) -> Result<StateStore> {
    let root = request["state_root"].as_str().map(Path::new);
    let deps = if request["real_dependencies"] == true {
        Dependencies::default()
    } else {
        Dependencies {
            clock: Arc::new(FixedClock(
                request["clock"]
                    .as_str()
                    .unwrap_or("2026-10-20T00:00:00+00:00")
                    .into(),
            )),
            ids: Arc::new(SequentialIds::default()),
        }
    };
    StateStore::with_dependencies(request["repo"].as_str().unwrap_or("owner/repo"), root, deps)
}
fn snapshot(state: &StateStore, stage: &str) -> Result<Value> {
    let mut logs = Vec::new();
    let mut after = 0;
    loop {
        let page = state.logs_page(None, after, 1000)?;
        if page.is_empty() {
            break;
        }
        after = page.last().unwrap()["id"].as_i64().unwrap();
        logs.extend(page);
    }
    Ok(
        json!({"tasks":state.all()?,"runs":state.runs(None)?,"logs":logs,"session":state.session(1,role(stage)?)?}),
    )
}
fn run(request: &Value) -> Result<Value> {
    let op = string(request, "op");
    let cwd = std::env::current_dir()?;
    match op.as_str() {
        "state.crash" => {
            let state = state(request)?;
            let mut task = json!({"number":1,"phase":"building"});
            state.save(&mut task)?;
            let id = state.begin_run(&mut task, "build", &cwd)?;
            state.record_events(
                &id,
                &[json!({"type":"thread.started","thread_id":"ses_crash"})],
                "m",
                "opencode",
                None,
            )?;
            let mut db = state.connect()?;
            let tx = db.transaction()?;
            StateStore::put(&tx, "meta:uncommitted", &json!("must roll back"))?;
            unsafe { libc::_exit(99) }
        }
        "controller.run" => controller_fixture(request),
        "board.project" => {
            let state = state(request)?;
            let transport = worklane_runtime::transport::GitHubTransport::new(&state, &cwd)?;
            let board =
                worklane_runtime::board::Board::open(&state.repo, transport, None, false, &[])?;
            Ok(board.project)
        }
        "workflow.review_threads" => {
            use worklane_runtime::{board::Board, transport::GitHubTransport, workflow::Workflow};
            let state = state(request)?;
            let transport = GitHubTransport::new(&state, &cwd)?;
            let board = Board {
                repo: state.repo.clone(),
                transport,
                project: json!({}),
                owner: "owner".into(),
                number: 1,
                field: json!({}),
                options: Default::default(),
                required_statuses: Vec::new(),
            };
            let agent = AgentHarness::new(&cwd, &state, &json!({}))?;
            Ok(json!(
                Workflow::new(state, board, agent, cwd, "main".into())
                    .review_threads(&string(request, "url"))?
            ))
        }
        "log_writer" => {
            use std::io::Write;
            let mut writer = worklane_runtime::controller::DatabaseLogWriter::new(
                state(request)?,
                request["stream"].as_str().unwrap_or("controller"),
                None,
            );
            for chunk in request["chunks"]
                .as_array()
                .ok_or_else(|| Error::new("validation", "Missing chunks."))?
            {
                writer.write_all(chunk.as_str().unwrap_or_default().as_bytes())?;
            }
            writer.flush()?;
            Ok(Value::Null)
        }
        "read.search" => worklane_runtime::read_model::ReadModel::new(state(request)?).search(
            &string(request, "query"),
            request["rows"]
                .as_array()
                .ok_or_else(|| Error::new("validation", "Missing rows."))?,
            100,
        ),
        "read.context" => Ok(json!(
            worklane_runtime::read_model::ReadModel::new(state(request)?)
                .search_context(&request["result"])?
        )),
        "text.wrap" => Ok(
            json!({"wrapped":worklane_runtime::read_model::wrap(&string(request,"value"),request["width"].as_u64().unwrap_or(1) as usize),"clipped":worklane_runtime::read_model::clip(&string(request,"value"),request["width"].as_u64().unwrap_or(1) as usize)}),
        ),
        "read.snapshot" => {
            worklane_runtime::read_model::ReadModel::new(state(request)?).snapshot(None)
        }
        "config.validate" => ConfigStore::validate(&request["values"]),
        "config.resolve" => ConfigStore::resolve(
            request["root"].as_str().map(Path::new).unwrap_or(&cwd),
            request.get("overrides").unwrap_or(&json!({})),
        ),
        "config.save" => ConfigStore::save(
            request["root"].as_str().map(Path::new).unwrap_or(&cwd),
            &request["values"],
        ),
        "session.validate" => Ok(json!(harness::validate_session_id(
            &string(request, "backend"),
            &string(request, "id")
        )?)),
        "log.readable" => Ok(json!(harness::readable_log(
            &string(request, "stream"),
            &string(request, "content")
        ))),
        "result.validate" => {
            harness::validate_result(&request["value"])?;
            Ok(Value::Null)
        }
        "opencode.decode" => {
            let mut turn = OpenCodeTurn::default();
            let mut events = Vec::new();
            for event in request["events"]
                .as_array()
                .ok_or_else(|| Error::new("validation", "Missing events."))?
            {
                events.push(turn.events(event)?);
            }
            Ok(json!({"events":events,"result":turn.result()?}))
        }
        "process.owned" | "process.terminate" => {
            let roots = request["roots"]
                .as_array()
                .unwrap_or(&vec![])
                .iter()
                .map(|r| ProcessRef {
                    pid: r["pid"].as_i64().unwrap_or_default() as i32,
                    birth: string(r, "birth"),
                })
                .collect::<Vec<_>>();
            let tokens = request["tokens"]
                .as_array()
                .unwrap_or(&vec![])
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect::<Vec<_>>();
            if op == "process.owned" {
                Ok(json!(ProcessSupervisor::owned(
                    &roots,
                    &tokens,
                    &Processes::new()
                )))
            } else {
                Ok(json!(ProcessSupervisor::terminate_owned(
                    &roots,
                    &tokens,
                    Duration::from_secs_f64(request["grace"].as_f64().unwrap_or(0.1)),
                    &Processes::new()
                )?))
            }
        }
        "state.trace" => {
            let mut state = state(request)?;
            let mut task = request
                .get("task")
                .cloned()
                .unwrap_or(json!({"number":1,"phase":"building"}));
            state.save(&mut task)?;
            let stage = request["stage"].as_str().unwrap_or("build");
            let _lock = state.controller_lock(true)?;
            let id = state.begin_run(&mut task, stage, &cwd)?;
            let mut output = Vec::new();
            for action in request["actions"]
                .as_array()
                .ok_or_else(|| Error::new("validation", "Missing actions."))?
            {
                match action["kind"].as_str().unwrap_or_default() {
                    "session" => state.attach_session(
                        &id,
                        &string(action, "id"),
                        action["model"].as_str().unwrap_or(harness::MODEL),
                        action["backend"].as_str().unwrap_or("codex"),
                    )?,
                    "queue" => state.queue_input(&id, &string(action, "text"))?,
                    "advance" => output.push(json!(state.advance_run(&id, action.get("result"))?)),
                    "recover" => state.recover_runs()?,
                    "reopen" => {
                        state = StateStore::with_dependencies(
                            &state.repo,
                            Some(state.path.parent().unwrap().parent().unwrap()),
                            state.deps.clone(),
                        )?
                    }
                    "snapshot" => output.push(snapshot(&state, stage)?),
                    _ => return fail("Unknown trace action."),
                }
            }
            Ok(json!(output))
        }
        "state.call" => {
            let state = state(request)?;
            let number = request["number"].as_i64().unwrap_or(1);
            let id = string(request, "run_id");
            match request["method"].as_str().unwrap_or_default() {
                "open" => Ok(json!(state.path)),
                "load" => state.load(number),
                "all" => Ok(json!(state.all()?)),
                "save" => {
                    let mut task = request["task"].clone();
                    state.save(&mut task)?;
                    Ok(task)
                }
                "begin_run" => {
                    let mut task = request["task"].clone();
                    let id = state.begin_run(
                        &mut task,
                        request["stage"].as_str().unwrap_or("build"),
                        Path::new(&string(request, "tree")),
                    )?;
                    Ok(json!({"id":id,"task":task}))
                }
                "get_meta" => state.get_meta(
                    &string(request, "key"),
                    request.get("default").cloned().unwrap_or(Value::Null),
                ),
                "set_meta" => {
                    state.set_meta(&string(request, "key"), &request["value"])?;
                    Ok(Value::Null)
                }
                "runs" => Ok(json!(state.runs(request["number"].as_i64())?)),
                "session" => Ok(json!(state.session(number, &string(request, "role"))?)),
                "record_events" => {
                    state.record_events(
                        &id,
                        request["events"].as_array().unwrap(),
                        request["model"].as_str().unwrap_or(harness::MODEL),
                        request["backend"].as_str().unwrap_or("codex"),
                        request.get("wire"),
                    )?;
                    Ok(Value::Null)
                }
                "attach_session" => {
                    state.attach_session(
                        &id,
                        &string(request, "session_id"),
                        request["model"].as_str().unwrap_or(harness::MODEL),
                        request["backend"].as_str().unwrap_or("codex"),
                    )?;
                    Ok(Value::Null)
                }
                "attach_process" => {
                    let mut task = request["task"].clone();
                    let pid = request["pid"].as_i64().unwrap_or(0) as i32;
                    state.attach_process(
                        &id,
                        &mut task,
                        &ProcessRef {
                            pid,
                            birth: ProcessSupervisor::identity(pid).unwrap_or_default(),
                        },
                    )?;
                    Ok(task)
                }
                "queue_input" => {
                    state.queue_input(&id, &string(request, "text"))?;
                    Ok(Value::Null)
                }
                "advance_run" => Ok(json!(state.advance_run(&id, request.get("result"))?)),
                "finish_run" => {
                    state.finish_run(
                        &id,
                        &string(request, "status"),
                        request["error"].as_str(),
                        request.get("result"),
                    )?;
                    Ok(Value::Null)
                }
                "recover_runs" => {
                    state.recover_runs()?;
                    Ok(Value::Null)
                }
                "request_stop" => {
                    state.request_stop(request["immediate"].as_bool().unwrap_or(false))
                }
                "paused" => Ok(json!(state.paused()?)),
                "pause" => {
                    state.pause(request["value"].as_bool().unwrap_or(false))?;
                    Ok(Value::Null)
                }
                "running" => Ok(json!(state.running()?)),
                "append_log" => {
                    state.append_log(
                        request["run_id"].as_str(),
                        &string(request, "stream"),
                        &string(request, "text"),
                    )?;
                    Ok(Value::Null)
                }
                "logs_page" => Ok(json!(state.logs_page(
                    request["number"].as_i64(),
                    request["after"].as_i64().unwrap_or(0),
                    request["limit"].as_u64().unwrap_or(1000) as usize
                )?)),
                "events" => {
                    let mut out = Vec::new();
                    let mut after = 0;
                    loop {
                        let page = state.events_page(&id, after, 1000)?;
                        if page.is_empty() {
                            break;
                        }
                        after = page.last().unwrap().0;
                        out.extend(page.into_iter().map(|(_, v)| v));
                    }
                    Ok(json!(out))
                }
                "backup" => {
                    state.backup(Path::new(&string(request, "target")))?;
                    Ok(Value::Null)
                }
                _ => fail("Unknown state method."),
            }
        }
        "agent.run" => {
            let state = state(request)?;
            let tree = PathBuf::from(request["tree"].as_str().unwrap_or(cwd.to_str().unwrap()));
            let root = PathBuf::from(request["root"].as_str().unwrap_or(cwd.to_str().unwrap()));
            let mut task=request.get("task").cloned().unwrap_or(json!({"number":1,"phase":"building","branch":"codex/1-worklane","base":"main","issue":{"title":"界 fixture"}}));
            let config = request.get("config").cloned().unwrap_or_else(
                || json!({"harness":request["backend"].as_str().unwrap_or("codex")}),
            );
            let mut agent = AgentHarness::new(&root, &state, &config)?;
            if let Some(b) = request["force_backend"].as_str() {
                agent.required_backend = Some(b.into());
            }
            agent.timeout = Duration::from_secs_f64(request["timeout"].as_f64().unwrap_or(28800.));
            agent.metadata_timeout =
                Duration::from_secs_f64(request["metadata_timeout"].as_f64().unwrap_or(15.));
            let lock = if request["runner_fd"].is_i64() {
                None
            } else {
                Some(state.controller_lock(true)?)
            };
            let fd = request["runner_fd"]
                .as_i64()
                .map(|n| n as i32)
                .unwrap_or_else(|| lock.as_ref().unwrap().as_raw_fd());
            let stage = request["stage"].as_str().unwrap_or("build");
            let result = agent.run(
                stage,
                &mut task,
                &tree,
                request["extra"].as_str().unwrap_or_default(),
                fd,
            )?;
            if request["include_task"] == true {
                Ok(json!({"result":result,"task":task}))
            } else {
                Ok(
                    json!({"result":result,"session":state.session(task["number"].as_i64().unwrap(),role(stage)?)?}),
                )
            }
        }
        "app_server.request" => {
            let mut server = harness::CodexAppServer::open(
                &cwd,
                Duration::from_secs_f64(request["timeout"].as_f64().unwrap_or(15.)),
            )?;
            server.request(&string(request, "method"), request["params"].clone())
        }
        _ => fail(format!("Unknown runtime operation: {op}")),
    }
}
fn controller_fixture(request: &Value) -> Result<Value> {
    use worklane_runtime::{
        board::{Board, HUMAN, PROGRESS, READY},
        controller::Controller,
        transport::{GitHubTransport, RepositoryLocator},
        workflow::Workflow,
    };
    let root = std::env::current_dir()?;
    let repo = RepositoryLocator::locate(&root)?;
    let mut values = json!({});
    let argv = request["argv"]
        .as_array()
        .ok_or_else(|| Error::new("validation", "Missing fixture argv."))?;
    let mut i = 1;
    while i < argv.len() {
        let arg = argv[i].as_str().unwrap_or_default();
        if arg == "--once" {
            i += 1;
            continue;
        }
        let key = arg.trim_start_matches("--").replace('-', "_");
        let v = argv
            .get(i + 1)
            .ok_or_else(|| Error::new("validation", "Missing fixture option."))?;
        values[&key] = if matches!(key.as_str(), "workers" | "interval") {
            json!(
                v.as_str()
                    .unwrap_or_default()
                    .parse::<i64>()
                    .map_err(|_| Error::new("validation", "Invalid integer option."))?
            )
        } else {
            v.clone()
        };
        i += 2;
    }
    let config = ConfigStore::resolve(&root, &values)?;
    let state = state(request)?;
    let observed = state.get_meta("shutdown", json!({}))?;
    let controller = Controller::acquire(&state, &root, &config, &observed, None)?;
    let transport = GitHubTransport::new(&state, &root)?;
    let mut board = Board::open(
        &repo.repo,
        transport.clone(),
        config["project"].as_str().filter(|s| !s.is_empty()),
        true,
        &[READY, PROGRESS, HUMAN],
    )?;
    worklane_runtime::transport::command(
        &["gh", "auth", "setup-git", "--hostname", "github.com"],
        &root,
        None,
    )?;
    board.ensure_review_status()?;
    let mut backends = std::collections::BTreeSet::new();
    for role in ["worker", "reviewer", "finalizer"] {
        backends.insert(
            config[format!("{role}_harness")]
                .as_str()
                .filter(|s| !s.is_empty())
                .unwrap_or(config["harness"].as_str().unwrap())
                .to_owned(),
        );
    }
    for backend in backends {
        let args = if backend == "codex" {
            vec!["codex", "login", "status"]
        } else {
            vec!["opencode", "auth", "list"]
        };
        worklane_runtime::transport::command(&args, &root, None)?;
    }
    if state.get_meta("shutdown", json!({}))?["requested"] == true {
        return Ok(json!(0));
    }
    println!(
        "Worklane: {}; {} workers, 1 reviewer; {} {}; board {}",
        repo.repo,
        config["workers"],
        string(&config, "harness"),
        config["model"]
            .as_str()
            .filter(|s| !s.is_empty())
            .unwrap_or("default model"),
        string(&board.project, "url")
    );
    println!(
        "State/logs: {}; intake {}",
        state.path.display(),
        if state.paused()? { "paused" } else { "running" }
    );
    let agent = AgentHarness::new(&root, &state, &config)?;
    let workflow = Workflow::new(
        state.clone(),
        board,
        agent,
        root,
        repo.default_branch.unwrap_or("main".into()),
    );
    controller.run(
        workflow,
        config["workers"].as_u64().unwrap() as usize,
        Duration::from_secs(config["interval"].as_u64().unwrap()),
        true,
    )?;
    let mut after = 0;
    loop {
        let page = state.logs_page(None, after, 1000)?;
        if page.is_empty() {
            break;
        }
        for log in page {
            after = log["id"].as_i64().unwrap();
            if log["stream"] == "controller" {
                println!("{}", string(&log, "content"));
            }
        }
    }
    Ok(json!(0))
}
fn main() {
    let mut input = String::new();
    if let Err(e) = io::stdin()
        .take(16 * 1024 * 1024)
        .read_to_string(&mut input)
    {
        eprintln!("{e}");
        std::process::exit(1);
    }
    let request = serde_json::from_str::<Value>(&input).map_err(Error::from);
    let detailed = request
        .as_ref()
        .is_ok_and(|v| v["real_dependencies"] == true);
    let result = request.and_then(|v| run(&v));
    match result {
        Ok(output) => println!("{}", harness::python_json(&json!({"ok":output}))),
        Err(e) => {
            println!(
                "{}",
                harness::python_json(&if detailed {
                    json!({"error":e.to_string(),"kind":e.kind})
                } else {
                    json!({"error":e.to_string()})
                })
            );
            std::process::exit(1);
        }
    }
}
