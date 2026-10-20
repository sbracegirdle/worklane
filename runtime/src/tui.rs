//! Native terminal presentation; all domain mutations go through shared Actions.
use crate::{
    Error, Result,
    actions::{Actions, IssueDraft},
    board::Invocation,
    config::ConfigStore,
    harness::AgentHarness,
    read_model::{ReadModel, wrap},
    state::StateStore,
    string,
    transport::{GitHubTransport, RepositoryLocator},
};
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    io::Read,
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    thread,
    time::{Duration, Instant},
};
mod terminal;
use terminal::{Key, Terminal};

/// `worklane tui` can call this directly; the standalone entrypoint uses the same loop.
#[derive(Clone)]
pub struct Options {
    pub root: PathBuf,
    pub refresh: Duration,
    pub invocation: Invocation,
    /// Native executable followed by its `run` command and explicit invocation options.
    pub controller: Vec<String>,
    pub no_color: bool,
    pub overrides: Value,
}
impl Options {
    pub fn new(root: PathBuf) -> Self {
        Self {
            root,
            refresh: Duration::from_secs(1),
            invocation: Invocation::default(),
            controller: Vec::new(),
            no_color: false,
            overrides: json!({}),
        }
    }
}

#[derive(Clone, Copy, PartialEq)]
enum View {
    Overview,
    Logs,
    Search,
    Issue,
    Board,
}
enum Editor {
    Config(Vec<(String, String)>),
    Issue(Box<IssueDraft>),
    Search(String),
    Input(String),
}
#[derive(Clone, Copy)]
enum Lane {
    Action,
    Read,
    Control,
}
enum Update {
    Done(Lane, Box<Result<Update>>),
    EditorError(Box<Editor>, String),
    Snapshot(Value),
    Page(View, Value, String),
    Search(Value),
    Editor(Box<Editor>),
    Saved(String),
    Message(String),
}
type Job = thread::JoinHandle<()>;
struct Dashboard {
    options: Options,
    actions: Actions,
    model: ReadModel,
    snapshot: Value,
    view: View,
    selected: usize,
    expanded: BTreeSet<i64>,
    chosen: Option<(i64, String)>,
    page: Value,
    results: Vec<Value>,
    editor: Option<Editor>,
    field: usize,
    scroll: usize,
    message: String,
    tx: mpsc::Sender<Result<Update>>,
    rx: mpsc::Receiver<Result<Update>>,
    refresh_job: Option<Job>,
    action_job: Option<Job>,
    read_job: Option<Job>,
    control_job: Option<Job>,
    next_refresh: Instant,
    cancelled: Arc<AtomicBool>,
}

pub fn run(options: Options) -> Result<()> {
    if options.refresh < Duration::from_millis(50) {
        return crate::fail("Refresh must be at least 0.05 seconds.");
    }
    // Validate TTY before opening/migrating any state.
    Terminal::check()?;
    let repository = RepositoryLocator::locate(&options.root)?;
    let config = ConfigStore::resolve(&repository.root, &options.overrides)?;
    let mut state = StateStore::open(&repository.repo, None)?;
    state.busy_timeout = Duration::from_millis(250);
    let cancelled = Arc::new(AtomicBool::new(false));
    let transport =
        GitHubTransport::new(&state, &repository.root)?.with_cancellation(cancelled.clone());
    let actions = Actions {
        state: state.clone(),
        root: repository.root.clone(),
        agent: AgentHarness::new(&repository.root, &state, &config)?,
        transport,
        invocation: options.invocation.clone(),
    };
    let (tx, rx) = mpsc::channel();
    let mut app = Dashboard {
        options,
        actions,
        model: ReadModel::new(state),
        snapshot: json!({"rows":[],"logs":[]}),
        view: View::Overview,
        selected: 0,
        expanded: BTreeSet::new(),
        chosen: None,
        page: Value::Null,
        results: Vec::new(),
        editor: None,
        field: 0,
        scroll: 0,
        message: String::new(),
        tx,
        rx,
        refresh_job: None,
        action_job: None,
        read_job: None,
        control_job: None,
        next_refresh: Instant::now(),
        cancelled,
    };
    let mut terminal = Terminal::open()?;
    let result = app.event_loop(&mut terminal);
    // Restore the screen immediately, then cancel/join only dashboard requests.
    drop(terminal);
    app.cancelled.store(true, Ordering::Relaxed);
    for job in [
        app.refresh_job.take(),
        app.action_job.take(),
        app.read_job.take(),
        app.control_job.take(),
    ]
    .into_iter()
    .flatten()
    {
        let _ = job.join();
    }
    result
}

