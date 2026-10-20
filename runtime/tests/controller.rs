//! Controlled command boundaries exercise the real scheduler without launching agents.
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    sync::{Arc, Condvar, Mutex, atomic::AtomicBool},
    time::{Duration, Instant},
};
use worklane_runtime::{
    Error,
    board::{Board, Invocation, PROGRESS, REVIEW},
    config::ConfigStore,
    controller::Controller,
    harness::AgentHarness,
    state::StateStore,
    transport::{CommandResult, GitHubTransport},
    workflow::Workflow,
};

#[test]
fn bounded_workers_independent_reviewer_pause_and_drain_keep_pending_work() {
    let root = tempfile::tempdir().unwrap();
    let state = StateStore::open("owner/repo", Some(root.path())).unwrap();
    let config = ConfigStore::resolve(root.path(), &json!({})).unwrap();
    let page =
        |nodes: Value| json!({"nodes":nodes,"pageInfo":{"hasNextPage":false,"endCursor":null}});
    let mut nodes = Vec::new();
    for number in 1..=7 {
        let status = if number <= 5 { PROGRESS } else { REVIEW };
        let item = json!({"id":number.to_string(),"status":status,"content":{"number":number}});
        let tree = root.path().join(format!("tree-{number}"));
        std::fs::create_dir(&tree).unwrap();
        state
            .save(
                &mut json!({"number":number,"phase":"interrupted","item":item,
            "question":true,"resume_stage":if number<=5 {"build"} else {"review"},
            "branch":format!("codex/{number}-worklane"),"base":"main","worker_worktree":tree,
            "preserved_input":"pending followup"}),
            )
            .unwrap();
        nodes.push(
            json!({"id":number.to_string(),"fieldValueByName":{"name":status},
            "links":{"pullRequests":page(json!([]))},
            "content":{"number":number,"title":"Fixture","state":"OPEN","body":"",
                "url":"issue","repository":{"nameWithOwner":"owner/repo"},
                "issueDependenciesSummary":{"blockedBy":0}}}),
        );
    }
    let items = json!({"data":{"node":{"items":{"nodes":nodes,"totalCount":7,
        "pageInfo":{"hasNextPage":false,"endCursor":null}}}}});
    let transport = GitHubTransport::new(&state, root.path()).unwrap().with_executor(Arc::new(move |request| {
        let output = if let Some(stdin) = &request.stdin {
            let data: Value = serde_json::from_str(stdin).unwrap();
            let query = data["query"].as_str().unwrap();
            if query.contains("viewerCanUpdate") {
                json!({"data":{"node":{"public":false,"viewerCanUpdate":true},
                    "repository":{"viewerPermission":"ADMIN"}}})
            } else if query.contains("items(first:100") {
                items.clone()
            } else if query.contains("issueDependenciesSummary") {
                json!({"data":{"repository":{"issue":{"issueDependenciesSummary":{"blockedBy":0}}}}})
            } else { panic!("Unexpected controller query: {query}"); }
        } else if request.argv.get(1).map(String::as_str)==Some("issue") {
            json!({"state":"OPEN","title":"Fixture","labels":[{"name":"question"}]})
        } else { panic!("Unexpected command: {:?}",request.argv); };
        Ok(CommandResult {stdout:output.to_string(),stderr:String::new(),exit:0})
    }));
    let board = Board {
        repo: "owner/repo".into(),
        transport,
        project: json!({"id":"project"}),
        owner: "owner".into(),
        number: 1,
        field: json!({"id":"status"}),
        options: BTreeMap::new(),
        required_statuses: vec![],
        invocation: Invocation::default(),
        security: json!({}),
        public_warning_shown: Arc::new(AtomicBool::new(false)),
    };
    let agent = AgentHarness::new(root.path(), &state, &config).unwrap();
    let gate = Arc::new((Mutex::new((Vec::<String>::new(), false)), Condvar::new()));
    let worker_gate = gate.clone();
    let workflow = Workflow::new(
        state.clone(),
        board,
        agent,
        root.path().into(),
        "main".into(),
    )
    .with_commands(Arc::new(move |request| {
        assert_eq!(request.argv, ["git", "branch", "--show-current"]);
        let (lock, signal) = &*worker_gate;
        let mut observed = lock.lock().unwrap();
        observed.0.push(
            request
                .cwd
                .file_name()
                .unwrap()
                .to_str()
                .unwrap()
                .to_owned(),
        );
        signal.notify_all();
        let deadline = Instant::now() + Duration::from_secs(10);
        while !observed.1 && Instant::now() < deadline {
            observed = signal
                .wait_timeout(observed, Duration::from_millis(100))
                .unwrap()
                .0;
        }
        assert!(observed.1, "Controller fixture was not released");
        Err(Error::new(
            "validation",
            "Controlled boundary: do not launch a provider",
        ))
    }));
    // Pause blocks intake, without consuming or discarding any saved assignment.
    state.pause(true).unwrap();
    Controller::acquire(&state, root.path(), &config, &json!({}), None)
        .unwrap()
        .run(workflow.clone(), 3, Duration::from_millis(10), true)
        .unwrap();
    assert!(gate.0.lock().unwrap().0.is_empty());
    assert!(
        state
            .all()
            .unwrap()
            .iter()
            .all(|t| t["phase"] == "interrupted")
    );
    state.pause(false).unwrap();
    let observed = state.get_meta("shutdown", json!({})).unwrap();
    let controller = Controller::acquire(&state, root.path(), &config, &observed, None).unwrap();
    let stop = controller.stop.clone();
    let handle =
        std::thread::spawn(move || controller.run(workflow, 3, Duration::from_millis(10), false));
    let (lock, signal) = &*gate;
    let mut observed = lock.lock().unwrap();
    let deadline = Instant::now() + Duration::from_secs(10);
    while observed.0.len() < 4 && Instant::now() < deadline {
        observed = signal
            .wait_timeout(observed, Duration::from_millis(100))
            .unwrap()
            .0;
    }
    let mut started = observed.0.clone();
    started.sort();
    assert_eq!(started, ["tree-1", "tree-2", "tree-3", "tree-6"]);
    assert!(state.controller_lock(false).is_err());
    for (number, slot) in [
        (1, "worker-1"),
        (2, "worker-2"),
        (3, "worker-3"),
        (6, "reviewer"),
    ] {
        assert_eq!(state.load(number).unwrap()["slot"], slot);
    }
    // Stop intake and drain: the lease and assignments survive until all jobs finish.
    state.pause(true).unwrap();
    state.request_stop(false).unwrap();
    stop.request();
    assert!(!handle.is_finished());
    assert!(state.controller_lock(false).is_err());
    observed.1 = true;
    signal.notify_all();
    drop(observed);
    handle.join().unwrap().unwrap();
    assert!(state.controller_lock(false).is_ok());
    assert_eq!(gate.0.lock().unwrap().0.len(), 4);
    for number in [4, 5, 7] {
        assert_eq!(state.load(number).unwrap()["phase"], "interrupted");
    }
    for task in state.all().unwrap() {
        assert_eq!(task["preserved_input"], "pending followup");
    }
    assert!(state.runs(None).unwrap().is_empty());
}

