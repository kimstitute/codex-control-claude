//! Native Windows Job Object supervisor helper. See
//! `docs/windows-helper-protocol.md` for the wire protocol.

use std::collections::HashMap;
use std::fmt;
#[cfg(windows)]
use std::io::Write;
use std::io::{self, BufRead};
#[cfg(windows)]
use std::sync::mpsc::{self, Sender};
#[cfg(windows)]
use std::thread;
#[cfg(windows)]
use std::time::Duration;

use serde::de::{self, MapAccess, Visitor};
use serde::{Deserialize, Deserializer};
use serde_json::Value;

const PROTOCOL_VERSION: u32 = 1;
#[cfg(windows)]
const HELPER_VERSION: &str = env!("CARGO_PKG_VERSION");
const MAX_FRAME_BYTES: usize = 1024 * 1024;
const MAX_GRACE_MS: u64 = 30_000;

#[derive(Debug, PartialEq, Eq)]
enum Frame {
    Line(String),
    Oversized,
    InvalidUtf8,
}

fn read_frame(reader: &mut impl BufRead) -> io::Result<Option<Frame>> {
    let mut bytes = Vec::new();
    let mut oversized = false;
    loop {
        let available = reader.fill_buf()?;
        if available.is_empty() {
            if bytes.is_empty() && !oversized {
                return Ok(None);
            }
            break;
        }
        let newline = available.iter().position(|byte| *byte == b'\n');
        let take = newline.unwrap_or(available.len());
        if !oversized {
            if bytes.len() + take > MAX_FRAME_BYTES {
                oversized = true;
                bytes.clear();
            } else {
                bytes.extend_from_slice(&available[..take]);
            }
        }
        let consumed = take + usize::from(newline.is_some());
        reader.consume(consumed);
        if newline.is_some() {
            break;
        }
    }
    if oversized {
        return Ok(Some(Frame::Oversized));
    }
    if bytes.last() == Some(&b'\r') {
        bytes.pop();
    }
    Ok(Some(match String::from_utf8(bytes) {
        Ok(line) => Frame::Line(line),
        Err(_) => Frame::InvalidUtf8,
    }))
}

mod cmdline {
    //! Win32 command-line quoting, matching the algorithm CommandLineToArgvW
    //! uses to split arguments back apart.

    pub fn quote_arg(arg: &str, out: &mut String) {
        let needs_quotes = arg.is_empty()
            || arg
                .contains(|c: char| c == ' ' || c == '\t' || c == '\n' || c == '\x0b' || c == '"');
        if !needs_quotes {
            out.push_str(arg);
            return;
        }
        out.push('"');
        let chars: Vec<char> = arg.chars().collect();
        let mut i = 0;
        while i < chars.len() {
            let mut num_backslashes = 0;
            while i < chars.len() && chars[i] == '\\' {
                num_backslashes += 1;
                i += 1;
            }
            if i == chars.len() {
                for _ in 0..num_backslashes * 2 {
                    out.push('\\');
                }
                break;
            } else if chars[i] == '"' {
                for _ in 0..num_backslashes * 2 + 1 {
                    out.push('\\');
                }
                out.push('"');
                i += 1;
            } else {
                for _ in 0..num_backslashes {
                    out.push('\\');
                }
                out.push(chars[i]);
                i += 1;
            }
        }
        out.push('"');
    }

    pub fn build_command_line(argv: &[String]) -> String {
        let mut out = String::new();
        for (i, arg) in argv.iter().enumerate() {
            if i > 0 {
                out.push(' ');
            }
            quote_arg(arg, &mut out);
        }
        out
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn simple_args_are_unquoted() {
            assert_eq!(
                build_command_line(&["a.exe".into(), "arg".into()]),
                "a.exe arg"
            );
        }

        #[test]
        fn args_with_spaces_are_quoted() {
            assert_eq!(
                build_command_line(&["a.exe".into(), "has space".into()]),
                "a.exe \"has space\""
            );
        }

        #[test]
        fn empty_arg_is_quoted() {
            assert_eq!(
                build_command_line(&["a.exe".into(), "".into()]),
                "a.exe \"\""
            );
        }

        #[test]
        fn embedded_quote_is_escaped() {
            assert_eq!(
                build_command_line(&["a.exe".into(), "say \"hi\"".into()]),
                "a.exe \"say \\\"hi\\\"\""
            );
        }

        #[test]
        fn trailing_backslashes_before_closing_quote_are_doubled() {
            let mut out = String::new();
            quote_arg("C:\\path with space\\", &mut out);
            assert_eq!(out, "\"C:\\path with space\\\\\"");
        }

        #[test]
        fn backslashes_not_before_quote_are_preserved() {
            let mut out = String::new();
            quote_arg("C:\\no\\spaces", &mut out);
            assert_eq!(out, "C:\\no\\spaces");
        }
    }
}

