"""Host identity, process liveness, and private-state primitives."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import uuid
from pathlib import Path

SYSTEM_SID = "S-1-5-18"
STILL_ACTIVE = 259
FILE_ALL_ACCESS = 0x001F01FF


class HostError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class LinuxHost:
    """Linux behavior retained behind the portable host interface."""

    name = "linux"

    def host_identity(self):
        machine = Path("/etc/machine-id").read_text().strip()
        if not machine:
            raise HostError("host_identity", "A nonempty /etc/machine-id is required.")
        return hashlib.sha256(machine.encode()).hexdigest()

    def boot_id(self):
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()

    def pid_namespace(self):
        return os.readlink("/proc/self/ns/pid")

    def proc_identity(self, pid):
        if not pid:
            return None
        try:
            tail = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()
            return {"start": tail[19], "state": tail[0], "group": int(tail[2])}
        except (FileNotFoundError, ProcessLookupError):
            return None

    def alive(self, pid, start, boot):
        if boot != self.boot_id():
            return False
        info = self.proc_identity(pid)
        return bool(info and info["start"] == start and info["state"] not in ("Z", "X"))

    def group_alive(self, group):
        for entry in Path("/proc").iterdir():
            if entry.name.isdigit():
                info = self.proc_identity(entry.name)
                if info and info["group"] == group and info["state"] not in ("Z", "X"):
                    return True
        return False

    def principal_identity(self):
        return f"uid:{os.getuid()}"

    def private_dir(self, path):
        path = Path(path).absolute()
        if path.is_symlink():
            raise HostError("unsafe_state", "State directory cannot be a symlink.")
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = path.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise HostError("unsafe_state", f"Require an owned mode-0700 directory: {path}")
        return path.resolve()

    def secure_new_file(self, path):
        os.chmod(path, 0o600)

    def reject_reparse(self, path):
        if Path(path).is_symlink():
            raise HostError("unsafe_state", f"Refusing a symlink: {path}")

    def flush_directory(self, path):
        directory_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def verify_private_entry(self, path):
        entry = Path(path)
        if (
            entry.is_symlink()
            or entry.stat().st_uid != os.getuid()
            or entry.stat().st_mode & 0o077
        ):
            raise HostError(
                "unsafe_state", f"State entry must be private and owned: {entry.name}"
            )

    def write_json(self, path, value):
        path = Path(path)
        tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        with tmp.open("x", encoding="utf-8") as handle:
            os.chmod(tmp, 0o600)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


class _Win32Api:
    """Typed Win32 calls, constructed only on Windows."""

    def __init__(self):
        import ctypes
        import winreg
        from ctypes import wintypes

        self.ctypes = ctypes
        self.wintypes = wintypes
        self.winreg = winreg
        self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.ntdll = ctypes.WinDLL("ntdll")
        self._bind()

    def _bind(self):
        ctypes, wintypes = self.ctypes, self.wintypes
        handle_p = ctypes.POINTER(wintypes.HANDLE)
        void_p_p = ctypes.POINTER(ctypes.c_void_p)

        self.advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, handle_p]
        self.advapi32.OpenProcessToken.restype = wintypes.BOOL
        self.advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.advapi32.GetTokenInformation.restype = wintypes.BOOL
        self.advapi32.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.LPWSTR),
        ]
        self.advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        self.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            void_p_p,
            ctypes.POINTER(wintypes.ULONG),
        ]
        self.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
        self.advapi32.SetFileSecurityW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        self.advapi32.SetFileSecurityW.restype = wintypes.BOOL
        self.advapi32.GetNamedSecurityInfoW.argtypes = [
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            void_p_p,
            void_p_p,
            void_p_p,
            void_p_p,
            void_p_p,
        ]
        self.advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
        self.advapi32.GetSecurityDescriptorControl.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.WORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
        self.advapi32.GetAclInformation.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self.advapi32.GetAclInformation.restype = wintypes.BOOL
        self.advapi32.GetAce.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            void_p_p,
        ]
        self.advapi32.GetAce.restype = wintypes.BOOL

        self.kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        self.kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel32.OpenProcess.restype = wintypes.HANDLE
        self.kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        self.kernel32.GetProcessTimes.restype = wintypes.BOOL
        self.kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        self.kernel32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
        self.kernel32.GetFileAttributesW.restype = wintypes.DWORD
        self.kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self.kernel32.CreateFileW.restype = wintypes.HANDLE
        self.kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        self.kernel32.FlushFileBuffers.restype = wintypes.BOOL
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL
        self.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self.kernel32.LocalFree.restype = ctypes.c_void_p

    def machine_guid(self):
        with self.winreg.OpenKey(
            self.winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography"
        ) as key:
            value, _ = self.winreg.QueryValueEx(key, "MachineGuid")
        return value

    def current_user_sid(self):
        ctypes, wintypes = self.ctypes, self.wintypes
        token = wintypes.HANDLE()
        if not self.advapi32.OpenProcessToken(
            self.kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)
        ):
            raise HostError("host_identity", "OpenProcessToken failed.")
        try:
            size = wintypes.DWORD()
            self.advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(size.value)
            if not self.advapi32.GetTokenInformation(
                token, 1, buffer, size.value, ctypes.byref(size)
            ):
                raise HostError("host_identity", "GetTokenInformation failed.")
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            return self._sid_to_string(sid)
        finally:
            self.close_handle(token)

    def _sid_to_string(self, sid):
        value = self.wintypes.LPWSTR()
        if not self.advapi32.ConvertSidToStringSidW(sid, self.ctypes.byref(value)):
            return None
        try:
            return value.value
        finally:
            self.kernel32.LocalFree(value)

    def boot_marker(self):
        ctypes, wintypes = self.ctypes, self.wintypes

        class TimeOfDay(ctypes.Structure):
            _fields_ = [
                ("BootTime", ctypes.c_int64),
                ("CurrentTime", ctypes.c_int64),
                ("TimeZoneBias", ctypes.c_int64),
                ("CurrentTimeZoneId", wintypes.ULONG),
                ("Reserved", wintypes.ULONG),
            ]

        value = TimeOfDay()
        status = self.ntdll.NtQuerySystemInformation(
            3, ctypes.byref(value), ctypes.sizeof(value), None
        )
        if status != 0 or not value.BootTime:
            raise HostError("host_identity", "Could not read the Windows boot identity.")
        return f"win-boot:{value.BootTime:016x}"

    def open_process(self, pid):
        return self.kernel32.OpenProcess(0x1000, False, int(pid)) or None

    def process_times(self, handle):
        values = [self.wintypes.FILETIME() for _ in range(4)]
        if not self.kernel32.GetProcessTimes(
            handle, *(self.ctypes.byref(value) for value in values)
        ):
            return None
        creation = values[0]
        return creation.dwLowDateTime, creation.dwHighDateTime

    def process_exit_code(self, handle):
        code = self.wintypes.DWORD()
        if not self.kernel32.GetExitCodeProcess(handle, self.ctypes.byref(code)):
            return None
        return code.value

    def close_handle(self, handle):
        self.kernel32.CloseHandle(handle)

    def is_reparse_point(self, path):
        attributes = self.kernel32.GetFileAttributesW(str(path))
        if attributes == 0xFFFFFFFF:
            return None
        return bool(attributes & 0x400)

    def set_private_dacl(self, path, sid):
        descriptor = self.ctypes.c_void_p()
        sddl = f"D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)"
        if not self.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, self.ctypes.byref(descriptor), None
        ):
            raise HostError("unsafe_state", "Could not build a private security descriptor.")
        try:
            if not self.advapi32.SetFileSecurityW(str(path), 0x00000004, descriptor):
                raise HostError("unsafe_state", "Could not apply the private DACL.")
        finally:
            self.kernel32.LocalFree(descriptor)

    def read_owner_and_dacl(self, path):
        ctypes, wintypes = self.ctypes, self.wintypes
        owner = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        result = self.advapi32.GetNamedSecurityInfoW(
            str(path),
            1,
            0x00000001 | 0x00000004,
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
        if result:
            raise HostError("unsafe_state", f"Could not read the security descriptor: {path}")
        try:
            control = wintypes.WORD()
            revision = wintypes.DWORD()
            if not self.advapi32.GetSecurityDescriptorControl(
                descriptor, ctypes.byref(control), ctypes.byref(revision)
            ):
                raise HostError("unsafe_state", "Could not inspect DACL protection.")
            return {
                "owner": self._sid_to_string(owner),
                "protected": bool(control.value & 0x1000),
                "entries": self._dacl_entries(dacl),
            }
        finally:
            self.kernel32.LocalFree(descriptor)

    def _dacl_entries(self, dacl):
        ctypes, wintypes = self.ctypes, self.wintypes

        class AclSizeInformation(ctypes.Structure):
            _fields_ = [
                ("AceCount", wintypes.DWORD),
                ("AclBytesInUse", wintypes.DWORD),
                ("AclBytesFree", wintypes.DWORD),
            ]

        info = AclSizeInformation()
        if not dacl or not self.advapi32.GetAclInformation(
            dacl, ctypes.byref(info), ctypes.sizeof(info), 2
        ):
            raise HostError("unsafe_state", "Could not inspect the state DACL.")
        entries = []
        for index in range(info.AceCount):
            ace = ctypes.c_void_p()
            if not self.advapi32.GetAce(dacl, index, ctypes.byref(ace)):
                raise HostError("unsafe_state", "Could not inspect a state DACL entry.")
            raw = ace.value
            ace_type = ctypes.c_ubyte.from_address(raw).value
            flags = ctypes.c_ubyte.from_address(raw + 1).value
            mask = wintypes.DWORD.from_address(raw + 4).value
            trustee = self._sid_to_string(ctypes.c_void_p(raw + 8)) if ace_type == 0 else None
            entries.append({"type": ace_type, "flags": flags, "mask": mask, "sid": trustee})
        return entries

    def open_directory(self, path):
        handle = self.kernel32.CreateFileW(
            str(path), 0x80000000, 0x1 | 0x2, None, 3, 0x02000000, None
        )
        invalid = self.wintypes.HANDLE(-1).value
        if not handle or handle == invalid:
            raise HostError("write_failed", "Could not open the directory for durable flush.")
        return handle

    def flush_file_buffers(self, handle):
        if not self.kernel32.FlushFileBuffers(handle):
            raise HostError("write_failed", "Could not confirm directory durability.")


class WindowsHost:
    name = "windows"

    def __init__(self, api=None):
        self._api = api

    @property
    def api(self):
        if self._api is None:
            self._api = _Win32Api()
        return self._api

    def host_identity(self):
        guid = (self.api.machine_guid() or "").strip()
        if not guid:
            raise HostError("host_identity", "A nonempty MachineGuid is required.")
        return hashlib.sha256(guid.encode()).hexdigest()

    def principal_identity(self):
        sid = self.api.current_user_sid()
        if not sid:
            raise HostError("host_identity", "Could not resolve the current user SID.")
        return sid

    def boot_id(self):
        return self.api.boot_marker()

    def pid_namespace(self):
        return f"win-host:{self.host_identity()}"

    def proc_identity(self, pid):
        if not pid:
            return None
        handle = self.api.open_process(pid)
        if handle is None:
            return None
        try:
            times = self.api.process_times(handle)
            if times is None:
                return None
            exit_code = self.api.process_exit_code(handle)
            return {
                "start": f"{times[1]:08x}{times[0]:08x}",
                "state": "R" if exit_code == STILL_ACTIVE else "X",
                "group": int(pid),
            }
        finally:
            self.api.close_handle(handle)

    def alive(self, pid, start, boot):
        if boot != self.boot_id():
            return False
        info = self.proc_identity(pid)
        return bool(info and info["start"] == start and info["state"] != "X")

    def group_alive(self, group):
        info = self.proc_identity(group)
        return bool(info and info["state"] != "X")

    def _reject_reparse(self, path):
        reparse = self.api.is_reparse_point(path)
        if reparse is None:
            raise HostError("unsafe_state", f"Could not read attributes for: {path}")
        if reparse:
            raise HostError("unsafe_state", f"Refusing a reparse point: {path}")

    def private_dir(self, path):
        path = Path(path).absolute()
        if path.exists():
            self.verify_private_entry(path)
        else:
            path.mkdir(parents=True)
            self._reject_reparse(path)
            self.api.set_private_dacl(str(path), self.principal_identity())
        return path.resolve()

    def secure_new_file(self, path):
        self._reject_reparse(path)
        self.api.set_private_dacl(str(path), self.principal_identity())

    def reject_reparse(self, path):
        self._reject_reparse(path)

    def flush_directory(self, path):
        handle = self.api.open_directory(path)
        try:
            self.api.flush_file_buffers(handle)
        finally:
            self.api.close_handle(handle)

    def verify_private_entry(self, path):
        path = Path(path)
        self._reject_reparse(path)
        verdict = self.api.read_owner_and_dacl(str(path))
        principal = self.principal_identity()
        allowed = {principal, SYSTEM_SID}
        entries = verdict.get("entries") or []
        if verdict.get("owner") != principal:
            raise HostError("unsafe_state", f"State entry has a foreign owner: {path.name}")
        if not verdict.get("protected"):
            raise HostError("unsafe_state", f"State entry inherits permissions: {path.name}")
        if not entries or principal not in {entry.get("sid") for entry in entries}:
            raise HostError("unsafe_state", f"State entry omits its owner: {path.name}")
        if any(
            entry.get("type") != 0
            or entry.get("sid") not in allowed
            or entry.get("mask") != FILE_ALL_ACCESS
            or entry.get("flags", 0) & 0x10
            for entry in entries
        ):
            raise HostError("unsafe_state", f"State entry has an unsafe DACL: {path.name}")

    def write_json(self, path, value):
        path = Path(path)
        tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        with tmp.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        self.secure_new_file(tmp)
        os.replace(tmp, path)
        handle = self.api.open_directory(path.parent)
        try:
            self.api.flush_file_buffers(handle)
        finally:
            self.api.close_handle(handle)


def select_host(os_name=None, sys_platform=None, **kwargs):
    os_name = os.name if os_name is None else os_name
    sys_platform = sys.platform if sys_platform is None else sys_platform
    if os_name == "nt":
        return WindowsHost(**kwargs)
    if sys_platform.startswith("linux"):
        return LinuxHost()
    raise HostError("unsupported_platform", f"Unsupported platform: {sys_platform}")


HOST = select_host()


def host_identity():
    return HOST.host_identity()


def boot_id():
    return HOST.boot_id()


def pid_namespace():
    return HOST.pid_namespace()


def proc_identity(pid):
    return HOST.proc_identity(pid)


def alive(pid, start, boot):
    return HOST.alive(pid, start, boot)


def group_alive(group):
    return HOST.group_alive(group)


def private_dir(path):
    return HOST.private_dir(path)


def secure_new_file(path):
    return HOST.secure_new_file(path)


def reject_reparse(path):
    return HOST.reject_reparse(path)


def flush_directory(path):
    return HOST.flush_directory(path)


def verify_private_entry(path):
    return HOST.verify_private_entry(path)


def write_json(path, value):
    return HOST.write_json(path, value)


def principal_identity():
    return HOST.principal_identity()
