use crate::{
    Error, Result, fail,
    process::{LockGuard, ProcessRef, ProcessSupervisor, Processes, StopSignals, StopToken},
    state::StateStore,
    string,
    workflow::Workflow,
};
use rusqlite::TransactionBehavior;
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    io::Write,
    os::fd::AsRawFd,
    path::Path,
    process::{Command, Stdio},
    thread::JoinHandle,
    time::{Duration, Instant},
};
/// Own this lease before authentication or any potentially blocking startup work.
pub struct Controller {
    pub state: StateStore,
    pub lock: LockGuard,
    pub stop: StopToken,
    pub generation: String,
    _signals: StopSignals,
}
impl Controller {
    pub fn acquire(
        state: &StateStore,
        root: &Path,
        config: &Value,
        observed: &Value,
        generation: Option<&str>,
    ) -> Result<Self> {
        let lock = state.controller_lock(false)?;
        let signals = StopSignals::install()?;
        let token = state.deps.ids.next().replace('-', "");
        let supplied_generation = generation.is_some();
        let generation = generation
            .map(str::to_owned)
            .unwrap_or_else(|| state.deps.ids.next().replace('-', ""));
        {
            let _launch = state.launch_lock()?;
            let mut db = state.connect()?;
            let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
            let shutdown = StateStore::get(&tx, "meta:shutdown", json!({}))?;
            if shutdown["requested_at"] != observed["requested_at"]
                || (observed.get("generation").is_some()
                    && shutdown["generation"] != observed["generation"])
                || (supplied_generation
                    && (shutdown["generation"] != generation || shutdown["requested"] == true))
            {
                return Err(Error::new(
                    "interrupted",
                    "Controller startup cancelled by a stop request.",
                ));
            }
            StateStore::put(
                &tx,
                "meta:shutdown",
                &json!({"immediate":false,"generation":generation}),
            )?;
            let mut runtime = config.clone();
            runtime["pid"] = json!(std::process::id());
            runtime["identity"] = json!(ProcessSupervisor::identity(std::process::id() as i32));
            runtime["ownership_token"] = json!(token);
            runtime["started_at"] = json!(state.deps.clock.now());
            runtime["checkout"] = json!(root);
            runtime["generation"] = json!(generation);
            StateStore::put(&tx, "meta:runtime", &runtime)?;
            tx.commit()?;
        }
        Ok(Self {
            state: state.clone(),
            lock,
            stop: StopToken::default(),
            generation,
            _signals: signals,
        })
    }
    pub fn run(
        self,
        mut workflow: Workflow,
        workers: usize,
        interval: Duration,
        once: bool,
    ) -> Result<()> {
        if !(1..=16).contains(&workers) {
            return fail("workers must be an integer between 1 and 16.");
        }
        if self.stop.requested() || self.state.get_meta("shutdown", json!({}))?["requested"] == true
        {
            return Ok(());
        }
        workflow.recover_tasks()?;
        let mut jobs: BTreeMap<String, (i64, JoinHandle<Result<()>>)> = BTreeMap::new();
        loop {
            let finished = jobs
                .iter()
                .filter(|(_, (_, j))| j.is_finished())
                .map(|(s, _)| s.clone())
                .collect::<Vec<_>>();
            for s in finished {
                if let Some((_, j)) = jobs.remove(&s) {
                    if j.join().is_err() {
                        self.state.append_log(
                            None,
                            "controller-error",
                            "Worker thread panicked; assignment requires recovery.",
                        )?;
                    }
                }
            }
            if self.stop.requested()
                || self.state.get_meta("shutdown", json!({}))?["requested"] == true
            {
                break;
            }
            let previous = workflow.board.transport.snapshot()?;
            if !self.state.paused()?
                && jobs.len() < workers + 1
                && workflow.board.transport.check().is_ok()
            {
                let result = (|| -> Result<()> {
                    let mut items = workflow.board.items()?;
                    workflow.stack_items = items.clone();
                    let active = jobs.values().map(|(n, _)| *n).collect::<BTreeSet<_>>();
                    {
                        let _boundary = self.state.dispatch_lock()?;
                        if !self.state.paused()? && !self.stop.requested() {
                            workflow.reconcile_completed(&items, &active)?;
                            workflow.reconcile_waiting(&mut items, &active)?;
                            workflow.poll_human_feedback(&mut items, &active)?;
                            workflow.reconcile_stacks(&mut items, &active)?;
                        }
                    }
                    let slots = (1..=workers)
                        .map(|i| format!("worker-{i}"))
                        .chain(std::iter::once("reviewer".into()))
                        .collect::<Vec<_>>();
                    for slot in slots {
                        if jobs.contains_key(&slot)
                            || self.stop.requested()
                            || self.state.paused()?
                        {
                            continue;
                        }
                        workflow.board.transport.check()?;
                        let _boundary = self.state.dispatch_lock()?;
                        if self.state.paused()? || self.stop.requested() {
                            continue;
                        }
                        let task = workflow.recover(&mut items, &slot)?;
                        let task = if task.is_some() {
                            task
                        } else {
                            let mut found = None;
                            for item in &mut items {
                                let expected = if slot == "reviewer" {
                                    crate::board::REVIEW
                                } else {
                                    crate::board::READY
                                };
                                if string(item, "status").to_lowercase() != expected.to_lowercase()
                                {
                                    continue;
                                }
                                workflow.board.transport.check()?;
                                if self.state.paused()? || self.stop.requested() {
                                    break;
                                }
                                match workflow.pick(item, &slot) {
                                    Ok(t) => found = t,
                                    Err(e) if e.kind == "quota" => return Err(e),
                                    Err(e) => {
                                        let n = item["content"]["number"].as_i64().unwrap_or(0);
                                        let mut t = self.state.load(n)?;
                                        if t.as_object().is_some_and(|o| o.is_empty()) {
                                            t = json!({"number":n,"item":item});
                                        }
                                        t["phase"] = json!("blocked");
                                        t["error"] = json!(e.to_string());
                                        self.state.save(&mut t)?;
                                    }
                                }
                                if found.is_some() {
                                    break;
                                }
                            }
                            found
                        };
                        if let Some(mut task) = task {
                            let n = task["number"].as_i64().unwrap();
                            let id = task["item"]["id"].clone();
                            items.retain(|i| i["id"] != id);
                            let copy = workflow.clone();
                            let review = slot == "reviewer";
                            let fd = self.lock.as_raw_fd();
                            self.state.append_log(
                                None,
                                "controller",
                                &format!(
                                    "{slot}: #{n} {}",
                                    task["issue"]["title"].as_str().unwrap_or_default()
                                ),
                            )?;
                            jobs.insert(
                                slot,
                                (
                                    n,
                                    std::thread::spawn(move || copy.execute(&mut task, review, fd)),
                                ),
                            );
                        }
                    }
                    workflow.board.transport.clear(&previous)?;
                    Ok(())
                })();
                if let Err(e) = result {
                    self.state.append_log(
                        None,
                        "controller-error",
                        &format!("Queue unavailable: {e}"),
                    )?;
                }
            }
            if once {
                break;
            }
            let deadline = Instant::now() + interval;
            while Instant::now() < deadline && !self.stop.requested() {
                if self.state.get_meta("shutdown", json!({}))?["requested"] == true {
                    break;
                }
                std::thread::sleep(
                    Duration::from_millis(50)
                        .min(deadline.saturating_duration_since(Instant::now())),
                );
            }
        }
        // Draining retains the controller lock until every assigned job completes.
        for (_, (_, job)) in jobs {
            if job.join().is_err() {
                self.state.append_log(
                    None,
                    "controller-error",
                    "Worker thread panicked during drain; assignment requires recovery.",
                )?;
            }
        }
        Ok(())
    }
    /// Spawn a native frontend's `run` command; generation is appended, never inferred from PID alone.
    pub fn start(
        state: &StateStore,
        argv: &[String],
        root: &Path,
        observed: &Value,
    ) -> Result<i32> {
        if argv.is_empty() {
            return fail("Empty controller command.");
        }
        {
            let _check = state.controller_lock(false)?;
        }
        let mut child;
        let generation;
        {
            let _launch = state.launch_lock()?;
            let mut db = state.connect()?;
            let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
            let shutdown = StateStore::get(&tx, "meta:shutdown", json!({}))?;
            if shutdown["requested_at"] != observed["requested_at"] {
                return Err(Error::new(
                    "interrupted",
                    "Start cancelled by a concurrent stop request.",
                ));
            }
            generation = state.deps.ids.next().replace('-', "");
            StateStore::put(
                &tx,
                "meta:shutdown",
                &json!({"immediate":false,"generation":generation}),
            )?;
            StateStore::put(
                &tx,
                "meta:control",
                &json!({"paused":false,"updated_at":state.deps.clock.now()}),
            )?;
            tx.commit()?;
            let mut cmd = Command::new(&argv[0]);
            cmd.args(&argv[1..])
                .args(["--generation", &generation])
                .current_dir(root)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null());
            ProcessSupervisor::prepare(&mut cmd, &[]);
            child = cmd.spawn()?;
        }
        let deadline = Instant::now() + Duration::from_secs(10);
        while Instant::now() < deadline {
            if child.try_wait()?.is_some() {
                return fail("Controller exited; see worklane logs.");
            }
            let runtime = state.get_meta("runtime", json!({}))?;
            if runtime["pid"].as_u64() == Some(child.id() as u64)
                && runtime["generation"] == generation
            {
                return Ok(child.id() as i32);
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        fail("Controller is still starting; check worklane logs before starting again.")
    }
    pub fn stop(state: &StateStore, immediate: bool) -> Result<Value> {
        let shutdown = state.request_stop(immediate)?;
        let runtime = state.get_meta("runtime", json!({}))?;
        let controller = ProcessRef {
            pid: runtime["pid"].as_i64().unwrap_or(0) as i32,
            birth: string(&runtime, "identity"),
        };
        let (roots, tokens, processes) = {
            let _launch = state.launch_lock()?;
            let tasks = state.all()?;
            let roots = tasks
                .iter()
                .map(|t| ProcessRef {
                    pid: t["child_pid"].as_i64().unwrap_or(0) as i32,
                    birth: string(t, "child_identity"),
                })
                .collect::<Vec<_>>();
            let mut tokens = vec![string(&runtime, "ownership_token")];
            tokens.extend(tasks.iter().map(|t| string(t, "ownership_token")));
            let mut all = roots.clone();
            all.push(controller.clone());
            let processes = ProcessSupervisor::owned(&all, &tokens, &Processes::new());
            (roots, tokens, processes)
        };
        ProcessSupervisor::signal_owned(
            &Processes::from([(controller.pid, controller.birth.clone())]),
            libc::SIGTERM,
        )?;
        if shutdown["immediate"] != true {
            return Ok(json!({"draining":true}));
        }
        let mut remaining = ProcessSupervisor::terminate_owned(
            &roots,
            &tokens,
            Duration::from_secs(2),
            &Processes::new(),
        )?;
        let deadline = Instant::now() + Duration::from_secs(3);
        while (!ProcessSupervisor::owned(std::slice::from_ref(&controller), &[], &Processes::new())
            .is_empty()
            || state.running()?)
            && Instant::now() < deadline
        {
            std::thread::sleep(Duration::from_millis(50));
        }
        remaining.extend(ProcessSupervisor::terminate_owned(
            &[controller],
            &tokens,
            Duration::ZERO,
            &processes,
        )?);
        remaining.extend(
            ProcessSupervisor::owned(&[], &[], &processes)
                .keys()
                .copied(),
        );
        remaining.sort();
        remaining.dedup();
        let result = json!({"at":state.deps.clock.now(),"remaining_pids":remaining,"running":state.running()?});
        state.set_meta("stop_result", &result)?;
        if !remaining.is_empty() || state.running()? {
            return fail(format!(
                "Immediate stop incomplete; remaining PIDs: {remaining:?}. Inspect status before starting."
            ));
        }
        Ok(result)
    }
}
/// Line-buffered persistent controller output with an optional frontend tee.
pub struct DatabaseLogWriter {
    pub state: StateStore,
    pub stream: String,
    pending: Vec<u8>,
    tee: Option<Box<dyn Write + Send>>,
}
impl DatabaseLogWriter {
    pub fn new(state: StateStore, stream: &str, tee: Option<Box<dyn Write + Send>>) -> Self {
        Self {
            state,
            stream: stream.into(),
            pending: Vec::new(),
            tee,
        }
    }
    fn persist(&self, bytes: &[u8]) -> std::io::Result<()> {
        self.state
            .append_log(None, &self.stream, &String::from_utf8_lossy(bytes))
            .map_err(std::io::Error::other)
    }
}
impl Write for DatabaseLogWriter {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        if let Some(t) = &mut self.tee {
            t.write_all(buf)?;
        }
        self.pending.extend_from_slice(buf);
        while let Some(i) = self.pending.iter().position(|b| *b == b'\n') {
            let line = self.pending.drain(..=i).collect::<Vec<_>>();
            self.persist(&line[..line.len() - 1])?;
        }
        if self.pending.len() > 65536 {
            let chunk = std::mem::take(&mut self.pending);
            self.persist(&chunk)?;
        }
        Ok(buf.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        if !self.pending.is_empty() {
            let bytes = std::mem::take(&mut self.pending);
            self.persist(&bytes)?;
        }
        if let Some(t) = &mut self.tee {
            t.flush()?;
        }
        Ok(())
    }
}
impl Drop for DatabaseLogWriter {
    fn drop(&mut self) {
        let _ = self.flush();
    }
}