mod envblock {
    //! CREATE_UNICODE_ENVIRONMENT block builder: entries sorted
    //! case-insensitively, each NUL-terminated, block double-NUL-terminated.

    use std::collections::HashMap;

    pub fn build_env_block(env: &HashMap<String, String>) -> Vec<u16> {
        let mut pairs: Vec<(&String, &String)> = env.iter().collect();
        pairs.sort_by(|a, b| a.0.to_lowercase().cmp(&b.0.to_lowercase()));
        let mut block: Vec<u16> = Vec::new();
        for (k, v) in pairs {
            let entry = format!("{k}={v}");
            block.extend(entry.encode_utf16());
            block.push(0);
        }
        if block.is_empty() {
            block.push(0);
        }
        block.push(0);
        block
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        fn decode(block: &[u16]) -> Vec<String> {
            block
                .split(|&c| c == 0)
                .filter(|s| !s.is_empty())
                .map(|s| String::from_utf16(s).unwrap())
                .collect()
        }

        #[test]
        fn empty_env_is_double_nul() {
            let block = build_env_block(&HashMap::new());
            assert_eq!(block, vec![0u16, 0u16]);
        }

        #[test]
        fn sorted_case_insensitively() {
            let mut env = HashMap::new();
            env.insert("banana".to_string(), "1".to_string());
            env.insert("Apple".to_string(), "2".to_string());
            env.insert("cherry".to_string(), "3".to_string());
            let entries = decode(&build_env_block(&env));
            assert_eq!(entries, vec!["Apple=2", "banana=1", "cherry=3"]);
        }

        #[test]
        fn double_nul_terminated() {
            let mut env = HashMap::new();
            env.insert("A".to_string(), "1".to_string());
            let block = build_env_block(&env);
            assert_eq!(&block[block.len() - 2..], &[0u16, 0u16]);
        }
    }
}

#[derive(Debug)]
struct ProtocolError(String);

impl fmt::Display for ProtocolError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

#[cfg_attr(not(windows), allow(dead_code))]
#[derive(Debug)]
enum Command {
    Hello {
        id: u64,
        protocol: u32,
    },
    Create {
        id: u64,
        run_id: String,
        job_name: String,
        argv: Vec<String>,
        cwd: String,
        env: HashMap<String, String>,
        stdin_path: String,
        stdout_path: String,
        stderr_path: String,
    },
    Resume {
        id: u64,
    },
    Stop {
        id: u64,
        grace_ms: u64,
    },
    Abort {
        id: u64,
    },
}

/// Parses a JSON object while rejecting duplicate top-level keys, which
/// `serde_json`'s normal map deserialization would silently overwrite.
struct RawObject(HashMap<String, Value>);

impl<'de> Deserialize<'de> for RawObject {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        struct RawObjectVisitor;
        impl<'de> Visitor<'de> for RawObjectVisitor {
            type Value = RawObject;
            fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str("a JSON object with unique keys")
            }
            fn visit_map<A>(self, mut map: A) -> Result<Self::Value, A::Error>
            where
                A: MapAccess<'de>,
            {
                let mut out = HashMap::new();
                while let Some((k, v)) = map.next_entry::<String, Value>()? {
                    if out.insert(k.clone(), v).is_some() {
                        return Err(de::Error::custom(format!("duplicate key: {k}")));
                    }
                }
                Ok(RawObject(out))
            }
        }
        deserializer.deserialize_map(RawObjectVisitor)
    }
}

fn field_str(raw: &mut HashMap<String, Value>, key: &str) -> Result<String, ProtocolError> {
    let v = raw
        .remove(key)
        .ok_or_else(|| ProtocolError(format!("missing field: {key}")))?;
    let s = v
        .as_str()
        .ok_or_else(|| ProtocolError(format!("field {key} must be a string")))?;
    if s.contains('\0') {
        return Err(ProtocolError(format!("field {key} contains NUL")));
    }
    Ok(s.to_string())
}

fn field_u64(raw: &mut HashMap<String, Value>, key: &str) -> Result<u64, ProtocolError> {
    let v = raw
        .remove(key)
        .ok_or_else(|| ProtocolError(format!("missing field: {key}")))?;
    v.as_u64()
        .ok_or_else(|| ProtocolError(format!("field {key} must be a non-negative integer")))
}

