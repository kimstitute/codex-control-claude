# Windows 설치와 운영

0.14.1은 Windows 10/11에서 같은 사용자로 로그인한 Claude Code 세션과
task, workflow, composition, provider 한도 모니터를 지원합니다. 네이티브
helper, supervisor protocol, suspended child 신원 기록·재개와 Job Object 실행은
실제 Windows x64에서 smoke test를 통과했습니다. 도구를 비활성화한 실제 Sonnet
호출도 성공했습니다. WSL2 workspace smoke는 WSL 서비스에 접근할 수 있는
Windows 사용자와 process token으로 실행해야 합니다.

## 실제 Windows 검증 결과

Claude 인증을 갱신한 뒤 커밋 `e3069a5`를 시험했습니다. 고정된 x64 helper가
protocol handshake를 마치고 Claude 자식을 Job Object 안에서 생성·재개했으며,
`claude-sonnet-5`가 `WINDOWS_SMOKE_OK`를 반환했습니다. 도구 호출은 0회,
종료 코드는 0, 검증 오류는 없었고 provider API 실행 시간은 약 2.4초였습니다.

WSL2/Bubblewrap 구간은 아직 미검증입니다. Codex task가 실행한 모든 명령은
제한된 sandbox token을 사용했습니다. 이 계정에서는 `py -3`가 사용자 Python을
찾지 못했고 directory durability 확인이 중단됐습니다. 해당 preflight만 진단
목적으로 지나간 실행은 WSL 서비스의 `E_ACCESSDENIED` 때문에
`wsl_transport_failed`에 도달했습니다. 이는 실행 계정 경계이며 소스나 WSL
설정 결함을 뜻하지 않습니다. 마지막 probe는 일반 Windows 터미널에서
실행합니다.

```powershell
py -3 .\work\windows-smoke\wsl_probe_smoke.py
```

## 지원 구조

- 네이티브 Claude 세션은 `ccc-win-supervisor.exe`가 Windows Job Object 안에서
  실행합니다. 자식은 suspended 상태로 생성되고 프로세스 신원이 원장에
  기록된 뒤에만 재개됩니다.
- workspace는 `wsl.exe`를 같은 supervisor로 관리하고, WSL2의 private ext4
  상태 디렉터리에서 기존 Bubblewrap 격리를 실행합니다.
- AppContainer와 비격리 workspace 대체 경로는 없습니다.
- Claude의 기본 도구와 MCP, 서버 간 작업 전달, 자동 merge/push는 계속
  비활성화됩니다.

## 준비 사항

네이티브 세션에는 Python 3.10+, Git, Claude Code, Codex와 아키텍처가 맞는
`ccc-win-supervisor.exe`가 필요합니다. workspace에는 추가로 WSL2와 WSL
배포판 내부의 `bwrap`이 필요합니다.

WSL 안에서 다음을 확인하세요.

```bash
command -v python3
command -v bwrap
test -d "$HOME"
```

컨트롤러의 WSL 상태 경로는 `/mnt` 아래가 아닌 배포판의 ext4 파일시스템에
있어야 합니다. 이 경계가 맞지 않거나 Bubblewrap probe가 실패하면 workspace
기능만 비활성화됩니다.

## 설치

릴리스의 Windows helper와 체크섬을 같은 위치에 받은 뒤 PowerShell에서
해시를 검증하고 설치합니다.

```powershell
$sha = (Get-FileHash .\ccc-win-supervisor.exe -Algorithm SHA256).Hash.ToLower()
py -3 install.py `
  --windows-helper .\ccc-win-supervisor.exe `
  --windows-helper-sha256 $sha
```

Graph/replay viewer도 함께 설치하려면 별도 릴리스 파일의 hash를 확인해 같은
명령에 추가합니다.

