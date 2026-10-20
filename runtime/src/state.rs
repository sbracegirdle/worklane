use crate::process::{LockGuard, ProcessRef, ProcessSupervisor};
use crate::unicode::CaseFold;
use crate::{Dependencies, Error, Result, fail, role, string};
use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params, types::ValueRef};
use serde_json::{Value, json};
use std::{
    fs,
    os::unix::fs::DirBuilderExt,
    path::{Path, PathBuf},
    time::Duration,
};

#[derive(Clone)]
pub struct StateStore {
    pub repo: String,
    pub path: PathBuf,
    pub database: PathBuf,
    pub deps: Dependencies,
    pub busy_timeout: Duration,
}
const SCHEMA: &[&str] = &[
    "CREATE TABLE IF NOT EXISTS tasks(number INTEGER PRIMARY KEY,phase TEXT NOT NULL,updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS state_values(owner TEXT NOT NULL,node INTEGER NOT NULL,parent INTEGER,position INTEGER,key TEXT,kind TEXT NOT NULL,text_value TEXT,integer_value INTEGER,real_value REAL,PRIMARY KEY(owner,node))",
    "CREATE TABLE IF NOT EXISTS sessions(number INTEGER NOT NULL REFERENCES tasks(number),role TEXT NOT NULL,session_id TEXT NOT NULL UNIQUE,backend TEXT NOT NULL,model TEXT NOT NULL,worktree TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(number,role))",
    "CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,number INTEGER NOT NULL REFERENCES tasks(number),role TEXT NOT NULL,stage TEXT NOT NULL,session_id TEXT,status TEXT NOT NULL,started_at TEXT NOT NULL,finished_at TEXT,pid INTEGER,identity TEXT,worktree TEXT NOT NULL,error TEXT)",
    "CREATE INDEX IF NOT EXISTS runs_task ON runs(number,started_at)",
    "CREATE TABLE IF NOT EXISTS run_events(id INTEGER PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(id),type TEXT NOT NULL,created_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY,run_id TEXT REFERENCES runs(id),stream TEXT NOT NULL,content TEXT NOT NULL,created_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS migration_archive(path TEXT PRIMARY KEY,content BLOB NOT NULL)",
];
impl StateStore {
    pub const VERSION: i64 = 2;
    pub fn open(repo: &str, root: Option<&Path>) -> Result<Self> {
        Self::with_dependencies(repo, root, Dependencies::default())
    }
    pub fn with_dependencies(repo: &str, root: Option<&Path>, deps: Dependencies) -> Result<Self> {
        if repo
            .split('/')
            .any(|s| s.is_empty() || s == "." || s == ".." || s.contains('\\'))
            || repo.matches('/').count() > 1
        {
            return fail("Invalid repository identity.");
        }
        let base = root
            .map(Path::to_path_buf)
            .or_else(|| std::env::var_os("XDG_STATE_HOME").map(PathBuf::from))
            .unwrap_or_else(|| {
                PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".local/state")
            });
        let path = base
            .join("worklane")
            .join(repo.casefold().replace('/', "--"));
        fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(&path)?;
        let state = Self {
            repo: repo.into(),
            database: path.join("state.sqlite3"),
            path,
            deps,
            busy_timeout: Duration::from_secs(30),
        };
        let db = state.connect()?;
        let version: i64 = db.pragma_query_value(None, "user_version", |r| r.get(0))?;
        if version > Self::VERSION {
            return fail("This state database needs a newer Worklane version.");
        }
        drop(db);
        if version != Self::VERSION {
            let _lock = state.controller_lock(false)?;
            state.upgrade()?;
        }
        state.remove_archived_files()?;
        Ok(state)
    }
    pub fn connect(&self) -> Result<Connection> {
        let db = Connection::open(&self.database)?;
        db.busy_timeout(self.busy_timeout)?;
        db.pragma_update(None, "foreign_keys", true)?;
        db.pragma_update(None, "synchronous", "FULL")?;
        Ok(db)
    }
    pub fn controller_lock(&self, blocking: bool) -> Result<LockGuard> {
        ProcessSupervisor::lock(&self.path.join("runner.lock"), blocking)
    }
    pub fn launch_lock(&self) -> Result<LockGuard> {
        ProcessSupervisor::lock(&self.path.join("launch.lock"), true)
    }
    pub fn dispatch_lock(&self) -> Result<LockGuard> {
        ProcessSupervisor::lock(&self.path.join("dispatch.lock"), true)
    }
    pub fn running(&self) -> Result<bool> {
        match self.controller_lock(false) {
            Ok(_) => Ok(false),
            Err(e) if e.kind == "busy" => Ok(true),
            Err(e) => Err(e),
        }
    }
    pub fn put(db: &Connection, owner: &str, value: &Value) -> Result<()> {
        db.execute("DELETE FROM state_values WHERE owner=?", [owner])?;
        #[allow(clippy::too_many_arguments)]
        fn visit(
            db: &Connection,
            owner: &str,
            value: &Value,
            node: &mut i64,
            parent: Option<i64>,
            position: i64,
            key: Option<&str>,
            depth: usize,
        ) -> Result<()> {
            if depth > 256 || *node > 1_000_000 {
                return fail("State value exceeds nesting or node limit.");
            }
            let id = *node;
            *node += 1;
            let (kind, text, int, real) = match value {
                Value::Null => ("null", None, None, None),
                Value::Bool(b) => ("bool", None, Some(*b as i64), None),
                Value::String(s) => ("str", Some(s.as_str()), None, None),
                Value::Number(n) if n.is_i64() || n.is_u64() => (
                    "int",
                    None,
                    Some(n.as_i64().ok_or_else(|| {
                        Error::new("validation", "Integer exceeds SQLite range.")
                    })?),
                    None,
                ),
                Value::Number(n) if n.is_f64() => ("float", None, None, n.as_f64()),
                Value::Number(_) => return fail("Integer exceeds SQLite range."),
                Value::Array(_) => ("list", None, None, None),
                Value::Object(_) => ("dict", None, None, None),
            };
            db.execute(
                "INSERT INTO state_values VALUES (?,?,?,?,?,?,?,?,?)",
                params![owner, id, parent, position, key, kind, text, int, real],
            )?;
            match value {
                Value::Array(a) => {
                    for (i, v) in a.iter().enumerate() {
                        visit(db, owner, v, node, Some(id), i as i64, None, depth + 1)?
                    }
                }
                Value::Object(o) => {
                    for (i, (k, v)) in o.iter().enumerate() {
                        visit(db, owner, v, node, Some(id), i as i64, Some(k), depth + 1)?
                    }
                }
                _ => (),
            }
            Ok(())
        }
        visit(db, owner, value, &mut 0, None, 0, None, 0)
    }
    pub fn get(db: &Connection, owner: &str, default: Value) -> Result<Value> {
        struct Node {
            parent: Option<i64>,
            key: Option<String>,
            value: Value,
        }
        let mut stmt=db.prepare("SELECT node,parent,key,kind,text_value,integer_value,real_value FROM state_values WHERE owner=? ORDER BY node")?;
        let mut rows = stmt.query([owner])?;
        let mut nodes = Vec::new();
        while let Some(r) = rows.next()? {
            let id: i64 = r.get(0)?;
            if id != nodes.len() as i64 || nodes.len() > 1_000_000 {
                return fail("Corrupt typed state node sequence.");
            }
            let kind: String = r.get(3)?;
            let value = match kind.as_str() {
                "dict" => json!({}),
                "list" => json!([]),
                "str" => json!(r.get::<_, String>(4)?),
                "bool" => json!(r.get::<_, i64>(5)? != 0),
                "int" => json!(r.get::<_, i64>(5)?),
                "float" => json!(r.get::<_, f64>(6)?),
                "null" => Value::Null,
                _ => return fail("Unknown typed state kind."),
            };
            nodes.push(Node {
                parent: r.get(1)?,
                key: r.get(2)?,
                value,
            });
        }
        if nodes.is_empty() {
            return Ok(default);
        }
        // Attach in reverse postorder, preserving original child order by inserting at the front.
        for i in (1..nodes.len()).rev() {
            let value = std::mem::take(&mut nodes[i].value);
            let parent = nodes[i]
                .parent
                .ok_or_else(|| Error::new("sqlite", "Missing state parent."))?
                as usize;
            if parent >= i {
                return fail("Corrupt typed state parent.");
            }
            let key = nodes[i].key.clone();
            match &mut nodes[parent].value {
                Value::Array(a) => a.insert(0, value),
                Value::Object(o) => {
                    o.shift_insert(
                        0,
                        key.ok_or_else(|| Error::new("sqlite", "Missing state key."))?,
                        value,
                    );
                }
                _ => return fail("Corrupt typed state container."),
            }
        }
        Ok(std::mem::take(&mut nodes[0].value))
    }
    fn save_db(&self, db: &Connection, task: &Value) -> Result<()> {
        let n = task["number"]
            .as_i64()
            .ok_or_else(|| Error::new("validation", "Task number must be an integer."))?;
        db.execute("INSERT INTO tasks VALUES (?,?,?) ON CONFLICT(number) DO UPDATE SET phase=excluded.phase,updated_at=excluded.updated_at",params![n,task["phase"].as_str().unwrap_or("unknown"),self.deps.clock.now()])?;
        let mut task = task.clone();
        task.as_object_mut()
            .ok_or_else(|| Error::new("validation", "Task must be an object."))?
            .remove("sessions");
        Self::put(db, &format!("task:{n}"), &task)
    }
    pub fn save(&self, task: &mut Value) -> Result<()> {
        task["updated_at"] = json!(self.deps.clock.now());
        let mut db = self.connect()?;
        let tx = db.transaction()?;
        self.save_db(&tx, task)?;
        tx.commit()?;
        Ok(())
    }
    pub fn load(&self, n: i64) -> Result<Value> {
        let db = self.connect()?;
        let mut task = Self::get(&db, &format!("task:{n}"), json!({}))?;
        if task.as_object().is_some_and(|o| !o.is_empty()) {
            let rows = query(
                &db,
                "SELECT role,session_id,backend,worktree,model FROM sessions WHERE number=?",
                [n],
            )?;
            let mut sessions = json!({});
            for row in rows {
                let key = string(&row, "role");
                sessions[key] = row;
            }
            task["sessions"] = sessions;
        }
        Ok(task)
    }
    /// Journal fresh assignment intent separately from the previous invocation.
    pub fn save_assignment(&self, task: &mut Value) -> Result<()> {
        task["updated_at"] = json!(self.deps.clock.now());
        let mut db = self.connect()?;
        let tx = db.transaction()?;
        self.save_db(&tx, task)?;
        for stage in ["build", "review", "finalize"] {
            Self::put(
                &tx,
                &format!("meta:assignment:{}:{stage}", task["number"]),
                &json!({"pending":true,"prior_run_id":task["active_run_id"]}),
            )?;
        }
        tx.commit()?;
        Ok(())
    }
    pub fn save_turn(&self, task: &mut Value, stage: &str) -> Result<()> {
        role(stage)?;
        task["updated_at"] = json!(self.deps.clock.now());
        let mut db = self.connect()?;
        let tx = db.transaction()?;
        self.save_db(&tx, task)?;
        let owner = format!("meta:assignment:{}:{stage}", task["number"]);
        let mut boundary = Self::get(&tx, &owner, json!({}))?;
        boundary["run_id"] = task["active_run_id"].clone();
        boundary["consumed"] = json!(true);
        Self::put(&tx, &owner, &boundary)?;
        tx.commit()?;
        Ok(())
    }
    /// Reuse a committed invocation if the controller died before task handoff.
    pub fn committed_turn(&self, task: &Value, stage: &str, tree: &Path) -> Result<Option<Value>> {
        let Some(id) = task["active_run_id"].as_str() else {
            return Ok(None);
        };
        let db = self.connect()?;
        let Some(run) = query(&db, "SELECT * FROM runs WHERE id=?", [id])?.pop() else {
            return Ok(None);
        };
        if run["number"] != task["number"]
            || run["stage"] != stage
            || run["role"] != role(stage)?
            || run["status"] != "completed"
        {
            return Ok(None);
        }
        let mapping = query(
            &db,
            "SELECT session_id FROM sessions WHERE number=? AND role=?",
            params![task["number"].as_i64(), role(stage)?],
        )?
        .pop();
        if run["session_id"].as_str().is_none_or(str::is_empty)
            || mapping
                .as_ref()
                .is_none_or(|s| s["session_id"] != run["session_id"])
        {
            return fail(
                "Completed turn has no matching saved session; inspect its history before handoff.",
            );
        }
        let boundary = Self::get(
            &db,
            &format!("meta:assignment:{}:{stage}", task["number"]),
            Value::Null,
        )?;
        if boundary["pending"] == true && boundary["prior_run_id"] == id {
            return Ok(None);
        }
        if boundary["run_id"] == id && boundary["consumed"] == true {
            return Ok(None);
        }
        let times = task["started_at"]
            .as_str()
            .zip(run["started_at"].as_str())
            .and_then(|(a, b)| {
                chrono::DateTime::parse_from_rfc3339(a)
                    .ok()
                    .zip(chrono::DateTime::parse_from_rfc3339(b).ok())
            });
        if times.is_some_and(|(a, b)| b < a) {
            return Ok(None);
        }
        let known_boundary = boundary["run_id"] == id;
        let timed_boundary = task["started_at"]
            .as_str()
            .zip(run["started_at"].as_str())
            .is_some_and(|(a, b)| {
                chrono::DateTime::parse_from_rfc3339(a)
                    .ok()
                    .zip(chrono::DateTime::parse_from_rfc3339(b).ok())
                    .is_some_and(|(a, b)| b >= a)
            });
        if !known_boundary && !timed_boundary {
            return fail(
                "Completed turn has no verified assignment boundary; inspect its saved result before retrying.",
            );
        }
        let saved = PathBuf::from(string(&run, "worktree"));
        if saved.canonicalize()? != tree.canonicalize()? {
            return fail("Completed turn belongs to another worktree; inspect before handoff.");
        }
        let result = Self::get(&db, &format!("result:{id}"), Value::Null)?;
        crate::harness::validate_result(&result)?;
        let result_key = match stage {
            "build" => "build_result",
            "review" => "review_result",
            _ => "handoff_result",
        };
        if !known_boundary && task[result_key] == result {
            return fail(
                "Completed turn may already have been consumed; inspect its saved result before retrying.",
            );
        }
        Ok(Some(result))
    }
    pub fn all(&self) -> Result<Vec<Value>> {
        let db = self.connect()?;
        let mut stmt = db.prepare("SELECT number FROM tasks ORDER BY number")?;
        let numbers = stmt
            .query_map([], |r| r.get::<_, i64>(0))?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        numbers.into_iter().map(|n| self.load(n)).collect()
    }
    pub fn get_meta(&self, key: &str, default: Value) -> Result<Value> {
        Self::get(&self.connect()?, &format!("meta:{key}"), default)
    }
    pub fn set_meta(&self, key: &str, value: &Value) -> Result<()> {
        let mut db = self.connect()?;
        let tx = db.transaction()?;
        Self::put(&tx, &format!("meta:{key}"), value)?;
        tx.commit()?;
        Ok(())
    }
    pub fn paused(&self) -> Result<bool> {
        Ok(self.get_meta("control", json!({}))?["paused"]
            .as_bool()
            .unwrap_or(false))
    }
    pub fn pause(&self, value: bool) -> Result<()> {
        let _boundary = self.dispatch_lock()?;
        let mut db = self.connect()?;
        let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        if !value
            && self.running()?
            && Self::get(&tx, "meta:shutdown", json!({}))?["requested"] == true
        {
            return fail("Controller is stopping; wait for stopped status, then start.");
        }
        Self::put(
            &tx,
            "meta:control",
            &json!({"paused":value,"updated_at":self.deps.clock.now()}),
        )?;
        tx.commit()?;
        Ok(())
    }
    pub fn request_stop(&self, immediate: bool) -> Result<Value> {
        let mut db = self.connect()?;
        let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let mut previous = Self::get(&tx, "meta:shutdown", json!({}))?;
        let now = self.deps.clock.now();
        Self::put(
            &tx,
            "meta:control",
            &json!({"paused":true,"updated_at":now}),
        )?;
        let immediate = immediate || previous["immediate"].as_bool().unwrap_or(false);
        previous["requested"] = json!(true);
        previous["immediate"] = json!(immediate);
        previous["requested_at"] = json!(now);
        Self::put(&tx, "meta:shutdown", &previous)?;
        tx.commit()?;
        Ok(previous)
    }
    pub fn session(&self, n: i64, role: &str) -> Result<Option<Value>> {
        Ok(self.load(n)?["sessions"].get(role).cloned())
    }
    pub fn runs(&self, n: Option<i64>) -> Result<Vec<Value>> {
        let db = self.connect()?;
        let mut rows = query(
            &db,
            "SELECT * FROM runs WHERE (?1 IS NULL OR number=?1) ORDER BY started_at",
            [n],
        )?;
        for row in &mut rows {
            row["result"] = Self::get(&db, &format!("result:{}", string(row, "id")), Value::Null)?;
        }
        Ok(rows)
    }
    pub fn begin_run(&self, task: &mut Value, stage: &str, tree: &Path) -> Result<String> {
        let id = self.deps.ids.next();
        let role = role(stage)?;
        let session = self.session(task["number"].as_i64().unwrap_or(0), role)?;
        task.as_object_mut().unwrap().remove("log");
        task["active_stage"] = json!(stage);
        task["active_run_id"] = json!(id);
        task["log_run_id"] = json!(id);
        let mut db = self.connect()?;
        let tx = db.transaction()?;
        self.save_db(&tx, task)?;
        Self::put(
            &tx,
            &format!("meta:assignment:{}:{stage}", task["number"]),
            &json!({"run_id":id}),
        )?;
        tx.execute("INSERT INTO runs(id,number,role,stage,session_id,status,started_at,worktree) VALUES (?,?,?,?,?,'starting',?,?)",params![id,task["number"].as_i64(),role,stage,session.as_ref().and_then(|v|v["session_id"].as_str()),self.deps.clock.now(),tree.to_string_lossy()])?;
        tx.commit()?;
        Ok(id)
    }
    pub fn attach_process(&self, id: &str, task: &mut Value, process: &ProcessRef) -> Result<()> {
        task["child_pid"] = json!(process.pid);
        task["child_identity"] = json!(process.birth);
        let mut db = self.connect()?;
        let tx = db.transaction()?;
        self.save_db(&tx, task)?;
        tx.execute(
            "UPDATE runs SET pid=?,identity=?,status='running' WHERE id=?",
            params![process.pid, process.birth, id],
        )?;
        tx.commit()?;
        Ok(())
    }
    fn attach_session_db(
        &self,
        db: &Connection,
        id: &str,
        session: &str,
        model: &str,
        backend: &str,
    ) -> Result<()> {
        let session = crate::harness::validate_session_id(backend, session)?;
        let run = query(db, "SELECT * FROM runs WHERE id=?", [id])?
            .pop()
            .ok_or_else(|| Error::new("not_found", "Run not found."))?;
        let old: Option<(String, String)> = db
            .query_row(
                "SELECT session_id,backend FROM sessions WHERE number=? AND role=?",
                params![run["number"].as_i64(), run["role"].as_str()],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()?;
        if old.is_some_and(|(s, b)| s != session || b != backend) {
            return fail(
                "Harness returned a different session ID; refusing to replace the saved conversation.",
            );
        }
        let stamp = self.deps.clock.now();
        db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(number,role) DO UPDATE SET updated_at=excluded.updated_at,worktree=excluded.worktree",params![run["number"].as_i64(),run["role"].as_str(),session,backend,model,run["worktree"].as_str(),stamp,stamp])?;
        db.execute(
            "UPDATE runs SET session_id=? WHERE id=?",
            params![session, id],
        )?;
        Ok(())
    }
    pub fn attach_session(
        &self,
        id: &str,
        session: &str,
        model: &str,
        backend: &str,
    ) -> Result<()> {
        let mut db = self.connect()?;
        let tx = db.transaction()?;
        self.attach_session_db(&tx, id, session, model, backend)?;
        tx.commit()?;
        Ok(())
    }
    fn log_db(&self, db: &Connection, id: Option<&str>, stream: &str, content: &str) -> Result<()> {
        db.execute(
            "INSERT INTO logs(run_id,stream,content,created_at) VALUES (?,?,?,?)",
            params![id, stream, content, self.deps.clock.now()],
        )?;
        Ok(())
    }
    fn event_db(&self, db: &Connection, id: &str, event: &Value) -> Result<()> {
        db.execute(
            "INSERT INTO run_events(run_id,type,created_at) VALUES (?,?,?)",
            params![
                id,
                event["type"].as_str().unwrap_or("unknown"),
                self.deps.clock.now()
            ],
        )?;
        Self::put(db, &format!("event:{}", db.last_insert_rowid()), event)?;
        self.log_db(db, Some(id), "agent", &crate::harness::event_text(event))
    }
    pub fn record_events(
        &self,
        id: &str,
        events: &[Value],
        model: &str,
        backend: &str,
        wire: Option<&Value>,
    ) -> Result<()> {
        let mut db = self.connect()?;
        let tx = db.transaction()?;
        if let Some(wire) = wire {
            tx.execute(
                "INSERT INTO run_events(run_id,type,created_at) VALUES (?,?,?)",
                params![
                    id,
                    format!("wire:{}", wire["type"].as_str().unwrap_or("unknown")),
                    self.deps.clock.now()
                ],
            )?;
            Self::put(&tx, &format!("event:{}", tx.last_insert_rowid()), wire)?;
        }
        for event in events {
            self.event_db(&tx, id, event)?;
            if event["type"] == "thread.started" {
                self.attach_session_db(
                    &tx,
                    id,
                    event["thread_id"]
                        .as_str()
                        .ok_or_else(|| Error::new("validation", "Missing thread ID."))?,
                    model,
                    backend,
                )?;
            }
        }
        tx.commit()?;
        Ok(())
    }
    pub fn append_log(&self, id: Option<&str>, stream: &str, text: &str) -> Result<()> {
        self.log_db(&self.connect()?, id, stream, text)
    }
    /// Keyset pagination keeps long-running histories bounded without dropping older entries.
    pub fn logs_page(&self, n: Option<i64>, after: i64, limit: usize) -> Result<Vec<Value>> {
        query(
            &self.connect()?,
            "SELECT logs.* FROM logs LEFT JOIN runs ON logs.run_id=runs.id WHERE (?1 IS NULL OR runs.number=?1) AND logs.id>?2 ORDER BY logs.id LIMIT ?3",
            params![n, after, limit.clamp(1, 1000) as i64],
        )
    }
    pub fn events_page(&self, id: &str, after: i64, limit: usize) -> Result<Vec<(i64, Value)>> {
        let db = self.connect()?;
        let mut stmt =
            db.prepare("SELECT id FROM run_events WHERE run_id=? AND id>? ORDER BY id LIMIT ?")?;
        let ids = stmt
            .query_map(params![id, after, limit.clamp(1, 1000) as i64], |r| {
                r.get::<_, i64>(0)
            })?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        ids.into_iter()
            .map(|n| Ok((n, Self::get(&db, &format!("event:{n}"), Value::Null)?)))
            .collect()
    }
    pub fn finish_run(
        &self,
        id: &str,
        status: &str,
        error: Option<&str>,
        result: Option<&Value>,
    ) -> Result<()> {
        let mut db = self.connect()?;
        let tx = db.transaction()?;
        tx.execute(
            "UPDATE runs SET status=?,finished_at=?,error=? WHERE id=?",
            params![status, self.deps.clock.now(), error, id],
        )?;
        if let Some(result) = result {
            Self::put(&tx, &format!("result:{id}"), result)?;
        }
        tx.commit()?;
        Ok(())
    }
    pub fn queue_input(&self, id: &str, text: &str) -> Result<()> {
        let text = crate::unicode::trim(text);
        if text.is_empty() || text.chars().count() > 8000 {
            return fail("Enter between 1 and 8000 characters.");
        }
        if !self.running()? {
            return fail("Worklane is stopped; use a to resume the saved conversation.");
        }
        let mut db = self.connect()?;
        let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let run = query(&tx, "SELECT * FROM runs WHERE id=?", [id])?
            .pop()
            .unwrap_or(Value::Null);
        let task = Self::get(&tx, &format!("task:{}", run["number"]), json!({}))?;
        if !matches!(run["status"].as_str(), Some("starting" | "running"))
            || run["session_id"].as_str().unwrap_or_default().is_empty()
            || task["active_run_id"] != id
        {
            return fail(
                "Session is no longer accepting input; refresh or use a after stopping Worklane.",
            );
        }
        let owner = format!("input:{}", string(&run, "session_id"));
        let mut inputs = Self::get(&tx, &owner, json!([]))?;
        let a = inputs
            .as_array_mut()
            .ok_or_else(|| Error::new("sqlite", "Invalid input queue."))?;
        if a.len() >= 20 {
            return fail("Session input queue is full; wait for the next turn.");
        }
        a.push(json!({"text":text,"status":"pending"}));
        Self::put(&tx, &owner, &inputs)?;
        self.log_db(&tx, Some(id), "user", text)?;
        self.log_db(
            &tx,
            Some(id),
            "input",
            "Input queued for the next turn, before workflow handoff.",
        )?;
        tx.commit()?;
        Ok(())
    }
    pub fn advance_run(&self, id: &str, result: Option<&Value>) -> Result<Vec<String>> {
        let mut db = self.connect()?;
        let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let run = query(&tx, "SELECT * FROM runs WHERE id=?", [id])?
            .pop()
            .ok_or_else(|| Error::new("not_found", "Run not found."))?;
        let owner = run["session_id"].as_str().map(|s| format!("input:{s}"));
        let mut inputs = if let Some(o) = &owner {
            Self::get(&tx, o, json!([]))?
        } else {
            json!([])
        };
        let a = inputs
            .as_array_mut()
            .ok_or_else(|| Error::new("sqlite", "Invalid input queue."))?;
        if result.is_some() {
            if a.iter().any(|v| v["status"] == "sending") {
                self.log_db(&tx, Some(id), "input", "Queued input turn completed.")?;
            }
            a.retain(|v| v["status"] != "sending");
        }
        if !a.is_empty() {
            for v in a.iter_mut() {
                v["status"] = json!("sending");
            }
            self.log_db(
                &tx,
                Some(id),
                "input",
                &format!(
                    "Delivering {} queued request(s) to the saved session.",
                    a.len()
                ),
            )?;
        } else if let Some(result) = result {
            tx.execute(
                "UPDATE runs SET status='completed',finished_at=? WHERE id=?",
                params![self.deps.clock.now(), id],
            )?;
            Self::put(&tx, &format!("result:{id}"), result)?;
        }
        let texts = a.iter().map(|v| string(v, "text")).collect();
        if let Some(owner) = owner {
            Self::put(&tx, &owner, &inputs)?;
        }
        tx.commit()?;
        Ok(texts)
    }
    pub fn recover_runs(&self) -> Result<()> {
        for run in self.runs(None)? {
            if matches!(run["status"].as_str(), Some("starting" | "running")) {
                self.finish_run(
                    &string(&run, "id"),
                    "interrupted",
                    Some("Controller exited before invocation completed."),
                    None,
                )?;
            }
        }
        Ok(())
    }
    pub fn backup(&self, target: &Path) -> Result<()> {
        let db = self.connect()?;
        db.backup(rusqlite::MAIN_DB, target, None)?;
        Ok(())
    }
    fn upgrade(&self) -> Result<()> {
        let mut files = Vec::new();
        fn collect(path: &Path, files: &mut Vec<PathBuf>) -> Result<()> {
            if let Ok(entries) = fs::read_dir(path) {
                for e in entries {
                    let e = e?;
                    let t = e.file_type()?;
                    if t.is_dir() {
                        collect(&e.path(), files)?;
                    } else if t.is_file() {
                        files.push(e.path());
                    }
                }
            }
            Ok(())
        }
        if let Ok(entries) = fs::read_dir(self.path.join("tasks")) {
            for e in entries {
                let e = e?;
                if e.file_type()?.is_file() && e.path().extension().is_some_and(|s| s == "json") {
                    files.push(e.path());
                }
            }
        }
        for name in ["runtime.json", "control.json", "controller.log"] {
            let p = self.path.join(name);
            if p.symlink_metadata().is_ok_and(|m| m.is_file()) {
                files.push(p);
            }
        }
        collect(&self.path.join("logs"), &mut files)?;
        files.sort();
        let mut db = self.connect()?;
        let version: i64 = db.pragma_query_value(None, "user_version", |r| r.get(0))?;
        if version == Self::VERSION {
            return Ok(());
        }
        db.pragma_update(None, "journal_mode", "WAL")?;
        db.pragma_update(None, "foreign_keys", false)?;
        let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let columns = query(&tx, "PRAGMA table_info(tasks)", [])?;
        let mixed = columns.iter().any(|r| r["name"] == "payload");
        let mut old = std::collections::BTreeMap::new();
        if mixed {
            for table in ["tasks", "sessions", "runs", "metadata"] {
                old.insert(table, query(&tx, &format!("SELECT * FROM {table}"), [])?);
            }
            for table in ["runs", "sessions", "tasks", "metadata"] {
                tx.execute(&format!("DROP TABLE {table}"), [])?;
            }
        }
        for sql in SCHEMA {
            tx.execute(sql, [])?;
        }
        if mixed {
            for row in &old["tasks"] {
                self.save_db(
                    &tx,
                    &serde_json::from_str::<Value>(&string(row, "payload"))?,
                )?;
            }
            for row in &old["metadata"] {
                Self::put(
                    &tx,
                    &format!("meta:{}", string(row, "key")),
                    &serde_json::from_str::<Value>(&string(row, "value"))?,
                )?;
            }
            for row in &old["sessions"] {
                tx.execute(
                    "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
                    params![
                        row["number"].as_i64(),
                        row["role"].as_str(),
                        row["session_id"].as_str(),
                        row["backend"].as_str(),
                        row["model"].as_str(),
                        row["worktree"].as_str(),
                        row["created_at"].as_str(),
                        row["updated_at"].as_str()
                    ],
                )?;
            }
            for row in &old["runs"] {
                let id = string(row, "id");
                tx.execute(
                    "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    params![
                        id,
                        row["number"].as_i64(),
                        row["role"].as_str(),
                        row["stage"].as_str(),
                        row["session_id"].as_str(),
                        row["status"].as_str(),
                        row["started_at"].as_str(),
                        row["finished_at"].as_str(),
                        row["pid"].as_i64(),
                        row["identity"].as_str(),
                        row["worktree"].as_str(),
                        row["error"].as_str()
                    ],
                )?;
                if let Some(r) = row["result"].as_str().filter(|s| !s.is_empty()) {
                    Self::put(
                        &tx,
                        &format!("result:{id}"),
                        &serde_json::from_str::<Value>(r)?,
                    )?;
                }
                let log = PathBuf::from(string(row, "log"));
                if log.is_file() {
                    for line in String::from_utf8_lossy(&fs::read(&log)?).lines() {
                        match serde_json::from_str::<Value>(line) {
                            Ok(e) => self.event_db(&tx, &id, &e)?,
                            Err(_) => self.log_db(&tx, Some(&id), "stdout", line)?,
                        }
                    }
                }
                let stderr = log.with_extension("stderr.log");
                if stderr.is_file() {
                    self.log_db(
                        &tx,
                        Some(&id),
                        "stderr",
                        &String::from_utf8_lossy(&fs::read(stderr)?),
                    )?;
                }
            }
        } else {
            for file in files
                .iter()
                .filter(|p| p.parent() == Some(self.path.join("tasks").as_path()))
            {
                let mut task: Value = serde_json::from_slice(&fs::read(file)?)?;
                let phase = task["failed_phase"]
                    .as_str()
                    .or_else(|| task["phase"].as_str())
                    .unwrap_or_default();
                let possible = if task["log"].as_str().is_none_or(str::is_empty) {
                    vec![]
                } else {
                    match phase {
                        "building" | "claiming" | "awaiting_review" => vec!["worker"],
                        "reviewing" | "rework" => vec!["worker", "reviewer"],
                        "finalizing" | "human_review" => vec!["worker", "reviewer", "finalizer"],
                        _ => vec![],
                    }
                };
                task["legacy_session_roles"] = json!(possible);
                self.save_db(&tx, &task)?;
                let stage = string(&task, "active_stage");
                if let Ok(role) = role(&stage) {
                    let ids = Self::log_sessions(task["log"].as_str().map(Path::new))?;
                    if ids.len() == 1 {
                        let name = if role == "worker" {
                            task["number"].to_string()
                        } else {
                            format!(
                                "review-{}-{}",
                                task["number"],
                                task["round"].as_i64().unwrap_or(0)
                            )
                        };
                        let tree = self.path.join("worktrees").join(name);
                        let stamp = self.deps.clock.now();
                        tx.execute(
                            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
                            params![
                                task["number"].as_i64(),
                                role,
                                ids[0],
                                "codex",
                                crate::harness::MODEL,
                                tree.to_string_lossy(),
                                stamp,
                                stamp
                            ],
                        )?;
                    }
                }
            }
            for key in ["control", "runtime"] {
                let p = self.path.join(format!("{key}.json"));
                if p.is_file() {
                    Self::put(
                        &tx,
                        &format!("meta:{key}"),
                        &serde_json::from_slice::<Value>(&fs::read(p)?)?,
                    )?;
                }
            }
        }
        for file in &files {
            let bytes = fs::read(file)?;
            tx.execute(
                "INSERT OR IGNORE INTO migration_archive VALUES (?,?)",
                params![
                    file.strip_prefix(&self.path).unwrap().to_string_lossy(),
                    bytes
                ],
            )?;
            if file.file_name().is_some_and(|n| n == "controller.log") {
                self.log_db(&tx, None, "controller", &String::from_utf8_lossy(&bytes))?;
            }
        }
        tx.pragma_update(None, "user_version", Self::VERSION)?;
        if !query(&tx, "PRAGMA foreign_key_check", [])?.is_empty() {
            return fail("State migration failed its foreign key check.");
        }
        tx.commit()?;
        Ok(())
    }
    fn remove_archived_files(&self) -> Result<()> {
        let db = self.connect()?;
        let mut stmt = db.prepare("SELECT path,content FROM migration_archive")?;
        let mut rows = stmt.query([])?;
        while let Some(r) = rows.next()? {
            let path: String = r.get(0)?;
            let relative = Path::new(&path);
            if relative.is_absolute()
                || relative
                    .components()
                    .any(|c| matches!(c, std::path::Component::ParentDir))
            {
                return fail("Invalid archived migration path.");
            }
            let file = self.path.join(relative);
            if file.symlink_metadata().is_ok_and(|m| m.is_file()) {
                if !file.canonicalize()?.starts_with(self.path.canonicalize()?) {
                    return fail(
                        "Archived migration path leaves the state directory; inspect before cleanup.",
                    );
                }
                let archived: Vec<u8> = r.get(1)?;
                if fs::read(&file)? != archived {
                    return fail(format!(
                        "Legacy file changed after migration; inspect {} before removing it.",
                        file.display()
                    ));
                }
                fs::remove_file(file)?;
            }
        }
        Ok(())
    }
    pub fn log_sessions(path: Option<&Path>) -> Result<Vec<String>> {
        let mut ids = Vec::new();
        if let Some(p) = path.filter(|p| p.is_file()) {
            for line in String::from_utf8_lossy(&fs::read(p)?).lines() {
                if let Ok(event) = serde_json::from_str::<Value>(line) {
                    if event["type"] == "thread.started" {
                        if let Some(s) = event["thread_id"].as_str().filter(|s| !s.is_empty()) {
                            let id = crate::harness::validate_session_id("codex", s)?;
                            if !ids.contains(&id) {
                                ids.push(id);
                            }
                        }
                    }
                }
            }
        }
        Ok(ids)
    }
}
pub fn query<P: rusqlite::Params>(db: &Connection, sql: &str, params: P) -> Result<Vec<Value>> {
    let mut stmt = db.prepare(sql)?;
    let names: Vec<_> = stmt.column_names().into_iter().map(str::to_owned).collect();
    let mut rows = stmt.query(params)?;
    let mut result = Vec::new();
    while let Some(r) = rows.next()? {
        let mut v = json!({});
        for (i, n) in names.iter().enumerate() {
            v[n] = match r.get_ref(i)? {
                ValueRef::Null => Value::Null,
                ValueRef::Integer(n) => json!(n),
                ValueRef::Real(n) => json!(n),
                ValueRef::Text(s) => json!(String::from_utf8_lossy(s)),
                ValueRef::Blob(_) => return fail("Blob requires explicit archive access."),
            };
        }
        result.push(v);
    }
    Ok(result)
}