fn field_argv(raw: &mut HashMap<String, Value>, key: &str) -> Result<Vec<String>, ProtocolError> {
    let v = raw
        .remove(key)
        .ok_or_else(|| ProtocolError(format!("missing field: {key}")))?;
    let arr = v
        .as_array()
        .ok_or_else(|| ProtocolError(format!("field {key} must be an array")))?;
    if arr.is_empty() {
        return Err(ProtocolError("argv must not be empty".to_string()));
    }
    let mut out = Vec::with_capacity(arr.len());
    for item in arr {
        let s = item
            .as_str()
            .ok_or_else(|| ProtocolError(format!("field {key} elements must be strings")))?;
        if s.contains('\0') {
            return Err(ProtocolError(format!("field {key} element contains NUL")));
        }
        out.push(s.to_string());
    }
    Ok(out)
}

fn field_env(
    raw: &mut HashMap<String, Value>,
    key: &str,
) -> Result<HashMap<String, String>, ProtocolError> {
    let v = raw
        .remove(key)
        .ok_or_else(|| ProtocolError(format!("missing field: {key}")))?;
    let obj = v
        .as_object()
        .ok_or_else(|| ProtocolError(format!("field {key} must be an object")))?;
    let mut out = HashMap::with_capacity(obj.len());
    let mut folded = std::collections::HashSet::with_capacity(obj.len());
    for (k, val) in obj {
        if k.is_empty() || !k.is_ascii() || k.contains('\0') || k.contains('=') {
            return Err(ProtocolError(format!("field {key} has invalid key")));
        }
        if !folded.insert(k.to_lowercase()) {
            return Err(ProtocolError(format!(
                "field {key} has duplicate case-insensitive key"
            )));
        }
        let s = val
            .as_str()
            .ok_or_else(|| ProtocolError(format!("field {key}.{k} must be a string")))?;
        if s.contains('\0') {
            return Err(ProtocolError(format!("field {key}.{k} contains NUL")));
        }
        out.insert(k.clone(), s.to_string());
    }
    Ok(out)
}

fn check_empty(raw: &HashMap<String, Value>) -> Result<(), ProtocolError> {
    if let Some(k) = raw.keys().next() {
        return Err(ProtocolError(format!("unknown field: {k}")));
    }
    Ok(())
}

fn valid_run_id(value: &str) -> bool {
    value.len() == 36
        && value.bytes().enumerate().all(|(index, byte)| {
            if matches!(index, 8 | 13 | 18 | 23) {
                byte == b'-'
            } else {
                byte.is_ascii_hexdigit()
            }
        })
}

fn de_command_from_raw(mut raw: HashMap<String, Value>) -> Result<Command, ProtocolError> {
    let op = raw
        .remove("op")
        .ok_or_else(|| ProtocolError("missing field: op".into()))?;
    let op = op
        .as_str()
        .ok_or_else(|| ProtocolError("field op must be a string".into()))?
        .to_string();
    let id = field_u64(&mut raw, "id")?;
    let cmd = match op.as_str() {
        "hello" => {
            let protocol = field_u64(&mut raw, "protocol")?;
            check_empty(&raw)?;
            if protocol != u64::from(PROTOCOL_VERSION) {
                return Err(ProtocolError(format!("unsupported protocol: {protocol}")));
            }
            Command::Hello {
                id,
                protocol: PROTOCOL_VERSION,
            }
        }
        "create" => {
            let run_id = field_str(&mut raw, "run_id")?;
            let job_name = field_str(&mut raw, "job_name")?;
            let argv = field_argv(&mut raw, "argv")?;
            let cwd = field_str(&mut raw, "cwd")?;
            let env = field_env(&mut raw, "env")?;
            let stdin_path = field_str(&mut raw, "stdin_path")?;
            let stdout_path = field_str(&mut raw, "stdout_path")?;
            let stderr_path = field_str(&mut raw, "stderr_path")?;
            check_empty(&raw)?;
            if !valid_run_id(&run_id) {
                return Err(ProtocolError("run_id must be a canonical UUID".into()));
            }
            if !job_name.starts_with(r"Local\ccc-") {
                return Err(ProtocolError(
                    "job_name must use the Local\\ccc- namespace".into(),
                ));
            }
            let folded_paths = [
                stdin_path.to_lowercase(),
                stdout_path.to_lowercase(),
                stderr_path.to_lowercase(),
            ];
            if folded_paths[0] == folded_paths[1]
                || folded_paths[0] == folded_paths[2]
                || folded_paths[1] == folded_paths[2]
            {
                return Err(ProtocolError("stdio paths must be distinct".into()));
            }
            Command::Create {
                id,
                run_id,
                job_name,
                argv,
                cwd,
                env,
                stdin_path,
                stdout_path,
                stderr_path,
            }
        }
        "resume" => {
            check_empty(&raw)?;
            Command::Resume { id }
        }
        "stop" => {
            let grace_ms = field_u64(&mut raw, "grace_ms")?;
            check_empty(&raw)?;
            if grace_ms > MAX_GRACE_MS {
                return Err(ProtocolError("grace_ms out of range".into()));
            }
            Command::Stop { id, grace_ms }
        }
        "abort" => {
            check_empty(&raw)?;
            Command::Abort { id }
        }
        other => return Err(ProtocolError(format!("unknown op: {other}"))),
    };
    Ok(cmd)
}

