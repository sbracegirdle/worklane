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
    process::{ProcessRef, ProcessSupervisor},
    state::StateStore,
    *,
};
fn state(request: &Value) -> Result<StateStore> {
    let root = request["state_root"].as_str().map(Path::new);
    let deps = if request["real_dependencies"] == true {
        let mut deps = Dependencies::default();
        if let Some(clock) = request["clock"].as_str() {
            deps.clock = Arc::new(FixedClock(clock.into()));
        }
        deps
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
        "transport.call" | "board.call" | "workflow.call" | "action.call" | "read.call"
        | "budget.call" => boundary_fixture(request),
        "security.call" => security_fixture(request),
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
                invocation: Default::default(),
                security: Value::Null,
                public_warning_shown: Default::default(),
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
        "config.number" => Ok(json!(ConfigStore::issue_number(&string(request, "value"))?)),
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
            let known = request["known"]
                .as_object()
                .map(|values| {
                    values
                        .iter()
                        .filter_map(|(pid, birth)| {
                            Some((pid.parse().ok()?, birth.as_str()?.to_string()))
                        })
                        .collect()
                })
                .unwrap_or_default();
            if op == "process.owned" {
                Ok(json!(ProcessSupervisor::owned(&roots, &tokens, &known)))
            } else {
                Ok(json!(ProcessSupervisor::terminate_owned(
                    &roots,
                    &tokens,
                    Duration::from_secs_f64(request["grace"].as_f64().unwrap_or(0.1)),
                    &known
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
            if request["guarded"] == true {
                let board = selected_board(request, &state, &root)?;
                let guarded = workflow::Workflow::new(
                    state.clone(),
                    board,
                    agent,
                    root.clone(),
                    "main".into(),
                );
                agent = guarded.agent;
            }
            if let (Some(fd), Some(reply_fd)) = (
                request["preflight_write_fd"].as_i64(),
                request["preflight_read_fd"].as_i64(),
            ) {
                use std::io::Write;
                use std::os::fd::FromRawFd;
                let output = unsafe { std::fs::File::from_raw_fd(fd as i32) };
                let input = unsafe { std::fs::File::from_raw_fd(reply_fd as i32) };
                unsafe {
                    libc::fcntl(fd as i32, libc::F_SETFD, libc::FD_CLOEXEC);
                    libc::fcntl(reply_fd as i32, libc::F_SETFD, libc::FD_CLOEXEC);
                }
                transport::nonblocking(fd as i32)?;
                transport::nonblocking(reply_fd as i32)?;
                let stream = std::sync::Mutex::new((input, output));
                agent.preflight = Some(Arc::new(move |stage, task| {
                    let mut stream = stream
                        .lock()
                        .map_err(|_| Error::new("io", "Test callback lock poisoned."))?;
                    let deadline = std::time::Instant::now() + Duration::from_secs(10);
                    let mut outgoing = serde_json::to_vec(&json!({"stage":stage,"task":task}))?;
                    outgoing.push(b'\n');
                    if outgoing.len() > 16 * 1024 * 1024 {
                        return fail("Oversized test callback request.");
                    }
                    let mut written = 0;
                    while written < outgoing.len() {
                        if std::time::Instant::now() >= deadline {
                            return fail("Test callback write timed out.");
                        }
                        match stream.1.write(&outgoing[written..]) {
                            Ok(0) => return fail("Test callback closed while writing."),
                            Ok(n) => written += n,
                            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                                std::thread::sleep(Duration::from_millis(1));
                            }
                            Err(error) => return Err(error.into()),
                        }
                    }
                    let mut line = Vec::new();
                    loop {
                        transport::readable(stream.0.as_raw_fd(), deadline)?;
                        let mut bytes = [0u8; 4096];
                        match stream.0.read(&mut bytes) {
                            Ok(0) => return fail("Test callback closed before replying."),
                            Ok(n) => line.extend_from_slice(&bytes[..n]),
                            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => continue,
                            Err(e) => return Err(e.into()),
                        }
                        if line.len() > 65536 {
                            return fail("Oversized test callback response.");
                        }
                        if line.last() == Some(&b'\n') {
                            break;
                        }
                    }
                    let response: Value = serde_json::from_slice(&line)?;
                    if let Some(error) = response["error"].as_str() {
                        return Err(Error::new(
                            if response["kind"] == "interrupted" {
                                "interrupted"
                            } else {
                                "blocked"
                            },
                            error,
                        ));
                    }
                    if response["ok"] != true {
                        return fail("Invalid test callback response.");
                    }
                    Ok(())
                }));
            }
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
fn selected_board(request: &Value, state: &StateStore, root: &Path) -> Result<board::Board> {
    Ok(board::Board {
        repo: state.repo.clone(),
        transport: transport::GitHubTransport::new(state, root)?,
        project: json!({"id":"project", "number":2, "url":"board-url"}),
        owner: "owner".into(),
        number: 2,
        field: json!({"id":"status-id","options":[{"id":"ready","name":"Ready for work"}]}),
        options: [
            ("ready for work".into(), "ready".into()),
            ("in progress".into(), "progress".into()),
        ]
        .into(),
        required_statuses: Vec::new(),
        invocation: board::Invocation {
            project: Some("owner/2".into()),
            dangerously_allow_public_board: request["override"] == true,
        },
        security: Value::Null,
        public_warning_shown: Default::default(),
    })
}
// Anonymous pipes are inherited only by this test driver, never by provider processes.
fn callback(request: &Value) -> Result<Arc<dyn Fn(Value) -> Result<Value> + Send + Sync>> {
    use std::{io::Write, os::fd::FromRawFd, sync::Mutex, time::Instant};
    let read = request["callback_read_fd"]
        .as_i64()
        .ok_or_else(|| Error::new("validation", "Missing callback input."))? as i32;
    let write = request["callback_write_fd"]
        .as_i64()
        .ok_or_else(|| Error::new("validation", "Missing callback output."))?
        as i32;
    unsafe {
        libc::fcntl(read, libc::F_SETFD, libc::FD_CLOEXEC);
        libc::fcntl(write, libc::F_SETFD, libc::FD_CLOEXEC);
    }
    transport::nonblocking(read)?;
    transport::nonblocking(write)?;
    let stream = Mutex::new(unsafe {
        (
            std::fs::File::from_raw_fd(read),
            std::fs::File::from_raw_fd(write),
        )
    });
    Ok(Arc::new(move |event| {
        let mut stream = stream
            .lock()
            .map_err(|_| Error::new("io", "Callback lock poisoned."))?;
        let deadline = Instant::now() + Duration::from_secs(10);
        let mut outgoing = serde_json::to_vec(&event)?;
        outgoing.push(b'\n');
        if outgoing.len() > 16 * 1024 * 1024 {
            return fail("Oversized callback request.");
        }
        let mut written = 0;
        while written < outgoing.len() {
            if Instant::now() >= deadline {
                return fail("Callback write timed out.");
            }
            match stream.1.write(&outgoing[written..]) {
                Ok(0) => return fail("Callback closed while writing."),
                Ok(n) => written += n,
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                    std::thread::sleep(Duration::from_millis(1))
                }
                Err(e) => return Err(e.into()),
            }
        }
        let mut incoming = Vec::new();
        loop {
            transport::readable(read, deadline)?;
            let mut bytes = [0; 4096];
            match stream.0.read(&mut bytes) {
                Ok(0) => return fail("Callback closed before replying."),
                Ok(n) => incoming.extend_from_slice(&bytes[..n]),
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => continue,
                Err(e) => return Err(e.into()),
            }
            if incoming.len() > 16 * 1024 * 1024 {
                return fail("Oversized callback response.");
            }
            if incoming.last() == Some(&b'\n') {
                break;
            }
        }
        let value: Value = serde_json::from_slice(&incoming)?;
        if let Some(error) = value["error"].as_str() {
            let kind = match value["kind"].as_str() {
                Some("quota") => "quota",
                Some("busy") => "busy",
                Some("io") => "io",
                _ => "github",
            };
            let mut e = if kind == "quota" {
                Error::quota(error, value["retry_at"].as_f64().unwrap_or(0.))
            } else {
                Error::new(kind, error)
            };
            e.deferred = value["deferred"] == true;
            return Err(e);
        }
        Ok(value["ok"].clone())
    }))
}
fn boundary_fixture(request: &Value) -> Result<Value> {
    let state = state(request)?;
    let root = request["root"]
        .as_str()
        .map(PathBuf::from)
        .unwrap_or(std::env::current_dir()?);
    let call = callback(request)?;
    let transport = transport::GitHubTransport::new(&state,&root)?.with_executor(Arc::new(move |r| {
        let v = call(json!({"argv":r.argv,"cwd":r.cwd,"stdin":r.stdin,"timeout":r.timeout.as_secs_f64()}))?;
        Ok(transport::CommandResult { stdout:string(&v,"stdout"),stderr:string(&v,"stderr"),exit:v["exit"].as_i64().unwrap_or(0) as i32 })
    }));
    let method = string(request, "method");
    if request["op"] == "budget.call" {
        let mut error = Error::quota(
            string(request, "error"),
            request["retry_at"].as_f64().unwrap_or(0.),
        );
        error.deferred = request["deferred"] == true;
        let result = match method.as_str() {
            "record" => transport.record_quota(&mut error).map(|_| Value::Null),
            "check" => transport.check().map(|_| Value::Null),
            "snapshot" => transport.snapshot(),
            "clear" => transport.clear(&request["previous"]).map(|_| Value::Null),
            "call" => transport
                .call(&transport::CommandRequest {
                    argv: vec!["gh".into(), "fixture".into()],
                    cwd: root,
                    stdin: None,
                    timeout: Duration::from_secs(120),
                    environment: Default::default(),
                })
                .map(|v| json!(v.stdout)),
            _ => fail("Unknown budget boundary method."),
        };
        return Ok(match result {
            Ok(value) => json!({"value":value,"deferred":error.deferred}),
            Err(e) => {
                json!({"error":e.message,"kind":e.kind,"retry_at":e.retry_at,"deferred":e.deferred})
            }
        });
    }
    if request["op"] == "read.call" {
        let argv = request["argv"]
            .as_array()
            .ok_or_else(|| Error::new("validation", "Missing read argv."))?;
        let argv = argv
            .iter()
            .map(|v| v.as_str().unwrap_or_default())
            .collect::<Vec<_>>();
        let (value, error) = read_model::ReadModel::new(state)
            .cached_page(&string(request, "key"), || transport.json(&argv))?;
        return Ok(json!({"value":value,"error":error}));
    }
    if request["op"] == "action.call" {
        let actions = actions::Actions {
            agent: AgentHarness::new(&root, &state, &json!({}))?,
            state,
            transport,
            root,
            invocation: Default::default(),
        };
        actions.open_pr(&request["task"])?;
        return Ok(Value::Null);
    }
    if request["op"] == "transport.call" {
        let result = if method == "rate" {
            Ok(json!(transport::rate_limit(
                &string(request, "detail"),
                request["now"].as_f64().unwrap_or(0.)
            )))
        } else if method == "graphql" {
            transport.graphql(&string(request, "query"), &request["variables"])
        } else {
            let argv = request["argv"]
                .as_array()
                .ok_or_else(|| Error::new("validation", "Missing argv."))?
                .iter()
                .map(|v| v.as_str().unwrap_or_default().to_string())
                .collect();
            transport
                .call(&transport::CommandRequest {
                    argv,
                    cwd: root,
                    stdin: request["stdin"].as_str().map(str::to_owned),
                    timeout: Duration::from_secs(120),
                    environment: Default::default(),
                })
                .map(|r| json!(r.stdout.trim()))
        };
        return Ok(match result {
            Ok(value) => json!({"value":value,"budget":transport.snapshot()?}),
            Err(error) => {
                json!({"error":error.message,"kind":error.kind,"retry_at":error.retry_at,"budget":transport.snapshot()?})
            }
        });
    }
    let mut board = selected_board(request, &state, &root)?;
    board.transport = transport;
    let attrs = &request["board"];
    if let Some(s) = attrs["repo"].as_str() {
        board.repo = s.into();
    }
    if let Some(s) = attrs["owner"].as_str() {
        board.owner = s.into();
    }
    if let Some(n) = attrs["number"].as_i64() {
        board.number = n;
    }
    if let Some(v) = attrs.get("project") {
        board.project = v.clone();
    }
    if let Some(v) = attrs.get("field") {
        board.field = v.clone();
    }
    if let Some(v) = attrs.get("options") {
        board.options = serde_json::from_value(v.clone())?;
    }
    if let Some(v) = attrs.get("required_statuses") {
        board.required_statuses = serde_json::from_value(v.clone())?;
    }
    if request["op"] == "board.call" {
        let value = match method.as_str() {
            "items" => json!(board.items()?),
            "linked_prs" => json!(board.linked_prs(&request["id"], request["connection"].clone())?),
            "refresh_fields" => {
                board.refresh_fields()?;
                Value::Null
            }
            "ensure_review_status" => {
                board.ensure_review_status()?;
                Value::Null
            }
            "dependencies_ready" => {
                let mut item = request["item"].clone();
                let ready = board.dependencies_ready(&mut item)?;
                json!({"ready":ready,"item":item})
            }
            "item_status" => json!(board.item_status(&request["item"])?),
            "move" => {
                board.move_item(
                    &request["item"],
                    &string(request, "status"),
                    &string(request, "expected"),
                )?;
                Value::Null
            }
            "dependency_count" => json!(board::dependency_count(&request["issue"])?),
            _ => return fail("Unknown board boundary method."),
        };
        return Ok(
            json!({"value":value,"board":{"project":board.project,"field":board.field,"options":board.options}}),
        );
    }
    let agent = AgentHarness::new(&root, &state, &json!({}))?;
    let mut flow = workflow::Workflow::new(state, board, agent, root, "main".into());
    flow.stack_items = request["stack_items"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    flow.feedback_unavailable = request["feedback_unavailable"]
        .as_array()
        .cloned()
        .unwrap_or_default()
        .iter()
        .filter_map(Value::as_i64)
        .collect();
    if matches!(
        method.as_str(),
        "build" | "review" | "execute" | "worktree" | "verify_pr" | "validate_stack" | "restack"
    ) {
        let mut task = request["task"].clone();
        let lock = flow.state.controller_lock(true)?;
        let result = match method.as_str() {
            "build" => flow.build(&mut task, lock.as_raw_fd()).map(|_| Value::Null),
            "review" => flow
                .review(&mut task, lock.as_raw_fd())
                .map(|_| Value::Null),
            "execute" => flow
                .execute(&mut task, request["review"] == true, lock.as_raw_fd())
                .map(|_| Value::Null),
            "worktree" => flow
                .worktree(&mut task, request["review"] == true)
                .map(|p| json!(p)),
            "verify_pr" => flow.verify_pr(&task, &string(request, "head")),
            "validate_stack" => flow.validate_stack(&task).map(|_| Value::Null),
            "restack" => {
                let mut item = request["item"].clone();
                let remaining = request["remaining"].as_array().cloned().unwrap_or_default();
                flow.restack(&mut task, &remaining, &mut item).map(|_| item)
            }
            _ => unreachable!(),
        };
        let mut output = json!({"task":task});
        match result {
            Ok(value) => output["value"] = value,
            Err(error) => {
                output["error"] = json!(error.message);
                output["kind"] = json!(error.kind);
            }
        }
        return Ok(output);
    }
    if matches!(
        method.as_str(),
        "poll"
            | "pick"
            | "recover"
            | "dependencies_ready"
            | "reconcile_completed"
            | "reconcile_waiting"
    ) {
        let mut items = request["items"].as_array().cloned().unwrap_or_default();
        let mut item = request["item"].clone();
        let active = request["active"]
            .as_array()
            .cloned()
            .unwrap_or_default()
            .iter()
            .filter_map(Value::as_i64)
            .collect();
        let result = match method.as_str() {
            "poll" => flow
                .poll_human_feedback(&mut items, &active)
                .map(|_| Value::Null),
            "pick" => flow
                .pick(&mut item, &string(request, "slot"))
                .map(|t| json!(t)),
            "recover" => flow
                .recover(&mut items, &string(request, "slot"))
                .map(|t| json!(t)),
            "dependencies_ready" => flow
                .dependencies_ready(&mut item, request.get("saved").filter(|v| !v.is_null()))
                .map(|r| json!(r)),
            "reconcile_completed" => flow
                .reconcile_completed(&items, &active)
                .map(|_| Value::Null),
            "reconcile_waiting" => flow
                .reconcile_waiting(&mut items, &active)
                .map(|_| Value::Null),
            _ => unreachable!(),
        };
        let mut output =
            json!({"item":item,"items":items,"feedback_unavailable":flow.feedback_unavailable});
        match result {
            Ok(value) => output["value"] = value,
            Err(error) => {
                output["error"] = json!(error.message);
                output["kind"] = json!(error.kind);
            }
        }
        return Ok(output);
    }
    match method.as_str() {
        "review_threads" => Ok(json!(flow.review_threads(&string(request, "url"))?)),
        "human_feedback" => flow.human_feedback(&request["task"]),
        "pr_number" => Ok(json!(flow.pr_number(&string(request, "url"))?)),
        _ => fail("Unknown workflow boundary method."),
    }
}
fn security_fixture(request: &Value) -> Result<Value> {
    let state = state(request)?;
    let root = request["root"]
        .as_str()
        .map(PathBuf::from)
        .unwrap_or(std::env::current_dir()?);
    let mut board = selected_board(request, &state, &root)?;
    let agent = AgentHarness::new(&root, &state, &json!({}))?;
    let mut flow = workflow::Workflow::new(
        state.clone(),
        board.clone(),
        agent.clone(),
        root.clone(),
        "main".into(),
    );
    let actions = actions::Actions {
        state: state.clone(),
        root,
        agent,
        transport: board.transport.clone(),
        invocation: board.invocation.clone(),
    };
    let mut task = request.get("task").cloned().unwrap_or(json!({"number":1}));
    let mut items = request["items"].as_array().cloned().unwrap_or_default();
    let mut item = request.get("item").cloned().unwrap_or(json!({}));
    let method = string(request, "method");
    let output = match method.as_str() {
        "report" => board.security_report()?,
        "open" => {
            board::Board::open_with_policy(
                &state.repo,
                board.transport.clone(),
                Some("owner/2"),
                false,
                &[],
                board.invocation.clone(),
                request["check_only"] == true,
            )?
            .security
        }
        "safe" => board.require_safe()?,
        "add" => json!(board.add_issue("Title", "", board::READY)?),
        "ensure_review" => {
            board.ensure_review_status()?;
            Value::Null
        }
        "move" => {
            board.move_item(&item, board::PROGRESS, board::READY)?;
            Value::Null
        }
        "feedback" => flow.human_feedback(&task)?,
        "trusted" => {
            flow.require_trusted_feedback(&task)?;
            Value::Null
        }
        "before_turn" => {
            flow.before_turn(request["stage"].as_str().unwrap_or("build"), &task)?;
            Value::Null
        }
        "agent_result" => flow.agent_result(&mut task, "build", &flow.root, "", -1)?,
        "recover" => json!(flow.recover(&mut items, "worker-1")?),
        "pick" => json!(flow.pick(&mut item, "worker-1")?),
        "poll" => {
            flow.poll_human_feedback(&mut items, &Default::default())?;
            Value::Null
        }
        "post" => {
            flow.post(&task, "review", &json!({}))?;
            Value::Null
        }
        "restack" => {
            flow.restack(&mut task, &[], &mut item)?;
            Value::Null
        }
        "retry" => {
            actions.retry(1)?;
            Value::Null
        }
        "resume" => json!(actions.resume_session(1, "worker")?),
        "queue" => {
            actions.queue_input(&string(request, "run_id"), "followup")?;
            Value::Null
        }
        "unpause" => {
            actions.pause(false)?;
            Value::Null
        }
        "save_editor" => {
            let mut draft = actions::IssueDraft {
                board,
                number: Some(1),
                item: Some(item),
                original: json!({"title":"Title","body":""}),
                title: "Edit".into(),
                body: "".into(),
                status: board::READY.into(),
                statuses: vec![board::READY.into()],
            };
            json!(actions.save_editor(&mut draft)?)
        }
        _ => return fail("Unknown security operation."),
    };
    Ok(output)
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
