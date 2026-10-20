use crate::{Error, Result};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{self, File, OpenOptions},
    os::{
        fd::{AsRawFd, FromRawFd, RawFd},
        unix::{fs::OpenOptionsExt, process::CommandExt},
    },
    path::Path,
    process::{Child, Command},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};
pub const OWNER_ENV: &str = "WORKLANE_PROCESS_OWNER";
pub const CONTROLLER_ENV: &str = "WORKLANE_CONTROLLER_OWNER";
pub type Processes = BTreeMap<i32, String>;
#[derive(Clone, Debug)]
pub struct ProcessRef {
    pub pid: i32,
    pub birth: String,
}
#[derive(Clone, Debug)]
pub struct Proc {
    pub birth: String,
    pub parent: i32,
    pub group: i32,
}
pub struct ProcessSupervisor;
impl ProcessSupervisor {
    pub fn snapshot() -> BTreeMap<i32, Proc> {
        let mut out = BTreeMap::new();
        if let Ok(entries) = fs::read_dir("/proc") {
            for e in entries.flatten() {
                let Ok(pid) = e.file_name().to_string_lossy().parse::<i32>() else {
                    continue;
                };
                let Ok(s) = fs::read_to_string(e.path().join("stat")) else {
                    continue;
                };
                let Some((_, tail)) = s.rsplit_once(')') else {
                    continue;
                };
                let f: Vec<_> = tail.split_whitespace().collect();
                if f.len() > 19 && f[0] != "Z" {
                    if let (Ok(parent), Ok(group)) = (f[1].parse(), f[2].parse()) {
                        out.insert(
                            pid,
                            Proc {
                                birth: f[19].into(),
                                parent,
                                group,
                            },
                        );
                    }
                }
            }
        }
        out
    }
    pub fn identity(pid: i32) -> Option<String> {
        Self::snapshot().get(&pid).map(|p| p.birth.clone())
    }
    pub fn owned(roots: &[ProcessRef], tokens: &[String], known: &Processes) -> Processes {
        let procs = Self::snapshot();
        let mut out = Processes::new();
        for (pid, birth) in known
            .iter()
            .map(|(p, b)| (*p, b))
            .chain(roots.iter().map(|p| (p.pid, &p.birth)))
        {
            if !birth.is_empty() && procs.get(&pid).is_some_and(|p| &p.birth == birth) {
                out.insert(pid, birth.clone());
            }
        }
        let tokens: Vec<Vec<u8>> = tokens
            .iter()
            .filter(|t| !t.is_empty())
            .flat_map(|t| {
                [
                    format!("{OWNER_ENV}={t}").into_bytes(),
                    format!("{CONTROLLER_ENV}={t}").into_bytes(),
                ]
            })
            .collect();
        if !tokens.is_empty() {
            for (pid, p) in &procs {
                if let Ok(env) = fs::read(format!("/proc/{pid}/environ")) {
                    if env
                        .split(|b| *b == 0)
                        .any(|s| tokens.iter().any(|t| s == t))
                    {
                        out.insert(*pid, p.birth.clone());
                    }
                }
            }
        }
        let groups: BTreeSet<_> = out
            .keys()
            .filter(|pid| procs.get(pid).is_some_and(|p| p.group == **pid))
            .copied()
            .collect();
        loop {
            let mut changed = false;
            for (pid, p) in &procs {
                if !out.contains_key(pid)
                    && (out.contains_key(&p.parent) || groups.contains(&p.group))
                {
                    out.insert(*pid, p.birth.clone());
                    changed = true;
                }
            }
            if !changed {
                break;
            }
        }
        out.remove(&(std::process::id() as i32));
        out
    }
    pub fn signal_owned(procs: &Processes, signal: i32) -> Result<()> {
        for (pid, birth) in procs {
            if birth.is_empty() || Self::identity(*pid).as_ref() != Some(birth) {
                continue;
            }
            let fd = unsafe { libc::syscall(libc::SYS_pidfd_open, *pid, 0) } as i32;
            let result = if fd >= 0 {
                let handle = unsafe { File::from_raw_fd(fd) };
                if Self::identity(*pid).as_ref() != Some(birth) {
                    continue;
                }
                unsafe {
                    libc::syscall(
                        libc::SYS_pidfd_send_signal,
                        handle.as_raw_fd(),
                        signal,
                        std::ptr::null::<libc::siginfo_t>(),
                        0,
                    )
                }
            } else {
                let e = std::io::Error::last_os_error();
                if e.raw_os_error() == Some(libc::ESRCH) {
                    continue;
                }
                if !matches!(e.raw_os_error(), Some(libc::ENOSYS | libc::EINVAL)) {
                    return Err(e.into());
                }
                if Self::identity(*pid).as_ref() != Some(birth) {
                    continue;
                }
                unsafe { libc::kill(*pid, signal) as libc::c_long }
            };
            if result != 0 {
                let e = std::io::Error::last_os_error();
                if e.raw_os_error() != Some(libc::ESRCH) {
                    return Err(e.into());
                }
            }
        }
        Ok(())
    }
    pub fn terminate_owned(
        roots: &[ProcessRef],
        tokens: &[String],
        grace: Duration,
        known: &Processes,
    ) -> Result<Vec<i32>> {
        let deadline = Instant::now() + grace;
        let mut known = Self::owned(roots, tokens, known);
        while !known.is_empty() {
            Self::signal_owned(
                &known,
                if Instant::now() < deadline {
                    libc::SIGTERM
                } else {
                    libc::SIGKILL
                },
            )?;
            std::thread::sleep(Duration::from_millis(50));
            known = Self::owned(roots, tokens, &known);
            if Instant::now() > deadline + Duration::from_secs(2) {
                break;
            }
        }
        Ok(known.keys().copied().collect())
    }
    pub fn lock(path: &Path, blocking: bool) -> Result<LockGuard> {
        if let Some(p) = path.parent() {
            fs::create_dir_all(p)?;
        }
        let file = OpenOptions::new()
            .create(true)
            .append(true)
            .read(true)
            .mode(0o600)
            .open(path)?;
        if unsafe {
            libc::flock(
                file.as_raw_fd(),
                libc::LOCK_EX | if blocking { 0 } else { libc::LOCK_NB },
            )
        } != 0
        {
            let e = std::io::Error::last_os_error();
            if e.raw_os_error() == Some(libc::EWOULDBLOCK) {
                return Err(Error::new(
                    "busy",
                    "Worklane is already running for this repository.",
                ));
            }
            return Err(e.into());
        }
        Ok(LockGuard(file))
    }
    /// Only async-signal-safe operations run between fork and exec.
    pub fn prepare(command: &mut Command, inherited: &[RawFd]) {
        let fds = inherited.to_vec();
        unsafe {
            command.pre_exec(move || {
                if libc::setsid() < 0 {
                    return Err(std::io::Error::last_os_error());
                }
                for fd in &fds {
                    let flags = libc::fcntl(*fd, libc::F_GETFD);
                    if flags < 0 || libc::fcntl(*fd, libc::F_SETFD, flags & !libc::FD_CLOEXEC) < 0 {
                        return Err(std::io::Error::last_os_error());
                    }
                }
                Ok(())
            });
        }
    }
}
// Do not explicitly LOCK_UN on drop: the inherited open-file description owns the lock.
pub struct LockGuard(File);
impl AsRawFd for LockGuard {
    fn as_raw_fd(&self) -> RawFd {
        self.0.as_raw_fd()
    }
}
pub struct OwnedChild {
    pub child: Child,
    pub root: ProcessRef,
    pub token: String,
}
impl OwnedChild {
    pub fn new(child: Child, token: String) -> Self {
        let pid = child.id() as i32;
        Self {
            child,
            root: ProcessRef {
                pid,
                birth: ProcessSupervisor::identity(pid).unwrap_or_default(),
            },
            token,
        }
    }
    pub fn terminate(&mut self) -> Result<()> {
        let remaining = ProcessSupervisor::terminate_owned(
            std::slice::from_ref(&self.root),
            std::slice::from_ref(&self.token),
            Duration::from_secs(2),
            &Processes::new(),
        )?;
        if self.child.try_wait()?.is_none() {
            self.child.kill()?;
            let deadline = Instant::now() + Duration::from_secs(2);
            while self.child.try_wait()?.is_none() {
                if Instant::now() > deadline {
                    return Err(Error::new(
                        "busy",
                        "Owned child did not exit after SIGKILL.",
                    ));
                }
                std::thread::sleep(Duration::from_millis(10));
            }
        }
        if !remaining.is_empty() {
            return Err(Error::new(
                "busy",
                format!("Owned processes survived termination: {remaining:?}"),
            ));
        }
        Ok(())
    }
}
impl Drop for OwnedChild {
    fn drop(&mut self) {
        let _ = self.terminate();
    }
}
static SIGNAL_STOP: AtomicBool = AtomicBool::new(false);
extern "C" fn stop_signal(_: i32) {
    SIGNAL_STOP.store(true, Ordering::SeqCst);
}
struct SignalHandlers {
    previous: Vec<(i32, libc::sigaction)>,
    users: usize,
}
static SIGNAL_HANDLERS: Mutex<Option<SignalHandlers>> = Mutex::new(None);
pub struct StopSignals {
    _private: (),
}
impl StopSignals {
    pub fn install() -> Result<Self> {
        let mut saved = SIGNAL_HANDLERS
            .lock()
            .map_err(|_| Error::new("busy", "Signal handler lock poisoned."))?;
        if let Some(active) = saved.as_mut() {
            active.users += 1;
            return Ok(Self { _private: () });
        }
        SIGNAL_STOP.store(false, Ordering::SeqCst);
        let mut previous = Vec::new();
        unsafe {
            let mut action: libc::sigaction = std::mem::zeroed();
            action.sa_sigaction = stop_signal as *const () as usize;
            libc::sigemptyset(&mut action.sa_mask);
            for signal in [libc::SIGTERM, libc::SIGINT] {
                let mut old: libc::sigaction = std::mem::zeroed();
                if libc::sigaction(signal, &action, &mut old) < 0 {
                    let error = std::io::Error::last_os_error();
                    for (signal, old) in &previous {
                        libc::sigaction(*signal, old, std::ptr::null_mut());
                    }
                    return Err(error.into());
                }
                previous.push((signal, old));
            }
        }
        *saved = Some(SignalHandlers { previous, users: 1 });
        Ok(Self { _private: () })
    }
    pub fn requested() -> bool {
        SIGNAL_STOP.load(Ordering::SeqCst)
    }
}
impl Drop for StopSignals {
    fn drop(&mut self) {
        if let Ok(mut saved) = SIGNAL_HANDLERS.lock() {
            if let Some(active) = saved.as_mut() {
                active.users -= 1;
                if active.users == 0 {
                    for (signal, old) in &active.previous {
                        unsafe {
                            libc::sigaction(*signal, old, std::ptr::null_mut());
                        }
                    }
                    *saved = None;
                    SIGNAL_STOP.store(false, Ordering::SeqCst);
                }
            }
        }
    }
}
#[derive(Clone, Default)]
pub struct StopToken(pub Arc<AtomicBool>);
impl StopToken {
    pub fn request(&self) {
        self.0.store(true, Ordering::SeqCst);
    }
    pub fn requested(&self) -> bool {
        self.0.load(Ordering::SeqCst) || StopSignals::requested()
    }
}