fn parse_command(line: &str) -> Result<Command, ProtocolError> {
    let mut de = serde_json::Deserializer::from_str(line);
    let RawObject(raw) = Deserialize::deserialize(&mut de)
        .map_err(|e| ProtocolError(format!("invalid json: {e}")))?;
    de.end()
        .map_err(|e| ProtocolError(format!("invalid trailing json: {e}")))?;
    de_command_from_raw(raw)
}

#[cfg(windows)]
fn extract_id(line: &str) -> Value {
    serde_json::from_str::<Value>(line)
        .ok()
        .and_then(|v| v.get("id").cloned())
        .unwrap_or(Value::Null)
}

#[cfg(windows)]
fn emit(out: &mut impl Write, value: &Value) {
    if writeln!(out, "{value}").is_err() || out.flush().is_err() {
        eprintln!("ccc-win-supervisor: protocol output closed unexpectedly");
        std::process::exit(1);
    }
}

#[cfg(windows)]
fn err_reply(id: u64, msg: &str) -> Value {
    serde_json::json!({ "id": id, "ok": false, "error": msg })
}

#[cfg(windows)]
fn err_reply_value(id: Value, msg: &str) -> Value {
    serde_json::json!({ "id": id, "ok": false, "error": msg })
}

#[cfg(windows)]
#[derive(Debug, PartialEq, Eq)]
enum Phase {
    Idle,
    Created,
    Running,
    Done,
}