#[test]
fn shared_start_keeps_shutdown_snapshot_from_before_security_preflight() {
    use worklane_runtime::actions::Actions;
    let root = tempfile::tempdir().unwrap();
    let state = StateStore::open("owner/repo", Some(root.path())).unwrap();
    let config = ConfigStore::defaults();
    let concurrent_stop = state.clone();
    let transport = GitHubTransport::new(&state, root.path()).unwrap().with_executor(Arc::new(move |request| {
        let output = if request.argv.get(1).map(String::as_str)==Some("project") {
            concurrent_stop.request_stop(true)?;
            json!({"id":"project","number":1,"url":"board"})
        } else {
            assert!(request.stdin.as_ref().unwrap().contains("viewerCanUpdate"));
            json!({"data":{"node":{"public":false,"viewerCanUpdate":true},"repository":{"viewerPermission":"ADMIN"}}})
        };
        Ok(CommandResult {stdout:output.to_string(),stderr:String::new(),exit:0})
    }));
    let actions = Actions {
        state: state.clone(),
        root: root.path().into(),
        agent: AgentHarness::new(root.path(), &state, &config).unwrap(),
        transport,
        invocation: Invocation {
            project: Some("owner/1".into()),
            dangerously_allow_public_board: false,
        },
    };
    let error = actions.start(&["/bin/false".into()]).unwrap_err();
    assert_eq!(error.kind, "interrupted");
    assert!(error.message.contains("concurrent stop"));
    assert!(state.paused().unwrap());
    assert!(!state.running().unwrap());
    assert_eq!(
        state.get_meta("shutdown", json!({})).unwrap()["immediate"],
        true
    );
    assert_eq!(state.get_meta("runtime", json!({})).unwrap(), json!({}));
}
