# Windows 네이티브 지원 구현 계획

작성: 2026-09-22  
기준: claude-control v0.13.0 / `33ecba8`  
상태: 설계 완료, 구현 미착수

## 1. 목표

Codex 데스크톱 앱이 실행되는 Windows 호스트에서 claude-control을 직접
설치하고, 같은 호스트의 네이티브 Claude Code 세션들을 관리한다. Linux에서
이미 제공하는 세션, task, queue, workflow, composition, monitor 기능을 Windows로
옮기되 다음 안전 조건은 약화하지 않는다.

1. 요청과 실행 예약은 멱등이며 같은 Claude 턴을 중복 실행하지 않는다.
2. PID 재사용으로 무관한 프로세스를 중단하지 않는다.
3. 중단과 timeout은 소유한 전체 프로세스 트리를 정리한다.
4. 실행 여부가 불명확한 run은 자동 재시도하지 않는다.
5. 상태·자격증명은 현재 OS 사용자에게만 공개한다.
6. workspace 검사는 검증된 sandbox 안에서만 실행한다.
7. Linux 동작과 기존 상태 이력은 그대로 보존한다.

## 2. 지원 범위

| 항목 | 목표 |
|---|---|
| 주 대상 | Windows 11 x64, Codex 데스크톱 앱, 네이티브 Claude Code |
| 보조 대상 | Windows 10 22H2 이상 |
| 셸 | PowerShell, Windows Terminal, Claude Code용 Git Bash |
| Python | 3.10 이상 |
| 인증 | Windows에 로그인된 Codex·Claude·Gemini·Cursor 계정 |
| 세션 | 생성, 재개, 중단, 병렬 실행, 역할·모델 지정 |
| 상위 기능 | task, queue, workflow, composition, monitor |
| Workspace 1차 | WSL2와 Bubblewrap을 이용한 로컬 격리 backend |
| Workspace 2차 | Windows AppContainer 기반 native backend |

서버 간 자동 작업 전달, 인증정보 복제, 자동 Git push, 사용자 동의 없는 권한
상승은 이 계획의 범위 밖이다. WSL2는 같은 Windows 호스트의 workspace 검사에만
사용하며 원격 호스트로 취급하지 않는다.

## 3. 현재 플랫폼 경계

현재 구현의 Linux 의존점은 다음과 같다.

- `store.py`: `/etc/machine-id`, `/proc`, UID·mode 검사, `fcntl.flock`, directory
  `fsync`.
- `runner.py`: `prctl(PR_SET_PDEATHSIG)`, POSIX signal, process group, `killpg`,
  `waitid`, `preexec_fn`, `execve`.
- `workspace_sandbox.py`: Bubblewrap, Linux namespace, `/proc`, `setrlimit`.
- `workspace_files.py`와 `workspace_policy.py`: `/usr/bin`, `/dev/null`, Unix mode,
  symlink 의미.
- `monitor.py`: stdlib `curses`.
- `provider_usage.py`: 일부 Linux 자격증명·실행 파일 경로.
- `install.py`: `fcntl`, Unix Python·launcher·설치 경로.

SQLite 상태기계, JSON 계약, task/workflow/composition 규칙, 모델 검증과 telemetry는
대부분 플랫폼 중립적이다. 따라서 OS 분기를 업무 로직에 흩뿌리지 않고 플랫폼
경계만 추출한다.

## 4. 목표 아키텍처

```text
Codex plugin / CLI
        |
Portable controller core
        |-- PlatformServices
        |     |-- LinuxPlatform
        |     `-- WindowsPlatform
        |-- ProcessBackend
        |     |-- PosixProcessBackend
        |     `-- WindowsJobBackend
        |-- SandboxBackend
        |     |-- LinuxBubblewrapBackend
        |     |-- WindowsWslBubblewrapBackend
        |     `-- WindowsAppContainerBackend (후속)
        |-- ConsoleBackend
        |     |-- CursesConsole
        |     `-- WindowsVTConsole
        `-- ProviderPaths
              |-- LinuxProviderPaths
              `-- WindowsProviderPaths
```