#[cfg(windows)]
mod win {
    use super::*;
    use std::ffi::c_void;
    use std::ffi::OsStr;
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Foundation::{
        CloseHandle, GetLastError, ERROR_ACCESS_DENIED, ERROR_ALREADY_EXISTS, FILETIME, HANDLE,
        HANDLE_FLAG_INHERIT, INVALID_HANDLE_VALUE, WAIT_OBJECT_0,
    };
    use windows_sys::Win32::Security::SECURITY_ATTRIBUTES;
    use windows_sys::Win32::Storage::FileSystem::{
        CreateFileW, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, FILE_SHARE_READ, FILE_SHARE_WRITE,
        GENERIC_READ, GENERIC_WRITE, OPEN_EXISTING,
    };
    use windows_sys::Win32::System::Console::{
        GenerateConsoleCtrlEvent, GetStdHandle, CTRL_BREAK_EVENT, STD_ERROR_HANDLE,
        STD_INPUT_HANDLE, STD_OUTPUT_HANDLE,
    };
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, IsProcessInJob,
        JobObjectExtendedLimitInformation, SetInformationJobObject, TerminateJobObject,
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };
    use windows_sys::Win32::System::Threading::{
        CreateProcessW, GetExitCodeProcess, GetProcessTimes, ResumeThread, SetHandleInformation,
        TerminateProcess, WaitForSingleObject, CREATE_BREAKAWAY_FROM_JOB, CREATE_NEW_PROCESS_GROUP,
        CREATE_SUSPENDED, CREATE_UNICODE_ENVIRONMENT, INFINITE, PROCESS_INFORMATION,
        STARTF_USESTDHANDLES, STARTUPINFOW,
    };

    pub enum Event {
        Line(String),
        Oversized,
        InvalidUtf8,
        Eof,
        ChildExited(u32),
    }

    fn to_wide(s: &str) -> Vec<u16> {
        OsStr::new(s)
            .encode_wide()
            .chain(std::iter::once(0))
            .collect()
    }

    struct Handle(HANDLE);
    unsafe impl Send for Handle {}
    impl Drop for Handle {
        fn drop(&mut self) {
            if !self.0.is_null() && self.0 != INVALID_HANDLE_VALUE {
                unsafe {
                    CloseHandle(self.0);
                }
            }
        }
    }

    struct SendableHandle(HANDLE);
    unsafe impl Send for SendableHandle {}

    pub struct Child {
        job: Handle,
        process: Handle,
        thread: Option<Handle>,
        pub pid: u32,
        pub creation_filetime: String,
        pub broke_away: bool,
    }

    fn last_error(context: &str) -> String {
        format!("{context} failed (GetLastError={})", unsafe {
            GetLastError()
        })
    }

    fn open_inheritable_file(path: &str, write: bool) -> Result<Handle, String> {
        let wide = to_wide(path);
        let sa = SECURITY_ATTRIBUTES {
            nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: std::ptr::null_mut(),
            bInheritHandle: 1,
        };
        let (access, disposition) = if write {
            (GENERIC_WRITE, CREATE_ALWAYS)
        } else {
            (GENERIC_READ, OPEN_EXISTING)
        };
        let handle = unsafe {
            CreateFileW(
                wide.as_ptr(),
                access,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                &sa,
                disposition,
                FILE_ATTRIBUTE_NORMAL,
                std::ptr::null_mut(),
            )
        };
        if handle == INVALID_HANDLE_VALUE {
            return Err(last_error(&format!("CreateFileW({path})")));
        }
        Ok(Handle(handle))
    }

    fn clear_control_handle_inheritance() -> Result<(), String> {
        for kind in [STD_INPUT_HANDLE, STD_OUTPUT_HANDLE, STD_ERROR_HANDLE] {
            let handle = unsafe { GetStdHandle(kind) };
            if !handle.is_null()
                && handle != INVALID_HANDLE_VALUE
                && unsafe { SetHandleInformation(handle, HANDLE_FLAG_INHERIT, 0) } == 0
            {
                return Err(last_error("SetHandleInformation(control pipe)"));
            }
        }
        Ok(())
    }

    pub fn create_child(
        job_name: &str,
        argv: &[String],
        cwd: &str,
        env: &HashMap<String, String>,
        stdin_path: &str,
        stdout_path: &str,
        stderr_path: &str,
    ) -> Result<Child, String> {
        let job_name_w = to_wide(job_name);
        let job_handle = unsafe { CreateJobObjectW(std::ptr::null(), job_name_w.as_ptr()) };
        if job_handle.is_null() {
            return Err(last_error("CreateJobObjectW"));
        }
        let job = Handle(job_handle);
        if unsafe { GetLastError() } == ERROR_ALREADY_EXISTS {
            return Err("job already exists".to_string());
        }

        let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { std::mem::zeroed() };
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let ok = unsafe {
            SetInformationJobObject(
                job.0,
                JobObjectExtendedLimitInformation,
                &limits as *const _ as *const c_void,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
        };
        if ok == 0 {
            return Err(last_error("SetInformationJobObject"));
        }

        let stdin_h = open_inheritable_file(stdin_path, false)?;
        let stdout_h = open_inheritable_file(stdout_path, true)?;
        let stderr_h = open_inheritable_file(stderr_path, true)?;

        let cmdline = cmdline::build_command_line(argv);
        let mut cmdline_w = to_wide(&cmdline);
        let cwd_w = to_wide(cwd);
        let mut env_block = envblock::build_env_block(env);

        let mut startup: STARTUPINFOW = unsafe { std::mem::zeroed() };
        startup.cb = std::mem::size_of::<STARTUPINFOW>() as u32;
        startup.dwFlags = STARTF_USESTDHANDLES;
        startup.hStdInput = stdin_h.0;
        startup.hStdOutput = stdout_h.0;
        startup.hStdError = stderr_h.0;

        let mut proc_info: PROCESS_INFORMATION = unsafe { std::mem::zeroed() };
        let base_flags = CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP | CREATE_UNICODE_ENVIRONMENT;
        let mut broke_away = true;
        let mut created = unsafe {
            CreateProcessW(
                std::ptr::null(),
                cmdline_w.as_mut_ptr(),
                std::ptr::null(),
                std::ptr::null(),
                1,
                base_flags | CREATE_BREAKAWAY_FROM_JOB,
                env_block.as_mut_ptr() as *mut c_void,
                cwd_w.as_ptr(),
                &startup,
                &mut proc_info,
            )
        };
        if created == 0 && unsafe { GetLastError() } == ERROR_ACCESS_DENIED {
            // The current job does not permit breakaway; retry nested.
            broke_away = false;
            cmdline_w = to_wide(&cmdline);
            proc_info = unsafe { std::mem::zeroed() };
            created = unsafe {
                CreateProcessW(
                    std::ptr::null(),
                    cmdline_w.as_mut_ptr(),
                    std::ptr::null(),
                    std::ptr::null(),
                    1,
                    base_flags,
                    env_block.as_mut_ptr() as *mut c_void,
                    cwd_w.as_ptr(),
                    &startup,
                    &mut proc_info,
                )
            };
        }

        unsafe {
            SetHandleInformation(stdin_h.0, HANDLE_FLAG_INHERIT, 0);
            SetHandleInformation(stdout_h.0, HANDLE_FLAG_INHERIT, 0);
            SetHandleInformation(stderr_h.0, HANDLE_FLAG_INHERIT, 0);
        }
        drop(stdin_h);
        drop(stdout_h);
        drop(stderr_h);

        if created == 0 {
            return Err(last_error("CreateProcessW"));
        }

        let process = Handle(proc_info.hProcess);
        let thread = Handle(proc_info.hThread);

        if unsafe { AssignProcessToJobObject(job.0, process.0) } == 0 {
            let msg = last_error("AssignProcessToJobObject");
            unsafe {
                TerminateProcess(process.0, 1);
            }
            return Err(msg);
        }

        let mut in_job: i32 = 0;
        let checked = unsafe { IsProcessInJob(process.0, job.0, &mut in_job) };
        if checked == 0 || in_job == 0 {
            unsafe {
                TerminateProcess(process.0, 1);
            }
            return Err("IsProcessInJob verification failed".to_string());
        }

        let mut creation: FILETIME = unsafe { std::mem::zeroed() };
        let mut exit_t: FILETIME = unsafe { std::mem::zeroed() };
        let mut kernel_t: FILETIME = unsafe { std::mem::zeroed() };
        let mut user_t: FILETIME = unsafe { std::mem::zeroed() };
        if unsafe {
            GetProcessTimes(
                process.0,
                &mut creation,
                &mut exit_t,
                &mut kernel_t,
                &mut user_t,
            )
        } == 0
        {
            unsafe {
                TerminateProcess(process.0, 1);
            }
            return Err(last_error("GetProcessTimes"));
        }
        let creation_ticks =
            ((creation.dwHighDateTime as u64) << 32) | creation.dwLowDateTime as u64;

        Ok(Child {
            job,
            process,
            thread: Some(thread),
            pid: proc_info.dwProcessId,
            creation_filetime: creation_ticks.to_string(),
            broke_away,
        })
    }

    pub fn resume(child: &mut Child) -> Result<(), String> {
        let thread = child
            .thread
            .take()
            .ok_or_else(|| "resume called twice".to_string())?;
        let prev = unsafe { ResumeThread(thread.0) };
        if prev != 1 {
            let message = if prev == u32::MAX {
                last_error("ResumeThread")
            } else {
                format!("ResumeThread returned unexpected suspend count {prev}")
            };
            terminate_job(child);
            return Err(message);
        }
        Ok(())
    }

    pub fn spawn_waiter(process: &Child, tx: Sender<Event>) {
        let handle = SendableHandle(process.process.0);
        thread::spawn(move || {
            let SendableHandle(h) = handle;
            if unsafe { WaitForSingleObject(h, INFINITE) } != WAIT_OBJECT_0 {
                eprintln!("ccc-win-supervisor: child wait failed");
                std::process::exit(1);
            }
            let mut code: u32 = 0;
            if unsafe { GetExitCodeProcess(h, &mut code) } == 0 {
                eprintln!("ccc-win-supervisor: child exit query failed");
                std::process::exit(1);
            }
            let _ = tx.send(Event::ChildExited(code));
        });
    }

    pub fn send_ctrl_break(pid: u32) {
        unsafe {
            GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, pid);
        }
    }

    /// `WAIT_OBJECT_0` is 0; a nonzero result means still running or an error.
    pub fn has_exited(child: &Child) -> bool {
        unsafe { WaitForSingleObject(child.process.0, 0) == WAIT_OBJECT_0 }
    }

    pub fn terminate_job(child: &Child) {
        if unsafe { TerminateJobObject(child.job.0, 1) } == 0 {
            eprintln!("ccc-win-supervisor: TerminateJobObject failed");
            std::process::exit(1);
        }
    }

    fn spawn_stdin_reader(tx: Sender<Event>) {
        thread::spawn(move || {
            let stdin = io::stdin();
            let mut reader = io::BufReader::new(stdin.lock());
            loop {
                match read_frame(&mut reader) {
                    Ok(None) => {
                        let _ = tx.send(Event::Eof);
                        break;
                    }
                    Ok(Some(Frame::Line(text))) => {
                        if tx.send(Event::Line(text)).is_err() {
                            break;
                        }
                    }
                    Ok(Some(Frame::Oversized)) => {
                        if tx.send(Event::Oversized).is_err() {
                            break;
                        }
                    }
                    Ok(Some(Frame::InvalidUtf8)) => {
                        if tx.send(Event::InvalidUtf8).is_err() {
                            break;
                        }
                    }
                    Err(_) => {
                        let _ = tx.send(Event::Eof);
                        break;
                    }
                }
            }
        });
    }

    fn handle_command(
        cmd: Command,
        phase: &mut Phase,
        child: &mut Option<Child>,
        has_waiter: &mut bool,
        tx: &Sender<Event>,
        out: &mut impl Write,
    ) {
        match cmd {
            Command::Hello { id, protocol } => {
                emit(
                    out,
                    &serde_json::json!({
                        "id": id, "ok": true, "event": "hello",
                        "protocol": protocol, "version": HELPER_VERSION
                    }),
                );
            }
            Command::Create {
                id,
                run_id,
                job_name,
                argv,
                cwd,
                env,
                stdin_path,
                stdout_path,
                stderr_path,
            } => {
                if *phase != Phase::Idle {
                    emit(out, &err_reply(id, "create called in invalid state"));
                    return;
                }
                eprintln!("ccc-win-supervisor: create run_id={run_id}");
                match create_child(
                    &job_name,
                    &argv,
                    &cwd,
                    &env,
                    &stdin_path,
                    &stdout_path,
                    &stderr_path,
                ) {
                    Ok(c) => {
                        let pid = c.pid;
                        let creation_filetime = c.creation_filetime.clone();
                        let broke_away = c.broke_away;
                        *child = Some(c);
                        spawn_waiter(child.as_ref().unwrap(), tx.clone());
                        *has_waiter = true;
                        *phase = Phase::Created;
                        emit(
                            out,
                            &serde_json::json!({
                                "id": id, "ok": true, "event": "created",
                                "pid": pid, "creation_filetime": creation_filetime,
                                "broke_away": broke_away
                            }),
                        );
                    }
                    Err(msg) => emit(out, &err_reply(id, &msg)),
                }
            }
            Command::Resume { id } => {
                if *phase != Phase::Created {
                    emit(out, &err_reply(id, "resume called in invalid state"));
                    return;
                }
                let c = child.as_mut().expect("phase Created implies child present");
                match resume(c) {
                    Ok(()) => {
                        *phase = Phase::Running;
                        emit(
                            out,
                            &serde_json::json!({ "id": id, "ok": true, "event": "running" }),
                        );
                    }
                    Err(msg) => emit(out, &err_reply(id, &msg)),
                }
            }
            Command::Stop { id, grace_ms } => {
                if *phase != Phase::Running {
                    emit(out, &err_reply(id, "stop called in invalid state"));
                    return;
                }
                let c = child.as_ref().expect("phase Running implies child present");
                send_ctrl_break(c.pid);
                thread::sleep(Duration::from_millis(grace_ms));
                if !has_exited(c) {
                    terminate_job(c);
                }
                emit(
                    out,
                    &serde_json::json!({ "id": id, "ok": true, "event": "stopping" }),
                );
            }
            Command::Abort { id } => {
                if *phase != Phase::Created && *phase != Phase::Running {
                    emit(out, &err_reply(id, "abort called in invalid state"));
                    return;
                }
                let c = child.as_ref().expect("phase implies child present");
                terminate_job(c);
                if !*has_waiter {
                    spawn_waiter(c, tx.clone());
                    *has_waiter = true;
                }
                emit(
                    out,
                    &serde_json::json!({ "id": id, "ok": true, "event": "aborted" }),
                );
            }
        }
    }

    pub fn win_main() {
        if let Err(message) = clear_control_handle_inheritance() {
            eprintln!("ccc-win-supervisor: {message}");
            std::process::exit(1);
        }
        let stdout = io::stdout();
        let mut out = stdout.lock();
        let (tx, rx) = mpsc::channel::<Event>();
        spawn_stdin_reader(tx.clone());

        let mut phase = Phase::Idle;
        let mut child: Option<Child> = None;
        let mut has_waiter = false;

        loop {
            let event = match rx.recv() {
                Ok(e) => e,
                Err(_) => break,
            };
            match event {
                Event::Eof => {
                    if phase != Phase::Done {
                        if let Some(c) = &child {
                            terminate_job(c);
                        }
                        eprintln!("ccc-win-supervisor: stdin closed before completion, exiting");
                        std::process::exit(1);
                    }
                    break;
                }
                Event::Oversized => {
                    emit(&mut out, &err_reply_value(Value::Null, "frame too large"));
                }
                Event::InvalidUtf8 => {
                    emit(
                        &mut out,
                        &err_reply_value(Value::Null, "frame is not valid UTF-8"),
                    );
                }
                Event::ChildExited(code) => {
                    phase = Phase::Done;
                    emit(
                        &mut out,
                        &serde_json::json!({ "event": "exited", "exit_code": code, "reason": Value::Null }),
                    );
                }
                Event::Line(text) => match parse_command(&text) {
                    Ok(cmd) => {
                        handle_command(cmd, &mut phase, &mut child, &mut has_waiter, &tx, &mut out)
                    }
                    Err(e) => emit(&mut out, &err_reply_value(extract_id(&text), &e.0)),
                },
            }
        }
        std::process::exit(0);
    }
}

