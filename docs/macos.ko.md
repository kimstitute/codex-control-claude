# macOS 설치와 지원 범위

0.27.0부터 Codex Control Claude는 Intel과 Apple Silicon macOS에서 같은 사용자로
로그인한 Claude Code를 제어하고 관측할 수 있습니다. Claude Code 자체 요구 사항에
맞춰 macOS 10.15 이상, Python 3.10 이상을 사용합니다.

## 지원표

| 기능 | macOS | 구현 경계 |
|---|---:|---|
| `start`, `followup`, `resume`, `restart`, `stop`, `reconcile` | 지원 | POSIX process group, 부팅·PID 시작 신원 검증 |
| `delegate`, `task`, queue, `workflow` | 지원 | 기존 불변 원장과 구조화 출력 계약 사용 |
| 역할별 모델·effort, `models catalog` | 지원 | 로컬 Claude Code CLI 기능을 probe한 뒤 사용 |
| telemetry, provider limits, Python TUI | 지원 | 사용자 로컬 상태와 provider 보고값만 읽음 |
| Rataflow graph/replay viewer | 지원 | `x86_64-apple-darwin`, `aarch64-apple-darwin` 릴리스 |
| `terminal start/logs/attach/stop/shell` | 지원 | 설치된 Claude Code가 background-agent 명령을 제공해야 함 |
| `workspace` | 차단 | 지원되는 fail-closed native sandbox backend 없음 |
| composition 계획·비평 단계 | 지원 | supplied-text task/workflow 경로 |
| composition 편집·검사·동결 단계 | 차단 | `workspace`와 같은 격리 요구 사항 |
| 서버 간 전달 | 차단 | 모든 상태와 실행은 현재 Mac에만 존재 |

## 왜 workspace를 차단하나요?

Linux 경로는 Bubblewrap의 user, PID, network, mount namespace를 사용하고 Windows
경로는 WSL2 내부에서 같은 Bubblewrap 계약을 실행합니다. macOS의 공식 App Sandbox는
서명된 앱과 entitlement에 결합되며, 일반 Python CLI가 임의 자식 프로세스에 즉시
씌우는 범용 샌드박스가 아닙니다. 예전 `sandbox-exec` 인터페이스는 이 프로젝트가
신뢰할 장기 지원 경계로 취급하지 않습니다.

따라서 macOS에서 `workspace doctor`는 `ready: false`를 반환하고 workspace 생성·실행은
`sandbox_unavailable`로 종료됩니다. 컨트롤러는 파일 검사 명령을 호스트에서 그대로
실행하거나 Docker·VM으로 조용히 대체하지 않습니다.

## 설치

```bash
git clone https://github.com/kimstitute/codex-control-claude.git
cd codex-control-claude
python3 install.py
```

릴리스 viewer를 설치할 때는 Mac 아키텍처와 일치하는 파일을 받고 게시된 SHA-256을
확인한 뒤 설치기에 전달합니다. 직접 빌드한다면 다음처럼 고정된 lockfile을 사용합니다.

```bash
cargo build --locked --release --manifest-path viewer/Cargo.toml
viewer_sha=$(shasum -a 256 viewer/target/release/ccc-viewer | awk '{print $1}')
python3 install.py --update \
  --viewer viewer/target/release/ccc-viewer \
  --viewer-sha256 "$viewer_sha"
```

기본 상태 경로는 `~/Library/Application Support/codex-control-claude/state`입니다.
`XDG_STATE_HOME`이 설정되어 있으면 `$XDG_STATE_HOME/claude-control`을 사용합니다.

## 검증

먼저 Claude Code에 같은 macOS 사용자로 로그인한 뒤 다음을 실행합니다.

```bash
claude_control doctor --auth --platform --json
claude_control workspace doctor
claude_control models catalog
claude_control monitor snapshot --no-live
```

`doctor`의 `platform.platform`은 `macos`여야 하고 `session_control`, `tui`,
`provider_usage`는 지원으로 표시되어야 합니다. `workspace`와 `sandbox`는 이유가 있는
비지원 상태가 정상입니다. 대화형 terminal은 `doctor`의 `terminal_supported`도
확인합니다.

현재 Linux 개발 호스트의 단위 테스트와 macOS GitHub Actions가 Python 코어와 두 Mac
viewer target을 검증합니다. 실제 계정 모델 호출과 attach 입력은 해당 Mac에서만
확인할 수 있으므로 릴리스 후 한 번의 opt-in smoke를 권장합니다.

## 프로세스와 상태 안전성

- 상태 저장소는 하드웨어 UUID의 SHA-256, 사용자 UID, 부팅 시각에 묶입니다.
- 실행은 PID뿐 아니라 `ps`가 보고한 시작 시각과 process group을 기록합니다.
- macOS에서 directory descriptor `fsync`가 거부되면 state metadata를 보수적으로
  system sync한 뒤 계속합니다.
- Linux의 `PR_SET_PDEATHSIG`은 macOS에 없으므로 worker는 정상 경로에서 process
  group 전체를 종료하고, 비정상 worker 소실 뒤 살아 있는 group은 자동 재시도하지
  않고 `unknown` 격리 상태로 남깁니다.