impl Dashboard {
    fn job(&mut self, lane: Lane) -> &mut Option<Job> {
        match lane {
            Lane::Action => &mut self.action_job,
            Lane::Read => &mut self.read_job,
            Lane::Control => &mut self.control_job,
        }
    }
    fn launch_in(&mut self, lane: Lane, f: impl FnOnce() -> Result<Update> + Send + 'static) {
        if self.job(lane).is_some() {
            self.message = "An action is already running; please wait.".into();
            return;
        }
        let tx = self.tx.clone();
        self.message = "Working… (navigation and quit remain available)".into();
        *self.job(lane) = Some(thread::spawn(move || {
            let result =
                std::panic::catch_unwind(std::panic::AssertUnwindSafe(f)).unwrap_or_else(|_| {
                    Err(Error::new(
                        "blocked",
                        "Background action failed; saved state retained.",
                    ))
                });
            let _ = tx.send(Ok(Update::Done(lane, Box::new(result))));
        }));
    }
    fn launch(&mut self, f: impl FnOnce() -> Result<Update> + Send + 'static) {
        self.launch_in(Lane::Action, f);
    }
    fn launch_read(&mut self, f: impl FnOnce() -> Result<Update> + Send + 'static) {
        self.launch_in(Lane::Read, f);
    }
    fn receive(&mut self, update: Result<Update>) {
        match update {
            Ok(Update::Done(lane, result)) => {
                // The result is the worker's final operation; join before showing completion.
                if let Some(job) = self.job(lane).take() {
                    let _ = job.join();
                }
                self.receive(*result);
            }
            Ok(Update::EditorError(editor, error)) => {
                self.editor = Some(*editor);
                self.field = 0;
                self.message = error;
            }
            Err(e) => self.message = e.to_string(),
            Ok(Update::Snapshot(s)) => {
                self.snapshot = s;
                // Only the overview uses the saved issue/role to restore selection.
                if self.view == View::Overview {
                    let rows = self.visible();
                    if let Some((n, role)) = &self.chosen {
                        if let Some(i) = rows
                            .iter()
                            .position(|r| r["number"] == *n && string(r, "role") == *role)
                        {
                            self.selected = i;
                        }
                    }
                }
                self.bound_selection();
            }
            Ok(Update::Page(view, value, error)) => {
                if self.view == view {
                    self.page = value;
                    self.bound_selection();
                }
                self.message = error;
            }
            Ok(Update::Search(value)) => {
                self.results = value["results"].as_array().cloned().unwrap_or_default();
                self.selected = 0;
                self.message = if value["truncated"] == true {
                    "First 100 matches".into()
                } else {
                    format!("{} matches", self.results.len())
                };
            }
            Ok(Update::Editor(editor)) => {
                self.editor = Some(*editor);
                self.field = 0;
                self.message.clear();
            }
            Ok(Update::Saved(message)) => {
                self.editor = None;
                self.view = View::Overview;
                self.page = Value::Null;
                self.message = message;
                self.next_refresh = Instant::now();
            }
            Ok(Update::Message(message)) => {
                self.message = message;
                self.next_refresh = Instant::now();
            }
        }
    }
    fn poll(&mut self) {
        while let Ok(update) = self.rx.try_recv() {
            self.receive(update);
        }
        for job in [&mut self.refresh_job] {
            if job.as_ref().is_some_and(Job::is_finished) && job.take().unwrap().join().is_err() {
                self.message = "Background action failed; saved state retained.".into();
            }
        }
        if self.refresh_job.is_none() && Instant::now() >= self.next_refresh {
            let model = self.model.clone();
            let selected = self.chosen.clone();
            let tx = self.tx.clone();
            self.refresh_job = Some(thread::spawn(move || {
                let _ = tx.send(
                    model
                        .snapshot(selected.as_ref().map(|(n, r)| (*n, r.as_str())))
                        .map(Update::Snapshot),
                );
            }));
            self.next_refresh = Instant::now() + self.options.refresh;
        }
    }
    fn visible(&self) -> Vec<Value> {
        let rows = self.snapshot["rows"]
            .as_array()
            .cloned()
            .unwrap_or_default();
        let mut out = Vec::new();
        for issue in ReadModel::issue_groups(&rows) {
            let n = issue["number"].as_i64().unwrap_or(0);
            out.push(issue.clone());
            if self.expanded.contains(&n) {
                out.extend(issue["sessions"].as_array().cloned().unwrap_or_default());
            }
        }
        out
    }
    fn row(&self) -> Value {
        if matches!(self.view, View::Issue | View::Board) {
            let number = self.number();
            if let Some(row) = self.snapshot["rows"].as_array().and_then(|rows| {
                rows.iter()
                    .find(|row| row["number"].as_i64() == number && number.is_some())
            }) {
                return row.clone();
            }
            if self.view == View::Board {
                if let Some(item) = self
                    .page
                    .as_array()
                    .and_then(|items| items.get(self.selected))
                {
                    return json!({"number":number,"task":{"number":number,"pr_url":item["linked pull requests"][0]}});
                }
            }
            return json!({"number":number,"task":{}});
        }
        if self.view == View::Search {
            return self
                .results
                .get(self.selected)
                .map(|r| r["row"].clone())
                .unwrap_or(Value::Null);
        }
        if self.view == View::Logs {
            if let Some((n, role)) = &self.chosen {
                return self.snapshot["rows"]
                    .as_array()
                    .and_then(|rows| {
                        rows.iter()
                            .find(|r| r["number"] == *n && r["role"] == *role)
                    })
                    .cloned()
                    .unwrap_or(Value::Null);
            }
        }
        self.visible()
            .get(self.selected)
            .cloned()
            .unwrap_or(Value::Null)
    }
    fn number(&self) -> Option<i64> {
        match self.view {
            View::Issue => self.page["number"].as_i64(),
            View::Board => self
                .page
                .as_array()
                .and_then(|a| a.get(self.selected))
                .and_then(|i| i["content"]["number"].as_i64()),
            _ => self.row()["number"].as_i64(),
        }
    }
    fn session(&self) -> Value {
        let row = self.row();
        if row["role"].is_string() {
            row
        } else {
            row["sessions"]
                .as_array()
                .and_then(|a| a.first())
                .cloned()
                .unwrap_or(Value::Null)
        }
    }
    fn remember(&mut self) {
        if self.view == View::Overview {
            let row = self.row();
            self.chosen = row["number"].as_i64().map(|n| (n, string(&row, "role")));
        }
    }
    fn event_loop(&mut self, terminal: &mut Terminal) -> Result<()> {
        loop {
            self.poll();
            self.draw(terminal)?;
            if terminal::stopped() {
                return Ok(());
            }
            let Some(key) = terminal.key()? else { continue };
            if key == Key::Ctrl('c') {
                return Ok(());
            }
            if self.editor.is_some() {
                self.edit_key(key);
                continue;
            }
            match key {
                Key::Char('q') if self.view == View::Overview => return Ok(()),
                Key::Escape | Key::Char('q' | 'b') | Key::Backspace => {
                    self.view = View::Overview;
                    self.page = Value::Null;
                    self.scroll = 0;
                    self.selected = 0;
                }
                Key::Down | Key::Char('j') => self.navigate(1),
                Key::Up | Key::Char('k') => self.navigate(-1),
                Key::PageUp => self.scroll = self.scroll.saturating_add(10),
                Key::PageDown => self.scroll = self.scroll.saturating_sub(10),
                Key::End => self.scroll = 0,
                Key::Enter | Key::Right | Key::Char('l') => self.enter(),
                Key::Left | Key::Char('h') if self.view == View::Overview => {
                    if let Some(n) = self.number() {
                        self.expanded.remove(&n);
                        self.selected = self.selected.min(self.visible().len().saturating_sub(1));
                        self.remember();
                    }
                }
                Key::Char('/') => {
                    self.editor = Some(Editor::Search(String::new()));
                    self.field = 0;
                }
                Key::Char('m') if self.view == View::Logs => {
                    self.editor = Some(Editor::Input(String::new()));
                    self.field = 0;
                }
                Key::Char('c') => {
                    let root = self.actions.root.clone();
                    self.launch(move || {
                        let config = ConfigStore::resolve(&root, &json!({}))?;
                        let fields = config
                            .as_object()
                            .unwrap()
                            .iter()
                            .map(|(k, v)| (k.clone(), v.to_string()))
                            .collect();
                        Ok(Update::Editor(Box::new(Editor::Config(fields))))
                    });
                }
                Key::Char('n' | 'e') => {
                    let n = if key == Key::Char('n') {
                        None
                    } else {
                        self.number()
                    };
                    let actions = self.actions.clone();
                    if key == Key::Char('e') && n.is_none() {
                        continue;
                    }
                    self.launch(move || {
                        let project = actions.invocation.project.clone().or_else(|| {
                            ConfigStore::resolve(&actions.root, &json!({}))
                                .ok()
                                .and_then(|c| {
                                    c["project"]
                                        .as_str()
                                        .filter(|s| !s.is_empty())
                                        .map(str::to_owned)
                                })
                        });
                        Ok(Update::Editor(Box::new(Editor::Issue(Box::new(
                            actions.load_editor(project.as_deref(), n)?,
                        )))))
                    });
                }
                Key::Char('i') => {
                    if let Some(n) = self.number() {
                        self.load_issue(n);
                    }
                }
                Key::Char('B') => self.load_board(),
                Key::Char('r') => {
                    self.next_refresh = Instant::now();
                    match self.view {
                        View::Issue => {
                            if let Some(n) = self.number() {
                                self.load_issue(n);
                            }
                        }
                        View::Board => self.load_board(),
                        _ => {}
                    }
                }
                Key::Char('p') => {
                    let actions = self.actions.clone();
                    let task = self.row()["task"].clone();
                    self.launch(move || {
                        actions.open_pr(&task)?;
                        Ok(Update::Message("Opened PR".into()))
                    });
                }
                Key::Char('o') if self.view == View::Issue => {
                    let actions = self.actions.clone();
                    if let Some(n) = self.number() {
                        self.launch(move || {
                            actions.transport.text(
                                &[
                                    "issue",
                                    "view",
                                    &n.to_string(),
                                    "--repo",
                                    &actions.state.repo,
                                    "--web",
                                ],
                                None,
                            )?;
                            Ok(Update::Message("Opened issue".into()))
                        });
                    }
                }
                Key::Char('s' | 'x' | 'X' | 'z' | 'u' | 't') => self.control(key),
                Key::Char('a') => {
                    if self.action_job.is_some()
                        || self.read_job.is_some()
                        || self.control_job.is_some()
                    {
                        self.message = "Wait for the current action before resuming.".into();
                        continue;
                    }
                    let row = self.session();
                    let n = row["number"].as_i64().unwrap_or(0);
                    let role = string(&row, "role");
                    // The harness owns the foreground terminal until it returns.
                    terminal.suspend()?;
                    let result = self.actions.resume_session(n, &role);
                    terminal.activate()?;
                    self.message = match result {
                        Ok(_) => "Session returned; inspect changes before restarting.".into(),
                        Err(e) => e.to_string(),
                    };
                }
                _ => {}
            }
        }
    }
    fn selection_len(&self) -> Option<usize> {
        match self.view {
            View::Overview => Some(self.visible().len()),
            View::Search => Some(self.results.len()),
            View::Board => Some(self.page.as_array().map_or(0, Vec::len)),
            View::Logs | View::Issue => None,
        }
    }
    fn bound_selection(&mut self) {
        if let Some(len) = self.selection_len() {
            self.selected = self.selected.min(len.saturating_sub(1));
        }
    }
    fn navigate(&mut self, delta: isize) {
        if matches!(self.view, View::Logs | View::Issue) {
            self.scroll = self
                .scroll
                .saturating_add_signed(if self.view == View::Logs {
                    -delta
                } else {
                    delta
                });
            return;
        }
        let len = self.selection_len().unwrap_or(0);
        self.selected = self
            .selected
            .saturating_add_signed(delta)
            .min(len.saturating_sub(1));
        self.remember();
    }
    fn enter(&mut self) {
        match self.view {
            View::Overview => {
                let row = self.row();
                if !row["role"].is_string() {
                    if let Some(n) = row["number"].as_i64() {
                        if !self.expanded.insert(n) {
                            self.expanded.remove(&n);
                        }
                    }
                } else {
                    self.chosen = Some((row["number"].as_i64().unwrap(), string(&row, "role")));
                    self.view = View::Logs;
                    self.page = Value::Null;
                    self.scroll = 0;
                    self.next_refresh = Instant::now();
                }
            }
            View::Search => {
                if let Some(result) = self.results.get(self.selected).cloned() {
                    let model = self.model.clone();
                    self.view = View::Logs;
                    let row = &result["row"];
                    self.chosen = Some((row["number"].as_i64().unwrap_or(0), string(row, "role")));
                    self.launch(move || {
                        Ok(Update::Page(
                            View::Logs,
                            json!(model.search_context(&result)?),
                            String::new(),
                        ))
                    });
                }
            }
            View::Board => {
                if let Some(n) = self.number() {
                    self.load_issue(n);
                }
            }
            _ => {}
        }
    }
    fn load_issue(&mut self, n: i64) {
        if self.read_job.is_some() {
            self.message = "A page is still loading; navigation and stop remain available.".into();
            return;
        }
        let actions = self.actions.clone();
        let model = self.model.clone();
        self.view = View::Issue;
        self.scroll = 0;
        self.page = json!({"number":n,"title":"Loading…"});
        self.launch_read(move||{
            let (value,error)=model.cached_page(&format!("issue:{n}"),||{
                let mut issue=actions.transport.json(&["issue","view",&n.to_string(),"--repo",&actions.state.repo,"--json","number,title,body,state,url"])?;
                let (owner,name)=actions.state.repo.split_once('/').ok_or_else(||Error::new("validation","Invalid repository"))?;
                issue["comments"]=json!(actions.transport.pages("query($owner:String!,$name:String!,$number:Int!,$cursor:String){repository(owner:$owner,name:$name){issue(number:$number){comments(first:100,after:$cursor){nodes{body author{login}} totalCount pageInfo{hasNextPage endCursor}}}}}",json!({"owner":owner,"name":name,"number":n}),&["repository","issue","comments"])?);
                Ok(issue)
            })?;Ok(Update::Page(View::Issue,value,error))
        });
    }
    fn load_board(&mut self) {
        if self.read_job.is_some() {
            self.message = "A page is still loading; navigation and stop remain available.".into();
            return;
        }
        let actions = self.actions.clone();
        let model = self.model.clone();
        self.view = View::Board;
        self.selected = 0;
        self.page = Value::Null;
        self.launch_read(move || {
            let (page, error) =
                model.cached_page("board", || Ok(json!(actions.security_board()?.items()?)))?;
            Ok(Update::Page(View::Board, page, error))
        });
    }
    fn control(&mut self, key: Key) {
        let actions = self.actions.clone();
        let number = self.number();
        let mut controller = self.options.controller.clone();
        let project = self.options.invocation.project.clone();
        let local = matches!(key, Key::Char('x' | 'X' | 'z'));
        self.launch_in(if local {Lane::Control} else {Lane::Action}, move || {
            let message = match key {
                Key::Char('x' | 'X') => {
                    actions.stop(key == Key::Char('X'))?;
                    "Stop requested; saved sessions retained."
                }
                Key::Char('z' | 'u') => {
                    actions.pause(key == Key::Char('z'))?;
                    "Dispatch control updated."
                }
                Key::Char('t') => {
                    actions.retry(number.ok_or_else(|| Error::new("validation", "Select an issue."))?)?;
                    "Retry saved."
                }
                _ => {
                    if controller.is_empty() {
                        let binary = std::env::current_exe()?.with_file_name("worklane");
                        controller = vec![binary.to_string_lossy().into_owned(), "run".into(), "--repo".into(), actions.root.to_string_lossy().into_owned()];
                    }
                    let mut magic = [0u8; 4];
                    std::fs::File::open(&controller[0])?.read_exact(&mut magic)?;
                    if magic != *b"\x7fELF" {
                        return crate::fail("Controller must be a native executable; install the native CLI from #51.");
                    }
                    if let Some(project) = project {controller.extend(["--project".into(), project]);}
                    actions.start(&controller)?;
                    "Controller started."
                }
            };
            Ok(Update::Message(message.into()))
        });
    }
    fn edit_key(&mut self, key: Key) {
        if key == Key::Escape {
            if self.action_job.is_none() {
                self.editor = None;
                self.message = "Cancelled; nothing saved.".into();
            }
            return;
        }
        let editor = self.editor.as_mut().unwrap();
        let count = match editor {
            Editor::Config(fields) => fields.len(),
            Editor::Issue(_) => 3,
            _ => 1,
        };
        match key {
            Key::Tab => {
                self.field = (self.field + 1) % count;
                return;
            }
            Key::BackTab => {
                self.field = (self.field + count - 1) % count;
                return;
            }
            Key::Save => {
                self.save_editor();
                return;
            }
            Key::Enter if matches!(editor, Editor::Search(_) | Editor::Input(_)) => {
                self.save_editor();
                return;
            }
            _ => {}
        }
        let multiline = matches!(editor, Editor::Issue(_));
        let target = match editor {
            Editor::Config(fields) => &mut fields[self.field].1,
            Editor::Issue(draft) => match self.field {
                0 => &mut draft.title,
                1 => &mut draft.body,
                _ => {
                    let pos = draft
                        .statuses
                        .iter()
                        .position(|s| *s == draft.status)
                        .unwrap_or(0);
                    let next = match key {
                        Key::Left | Key::Up => pos.saturating_sub(1),
                        Key::Right | Key::Down => (pos + 1).min(draft.statuses.len() - 1),
                        _ => pos,
                    };
                    draft.status = draft.statuses[next].clone();
                    return;
                }
            },
            Editor::Search(text) | Editor::Input(text) => text,
        };
        match key {
            Key::Char(c) => target.push(c),
            Key::Backspace => {
                target.pop();
            }
            Key::Ctrl('u') => target.clear(),
            Key::Enter if multiline => target.push('\n'),
            _ => {}
        }
    }
    fn save_editor(&mut self) {
        if self.action_job.is_some() {
            return;
        }
        match self.editor.take().unwrap() {
            Editor::Search(query) => {
                let model = self.model.clone();
                let rows = self.snapshot["rows"]
                    .as_array()
                    .cloned()
                    .unwrap_or_default();
                self.view = View::Search;
                self.launch(move || Ok(Update::Search(model.search(&query, &rows, 100)?)));
            }
            Editor::Input(text) => {
                let actions = self.actions.clone();
                let row = self.session();
                let id = string(&row["run"], "id");
                self.launch(move || {
                    if let Err(error) = actions.queue_input(&id, &text) {
                        return Ok(Update::EditorError(
                            Box::new(Editor::Input(text)),
                            error.to_string(),
                        ));
                    }
                    Ok(Update::Message(
                        "Input queued for the saved session.".into(),
                    ))
                });
            }
            Editor::Config(fields) => {
                // Parsing/validation failures retain the unsaved fields.
                let result = (|| -> Result<Value> {
                    let mut values = serde_json::Map::new();
                    for (k, v) in &fields {
                        values.insert(
                            k.clone(),
                            serde_json::from_str(v).map_err(|e| {
                                Error::new("validation", format!("{k}: {e}; strings need quotes"))
                            })?,
                        );
                    }
                    ConfigStore::save(&self.actions.root, &Value::Object(values))
                })();
                match result {
                    Ok(_) => self.message = "Configuration saved; applies on next start.".into(),
                    Err(e) => {
                        self.message = e.to_string();
                        self.editor = Some(Editor::Config(fields));
                    }
                }
            }
            Editor::Issue(draft) => {
                let actions = self.actions.clone();
                let mut draft = *draft;
                self.launch(move || match actions.save_editor(&mut draft) {
                    Ok(url) => {
                        let _ = actions.state.set_meta("tui:board", &json!({}));
                        if let Some(number) = draft.number {
                            let _ = actions
                                .state
                                .set_meta(&format!("tui:issue:{number}"), &json!({}));
                        }
                        Ok(Update::Saved(format!("Saved {url}")))
                    }
                    Err(e) => Ok(Update::EditorError(
                        Box::new(Editor::Issue(Box::new(draft))),
                        e.to_string(),
                    )),
                });
            }
        }
    }
    fn draw(&self, terminal: &mut Terminal) -> Result<()> {
        let (width, height) = terminal.size();
        let width = width.saturating_sub(1).max(1);
        let body_height = height.saturating_sub(5).max(1);
        let mut body = Vec::new();
        let title;
        if width < 49 || height < 12 {
            return terminal.draw(
                &["Resize terminal to at least 50×12. Ctrl-C quits.".into()],
                self.options.no_color,
            );
        }
        if let Some(editor) = &self.editor {
            title = "Editor · Tab fields · Ctrl-U clear · F2/Ctrl-S save · Esc cancel".into();
            let fields = match editor {
                Editor::Config(fields) => fields
                    .iter()
                    .map(|(k, v)| format!("{k}: {v}"))
                    .collect::<Vec<_>>(),
                Editor::Issue(draft) => vec![
                    format!("Title: {}", draft.title),
                    format!("Description: {}", draft.body),
                    format!("Status: {}", draft.status),
                ],
                Editor::Search(text) => vec![format!("Search (Enter): {text}")],
                Editor::Input(text) => vec![format!("Input (Enter queues): {text}")],
            };
            let mut insertion_row = 0;
            for (i, field) in fields.iter().enumerate() {
                let prefix = if i == self.field { "> " } else { "  " };
                let mut lines = Vec::new();
                for line in field.split('\n') {
                    lines.extend(wrap(&format!("{prefix}{line}"), width));
                }
                body.extend(lines);
                if i == self.field {
                    insertion_row = body.len().saturating_sub(1);
                }
            }
            // Fields are append-only: keep their actual wrapped insertion row visible.
            if body.len() > body_height {
                let start = insertion_row.saturating_sub(body_height.saturating_sub(1));
                body = body.into_iter().skip(start).collect();
            }
        } else {
            title = format!(
                "Worklane · {} · {}{}",
                self.actions.state.repo,
                if self.snapshot["busy"] == true {
                    "running"
                } else {
                    "stopped"
                },
                if self.snapshot["paused"] == true {
                    " · paused"
                } else {
                    ""
                }
            );
            match self.view {
                View::Overview => {
                    body.push(
                        "Overview · Enter expands / opens logs · i details · / search".into(),
                    );
                    let rows = self.visible();
                    for (i, row) in rows.iter().enumerate() {
                        body.push(format!(
                            "{} {}#{} {} · {}",
                            if i == self.selected { ">" } else { " " },
                            if row["role"].is_string() { "  " } else { "" },
                            row["number"],
                            row["role"]
                                .as_str()
                                .unwrap_or_else(|| row["title"].as_str().unwrap_or("")),
                            string(row, "status")
                        ));
                    }
                    if rows.is_empty() {
                        body.push("No saved tasks. n creates an issue; B opens board.".into());
                    }
                    if self.selected >= body_height.saturating_sub(1) {
                        body = body
                            .into_iter()
                            .skip(self.selected + 2 - body_height)
                            .collect();
                    }
                }
                View::Logs => {
                    let row = self.session();
                    body.push(format!(
                        "Logs #{} · {} · session {}",
                        row["number"],
                        string(&row, "role"),
                        string(&row["session"], "session_id")
                    ));
                    body.push(format!(
                        "Worktree: {} · blocker: {}",
                        string(&row["session"], "worktree"),
                        string(&row["task"], "blocker")
                    ));
                    let logs = if self.page.is_array() {
                        &self.page
                    } else {
                        &self.snapshot["logs"]
                    };
                    let mut lines = Vec::new();
                    if let Some(logs) = logs.as_array() {
                        for log in logs {
                            for line in string(log, "content").split('\n') {
                                lines.extend(wrap(line, width));
                            }
                        }
                    }
                    let end = lines.len().saturating_sub(self.scroll);
                    let start = end.saturating_sub(body_height.saturating_sub(2));
                    body.extend(lines[start..end].iter().cloned());
                }
                View::Search => {
                    body.push("Search results · Enter opens context".into());
                    for (i, result) in self.results.iter().enumerate() {
                        body.push(format!(
                            "{} {}",
                            if i == self.selected { ">" } else { " " },
                            string(result, "text")
                        ));
                    }
                    if self.selected >= body_height.saturating_sub(1) {
                        body = body
                            .into_iter()
                            .skip(self.selected + 2 - body_height)
                            .collect();
                    }
                }
                View::Issue => {
                    body.push(format!(
                        "Issue #{} · {}",
                        self.page["number"],
                        string(&self.page, "title")
                    ));
                    let mut lines = Vec::new();
                    for line in string(&self.page, "body").split('\n') {
                        lines.extend(wrap(line, width));
                    }
                    if let Some(comments) = self.page["comments"].as_array() {
                        for comment in comments {
                            lines.push(format!(
                                "Comment by {}",
                                string(&comment["author"], "login")
                            ));
                            for line in string(comment, "body").split('\n') {
                                lines.extend(wrap(line, width));
                            }
                        }
                    }
                    body.extend(lines.into_iter().skip(self.scroll));
                }
                View::Board => {
                    body.push("Board · Enter details · n create · e edit".into());
                    if let Some(items) = self.page.as_array() {
                        for (i, item) in items.iter().enumerate() {
                            body.push(format!(
                                "{} #{} {} · {}",
                                if i == self.selected { ">" } else { " " },
                                item["content"]["number"],
                                string(item, "title"),
                                string(item, "status")
                            ));
                        }
                    }
                    if self.selected >= body_height.saturating_sub(1) {
                        body = body
                            .into_iter()
                            .skip(self.selected + 2 - body_height)
                            .collect();
                    }
                }
            }
        }
        let mut lines = vec![title];
        lines.extend(body.into_iter().take(body_height));
        while lines.len() < height.saturating_sub(3) {
            lines.push(String::new());
        }
        lines.push(self.message.clone());
        lines.push("s start · z pause · u resume · x drain · X stop now · t retry".into());
        lines.push(
            "c config · n/e issue · p PR · o browser · a session · m input · q back/quit".into(),
        );
        terminal.draw(&lines, self.options.no_color)
    }
}