### 4.1 PlatformServices

```python
class PlatformServices:
    def host_identity(self): ...
    def principal_identity(self): ...
    def private_directory(self, path): ...
    def acquire_lock(self, path, mode, blocking=True): ...
    def atomic_write(self, path, content): ...
    def process_identity(self, pid): ...
    def process_alive(self, identity): ...
```

Windows는 사용자 이름 대신 SID를 해시해 `principal_identity`로 기록한다. 상태
디렉터리는 기본적으로 `%LOCALAPPDATA%\codex-control-claude\state`를 사용하고,
현재 사용자 SID와 `SYSTEM`만 허용하는 보호된 DACL을 요구한다. 파일 락은 공유,
배타, non-blocking 의미를 유지할 수 있는 `LockFileEx`로 구현한다.

### 4.2 ProcessBackend

```python
class ProcessBackend:
    def launch_supervisor(self, spec): ...
    def status(self, identity): ...
    def terminate(self, identity, graceful=True): ...
    def reconcile(self, identity): ...
```

Windows에서는 PID만으로 소유권을 판단하지 않고 다음 identity를 저장한다.

```text
PID + GetProcessTimes creation FILETIME + supervisor nonce
```

### 4.3 SandboxBackend

```python
class SandboxBackend:
    def probe(self): ...
    def execute_check(self, workspace, check_spec, limits): ...
    def cleanup(self, run_id): ...
```

지원 backend가 없으면 `sandbox_unavailable`로 실패한다. 일반 임시 디렉터리나
unconfined subprocess를 sandbox라고 취급하지 않는다.

### 4.4 ConsoleBackend와 ProviderPaths

Linux는 현재 curses 구현을 유지한다. Windows는 ANSI/VT 출력과
`msvcrt.getwch()` 입력을 사용하고, VT를 지원하지 않는 콘솔에는 줄 단위
`monitor watch`를 제공한다. Provider 경로는 명시적 환경변수를 우선하고 그 다음
플랫폼 기본 경로를 검색한다.

## 5. 구현 단계

### P0 — 플랫폼 계약과 capability matrix

`doctor`가 단일 지원/미지원 값 대신 기능별 결과를 반환하게 한다.

```json
{
  "platform": "windows",
  "capabilities": {
    "provider_usage": true,
    "tui": true,
    "session_control": true,
    "workspace": true,
    "sandbox": "wsl2-bubblewrap"
  }
}
```

추가 명령:

```powershell
claude_control doctor --platform
claude_control doctor --json
```

검사 항목은 Codex·Claude·Git 실행 파일, 상태 디렉터리 ACL, Job Object,
Windows VT console, WSL2, WSL 내부 Bubblewrap, provider 로그인 경로와 긴 경로
지원이다.

통과 조건:

- 지원하지 않는 기능만 비활성화한다.
- sandbox가 없어도 monitor와 안전한 세션 제어는 동작한다.
- 보안 기능은 fail closed하고 대체 unconfined 경로를 만들지 않는다.

### P1 — 플랫폼 계층 추출

새 모듈:

```text
claude_control/platform/
├── __init__.py
├── base.py
├── linux.py
├── windows.py
├── locks.py
├── paths.py
└── process_identity.py
```

작업:

1. 모든 `fcntl.flock` 사용을 공통 lock API로 옮긴다.
2. `/proc` 조회를 Linux process identity 구현으로 옮긴다.
3. UID·mode 검사와 Windows SID·DACL 검사를 private directory API로 통합한다.
4. 원자 쓰기와 durability 동작을 플랫폼별로 분리한다.
5. Linux 구현은 기존 코드를 이동하고 동작은 바꾸지 않는다.

현재 schema 12에서 다음 버전으로 올릴 때 다음 필드를 추가한다.

```text
platform
principal_id
worker_identity_json
child_identity_json
execution_scope
process_backend
sandbox_backend
```

기존 Linux PID와 namespace 필드는 과거 이력 호환을 위해 보존한다.

통과 조건:

- 기존 Linux 전체 회귀 테스트가 유지된다.
- Windows 동시 초기화 두 개 중 하나만 성공한다.
- 다른 사용자 SID의 상태 저장소를 거부한다.
- migration과 일반 DB 접근의 lifecycle lock 의미가 유지된다.
- symlink, junction, reparse point 상태 경로를 거부한다.

### P2 — Windows 설치와 사용량 모니터

작업:

- `install.py`에서 POSIX lock과 고정 Python 경로를 제거한다.
- `%CODEX_HOME%`, `%USERPROFILE%`, `%LOCALAPPDATA%`를 지원한다.
- `claude_control.cmd`와 PowerShell launcher를 만든다.
- `.exe`, `.cmd`, `.bat` 실행 파일을 안전하게 탐색한다.
- Codex·Claude·Gemini·Cursor Windows 로그인 경로를 추가한다.
- curses renderer와 Windows VT renderer를 분리한다.

통과 조건:

- 관리자 권한 없이 설치·갱신·제거된다.
- `monitor limits`, `snapshot --limits`, `monitor tui`가 동작한다.
- 설치되지 않은 provider만 `unavailable`로 표시한다.
- access token, 이메일, account ID, project ID를 출력하지 않는다.
- `q`, 방향키, `Tab`, `r`, terminal resize가 동작한다.
- provider 조회 실패 시 마지막 정상 결과에 stale 표시를 붙인다.

### P3 — Windows 세션 프로세스 제어

Windows process tree는 Job Object로 관리한다. Python `subprocess`만으로는
프로세스를 생성한 뒤 Job에 넣기 전의 경쟁 조건을 제거하기 어려우므로 작은 native
helper를 둔다.

```text
native/windows/ccc-win-supervisor.exe
```

권장 구현은 Rust와 Windows API이며 JSON Lines로 통신한다.

```json
{"op":"spawn","run_id":"...","argv":["claude.exe","..."]}
{"status":"running","pid":1234,"created_at_filetime":"..."}
{"op":"terminate","run_id":"...","grace_ms":3000}
{"status":"terminated","exit_code":1}
```

실행 순서:

1. Claude 프로세스를 suspended 상태로 생성한다.
2. Job Object를 만들고 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`를 설정한다.
3. suspended 프로세스를 Job에 배정한다.
4. process identity를 SQLite에 기록한다.
5. main thread를 resume한다.
6. supervisor가 Job handle을 계속 보유한다.
7. supervisor 종료 시 Job 전체가 종료된다.

`.cmd` 실행을 위해 임의 `shell=True`를 사용하지 않는다. 고정된 cmd adapter 또는
실제 executable을 사용하며 사용자 입력은 argv와 stdin으로 분리한다.

통과 조건:

- 새 세션, 정확한 세션 재개, follow-up이 성공한다.
- 요청 모델과 실제 모델 계열을 검증한다.
- 동시 실행 한도를 지킨다.
- stop과 timeout 후 Claude의 모든 자식 프로세스가 종료된다.
- controller, worker, supervisor를 각 단계에서 강제 종료해도 중복 호출이 없다.
- PID 재사용으로 무관한 프로세스를 종료하지 않는다.
- 불명 실행을 자동 재시도하지 않는다.
- Windows 재부팅 뒤 보수적으로 reconcile할 수 있다.

### P4 — task·queue·workflow·composition 동등성

P1과 P3 위에서 기존 플랫폼 중립 상태기계를 실행한다.

검증 범위:

- task 생성·개정·승인
- FIFO queue와 dependency
- bounded workflow
- Sonnet worker와 Fable reviewer 분리
- composition 계획→편집→검토
- message queue
- token·cost ledger
- 실시간 agent graph

Windows path validator는 다음을 명시적으로 처리한다.

- 대소문자 충돌
- `CON`, `PRN`, `AUX`, `NUL`, `COM1` 같은 예약 이름
- trailing dot와 trailing space
- NTFS alternate data stream의 `:`
- UNC와 drive-relative path
- junction, reparse point와 symlink
- `\\?\` 경로

프로토콜 내부 경로는 계속 `/` 구분자의 정규화된 상대 경로로 유지하고 실제 Windows
경로 변환은 platform adapter에서만 수행한다.

통과 조건:

- Linux와 Windows가 동일한 task 상태 전이를 보인다.
- operation ID 멱등성이 유지된다.
- case collision, 예약 파일명, ADS 경로를 snapshot 전에 거부한다.
- reviewer 권고와 Codex 승인이 분리된다.
- 동일 assignment/report JSON을 양쪽 플랫폼에서 같은 방식으로 검증한다.

### P5 — WSL2 Bubblewrap workspace backend

첫 Windows workspace backend는 WSL2와 Bubblewrap 조합으로 구현한다.

```text
Windows controller
    -> JSON request + deterministic tar stream