```powershell
$viewerSha = (Get-FileHash .\ccc-viewer.exe -Algorithm SHA256).Hash.ToLower()
py -3 install.py --update `
  --windows-helper .\ccc-win-supervisor.exe `
  --windows-helper-sha256 $sha `
  --viewer .\ccc-viewer.exe `
  --viewer-sha256 $viewerSha
```

viewer는 native Windows console application이므로 WSL2가 필요하지 않습니다.
WSL2는 격리된 workspace와 composition 실행에만 계속 필요합니다.

업데이트도 같은 고정값을 명시할 수 있습니다.

```powershell
py -3 install.py --update `
  --windows-helper .\ccc-win-supervisor.exe `
  --windows-helper-sha256 $sha
```

설치기는 helper 바이트를 한 번 읽어 SHA-256을 확인하고 아키텍처별 이름과
manifest를 함께 저장합니다. helper가 없거나 해시·아키텍처가 맞지 않으면
session control은 다른 launcher로 대체되지 않고 중단됩니다.

소스에서 helper를 만들 때는 Visual Studio Build Tools와 Rust MSVC toolchain을
사용합니다.

```powershell
cd native\windows\ccc-win-supervisor
cargo test --release
cargo build --release
```

## 초기화와 확인

```powershell
py -3 "$HOME\plugins\claude-control\scripts\claude_control_cli.py" init `
  --claude-bin "C:\absolute\path\to\claude.exe" `
  --allow-root "C:\work\project" `
  --max-parallel 2

py -3 "$HOME\plugins\claude-control\scripts\claude_control_cli.py" doctor --auth
py -3 "$HOME\plugins\claude-control\scripts\claude_control_cli.py" workspace doctor
```

`doctor --auth`는 Claude 로그인과 필수 CLI 옵션을 확인합니다. `workspace
doctor`는 설치된 helper, WSL2, WSL 내부 Python/Bubblewrap과 private ext4 상태
경계를 실제로 probe합니다. 실패한 항목이 있으면 workspace를 실행하지 않습니다.

## 파일과 apply 보장

컨트롤러는 NTFS reparse point와 안전하지 않은 state DACL을 거절합니다.
Git 스냅샷에서는 예약 장치명, ADS colon, component 끝의 점·공백과
대소문자를 무시했을 때 충돌하는 path prefix를 거절합니다. NTFS가 실행 비트를
표현하지 못하므로 Git mode는 model이 보지 못하는 canonical sidecar에 저장하고
파일 집합과 정확히 일치하는지 매번 검사합니다.

`workspace apply`는 원본 HEAD, clean worktree, 동결 manifest, patch와
Git-normalized blob hash를 모두 확인합니다. 적용만 수행하며 commit, merge,
push는 하지 않습니다. Windows에서 새 executable 파일은 worktree가 Git mode를
충실히 만들 수 없으므로 거절됩니다.

## 문제 해결

| 증상 | 확인할 항목 |
|---|---|
| session control 비활성 | helper 아키텍처, 설치 manifest, 실제 파일 SHA-256 |
| `401 OAuth access token has expired` | 같은 Windows 사용자의 일반 터미널에서 `claude auth login` 실행 |
| WSL `E_ACCESSDENIED` | 제한된 앱 sandbox 밖의 일반 사용자 터미널에서 WSL 서비스 접근 확인 |
| Codex에 이전 plugin cache만 표시 | 같은 Windows 사용자로 updater 실행 후 `--version`을 확인하고 새 Codex task 시작 |
| `workspace doctor`가 WSL 실패 | `wsl.exe --status`, 기본/선택 배포판 실행 여부 |
| `bwrap`을 찾지 못함 | 같은 WSL 배포판에 Bubblewrap 설치 |
| state 경로 거절 | `/mnt` 밖의 WSL ext4 경로 사용 |
| reparse/DACL 오류 | junction·symlink를 제거하고 현재 사용자 전용 디렉터리 사용 |
| apply mode 오류 | 새 executable 추가를 Linux에서 적용하거나 파일을 일반 mode로 유지 |
