use crate::{
    Error, Result,
    harness::readable_log,
    role,
    state::{StateStore, query},
    string,
    unicode::{casefold, cell_width, plain},
};
use rusqlite::{Connection, functions::FunctionFlags, params};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};
const GROUPS: &[&str] = &[
    "running",
    "blocked",
    "failed",
    "interrupted",
    "retry",
    "awaiting_review",
    "human_review",
    "completed",
    "queued",
];
fn rank(s: &str) -> usize {
    GROUPS.iter().position(|v| *v == s).unwrap_or(GROUPS.len())
}
fn rrole(task: &Value) -> &str {
    role(
        task["active_stage"]
            .as_str()
            .or_else(|| task["resume_stage"].as_str())
            .unwrap_or("build"),
    )
    .unwrap_or("worker")
}
fn functions(db: &Connection) -> Result<()> {
    let flags = FunctionFlags::SQLITE_UTF8 | FunctionFlags::SQLITE_DETERMINISTIC;
    db.create_scalar_function("readable_log", 2, flags, |ctx| {
        Ok(readable_log(&ctx.get::<String>(0)?, &ctx.get::<String>(1)?))
    })?;
    db.create_scalar_function("search_text", 2, flags, |ctx| {
        Ok(casefold(&plain(&readable_log(
            &ctx.get::<String>(0)?,
            &ctx.get::<String>(1)?,
        ))))
    })?;
    Ok(())
}
#[derive(Clone)]
pub struct ReadModel {
    pub state: StateStore,
}
impl ReadModel {
    pub fn new(state: StateStore) -> Self {
        Self { state }
    }
    pub fn pr_status(
        &self,
        transport: &crate::transport::GitHubTransport,
        task: &Value,
    ) -> Result<(Value, String)> {
        let reference = task["pr_url"]
            .as_str()
            .filter(|s| !s.is_empty())
            .or_else(|| task["branch"].as_str())
            .unwrap_or_default();
        if reference.is_empty() {
            return Ok((Value::Null, String::new()));
        }
        self.cached_page(&format!("pr:{reference}"), || {
            if task["pr_url"].as_str().is_some_and(|s| !s.is_empty()) {
                transport.json(&[
                    "pr",
                    "view",
                    reference,
                    "--repo",
                    &self.state.repo,
                    "--json",
                    "number,url,state,isDraft",
                ])
            } else {
                let prs = transport.json(&[
                    "pr",
                    "list",
                    "--repo",
                    &self.state.repo,
                    "--head",
                    reference,
                    "--state",
                    "all",
                    "--limit",
                    "1",
                    "--json",
                    "number,url,state,isDraft",
                ])?;
                Ok(prs
                    .as_array()
                    .and_then(|a| a.first())
                    .cloned()
                    .unwrap_or(Value::Null))
            }
        })
    }
    pub fn snapshot(&self, selected: Option<(i64, &str)>) -> Result<Value> {
        let busy = self.state.running()?;
        let mut db = self.state.connect()?;
        functions(&db)?;
        let tx = db.transaction()?;
        let control = StateStore::get(&tx, "meta:control", json!({}))?;
        let shutdown = StateStore::get(&tx, "meta:shutdown", json!({}))?;
        let mut tasks = BTreeMap::new();
        for n in query(&tx, "SELECT number FROM tasks ORDER BY number", [])? {
            let n = n["number"].as_i64().unwrap();
            tasks.insert(n, StateStore::get(&tx, &format!("task:{n}"), json!({}))?);
        }
        let sessions = query(&tx, "SELECT * FROM sessions", [])?
            .into_iter()
            .map(|r| ((r["number"].as_i64().unwrap(), string(&r, "role")), r))
            .collect::<BTreeMap<_, _>>();
        let runs=query(&tx,"SELECT r.* FROM runs r WHERE r.rowid = (SELECT newer.rowid FROM runs newer WHERE newer.number=r.number AND newer.role=r.role ORDER BY newer.started_at DESC,newer.rowid DESC LIMIT 1)",[])?.into_iter().map(|r|((r["number"].as_i64().unwrap(),string(&r,"role")),r)).collect::<BTreeMap<_,_>>();
        let mut keys = sessions
            .keys()
            .chain(runs.keys())
            .cloned()
            .collect::<BTreeSet<_>>();
        for (n, t) in &tasks {
            let role = match t["phase"].as_str() {
                Some("reviewing") => "reviewer",
                Some("finalizing") => "finalizer",
                Some("claiming" | "building") => "worker",
                _ => rrole(t),
            };
            keys.insert((*n, role.into()));
        }
        let mut rows = Vec::new();
        for (n, role) in keys {
            let t = tasks
                .get(&n)
                .ok_or_else(|| Error::new("sqlite", "Session has no task."))?;
            let run = runs.get(&(n, role.clone())).cloned().unwrap_or(json!({}));
            let session = sessions
                .get(&(n, role.clone()))
                .cloned()
                .unwrap_or(json!({}));
            let active = busy && matches!(run["status"].as_str(), Some("starting" | "running"));
            let mut status = run["status"]
                .as_str()
                .or_else(|| t["phase"].as_str())
                .unwrap_or("unknown")
                .to_string();
            if !busy && matches!(status.as_str(), "starting" | "running") {
                status = "interrupted".into();
            }
            if t["phase"] == "done" && !active {
                status = "completed".into();
            } else if rrole(t) == role
                && matches!(
                    t["phase"].as_str(),
                    Some("blocked" | "interrupted" | "retry" | "awaiting_review" | "human_review")
                )
            {
                status = string(t, "phase");
            }
            let latest=query(&tx,"SELECT readable_log(stream,content) AS content,created_at FROM logs WHERE run_id=? AND readable_log(stream,content)!='' ORDER BY id DESC LIMIT 1",[run["id"].as_str()])?.pop();
            let title = t["issue"]["title"]
                .as_str()
                .filter(|s| !s.is_empty())
                .or_else(|| t["item"]["content"]["title"].as_str())
                .unwrap_or("Untitled issue");
            rows.push(json!({"key":[n,role],"number":n,"role":role,"task":t,"title":title,"run":run,"session":session,"active":active,"status":status,"snippet":latest.as_ref().map(|v|string(v,"content")).unwrap_or("Waiting for activity…".into()),"activity_at":latest.as_ref().map(|v|string(v,"created_at")).unwrap_or_else(||string(&run,"started_at"))}));
        }
        rows.sort_by_key(|r| {
            let status = if r["active"] == true {
                "running"
            } else {
                match r["status"].as_str().unwrap_or_default() {
                    "claiming" | "building" | "reviewing" | "finalizing" | "ready" => "queued",
                    s => s,
                }
            };
            (
                rank(status),
                status.to_string(),
                r["number"].as_i64().unwrap_or(0),
                string(r, "role"),
            )
        });
        let chosen = selected
            .and_then(|(n, role)| rows.iter().find(|r| r["number"] == n && r["role"] == role))
            .or_else(|| rows.first())
            .cloned();
        let mut logs = if let Some(row) = &chosen {
            if row["run"].as_object().is_some_and(|o| !o.is_empty()) {
                query(
                    &tx,
                    "SELECT id,run_id,stream,readable_log(stream,content) AS content,created_at FROM logs WHERE run_id=? AND readable_log(stream,content)!='' ORDER BY id DESC LIMIT 40",
                    [row["run"]["id"].as_str()],
                )?
            } else {
                Vec::new()
            }
        } else {
            query(
                &tx,
                "SELECT id,run_id,stream,readable_log(stream,content) AS content,created_at FROM logs WHERE run_id IS NULL AND readable_log(stream,content)!='' ORDER BY id DESC LIMIT 40",
                [],
            )?
        };
        logs.reverse();
        Ok(
            json!({"busy":busy,"paused":control["paused"].as_bool().unwrap_or(false),"stopping":shutdown["requested"].as_bool().unwrap_or(false),"rows":rows,"selected":chosen,"logs":logs}),
        )
    }
    pub fn issue_groups(rows: &[Value]) -> Vec<Value> {
        let mut issues = BTreeMap::new();
        for row in rows {
            let n = row["number"].as_i64().unwrap_or(0);
            let issue = issues.entry(n).or_insert_with(
                || json!({"number":n,"title":row["title"],"task":row["task"],"sessions":[]}),
            );
            issue["sessions"].as_array_mut().unwrap().push(row.clone());
        }
        for issue in issues.values_mut() {
            let t = issue["task"].clone();
            let sessions = issue["sessions"].as_array_mut().unwrap();
            sessions.sort_by_key(|r| {
                ["worker", "reviewer", "finalizer"]
                    .iter()
                    .position(|s| r["role"] == *s)
                    .unwrap_or(3)
            });
            let phase = t["phase"].as_str().unwrap_or("unknown");
            let status = match phase {
                "done" => "completed",
                "blocked" | "failed" | "interrupted" | "retry" | "awaiting_review"
                | "human_review" | "completed" => phase,
                _ if sessions.iter().any(|s| s["active"] == true) => "running",
                "claiming" | "building" | "reviewing" | "finalizing" => {
                    if sessions
                        .iter()
                        .any(|s| s["run"].as_object().is_some_and(|o| !o.is_empty()))
                    {
                        "interrupted"
                    } else {
                        "queued"
                    }
                }
                _ => phase,
            };
            issue["status"] = json!(status);
        }
        let mut out = issues.into_values().collect::<Vec<_>>();
        out.sort_by_key(|i| {
            (
                rank(&string(i, "status")),
                i["number"].as_i64().unwrap_or(0),
            )
        });
        out
    }
    pub fn logs(
        &self,
        number: Option<i64>,
        raw: bool,
        mut emit: impl FnMut(&Value) -> Result<()>,
    ) -> Result<()> {
        let mut after = 0;
        let cutoff =
            self.state
                .connect()?
                .query_row("SELECT coalesce(max(id),0) FROM logs", [], |r| {
                    r.get::<_, i64>(0)
                })?;
        loop {
            if after >= cutoff {
                return Ok(());
            }
            let page = self.state.logs_page(number, after, 1000)?;
            if page.is_empty() {
                return Ok(());
            }
            for mut row in page {
                after = row["id"].as_i64().unwrap();
                if after > cutoff {
                    return Ok(());
                }
                if !raw {
                    row["content"] = json!(readable_log(
                        &string(&row, "stream"),
                        &string(&row, "content")
                    ));
                }
                if row["content"] != "" {
                    emit(&row)?;
                }
            }
        }
    }
    pub fn cached_page(
        &self,
        key: &str,
        loader: impl FnOnce() -> Result<Value>,
    ) -> Result<(Value, String)> {
        let owner = format!("tui:{key}");
        let mut saved = self.state.get_meta(&owner, json!({}))?;
        let now = chrono::DateTime::parse_from_rfc3339(&self.state.deps.clock.now())
            .map_err(|e| Error::new("validation", e.to_string()))?
            .timestamp() as f64;
        if now >= saved["retry_at"].as_f64().unwrap_or(0.) {
            match loader() {
                Ok(value) => saved = json!({"value":value,"retry_at":now+60.}),
                Err(e) => {
                    saved["error"] = json!(e.to_string());
                    saved["retry_at"] = json!(now + 60.);
                }
            }
            self.state.set_meta(&owner, &saved)?;
        }
        Ok((saved["value"].clone(), string(&saved, "error")))
    }
    pub fn search(&self, query_text: &str, rows: &[Value], limit: usize) -> Result<Value> {
        let limit = limit.min(100);
        let needle = casefold(plain(query_text).trim());
        if needle.is_empty() {
            return Ok(json!({"results":[],"truncated":false}));
        }
        fn metadata(r: &Value) -> String {
            format!(
                "#{} · {} · {} · {} · {} · {}",
                r["number"],
                string(r, "title"),
                string(r, "role"),
                string(r, "status"),
                string(&r["session"], "session_id"),
                r["session"]["worktree"]
                    .as_str()
                    .filter(|s| !s.is_empty())
                    .or_else(|| r["run"]["worktree"].as_str())
                    .unwrap_or_default()
            )
        }
        fn invocation(row: &Value, run: &Value) -> Value {
            let mut r = row.clone();
            r["run"] = run.clone();
            r["status"] = run["status"].clone();
            r["session"]["session_id"] = json!(
                run["session_id"]
                    .as_str()
                    .filter(|s| !s.is_empty())
                    .unwrap_or("not saved yet")
            );
            r["session"]["worktree"] = run["worktree"].clone();
            r
        }
        let mut results = Vec::new();
        for row in rows {
            let text = metadata(row);
            if casefold(&plain(&text)).contains(&needle) {
                results.push(json!({"row":row,"log":null,"text":text}));
                if results.len() > limit {
                    results.truncate(limit);
                    return Ok(json!({"results":results,"truncated":true}));
                }
            }
        }
        let mut db = self.state.connect()?;
        functions(&db)?;
        let tx = db.transaction()?;
        // Stream historical invocations; retain at most limit+1 results.
        {
            let mut stmt = tx.prepare("SELECT * FROM runs ORDER BY started_at DESC,rowid DESC")?;
            let mut records = stmt.query([])?;
            while let Some(record) = records.next()? {
                let id: String = record.get("id")?;
                let number: i64 = record.get("number")?;
                let role: String = record.get("role")?;
                let Some(row) = rows
                    .iter()
                    .find(|r| r["number"] == number && r["role"] == role)
                else {
                    continue;
                };
                if row["run"]["id"] == id {
                    continue;
                }
                let run = crate::state::query(&tx, "SELECT * FROM runs WHERE id=?", [&id])?
                    .pop()
                    .unwrap();
                let r = invocation(row, &run);
                let text = metadata(&r);
                if casefold(&plain(&text)).contains(&needle) {
                    results.push(json!({"row":r,"log":null,"text":text}));
                    if results.len() > limit {
                        results.truncate(limit);
                        return Ok(json!({"results":results,"truncated":true}));
                    }
                }
            }
        }
        let logs = query(
            &tx,
            "SELECT l.id,l.run_id,l.stream,l.created_at,readable_log(l.stream,l.content) AS content,r.number,r.role FROM logs l JOIN runs r ON r.id=l.run_id WHERE instr(search_text(l.stream,l.content),?)>0 ORDER BY l.id DESC LIMIT ?",
            params![needle, (limit + 1 - results.len()) as i64],
        )?;
        for log in logs {
            let Some(row) = rows
                .iter()
                .find(|r| r["number"] == log["number"] && r["role"] == log["role"])
            else {
                continue;
            };
            let run = query(
                &tx,
                "SELECT * FROM runs WHERE id=?",
                [log["run_id"].as_str()],
            )?
            .pop()
            .ok_or_else(|| Error::new("sqlite", "Missing historical invocation."))?;
            let text = plain(&string(&log, "content"));
            let pos = search_position(&text, &needle).unwrap_or_default();
            let excerpt = text
                .chars()
                .skip(pos.saturating_sub(25))
                .collect::<String>();
            results.push(json!({"row":invocation(row,&run),"log":log,"text":excerpt}));
        }
        let truncated = results.len() > limit;
        results.truncate(limit);
        Ok(json!({"results":results,"truncated":truncated}))
    }
    pub fn search_context(&self, result: &Value) -> Result<Vec<Value>> {
        let mut connection = self.state.connect()?;
        functions(&connection)?;
        let db = connection.transaction()?;
        let log = &result["log"];
        if log.is_null() {
            let mut rows = query(
                &db,
                "SELECT id,run_id,stream,created_at,readable_log(stream,content) AS content FROM logs WHERE run_id=? AND readable_log(stream,content)!='' ORDER BY id DESC LIMIT 40",
                [result["row"]["run"]["id"].as_str()],
            )?;
            rows.reverse();
            return Ok(rows);
        }
        let mut before = query(
            &db,
            "SELECT id,run_id,stream,created_at,readable_log(stream,content) AS content FROM logs WHERE run_id=? AND id<=? AND readable_log(stream,content)!='' ORDER BY id DESC LIMIT 20",
            params![log["run_id"].as_str(), log["id"].as_i64()],
        )?;
        before.reverse();
        before.extend(crate::state::query(&db,"SELECT id,run_id,stream,created_at,readable_log(stream,content) AS content FROM logs WHERE run_id=? AND id>? AND readable_log(stream,content)!='' ORDER BY id LIMIT 20",params![log["run_id"].as_str(),log["id"].as_i64()])?);
        Ok(before)
    }
}
pub fn search_position(text: &str, needle: &str) -> Option<usize> {
    let folded = casefold(text);
    let byte = folded.find(needle)?;
    let mut left = folded[..byte].chars().count();
    for (i, c) in text.chars().enumerate() {
        let width = casefold(&c.to_string()).chars().count();
        if left < width {
            return Some(i);
        }
        left -= width;
    }
    Some(text.chars().count())
}
pub fn wrap(value: &str, width: usize) -> Vec<String> {
    let mut rows = Vec::new();
    let mut row = Vec::new();
    let mut used = 0;
    for c in plain(value).chars() {
        let size = cell_width(c);
        if !row.is_empty() && used + size > width {
            let space = row.iter().rposition(|c| *c == ' ');
            if let Some(i) = space.filter(|i| *i > 0 && row[..*i].iter().any(|c| *c != ' ')) {
                rows.push(row.drain(..=i).collect());
                used = row.iter().map(|c| cell_width(*c)).sum();
            } else {
                rows.push(row.drain(..).collect());
                used = 0;
            }
        }
        row.push(c);
        used += size;
    }
    rows.push(row.iter().collect());
    rows
}
pub fn clip(value: &str, width: usize) -> String {
    let mut out = String::new();
    let mut used = 0;
    for c in plain(value).chars() {
        let size = cell_width(c);
        if used + size > width {
            break;
        }
        out.push(c);
        used += size;
    }
    out
}