WSL2 helper
    -> private ext4 workspace
Bubblewrap
    -> networkless named check
receipt JSON + stdout/stderr digest
```

Windows repository를 `/mnt/c`에서 직접 검사하지 않는다. 허용된 파일을 canonical
tar로 만들고 WSL의 private ext4 디렉터리에 풀어 검사한다.

규칙:

- provider 자격증명과 로그인 파일은 전달하지 않는다.
- network namespace를 차단한다.
- 허용된 파일만 materialize한다.
- named check allowlist를 유지한다.
- 시간, 메모리, 출력, 파일과 프로세스 수를 제한한다.
- 손자 프로세스까지 종료됐는지 확인한다.
- 입력 manifest digest와 check receipt를 반환한다.
- transport가 끊기면 성공이 아니라 `unknown`으로 처리한다.

통과 조건:

- workspace 밖 파일을 읽지 못한다.
- `.claude`, `.codex`, `.gemini`, Cursor auth를 읽지 못한다.
- 네트워크 연결이 실패한다.
- fork된 손자 프로세스도 timeout 후 종료된다.
- check가 Windows 원본 repository를 변경하지 않는다.
- check snapshot digest와 frozen manifest가 일치한다.
- WSL 종료와 transport 단절을 성공으로 처리하지 않는다.
- Bubblewrap probe 실패 시 workspace 실행을 차단한다.

### P6 — Windows Git와 line-ending 안전성

Windows `core.autocrlf` 때문에 직접 쓰기 결과와 검토한 Git blob이 달라질 수 있다.

작업:

- snapshot은 Git blob의 canonical bytes를 사용한다.
- 내부 patch는 UTF-8과 LF로 정규화한다.
- apply 전에 `git apply --check`를 실행한다.
- apply 후 `git hash-object --path`로 정규화된 blob digest를 확인한다.
- 원본 HEAD와 clean 상태를 apply 직전에 다시 검사한다.
- executable bit는 manifest metadata로 유지한다.
- Windows에서 표현할 수 없는 mode 변경은 거부한다.

통과 조건:

- `core.autocrlf=true`, `false`, `input`을 모두 시험한다.
- 공백, 한글, 긴 경로를 시험한다.
- case-only rename은 거부하거나 명시적 2단계로 처리한다.
- apply 결과 digest가 frozen 예상값과 일치한다.
- 원본 HEAD가 변하면 파일 쓰기 전에 거부한다.

### P7 — 선택적 Windows AppContainer backend

WSL 없이 workspace 검사를 실행하기 위한 후속 단계다.

구성 후보:

- restricted token
- AppContainer profile
- workspace 전용 임시 ACL
- network capability 없음
- Job Object resource limits
- 별도 window station/desktop
- 실행 종료 후 profile과 ACL 정리

Windows 버전과 개발 도구 호환성을 실제 검증한 뒤 기본값 승격 여부를 결정한다.
Job Object만으로는 filesystem과 network가 격리되지 않으므로 AppContainer가
완성되기 전에는 native unconfined check를 제공하지 않는다.

### P8 — CI, 실제 호스트 검증과 배포

CI matrix:

```text
ubuntu-latest:
  Python 3.10
  Python 3.14
  Bubblewrap tests

windows-latest:
  Python 3.10
  Python 3.14
  Job Object tests
  ACL/locking/path tests
  fake-Claude E2E