fn main() {
    #[cfg(windows)]
    {
        win::win_main();
    }
    #[cfg(not(windows))]
    {
        eprintln!("ccc-win-supervisor only runs on Windows");
        std::process::exit(2);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_hello() {
        let cmd = parse_command(r#"{"id":1,"op":"hello","protocol":1}"#).unwrap();
        assert!(matches!(cmd, Command::Hello { id: 1, protocol: 1 }));
    }

    #[test]
    fn bounded_frame_reader_drains_oversized_input() {
        let mut input = vec![b'x'; MAX_FRAME_BYTES + 1];
        input.extend_from_slice(b"\n{\"id\":1}\n");
        let mut reader = io::BufReader::new(io::Cursor::new(input));
        assert_eq!(read_frame(&mut reader).unwrap(), Some(Frame::Oversized));
        assert_eq!(
            read_frame(&mut reader).unwrap(),
            Some(Frame::Line("{\"id\":1}".into()))
        );
    }

    #[test]
    fn frame_reader_rejects_invalid_utf8() {
        let mut reader = io::BufReader::new(io::Cursor::new(vec![0xff, b'\n']));
        assert_eq!(read_frame(&mut reader).unwrap(), Some(Frame::InvalidUtf8));
    }

    #[test]
    fn rejects_unknown_protocol() {
        let err = parse_command(r#"{"id":1,"op":"hello","protocol":2}"#).unwrap_err();
        assert!(err.0.contains("unsupported protocol"));
    }

    #[test]
    fn rejects_protocol_integer_that_would_truncate_to_one() {
        let err = parse_command(r#"{"id":1,"op":"hello","protocol":4294967297}"#).unwrap_err();
        assert!(err.0.contains("unsupported protocol"));
    }

    #[test]
    fn rejects_trailing_json() {
        let err = parse_command(r#"{"id":1,"op":"hello","protocol":1} {}"#).unwrap_err();
        assert!(err.0.contains("trailing"));
    }

    #[test]
    fn rejects_unknown_field() {
        let err = parse_command(r#"{"id":1,"op":"hello","protocol":1,"extra":true}"#).unwrap_err();
        assert!(err.0.contains("unknown field"));
    }

    #[test]
    fn rejects_missing_field() {
        let err = parse_command(r#"{"id":1,"op":"hello"}"#).unwrap_err();
        assert!(err.0.contains("missing field"));
    }

    #[test]
    fn rejects_duplicate_key() {
        let err = parse_command(r#"{"id":1,"id":2,"op":"hello","protocol":1}"#).unwrap_err();
        assert!(err.0.contains("invalid json"));
    }

    #[test]
    fn rejects_unknown_op() {
        let err = parse_command(r#"{"id":1,"op":"nope"}"#).unwrap_err();
        assert!(err.0.contains("unknown op"));
    }

    #[test]
    fn rejects_empty_argv() {
        let line = r#"{"id":1,"op":"create","run_id":"r","job_name":"j","argv":[],
            "cwd":"c","env":{},"stdin_path":"i","stdout_path":"o","stderr_path":"e"}"#;
        let err = parse_command(line).unwrap_err();
        assert!(err.0.contains("argv must not be empty"));
    }

    #[test]
    fn rejects_nul_in_string_field() {
        let line = "{\"id\":1,\"op\":\"create\",\"run_id\":\"r\\u0000\",\"job_name\":\"j\",\"argv\":[\"a\"],\
            \"cwd\":\"c\",\"env\":{},\"stdin_path\":\"i\",\"stdout_path\":\"o\",\"stderr_path\":\"e\"}";
        let err = parse_command(line).unwrap_err();
        assert!(err.0.contains("NUL"));
    }

    #[test]
    fn rejects_out_of_range_grace_ms() {
        let err = parse_command(r#"{"id":1,"op":"stop","grace_ms":30001}"#).unwrap_err();
        assert!(err.0.contains("grace_ms out of range"));
    }

    #[test]
    fn accepts_valid_create() {
        let line = r#"{"id":1,"op":"create",
            "run_id":"12345678-1234-1234-1234-123456789abc",
            "job_name":"Local\\ccc-123","argv":["a.exe"],
            "cwd":"c","env":{"A":"1"},"stdin_path":"i","stdout_path":"o","stderr_path":"e"}"#;
        assert!(parse_command(line).is_ok());
    }

    #[test]
    fn rejects_noncanonical_run_id_and_unsafe_job_namespace() {
        let bad_run = r#"{"id":1,"op":"create","run_id":"r","job_name":"Local\\ccc-1",
            "argv":["a.exe"],"cwd":"c","env":{},"stdin_path":"i","stdout_path":"o","stderr_path":"e"}"#;
        assert!(parse_command(bad_run).unwrap_err().0.contains("run_id"));
        let bad_job = r#"{"id":1,"op":"create","run_id":"12345678-1234-1234-1234-123456789abc",
            "job_name":"Global\\other","argv":["a.exe"],"cwd":"c","env":{},
            "stdin_path":"i","stdout_path":"o","stderr_path":"e"}"#;
        assert!(parse_command(bad_job).unwrap_err().0.contains("job_name"));
    }
}
