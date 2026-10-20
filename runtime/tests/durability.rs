use serde_json::json;
use std::{
    collections::BTreeMap,
    io::Write,
    os::fd::AsRawFd,
    process::{Command, Stdio},
    sync::{Arc, Barrier},
    time::{Duration, Instant},
};
use worklane_runtime::{
    config::ConfigStore,
    controller::Controller,
    harness::{AgentHarness, validate_result},
    process::{ProcessRef, ProcessSupervisor, Processes},
    read_model::{ReadModel, clip, search_position, wrap},
    state::StateStore,
};

fn store() -> (tempfile::TempDir, StateStore) {
    let root = tempfile::tempdir().unwrap();
    let state = StateStore::open("Owner/Repo", Some(root.path())).unwrap();
    (root, state)
}
#[test]
fn injected_quota_is_journaled_once_and_stale_success_cannot_clear_new_failure() {
    use worklane_runtime::{Dependencies, Error, FixedClock, transport::GitHubTransport};
    let root = tempfile::tempdir().unwrap();
    let state = StateStore::with_dependencies(
        "owner/repo",
        Some(root.path()),
        Dependencies {
            clock: Arc::new(FixedClock("1970-01-01T00:16:40+00:00".into())),
            ..Dependencies::default()
        },
    )
    .unwrap();
    let transport = GitHubTransport::new(&state, root.path())
        .unwrap()
        .with_executor(Arc::new(|_| Err(Error::quota("fixture quota", 5000.))));
    let mut error = transport.text(&["fixture"], None).unwrap_err();
    assert!(error.deferred);
    let first = transport.snapshot().unwrap();
    assert_eq!(first["delay"], 60);
    assert_eq!(first["until"], 5000.);
    transport.record_quota(&mut error).unwrap();
    let mut deferred = transport.check().unwrap_err();
    assert!(deferred.deferred);
    transport.record_quota(&mut deferred).unwrap();
    assert_eq!(transport.snapshot().unwrap(), first);
    let mut next = Error::quota("second failure", 0.);
    transport.record_quota(&mut next).unwrap();
    transport.clear(&first).unwrap();
    assert_eq!(transport.snapshot().unwrap()["delay"], 120);
    assert_eq!(transport.snapshot().unwrap()["error"], "second failure");
}
fn run(state: &StateStore) -> (serde_json::Value, String) {
    let mut task = json!({"number":1,"phase":"building","stack":[{"number":2,"head_sha":"abc"}]});
    state.save(&mut task).unwrap();
    let id = state.begin_run(&mut task, "build", &state.path).unwrap();
    (task, id)
}
#[test]
fn typed_state_preserves_empty_null_integer_boolean_and_float() {
    let (_root, s) = store();
    let v = json!({"空":[{},[],null,true,false,1,-12,1.25,"界"],"empty":""});
    s.set_meta("nested", &v).unwrap();
    assert_eq!(s.get_meta("nested", json!(0)).unwrap(), v);
    let db = s.connect().unwrap();
    assert_eq!(
        db.query_row(
            "SELECT kind FROM state_values WHERE owner='meta:nested' AND key='空'",
            [],
            |r| r.get::<_, String>(0)
        )
        .unwrap(),
        "list"
    );
}
#[test]
fn reopen_does_not_erase_normalized_sessions_on_stale_task_save() {
    let (_r, s) = store();
    let (mut t, id) = run(&s);
    let session = uuid::Uuid::new_v4().to_string();
    s.attach_session(&id, &session, "model", "codex").unwrap();
    t["sessions"] = json!({});
    s.save(&mut t).unwrap();
    let reopened = StateStore::open(
        "owner/repo",
        Some(s.path.parent().unwrap().parent().unwrap()),
    )
    .unwrap();
    assert_eq!(
        reopened.session(1, "worker").unwrap().unwrap()["session_id"],
        session
    );
    assert_eq!(reopened.path, s.path);
}
#[test]
fn event_mapping_and_readable_log_rollback_as_one_unit() {
    let (_r, s) = store();
    let (_, id) = run(&s);
    let a = uuid::Uuid::new_v4().to_string();
    let b = uuid::Uuid::new_v4().to_string();
    let events = [
        json!({"type":"thread.started","thread_id":a}),
        json!({"type":"thread.started","thread_id":b}),
    ];
    assert!(
        s.record_events(&id, &events, "model", "codex", Some(&json!({"type":"raw"})))
            .is_err()
    );
    assert!(s.events_page(&id, 0, 100).unwrap().is_empty());
    assert!(s.logs_page(None, 0, 100).unwrap().is_empty());
    assert!(s.session(1, "worker").unwrap().is_none());
}
#[test]
fn uniqueness_across_roles_is_database_enforced() {
    let (_r, s) = store();
    let (mut t, id) = run(&s);
    let a = uuid::Uuid::new_v4().to_string();
    s.attach_session(&id, &a, "model", "codex").unwrap();
    let review = s.begin_run(&mut t, "review", &s.path).unwrap();
    assert!(
        s.record_events(
            &review,
            &[json!({"type":"thread.started","thread_id":a})],
            "model",
            "codex",
            None
        )
        .is_err()
    );
    assert!(s.events_page(&review, 0, 100).unwrap().is_empty());
    assert!(s.session(1, "reviewer").unwrap().is_none());
}
#[test]
fn future_schema_refusal_does_not_modify_database() {
    let (_r, s) = store();
    s.set_meta("sentinel", &json!("keep")).unwrap();
    s.connect()
        .unwrap()
        .pragma_update(None, "user_version", 3)
        .unwrap();
    let root = s.path.parent().unwrap().parent().unwrap();
    assert!(
        StateStore::open("owner/repo", Some(root))
            .unwrap_err_message()
            .contains("newer Worklane")
    );
    assert_eq!(s.get_meta("sentinel", json!(null)).unwrap(), "keep");
    assert_eq!(
        s.connect()
            .unwrap()
            .query_row("PRAGMA user_version", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        3
    );
}
trait ErrorText {
    fn unwrap_err_message(self) -> String;
}
impl<T> ErrorText for worklane_runtime::Result<T> {
    fn unwrap_err_message(self) -> String {
        match self {
            Ok(_) => panic!("expected failure"),
            Err(e) => e.to_string(),
        }
    }
}
#[test]
fn wal_backup_restores_committed_events_and_pending_inputs() {
    let (root, s) = store();
    let _lock = s.controller_lock(false).unwrap();
    let (_, id) = run(&s);
    s.attach_session(&id, "ses_backup", "m", "opencode")
        .unwrap();
    s.queue_input(&id, "retain 界").unwrap();
    let db = s.connect().unwrap();
    db.pragma_update(None, "wal_autocheckpoint", 0).unwrap();
    s.append_log(Some(&id), "stdout", "WAL-only tail").unwrap();
    let backup = root.path().join("backup.sqlite3");
    s.backup(&backup).unwrap();
    let restore_root = root.path().join("restore");
    let restore_path = restore_root.join("worklane/owner--repo");
    std::fs::create_dir_all(&restore_path).unwrap();
    std::fs::copy(&backup, restore_path.join("state.sqlite3")).unwrap();
    let restored = StateStore::open("owner/repo", Some(&restore_root)).unwrap();
    assert_eq!(restored.advance_run(&id, None).unwrap(), vec!["retain 界"]);
    assert_eq!(
        restored.logs_page(None, 0, 100).unwrap()[2]["content"],
        "WAL-only tail"
    );
    assert_eq!(
        restored
            .connect()
            .unwrap()
            .query_row("PRAGMA integrity_check", [], |r| r.get::<_, String>(0))
            .unwrap(),
        "ok"
    );
}
#[test]
fn bounded_writer_wait_and_rollback_leave_prior_commit() {
    let (_r, mut s) = store();
    s.set_meta("keep", &json!(1)).unwrap();
    s.busy_timeout = Duration::from_millis(80);
    let db = s.connect().unwrap();
    db.execute_batch("BEGIN IMMEDIATE").unwrap();
    let start = Instant::now();
    assert!(s.set_meta("keep", &json!(2)).is_err());
    assert!(start.elapsed() < Duration::from_secs(2));
    db.execute_batch("ROLLBACK").unwrap();
    assert_eq!(s.get_meta("keep", json!(0)).unwrap(), 1);
}
#[test]
fn interrupted_delivery_survives_recovery_until_turn_commits() {
    let (_r, s) = store();
    let _lock = s.controller_lock(false).unwrap();
    let (_, id) = run(&s);
    s.attach_session(&id, "ses_queue", "m", "opencode").unwrap();
    s.queue_input(&id, "action").unwrap();
    assert_eq!(s.advance_run(&id, None).unwrap(), vec!["action"]);
    s.recover_runs().unwrap();
    let mut t = s.load(1).unwrap();
    let next = s.begin_run(&mut t, "build", &s.path).unwrap();
    assert_eq!(s.advance_run(&next, None).unwrap(), vec!["action"]);
    let result = json!({"verdict":"pass","summary":"done"});
    assert!(s.advance_run(&next, Some(&result)).unwrap().is_empty());
    assert_eq!(s.runs(None).unwrap().last().unwrap()["result"], result);
    assert!(s.queue_input(&next, "late").is_err());
}
#[test]
fn input_completion_and_submission_serialize() {
    let (_r, s) = store();
    let _lock = s.controller_lock(false).unwrap();
    for n in 0..16 {
        let (_, id) = run(&s);
        s.attach_session(&id, "ses_race", "m", "opencode").unwrap();
        let barrier = Arc::new(Barrier::new(2));
        let b = barrier.clone();
        let copy = s.clone();
        let rid = id.clone();
        let send = std::thread::spawn(move || {
            b.wait();
            copy.queue_input(&rid, "racing").is_ok()
        });
        barrier.wait();
        let input = s.advance_run(&id, Some(&json!({"turn":n}))).unwrap();
        let accepted = send.join().unwrap();
        assert_eq!(input.len(), usize::from(accepted));
        if accepted {
            s.advance_run(&id, Some(&json!({"done":true}))).unwrap();
        }
    }
}
#[test]
fn immediate_stop_never_waits_for_dispatch_or_downgrades() {
    let (_r, s) = store();
    let _boundary = s.dispatch_lock().unwrap();
    let start = Instant::now();
    s.request_stop(true).unwrap();
    assert_eq!(s.request_stop(false).unwrap()["immediate"], true);
    assert!(start.elapsed() < Duration::from_secs(1));
    assert!(s.paused().unwrap());
}
#[test]
fn logs_are_keyset_paged_without_losing_history() {
    let (_r, s) = store();
    for i in 0..2010 {
        s.append_log(None, "controller", &format!("line {i} 界"))
            .unwrap();
    }
    let page = s.logs_page(None, 0, 10000).unwrap();
    assert_eq!(page.len(), 1000);
    let second = s.logs_page(None, 1000, 1000).unwrap();
    assert_eq!(second[0]["content"], "line 1000 界");
    let snapshot = ReadModel::new(s.clone()).snapshot(None).unwrap();
    assert_eq!(snapshot["logs"].as_array().unwrap().len(), 40);
    assert_eq!(snapshot["logs"][39]["content"], "line 2009 界");
}
#[test]
fn literal_unicode_search_finds_historical_turn_and_bounds_context() {
    let (_r, s) = store();
    let (mut t, first) = run(&s);
    s.attach_session(&first, "ses_history", "m", "opencode")
        .unwrap();
    s.append_log(Some(&first), "agent", "Straße 界 [literal].*")
        .unwrap();
    s.finish_run(&first, "completed", None, None).unwrap();
    let second = s.begin_run(&mut t, "build", &s.path).unwrap();
    s.append_log(Some(&second), "agent", "current").unwrap();
    let read = ReadModel::new(s);
    let snap = read.snapshot(None).unwrap();
    let rows = snap["rows"].as_array().unwrap();
    let found = read.search("STRASSE", rows, 100).unwrap();
    assert_eq!(found["results"][0]["row"]["run"]["id"], first);
    let context = read.search_context(&found["results"][0]).unwrap();
    assert_eq!(context.len(), 1);
    assert_eq!(context[0]["run_id"], first);
    assert_eq!(search_position("ß Straße", "strasse"), Some(2));
    assert_eq!(
        read.search("[literal].*", rows, 100).unwrap()["results"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
}
#[test]
fn config_errors_preserve_file_and_role_overrides() {
    let root = tempfile::tempdir().unwrap();
    ConfigStore::save(
        root.path(),
        &json!({"harness":"opencode","worker_harness":"codex","completion_gates":[2]}),
    )
    .unwrap();
    let old = std::fs::read(root.path().join(".worklane.toml")).unwrap();
    assert!(ConfigStore::save(root.path(), &json!({"workers":true})).is_err());
    assert_eq!(
        std::fs::read(root.path().join(".worklane.toml")).unwrap(),
        old
    );
    let c = ConfigStore::resolve(root.path(), &json!({"workers":2})).unwrap();
    assert_eq!(c["harness"], "opencode");
    assert_eq!(c["worker_harness"], "codex");
    std::fs::write(root.path().join(".worklane.toml"), "workers = 'bad'\n").unwrap();
    assert!(ConfigStore::resolve(root.path(), &json!({"workers":3})).is_err());
}
#[test]
fn result_validation_rejects_extra_fields_and_false_findings() {
    for value in [
        json!({"verdict":"pass"}),
        json!({"verdict":"pass","pr_url":"","head_sha":"","summary":"","findings":[true]}),
        json!({"verdict":"pass","pr_url":"","head_sha":"","summary":"","findings":[],"extra":true}),
    ] {
        assert!(validate_result(&value).is_err());
    }
}
#[test]
fn unicode_wrapping_retains_combining_wide_and_word_spaces() {
    assert_eq!(wrap("ab 界e\u{301}", 4).concat(), "ab 界e\u{301}");
    assert_eq!(clip("界e\u{301}x", 3), "界e\u{301}");
    assert_eq!(clip("escape\u{1b}[31m", 50), "escape[31m");
}
#[test]
fn stale_birth_cannot_claim_or_signal_unrelated_process() {
    let mut child = Command::new("sleep").arg("30").spawn().unwrap();
    let root = ProcessRef {
        pid: child.id() as i32,
        birth: "wrong".into(),
    };
    assert!(
        ProcessSupervisor::owned(std::slice::from_ref(&root), &[], &Processes::new()).is_empty()
    );
    ProcessSupervisor::signal_owned(&BTreeMap::from([(root.pid, root.birth)]), libc::SIGKILL)
        .unwrap();
    assert!(child.try_wait().unwrap().is_none());
    child.kill().unwrap();
    child.wait().unwrap();
}
#[test]
fn child_inherits_controller_lock_after_parent_guard_closes() {
    let (_r, s) = store();
    let lock = s.controller_lock(false).unwrap();
    let mut command = Command::new("sleep");
    command.arg("30").stdin(Stdio::null()).stdout(Stdio::null());
    ProcessSupervisor::prepare(&mut command, &[lock.as_raw_fd()]);
    let mut child = command.spawn().unwrap();
    drop(lock);
    assert!(s.running().unwrap());
    child.kill().unwrap();
    child.wait().unwrap();
    assert!(!s.running().unwrap());
}
#[test]
fn failed_migration_rolls_back_and_keeps_original_bytes() {
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("worklane/owner--repo/tasks");
    std::fs::create_dir_all(&path).unwrap();
    let original = b"{\"number\":7,\"phase\":\"building\"}";
    std::fs::write(path.join("7.json"), original).unwrap();
    std::fs::write(path.join("8.json"), b"invalid").unwrap();
    assert!(StateStore::open("owner/repo", Some(root.path())).is_err());
    assert_eq!(std::fs::read(path.join("7.json")).unwrap(), original);
    std::fs::remove_file(path.join("8.json")).unwrap();
    let s = StateStore::open("owner/repo", Some(root.path())).unwrap();
    assert_eq!(s.load(7).unwrap()["phase"], "building");
    assert!(!path.join("7.json").exists());
    let db = s.connect().unwrap();
    assert_eq!(
        db.query_row(
            "SELECT content FROM migration_archive WHERE path='tasks/7.json'",
            [],
            |r| r.get::<_, Vec<u8>>(0)
        )
        .unwrap(),
        original
    );
}
#[test]
fn uncommitted_crash_is_not_visible_and_committed_event_survives() {
    let (_r, s) = store();
    let (_, id) = run(&s);
    {
        let mut db = s.connect().unwrap();
        let tx = db.transaction().unwrap();
        StateStore::put(&tx, "meta:uncommitted", &json!("lost")).unwrap();
    }
    assert_eq!(
        s.get_meta("uncommitted", json!(null)).unwrap(),
        serde_json::Value::Null
    );
    s.record_events(
        &id,
        &[json!({"type":"thread.started","thread_id":"ses_crash"})],
        "m",
        "opencode",
        None,
    )
    .unwrap();
    let reopened = StateStore::open(
        "owner/repo",
        Some(s.path.parent().unwrap().parent().unwrap()),
    )
    .unwrap();
    reopened.recover_runs().unwrap();
    assert_eq!(
        reopened.session(1, "worker").unwrap().unwrap()["session_id"],
        "ses_crash"
    );
    assert_eq!(reopened.events_page(&id, 0, 100).unwrap().len(), 1);
}
#[test]
fn old_start_generation_cannot_clear_new_stop() {
    let (_r, s) = store();
    let observed = s.get_meta("shutdown", json!({})).unwrap();
    s.request_stop(true).unwrap();
    assert!(Controller::acquire(&s, &s.path, &json!({}), &observed, Some("old")).is_err());
    assert_eq!(
        s.get_meta("shutdown", json!({})).unwrap()["immediate"],
        true
    );
    assert!(!s.running().unwrap());
}
#[test]
fn saved_model_and_harness_win_new_role_configuration() {
    let (_r, s) = store();
    let agent = AgentHarness::new(
        &s.path,
        &s,
        &json!({"harness":"codex","model":"new","worker_harness":"opencode"}),
    )
    .unwrap();
    assert_eq!(
        agent.selection("worker", None),
        ("opencode".into(), "new".into())
    );
    assert_eq!(
        agent.selection("worker", Some(&json!({"backend":"codex","model":"old"}))),
        ("codex".into(), "old".into())
    );
}
#[test]
fn controller_log_writer_flushes_without_persistent_log_files() {
    let (_r, s) = store();
    {
        let mut writer =
            worklane_runtime::controller::DatabaseLogWriter::new(s.clone(), "controller", None);
        write!(writer, "first\nsecond").unwrap();
    }
    assert_eq!(s.logs_page(None, 0, 100).unwrap().len(), 2);
    assert!(!s.path.join("controller.log").exists());
}

#[test]
fn abrupt_process_exit_preserves_committed_session_and_discards_inflight_transaction() {
    let root = tempfile::tempdir().unwrap();
    let mut child = Command::new(env!("CARGO_BIN_EXE_runtime-fixture-adapter"))
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(
            json!({"op":"state.crash","real_dependencies":true,"state_root":root.path()})
                .to_string()
                .as_bytes(),
        )
        .unwrap();
    assert_eq!(child.wait().unwrap().code(), Some(99));
    let s = StateStore::open("owner/repo", Some(root.path())).unwrap();
    assert_eq!(
        s.get_meta("uncommitted", json!(null)).unwrap(),
        serde_json::Value::Null
    );
    s.recover_runs().unwrap();
    assert_eq!(
        s.session(1, "worker").unwrap().unwrap()["session_id"],
        "ses_crash"
    );
    assert_eq!(s.runs(None).unwrap()[0]["status"], "interrupted");
}

#[test]
fn migration_cleanup_never_follows_replaced_parent_outside_state() {
    let (root, s) = store();
    let outside = root.path().join("outside");
    std::fs::create_dir(&outside).unwrap();
    let original = outside.join("original");
    std::fs::write(&original, b"preserve").unwrap();
    std::os::unix::fs::symlink(&outside, s.path.join("logs")).unwrap();
    s.connect()
        .unwrap()
        .execute(
            "INSERT INTO migration_archive VALUES (?,?)",
            rusqlite::params!["logs/original", b"preserve".as_slice()],
        )
        .unwrap();
    assert!(StateStore::open("owner/repo", Some(root.path())).is_err());
    assert_eq!(std::fs::read(original).unwrap(), b"preserve");
}

#[test]
fn fresh_assignment_cannot_reuse_previous_completed_turn_even_with_identical_clock() {
    use worklane_runtime::{Dependencies, FixedClock, SequentialIds};
    let root = tempfile::tempdir().unwrap();
    let state = StateStore::with_dependencies(
        "owner/repo",
        Some(root.path()),
        Dependencies {
            clock: Arc::new(FixedClock("2026-10-20T00:00:00+00:00".into())),
            ids: Arc::new(SequentialIds::default()),
        },
    )
    .unwrap();
    let (mut task, id) = run(&state);
    state
        .attach_session(&id, "ses_assignment", "m", "opencode")
        .unwrap();
    let result =
        json!({"verdict":"pass","pr_url":"","head_sha":"","summary":"prior turn","findings":[]});
    state.advance_run(&id, Some(&result)).unwrap();
    assert_eq!(
        state.committed_turn(&task, "build", &state.path).unwrap(),
        Some(result)
    );
    task["phase"] = json!("claiming");
    state.save_assignment(&mut task).unwrap();
    assert_eq!(
        state.committed_turn(&task, "build", &state.path).unwrap(),
        None
    );
}

#[test]
fn python_completed_ledger_requires_exact_assignment_timing_and_session() {
    let (_root, state) = store();
    let (mut task, id) = run(&state);
    state
        .attach_session(&id, "ses_python", "m", "opencode")
        .unwrap();
    let result = json!({"verdict":"pass","pr_url":"","head_sha":"","summary":"Python committed","findings":[]});
    state
        .finish_run(&id, "completed", None, Some(&result))
        .unwrap();
    state
        .connect()
        .unwrap()
        .execute(
            "DELETE FROM state_values WHERE owner=?",
            ["meta:assignment:1:build"],
        )
        .unwrap();
    assert!(state.committed_turn(&task, "build", &state.path).is_err());
    task["started_at"] = json!("2000-01-01T00:00:00+00:00");
    assert_eq!(
        state.committed_turn(&task, "build", &state.path).unwrap(),
        Some(result)
    );
    task["started_at"] = json!("2100-01-01T00:00:00+00:00");
    assert_eq!(
        state.committed_turn(&task, "build", &state.path).unwrap(),
        None
    );
}

#[test]
fn log_history_stops_at_its_initial_boundary_while_writers_append() {
    let (_root, state) = store();
    for _ in 0..3 {
        state.append_log(None, "controller", "original").unwrap();
    }
    let mut seen = 0;
    ReadModel::new(state.clone())
        .logs(None, true, |_| {
            seen += 1;
            if seen > 10 {
                return worklane_runtime::fail("Unbounded live history");
            }
            state.append_log(None, "controller", "later")
        })
        .unwrap();
    assert_eq!(seen, 3);
}

#[test]
fn oversized_integers_cannot_silently_become_floats_or_erase_prior_values() {
    let (_root, state) = store();
    state.set_meta("keep", &json!({"valid":true})).unwrap();
    let oversized: serde_json::Value =
        serde_json::from_str("{\"integer\":184467440737095516160}").unwrap();
    assert!(state.set_meta("keep", &oversized).is_err());
    assert_eq!(
        state.get_meta("keep", json!(null)).unwrap(),
        json!({"valid":true})
    );
}

fn quota_transport() -> (
    tempfile::TempDir,
    worklane_runtime::transport::GitHubTransport,
) {
    use worklane_runtime::{Dependencies, FixedClock, SequentialIds};
    let root = tempfile::tempdir().unwrap();
    let state = StateStore::with_dependencies(
        "owner/repo",
        Some(root.path()),
        Dependencies {
            clock: Arc::new(FixedClock("1970-01-01T00:16:40+00:00".into())),
            ids: Arc::new(SequentialIds::default()),
        },
    )
    .unwrap();
    let transport = worklane_runtime::transport::GitHubTransport::new(&state, root.path()).unwrap();
    (root, transport)
}
fn fake_api(cwd: &std::path::Path, script: &str) -> worklane_runtime::transport::CommandRequest {
    worklane_runtime::transport::CommandRequest {
        argv: vec![
            "/bin/sh".into(),
            "-c".into(),
            script.into(),
            "graphql".into(),
        ],
        cwd: cwd.into(),
        stdin: None,
        timeout: Duration::from_secs(2),
        environment: Default::default(),
    }
}
#[test]
fn successful_issue_body_cannot_publish_a_false_global_quota_cooldown() {
    let (root, transport) = quota_transport();
    transport
        .call(&fake_api(
            root.path(),
            "printf '%s' '{\"data\":{\"issue\":{\"body\":\"a rate limit story\"}}}'",
        ))
        .unwrap();
    assert_eq!(transport.snapshot().unwrap(), json!({}));
}
#[test]
fn quota_uses_injected_clock_authoritative_headers_and_blocks_further_processes() {
    let (root, transport) = quota_transport();
    let previous = transport.snapshot().unwrap();
    let error=transport.call(&fake_api(root.path(),"printf 'rate limit exhausted\\nretry-after: 120\\nx-ratelimit-remaining: 0\\nx-ratelimit-reset: 1100\\n'; exit 1")).unwrap_err();
    assert_eq!(error.kind, "quota");
    assert_eq!(transport.snapshot().unwrap()["until"].as_f64(), Some(1120.));
    assert_eq!(
        transport
            .call(&fake_api(root.path(), "touch unexpected"))
            .unwrap_err()
            .kind,
        "quota"
    );
    assert!(!root.path().join("unexpected").exists());
    transport.clear(&previous).unwrap();
    assert_eq!(transport.snapshot().unwrap()["delay"], 60);
}
#[test]
fn successful_last_point_preserves_response_without_copying_body_into_cooldown_state() {
    let (root, transport) = quota_transport();
    let response=transport.call(&fake_api(root.path(),"printf 'HTTP/1.1 200 OK\\nx-ratelimit-remaining: 0\\nx-ratelimit-reset: 1100\\n\\n{\"data\":{\"issue\":{\"body\":\"private text\"}}}'")).unwrap();
    assert!(response.stdout.contains("private text"));
    let saved = transport.snapshot().unwrap();
    assert!(!saved["error"].as_str().unwrap().contains("private text"));
    assert_eq!(saved["until"].as_f64(), Some(1101.));
}

#[test]
fn quota_probe_failure_keeps_known_quota_and_shared_cooldown() {
    use std::sync::atomic::{AtomicUsize, Ordering};
    use worklane_runtime::{Error, transport::CommandResult};
    let (root, transport) = quota_transport();
    let calls = Arc::new(AtomicUsize::new(0));
    let seen = calls.clone();
    let transport = transport.with_executor(Arc::new(move |request| {
        if seen.fetch_add(1, Ordering::SeqCst) == 0 {
            return Ok(CommandResult {
                stdout: String::new(),
                stderr: "API rate limit exceeded".into(),
                exit: 1,
            });
        }
        assert_eq!(request.argv[0], "/custom/gh");
        assert!(request.argv.iter().any(|arg| arg == "graphql"));
        Err(Error::new("busy", "probe timed out"))
    }));
    let mut request = fake_api(root.path(), "");
    request.argv = vec!["/custom/gh".into(), "pr".into(), "list".into()];
    let error = transport.call(&request).unwrap_err();
    assert_eq!(error.kind, "quota");
    assert_eq!(error.retry_at, Some(0.));
    assert_eq!(error.message, "API rate limit exceeded");
    assert_eq!(transport.snapshot().unwrap()["until"], 1060.);
    assert_eq!(transport.call(&request).unwrap_err().retry_at, Some(1060.));
    assert_eq!(calls.load(Ordering::SeqCst), 2);
}

#[test]
fn successful_probe_body_is_not_a_quota_error_or_persisted_diagnostic() {
    use std::sync::atomic::{AtomicUsize, Ordering};
    use worklane_runtime::transport::CommandResult;
    let (root, transport) = quota_transport();
    let calls = Arc::new(AtomicUsize::new(0));
    let transport = transport.with_executor(Arc::new(move |_| {
        Ok(if calls.fetch_add(1, Ordering::SeqCst) == 0 {
            CommandResult {
                stdout: String::new(),
                stderr: "unknown owner type".into(),
                exit: 1,
            }
        } else {
            CommandResult {
                stdout: json!({"data":{"viewer":{"login":"private rate limit detail"}}})
                    .to_string(),
                stderr: String::new(),
                exit: 0,
            }
        })
    }));
    let mut request = fake_api(root.path(), "");
    request.argv = vec!["gh".into(), "project".into(), "view".into(), "2".into()];
    let error = transport.call(&request).unwrap_err();
    assert_eq!(error.kind, "github");
    assert_eq!(error.message, "gh failed: unknown owner type");
    assert_eq!(transport.snapshot().unwrap(), json!({}));
}

#[test]
fn unicode_decimal_issue_aliases_cannot_evade_cycles_or_completion_gates() {
    use worklane_runtime::config::ConfigStore;
    assert_eq!(ConfigStore::issue_number("4९").unwrap(), 49);
    assert!(ConfigStore::issue_number("4!").is_err());
    assert!(ConfigStore::issue_number("").is_err());
    let values = json!({"stack_dependencies":["50:4९"],"project":"owner/2९"});
    assert_eq!(
        ConfigStore::validate(&values).unwrap()["stack_dependencies"],
        values["stack_dependencies"]
    );
    assert!(
        ConfigStore::validate(&json!({"stack_dependencies":["50:5३"],"completion_gates":[53]}))
            .unwrap_err()
            .message
            .contains("Completion gate #53")
    );
    assert!(
        ConfigStore::validate(&json!({"stack_dependencies":["50:5३","53:50"]}))
            .unwrap_err()
            .message
            .contains("cycle")
    );
}

#[test]
fn cached_browser_launch_survives_helper_exit_without_controller_ownership_or_cooldown_changes() {
    use std::os::unix::fs::PermissionsExt;
    use worklane_runtime::{actions::Actions, harness::AgentHarness};
    let (root, mut transport) = quota_transport();
    let state = StateStore::with_dependencies(
        "owner/repo",
        Some(root.path()),
        transport.budget.deps.clone(),
    )
    .unwrap();
    let url = "https://github.com/owner/repo/pull/12";
    state
        .set_meta(
            &format!("tui:pr:{url}"),
            &json!({"value":{"number":12,"url":url},"retry_at":5000.}),
        )
        .unwrap();
    let cooldown = json!({"until":5000.,"error":"fixture quota","delay":60});
    transport
        .budget
        .set_meta("github_backoff", &cooldown)
        .unwrap();
    let executable = root.path().join("fake-gh");
    std::fs::write(
        &executable,
        "#!/bin/sh\nsleep 60 </dev/null >/dev/null 2>&1 &\nprintf '%s' \"$!\" > browser.pid\n",
    )
    .unwrap();
    std::fs::set_permissions(&executable, std::fs::Permissions::from_mode(0o700)).unwrap();
    transport.executable = executable;
    let actions = Actions {
        agent: AgentHarness::new(root.path(), &state, &json!({})).unwrap(),
        state,
        root: root.path().into(),
        transport: transport.clone(),
        invocation: Default::default(),
    };
    actions.open_pr(&json!({"pr_url":url})).unwrap();
    let pid = std::fs::read_to_string(root.path().join("browser.pid"))
        .unwrap()
        .parse::<i32>()
        .unwrap();
    let birth =
        ProcessSupervisor::identity(pid).expect("launched browser must survive the gh helper");
    // Clean up only the fake browser created by this test, including assertion failures.
    struct Browser(worklane_runtime::process::ProcessRef);
    impl Drop for Browser {
        fn drop(&mut self) {
            let _ = ProcessSupervisor::terminate_owned(
                std::slice::from_ref(&self.0),
                &[],
                Duration::ZERO,
                &Default::default(),
            );
        }
    }
    let _browser = Browser(worklane_runtime::process::ProcessRef { pid, birth });
    let environment = std::fs::read(format!("/proc/{pid}/environ")).unwrap();
    assert!(
        !environment
            .split(|b| *b == 0)
            .any(|value| value.starts_with(b"WORKLANE_PROCESS_OWNER=")
                || value.starts_with(b"WORKLANE_CONTROLLER_OWNER="))
    );
    assert_eq!(transport.snapshot().unwrap(), cooldown);
}

#[test]
fn prepared_schema_is_in_memory_and_uses_the_shared_schema_argument() {
    use std::{
        fs::File,
        io::{Read, Seek},
        os::fd::FromRawFd,
    };
    let (_root, state) = store();
    let agent = AgentHarness::new(&state.path, &state, &json!({})).unwrap();
    let schema_fd = unsafe { libc::memfd_create(c"test-schema".as_ptr(), libc::MFD_CLOEXEC) };
    let result_fd = unsafe { libc::memfd_create(c"test-result".as_ptr(), libc::MFD_CLOEXEC) };
    assert!(schema_fd >= 0 && result_fd >= 0);
    let mut schema = unsafe { File::from_raw_fd(schema_fd) };
    let mut result = unsafe { File::from_raw_fd(result_fd) };
    let supplied = json!({"type":"object","description":"界"});
    let prepared = agent
        .prepare(
            "build",
            &json!({"number":1}),
            &state.path,
            None,
            "prompt".into(),
            &supplied,
            &mut schema,
            &mut result,
        )
        .unwrap();
    assert!(
        prepared
            .argv
            .contains(&format!("/proc/self/fd/{schema_fd}"))
    );
    schema.rewind().unwrap();
    let mut text = String::new();
    schema.read_to_string(&mut text).unwrap();
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(&text).unwrap(),
        supplied
    );
    assert!(text.contains("\\u754c"));
    assert!(
        std::fs::read_link(format!("/proc/self/fd/{schema_fd}"))
            .unwrap()
            .to_string_lossy()
            .contains("memfd:test-schema")
    );
}