```

테스트 계층:

| 계층 | 검증 내용 |
|---|---|
| Unit | Windows path, SID, ACL, lock, process identity, provider paths |
| Integration | 실제 subprocess, Job Object, migration, console renderer |
| Fault injection | reserve, spawn, Job 배정, identity 기록, output 중 각각 강제 종료 |
| E2E fake | 가짜 Claude CLI로 세션·재개·중단·병렬 실행 |
| E2E live | 실제 Windows Sonnet/Fable 단일 호출과 세션 재개 |
| Sandbox hostile | 외부 파일, network, fork bomb, timeout, output 폭주 |
| Cross-platform | 같은 contract를 Linux와 Windows에서 비교 |
| Regression | 기존 Linux 전체 suite |

실계정 smoke는 CI secret을 사용하지 않고 사용자의 Windows 호스트에서 실행한다.

```powershell
py -3 install.py --update
claude_control doctor --platform
claude_control monitor limits
py -3 tests\live_windows_smoke.py --model sonnet
py -3 tests\live_windows_smoke.py --model fable
```

## 6. 릴리스 순서

| 단계 | 제공 기능 | Workspace |
|---|---|---|
| Windows Preview 1 | 설치, doctor, provider usage, snapshot, TUI | 비활성 |
| Windows Preview 2 | 세션 생성·재개·중단, 병렬 실행 | 비활성 |
| Windows Preview 3 | task, queue, workflow, composition | check 차단 |
| Windows Beta | WSL2/Bubblewrap check, freeze, apply | 지원 |
| Windows Stable | 전체 회귀·실계정 검증·문서화 | WSL2 기본 |
| Native Sandbox Preview | AppContainer backend | 선택 기능 |

Preview 단계에서는 capability matrix와 README에 workspace 제약을 명시한다.

## 7. 위험과 대응

| 위험 | 대응 |
|---|---|
| Codex 앱이 이미 Job Object 안에서 실행됨 | nested Job probe; suspended 배정 실패 시 resume 전에 종료 |
| PID 재사용 | PID와 process creation FILETIME 결합 |
| `.cmd` quoting | 임의 `shell=True` 금지; 고정 adapter와 hostile quoting tests |
| ACL 오설정 | SID 기반 DACL 검사, 상속 차단, doctor fail closed |
| junction·ADS·case collision | canonical path validator에서 snapshot 전에 거부 |
| WSL transport 뒤 check 잔존 | WSL helper supervisor와 Linux process group을 함께 사용 |
| CRLF로 freeze/apply 불일치 | Git blob canonicalization과 apply 후 digest 재검증 |
| 백신이 native helper를 차단 | checksum, reproducible build, 후속 code signing |
| provider 저장 경로 변경 | provider path adapter와 `signed_in_no_data` 상태 |
| sandbox 없는 검사 | unconfined fallback 금지 |

## 8. 완료 기준

Windows 지원은 다음 조건을 모두 충족해야 완료로 선언한다.

1. 관리자 권한 없이 설치·업데이트·제거된다.
2. Codex 앱에 플러그인과 스킬이 표시된다.
3. Windows 로그인을 사용해 Claude 세션을 생성하고 정확한 세션을 재개한다.
4. stop, timeout, controller crash 후 Claude 자식 프로세스가 남지 않는다.
5. task, workflow, composition 상태와 멱등성이 Linux와 같다.
6. provider 사용량 TUI가 Windows에서 동작한다.
7. workspace 검사는 WSL2/Bubblewrap 또는 검증된 AppContainer에서만 실행된다.
8. credential, 이메일, account ID가 로그·JSON·sandbox에 들어가지 않는다.
9. 기존 Linux 전체 회귀 테스트가 통과한다.
10. Windows 실제 Sonnet/Fable 세션과 hostile sandbox 테스트가 통과한다.

구현 순서는 `P0 -> P1 -> P2 -> P3 -> P4 -> P5 -> P6 -> P7 -> P8`이다.
P2가 첫 사용자 가치 전달점이고, P5가 기존 workspace 안전 보장까지 포함하는
Windows Beta의 완료 지점이다.
