use crate::{Result, read_model::wrap};
use std::{
    io::{Read, Write},
    sync::atomic::{AtomicBool, Ordering},
    time::{Duration, Instant},
};
static STOP: AtomicBool = AtomicBool::new(false);
extern "C" fn stop(_: libc::c_int) {
    STOP.store(true, Ordering::Relaxed);
}
pub fn stopped() -> bool {
    STOP.load(Ordering::Relaxed)
}
#[derive(Debug, Clone, Copy, PartialEq)]
pub(super) enum Key {
    Char(char),
    Ctrl(char),
    Enter,
    Tab,
    BackTab,
    Escape,
    Backspace,
    Up,
    Down,
    Left,
    Right,
    PageUp,
    PageDown,
    End,
    Save,
    Ignored,
}

pub(super) struct Terminal {
    saved: libc::termios,
    signals: Vec<(i32, libc::sigaction)>,
    active: bool,
    input: Vec<u8>,
    last_input: Instant,
}
impl Terminal {
    pub fn check() -> Result<()> {
        if unsafe { libc::isatty(0) } != 1 || unsafe { libc::isatty(1) } != 1 {
            return crate::fail(
                "TUI requires an interactive terminal; use status or logs for redirected output.",
            );
        }
        Ok(())
    }
    pub fn open() -> Result<Self> {
        Self::check()?;
        STOP.store(false, Ordering::Relaxed);
        let mut saved = unsafe { std::mem::zeroed() };
        if unsafe { libc::tcgetattr(0, &mut saved) } != 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        let mut terminal = Self {
            saved,
            signals: Vec::new(),
            active: false,
            input: Vec::new(),
            last_input: Instant::now(),
        };
        for signal in [libc::SIGINT, libc::SIGTERM, libc::SIGHUP] {
            let mut action: libc::sigaction = unsafe { std::mem::zeroed() };
            let mut old = unsafe { std::mem::zeroed() };
            action.sa_sigaction = stop as *const () as usize;
            if unsafe { libc::sigaction(signal, &action, &mut old) } != 0 {
                return Err(std::io::Error::last_os_error().into());
            }
            terminal.signals.push((signal, old));
        }
        terminal.activate()?;
        Ok(terminal)
    }
    pub fn activate(&mut self) -> Result<()> {
        let mut raw = self.saved;
        unsafe { libc::cfmakeraw(&mut raw) };
        if unsafe { libc::tcsetattr(0, libc::TCSANOW, &raw) } != 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        self.active = true;
        let mut out = std::io::stdout().lock();
        out.write_all(b"\x1b[?1049h\x1b[?25l\x1b[2J")?;
        out.flush()?;
        Ok(())
    }
    pub fn suspend(&mut self) -> Result<()> {
        if self.active {
            self.active = false;
            let result = unsafe { libc::tcsetattr(0, libc::TCSANOW, &self.saved) };
            let mut out = std::io::stdout().lock();
            out.write_all(b"\x1b[0m\x1b[?25h\x1b[?1049l")?;
            out.flush()?;
            if result != 0 {
                return Err(std::io::Error::last_os_error().into());
            }
        }
        Ok(())
    }
    pub fn size(&self) -> (usize, usize) {
        let mut size: libc::winsize = unsafe { std::mem::zeroed() };
        if unsafe { libc::ioctl(1, libc::TIOCGWINSZ, &mut size) } == 0 {
            (
                usize::from(size.ws_col).max(1),
                usize::from(size.ws_row).max(1),
            )
        } else {
            (80, 24)
        }
    }
    pub fn draw(&self, lines: &[String], no_color: bool) -> Result<()> {
        let (w, h) = self.size();
        let mut output = String::from("\x1b[H");
        for (y, line) in lines.iter().take(h).enumerate() {
            output.push_str(&format!("\x1b[{};1H\x1b[2K", y + 1));
            if !no_color && y == 0 {
                output.push_str("\x1b[1;36m");
            }
            let clean = line.chars().filter(|c| !c.is_control()).collect::<String>();
            output.push_str(
                wrap(&clean, w.saturating_sub(1).max(1))
                    .first()
                    .map(String::as_str)
                    .unwrap_or(""),
            );
            output.push_str("\x1b[0m");
        }
        output.push_str("\x1b[J");
        let mut out = std::io::stdout().lock();
        out.write_all(output.as_bytes())?;
        out.flush()?;
        Ok(())
    }
    pub fn key(&mut self) -> Result<Option<Key>> {
        if let Some((key, n)) = decode(&self.input, false) {
            self.input.drain(..n);
            return Ok(Some(key));
        }
        let mut p = libc::pollfd {
            fd: 0,
            events: libc::POLLIN,
            revents: 0,
        };
        let result = unsafe { libc::poll(&mut p, 1, 50) };
        if result < 0 {
            if std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted {
                return Ok(None);
            }
            return Err(std::io::Error::last_os_error().into());
        }
        if result > 0 {
            let mut bytes = [0; 256];
            let n = std::io::stdin().read(&mut bytes)?;
            if n == 0 {
                return crate::fail("Terminal input closed.");
            }
            self.input.extend_from_slice(&bytes[..n]);
            self.last_input = Instant::now();
        }
        let expired = self.last_input.elapsed() >= Duration::from_millis(100);
        if let Some((key, n)) = decode(&self.input, expired) {
            self.input.drain(..n);
            Ok(Some(key))
        } else {
            Ok(None)
        }
    }
}
impl Drop for Terminal {
    fn drop(&mut self) {
        let _ = self.suspend();
        for (signal, old) in &self.signals {
            unsafe {
                libc::sigaction(*signal, old, std::ptr::null_mut());
            }
        }
    }
}
fn decode(input: &[u8], expired: bool) -> Option<(Key, usize)> {
    let first = *input.first()?;
    if first == 27 {
        let sequences: &[(&[u8], Key)] = &[
            (b"\x1b[A", Key::Up),
            (b"\x1b[B", Key::Down),
            (b"\x1b[C", Key::Right),
            (b"\x1b[D", Key::Left),
            (b"\x1b[5~", Key::PageUp),
            (b"\x1b[6~", Key::PageDown),
            (b"\x1b[F", Key::End),
            (b"\x1b[4~", Key::End),
            (b"\x1bOQ", Key::Save),
            (b"\x1b[12~", Key::Save),
            (b"\x1b[Z", Key::BackTab),
        ];
        for (seq, key) in sequences {
            if input.starts_with(seq) {
                return Some((*key, seq.len()));
            }
        }
        if !expired && (input.len() == 1 || sequences.iter().any(|(seq, _)| seq.starts_with(input)))
        {
            return None;
        }
        if input.get(1) == Some(&b'[') || input.get(1) == Some(&b'O') {
            // Consume unsupported terminal sequences as a unit, never as commands/text.
            if let Some(end) = input
                .iter()
                .enumerate()
                .skip(2)
                .find(|(_, b)| (0x40..=0x7e).contains(*b))
                .map(|(i, _)| i)
            {
                return Some((Key::Ignored, end + 1));
            }
            if !expired {
                return None;
            }
            return Some((Key::Ignored, input.len()));
        }
        return Some((Key::Escape, 1));
    }
    let key = match first {
        b'\r' | b'\n' => Key::Enter,
        b'\t' => Key::Tab,
        127 | 8 => Key::Backspace,
        19 => Key::Save,
        1..=26 => Key::Ctrl((b'a' + first - 1) as char),
        _ => {
            let size = if first < 128 {
                1
            } else if first < 224 {
                2
            } else if first < 240 {
                3
            } else {
                4
            };
            if input.len() < size && !expired {
                return None;
            }
            return Some(
                match std::str::from_utf8(&input[..size.min(input.len())])
                    .ok()
                    .and_then(|s| s.chars().next())
                {
                    Some(c) if !c.is_control() => (Key::Char(c), size),
                    _ => (Key::Ignored, 1),
                },
            );
        }
    };
    Some((key, 1))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn fragmented_unicode_and_escape_sequences() {
        assert_eq!(decode(&[0xe7, 0x95], false), None);
        assert_eq!(decode("界".as_bytes(), false), Some((Key::Char('界'), 3)));
        assert_eq!(decode(b"\x1b[", false), None);
        assert_eq!(decode(b"\x1b[A", false), Some((Key::Up, 3)));
        assert_eq!(decode(b"\x1b", true), Some((Key::Escape, 1)));
        assert_eq!(decode(b"\x1b[99~", true), Some((Key::Ignored, 5)));
    }
}
