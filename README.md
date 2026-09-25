<p align="center">
  <img src="plugins/claude-control/assets/logo.png" alt="Codex Control Claude 로고" width="128">
</p>

<h1 align="center">Codex Control Claude</h1>

<p align="center">Codex에서 여러 로컬 Claude Code 세션을 지속적으로 관리합니다.</p>

<p align="center">
  <strong>Linux · Windows 10/11</strong> · <strong>Python 3.10+</strong> · <strong>실행 시 외부 Python 의존성 없음</strong>
</p>

<p align="center">
  <a href="README.en.md">English</a> · 한국어 · <a href="docs/cli.ko.md">CLI 레퍼런스</a> · <a href="docs/architecture.ko.md">아키텍처</a> · <a href="CHANGELOG.md">변경 기록</a>
</p>

Codex Control Claude는 Codex가 Claude Code에 작업을 위임하고 그 실행을
관리하도록 돕는 플러그인과 로컬 컨트롤러입니다. 대화, 실행, 작업 개정,
검토 근거와 승인 기록을 호스트의 영속 저장소에 보관합니다. 역할마다 Sonnet,
Opus, Haiku, Fable의 별칭이나 정확한 버전 ID와 노력 수준을 배정하고 각 대화의
정확한 세션을 계속 이어갈 수 있습니다.

0.9 버전은 **계획 → 독립 검토 → 계획 승인 → 통제된 편집 → 동결본 검토 →
최종 승인** 흐름을 제공합니다. 계획과 편집 단계를 연결하지만 자동 승인,
자동 재시도, 원본 반영, merge, push, 서버 간 전달은 수행하지 않습니다.

0.10 버전은 구조화 task에 Claude Code `--json-schema`를 적용하고,
base hash 기반 hunk patch, 부작용 없는 형식 오류의 1회 보정, 최종 reviewer
거부권, 원장 검증형 acceptance evidence를 추가합니다.

0.11 버전은 실제 Sonnet 편집→검사→동결→Fable 검토 파일럿을 통과했고,
provider 원본 usage·비용·지연 기록, 원본 HEAD를 고정하는 명시적 `workspace apply`,
계획 전에 저장소를 읽는 read-only Sonnet scout를 추가합니다.

0.12 버전은 에이전트·task·workspace·workflow·composition 관계를 그래프로
보여주는 실시간 터미널 모니터와 실행 기록·토큰·비용 조회를 추가합니다.

0.13 버전은 같은 TUI에 Codex, Claude Code, Gemini CLI, Cursor CLI의 계정별
한도 화면을 추가합니다. provider가 공개한 사용/남은 비율, 초기화 시각,
토큰·요청·초과 지출 단위를 그대로 표시하며 공개되지 않은 토큰 상한은
추정하지 않습니다.

0.14 버전은 Windows 10/11에서 같은 호스트의 Claude 세션, task, workflow,
composition, provider 한도와 ANSI/VT TUI를 지원합니다. 네이티브 세션은
SHA-256으로 고정한 `ccc-win-supervisor.exe`가 관리하며, Windows workspace는
WSL2 안의 Bubblewrap으로만 실행됩니다. AppContainer와 비격리 대체 실행은
포함하지 않습니다.

0.15 버전은 Codex 리더가 작성한 불변 명세, 편집자가 쓸 수 없는 동결 테스트,
최종 트리에 묶인 검사 gate와 read-only composition 평가를 추가합니다.

0.16 버전은 task 유형·위험도·라우팅 정책·모델 선택 사유·승격 계보를 불변
기록으로 남기고, 여러 composition을 전역 동시 실행 한도 안에서 공정하게 진행하며,
정확히 일치하고 다시 검증된 Sonnet scout 결과를 선택적으로 재사용합니다.

0.17 버전은 역할마다 모델 별칭 또는 정확한 `claude-...` 버전 ID와
`low`~`max` effort를 설정합니다. 새 작업은 역할 기본값을 해석해 동결하고,
실제 응답 모델이 지정한 계열이나 정확한 ID와 다르면 실패합니다.

0.18 버전은 로그인된 Claude Code 계정이 현재 선택할 수 있는 모델과 지원 effort를
`models catalog`로 조회합니다. 모델 prompt를 보내지 않으며 계정 식별자는 반환하거나
저장하지 않습니다.

0.19 버전은 append-only 관측 원장, 실시간 그래프와 cursor 기반 과거 재생,
OpenTelemetry trace export, AG-UI 1.0 snapshot/event 출력을 추가합니다. TUI에서
기록을 한 단계씩 이동하거나 자동 재생할 수 있으며 prompt·reasoning·tool 본문은
관측 형식에 기록하지 않습니다.

0.20 버전은 독립 구현한 Rust graph/replay viewer를 추가합니다. spatial graph,
minimap, overview/follow/manual camera와 event timeline을 제공하며 AG-UI JSONL을
한 장기 실행 process로 따라갑니다. Zoetrope 소스와 asset은 포함하지 않습니다.

0.21 버전은 viewer를 semantic zoom과 안정된 grid layout으로 다시 구성하고 마우스
조작을 추가합니다. 카드 선택·inspector, drag pan, pointer 중심 wheel zoom, timeline
scrub, minimap 이동, PLAY/LIVE transport를 터미널에서 직접 제어할 수 있습니다.

0.22 버전은 직접 만든 문자 그래프를 `rataflow` 엔진으로 교체하고 기본 화면을 최신
연결 작업에 집중하는 `focus` scope로 정리합니다. `a`로 `focus → recent → all`을
순환하며, 모든 cursor의 과거 재생은 그대로 유지됩니다. 희소 배경, compact card,
일관된 edge clipping, minimap과 2행 activity timeline을 같은 viewport에서 렌더합니다.

0.23 버전은 선택 에이전트의 run·모델·effort·token·비용과 controller operation
이력을 content-free Safe Inspector에서 스크롤해 확인할 수 있게 합니다. 카드에는
`R/W/P/C` operation chip이 표시되고, `monitor viewer --tree`는 숨은 run과 operation까지
포함한 결정적 전체 트리를 출력합니다. 재생은 0.25×–8× 속도, 실제 시간 간격 압축,
run 시작 구간 이동을 키보드와 마우스로 제어할 수 있습니다.

0.24 버전은 기본 content-free 관측과 분리된 opt-in Local Detail을 추가합니다.
키보드·마우스 session picker에서 관리 run, Claude Code 또는 Codex 전사를 고르면
Provenance·Tools·Activity 탭으로 prompt, response/reasoning, tool timing과 semantic
event를 로컬에서만 볼 수 있습니다. `/` 검색, `n/N` 결과 이동, `p/P` prompt 이동과
marker filter도 지원합니다. AG-UI, OTLP, `--inspect`, `--tree`에는 이 내용이 들어가지 않습니다.

0.25 버전은 Claude Code의 네이티브 background PTY/ConPTY로 새 대화형 agent를
시작하고 관찰하는 `terminal` 기능을 추가합니다. Local Detail의 **TERMINAL** 탭에서
최근 화면을 보고 `t` 또는 마우스 **ATTACH**로 한 명의 로컬 조작자가 직접 입력할 수
있습니다. `s` 또는 **SHELL**은 허용된 project에 별도 사용자 shell을 엽니다. 기존
비대화형 `-p` 실행은 소급 attach하지 않으며 terminal 본문은 기본 관측 export에 포함하지 않습니다.

0.26 버전은 대화형 Claude transcript의 input·cache write/read·output·thinking token을
응답 ID별로 중복 없이 집계하고, provider 비용이 없으면 추정하지 않고 `unavailable`로
표시합니다. 처음에는 transcript가 없던 `terminal:` 관측도 파일이 생기면 같은 session
identity를 유지한 채 `claude:` 상세 관측으로 자동 승격합니다. Claude self-update로 고정된
버전 경로가 사라지면 검증된 stable launcher로 새 작업 경로를 복구하되, 기존 headless
대화의 binary identity는 바꾸지 않아 재개 시 명시적으로 거부합니다.

## 어떤 명령을 선택해야 하나요?

| 원하는 일 | 사용할 기능 | 추가되는 보장 |
|---|---|---|
| Claude에게 질문 한 번 보내기 | `start` | 지속 가능한 비정형 대화 세션 |
| 역할과 출력 형식을 정해 한 번 위임하기 | `delegate` | 역할 지침과 구조화된 보고서 검증 |
| 수정 요청과 최종 승인을 기록하기 | `task` | 불변 개정, 정확한 결과 해시, 기준별 승인 근거 |
| 계획을 만들고 별도 Fable에게 비평시키기 | `workflow` | 호출·수정·시간 예산이 고정된 P4 흐름 |
| Claude가 제한된 파일을 수정하게 하기 | `workspace` | 비공개 Git 사본, 정확한 쓰기 경로, 이름 있는 검사 |
| 계획부터 구현·검토까지 연결하기 | `composition` | P4 계획과 P5 편집·동결 검토의 명시적 연결 |
| 의존 작업을 순서대로 실행하기 | `task enqueue` + `dispatch` | FIFO 대기열과 정확한 부모 승인 조건 |
| 다음 턴에 전달할 지시를 저장하기 | `message` | 선택된 개정에만 전달되는 지시와 결과 인수인계 |
| multi-agent graph와 과거 실행 관찰하기 | `monitor viewer` | focus/recent/all graph, minimap, mouse camera, activity replay, agent별 실행 토큰 |
| 로컬 Claude/Codex 전사까지 상세 관찰하기 | `monitor viewer --local-detail` | mouse session picker, provenance/tool/activity 탭, 검색·prompt 이동; export와 분리 |
| 화면을 보며 Claude agent에 직접 입력하기 | `terminal start` + `monitor viewer --local-detail` | native PTY/ConPTY, 단일 조작자 lease, TERMINAL 탭과 ATTACH |
| 같은 project에서 직접 명령 실행하기 | `terminal shell` 또는 viewer의 `s` | agent 입력과 분리된 사용자 운영 shell, 허용 root 확인 |
| 계정 한도와 portable fallback 보기 | `monitor tui` / `monitor limits` | Codex·Claude·Gemini·Cursor quota와 Python-only 화면 |
| 계정에서 선택 가능한 모델 확인하기 | `models catalog` | 현재 selector·해석된 모델·지원 effort, 계정 식별자 제거 |
| 역할별 모델과 effort 바꾸기 | `models show/configure/reset` | 별칭·정확한 버전 ID, 원자적 설정 교체, 기존 작업 동결 |

Codex 앱에서 사용할 때는 설치된 `$claude-control` 스킬을 명시하면 됩니다.
직접 상태를 확인하거나 운영하려면 아래 CLI를 사용하세요.

```bash
claude_control monitor viewer
claude_control monitor viewer --tree
claude_control monitor viewer --local-detail
claude_control monitor viewer --detail-current
claude_control monitor tui
claude_control monitor limits
claude_control monitor agui stream --after 0 --limit 100
```

대화형 agent를 새로 만들 때는 고유한 request ID를 사용합니다. terminal은 입력을
기다리는 빈 화면으로 시작하며, attach한 사용자가 직접 첫 지시를 입력합니다.

```bash
claude_control terminal start \
  --name interactive-editor \
  --role executor \
  --project /absolute/project \
  --request-id terminal-editor-001
claude_control terminal list
claude_control terminal logs --id <8-character-id> --bytes 8192
claude_control terminal attach --id <8-character-id>
claude_control terminal stop --id <8-character-id>
claude_control terminal shell --project /absolute/project
```

마우스·키보드 조작과 토큰 수치의 의미는 [실시간 모니터 안내](docs/monitor.ko.md)를
참고하세요.

역할별 모델과 effort 설정 파일, 정확한 버전 고정, 개별 assignment 덮어쓰기는
[모델 설정 안내](docs/models.ko.md)를 참고하세요.

## 안전 경계

- 한 설치본은 **같은 호스트, 같은 운영체제 사용자**의 Claude Code만
  관리합니다.
- 컨트롤러가 실행하는 Claude의 기본 도구와 MCP는 비활성화됩니다. 텍스트
  작업에서 경로를 언급해도 Claude가 그 파일을 직접 읽을 수 없습니다.
- `terminal start`도 safe mode, 빈 setting source, 빈 MCP와 native tool 비활성화
  정책을 사용합니다. attach는 agent 대화 입력용입니다. `terminal shell`은 사용자가
  직접 운영하는 별도 shell이므로 그 shell의 명령과 파일 변경은 사용자의 책임이며
  controller workspace 격리나 승인 경로를 통과하지 않습니다.
- 다른 도구가 만든 Claude session은 상태 목록에서만 관측합니다. controller가 만든
  safe-profile terminal만 화면 본문, attach와 stop을 허용합니다.
- 파일 수정은 커밋된 Git 트리의 비공개 사본에서 이뤄집니다. 원본
  저장소는 자동으로 바뀌지 않습니다.
- 검사는 정책에 미리 등록한 이름으로만 요청할 수 있습니다. 실제 명령은
  Bubblewrap 격리 환경에서 컨트롤러가 실행합니다.
- 모델 실행 완료, 검토 권고, Codex 승인은 서로 다른 상태입니다.
- 실패·시간 초과·잘못된 보고서·실행 불명 상태는 자동 재시도하지 않습니다.
- 상주 스케줄러가 없습니다. `workflow run`, `workspace run`,
  `composition run`, `dispatch`를 명시적으로 호출해야 다음 단계가 진행됩니다.
- 프롬프트, 응답, 세션 ID와 실행 상태는 플러그인 소스와 별도 위치에
  저장됩니다.

로컬 제어가 로컬 추론을 뜻하지는 않습니다. Claude Code는 기존 로그인으로
Claude 서비스에 모델 요청을 보냅니다.

## 준비 사항

### 공통

- Linux 또는 Windows 10/11
- Python 3.10 이상
- 같은 사용자로 설치하고 로그인한 Claude Code
- 플러그인 기능과 `plugin-creator` 도구가 포함된 Codex CLI
- 설치 시 플러그인 검증에 사용할 PyYAML 포함 Python 3.10+

컨트롤러 실행 자체는 Python 표준 라이브러리만 사용합니다. 설치기는 빠진
패키지를 자동으로 설치하지 않습니다.

### Linux workspace와 composition

- Git
- Bubblewrap (`bwrap`)
- 사용할 수 있는 비특권 user, PID, network namespace

### Windows

- 아키텍처가 일치하는 `ccc-win-supervisor.exe`와 배포자가 제공한 SHA-256
- workspace와 composition에는 WSL2와 WSL 배포판 내부의 `bwrap`
- WSL workspace 상태를 둘 `/mnt` 밖의 private ext4 경로

Windows 네이티브 세션만 사용할 때는 WSL2가 필요하지 않습니다. 자세한 설치와
지원 범위는 [Windows 안내](docs/windows.ko.md)를 참고하세요.

첫 편집 전에 `workspace doctor`를 실행하세요. 격리를 사용할 수 없으면
workspace 실행을 거절하며, 격리 없는 대체 경로는 없습니다.

지원 모델 별칭은 `sonnet`, `opus`, `haiku`, `fable`입니다. 정확한
`claude-...` ID와 `[1m]` selector도 설정할 수 있습니다. 실제 사용 가능 여부는 Claude
계정에 따라 달라집니다. 선택한 모델을 쓸 수 없을 때 다른 모델로 조용히
바꾸지 않습니다.

## 설치

```bash
git clone https://github.com/kimstitute/codex-control-claude.git
cd codex-control-claude
python3 install.py
```

Windows에서는 helper의 SHA-256을 먼저 확인하고 함께 설치합니다.

```powershell
$sha = (Get-FileHash .\ccc-win-supervisor.exe -Algorithm SHA256).Hash.ToLower()
py -3 install.py --windows-helper .\ccc-win-supervisor.exe --windows-helper-sha256 $sha
```

Graph/replay viewer는 릴리스 바이너리 또는 직접 빌드한 바이너리의 hash를 함께
지정합니다. 설치기가 네트워크에서 바이너리를 받거나 다른 platform 파일로
대체하지 않습니다.

```bash
cargo build --locked --release --manifest-path viewer/Cargo.toml
viewer_sha=$(sha256sum viewer/target/release/ccc-viewer | cut -d' ' -f1)
python3 install.py --update \
  --viewer viewer/target/release/ccc-viewer \
  --viewer-sha256 "$viewer_sha"
```

viewer를 직접 빌드할 때만 Rust 1.88 이상이 필요합니다. 조작법, offline JSONL 재생,
headless inspect와 Windows 명령은 [실시간 모니터 안내](docs/monitor.ko.md)에 있습니다.

helper가 없거나 해시가 다르면 설치 또는 Windows session control이 fail-closed로
중단되며 다른 실행 경로로 대체되지 않습니다.

설치기는 다음 작업을 수행합니다.

1. 플러그인 구조와 스킬을 검증합니다.
2. 소스를 `$HOME/plugins/claude-control`에 복사합니다.
3. 개인 Codex marketplace에 등록합니다.
4. 캐시 구분자가 붙은 설치본을 Codex에 설치합니다.

실행 상태는 플러그인 디렉터리에 저장하지 않습니다. 도구 위치가 기본값과
다르면 명시하세요.

```bash
python3 install.py \
  --plugin-creator-root /absolute/path/to/plugin-creator \
  --helper-python /absolute/path/to/python-with-pyyaml
```

설치 후 새 Codex 작업을 열어야 새 스킬을 확실히 불러옵니다.

## 이 호스트 초기화

Claude 실행 파일을 찾고, 컨트롤러가 사용할 수 있는 프로젝트 루트를
정합니다.

```bash
command -v claude

python3 "$HOME/plugins/claude-control/scripts/claude_control_cli.py" init \
  --claude-bin /absolute/path/to/claude \
  --allow-root /srv/my-project \
  --max-parallel 2 \
  --max-queued 100
```

여러 프로젝트 루트를 허용하려면 초기화할 때 `--allow-root`를 반복합니다.
이후 assignment의 `project`는 허용된 루트 안에 있어야 합니다.
`--claude-bin`에는 가능하면 `~/.local/bin/claude`처럼 설치기가 갱신하는 stable symlink를
지정하세요. 컨트롤러는 실제 version binary를 고정해 실행하고, 그 파일이 사라진 경우에만
동일한 versions directory를 가리키는 launcher를 검증해 새 작업용 경로를 원자적으로 복구합니다.
기존 대화는 자동으로 새 binary에 재개되지 않습니다.

설치, 로그인과 격리 환경을 확인합니다.

```bash
python3 "$HOME/plugins/claude-control/scripts/claude_control_cli.py" doctor --auth
python3 "$HOME/plugins/claude-control/scripts/claude_control_cli.py" workspace doctor
```

`doctor --auth` 결과에는 계정 식별자가 포함되지 않습니다. 정상 상태는
`"ready": true`로 표시됩니다. 로그인 계정에 제공되는 모델과 effort는 모델 추론을
호출하지 않는 `models catalog`로 확인합니다. 실제 작업 호출의 모델 ID는 기존처럼
실행 결과에서 다시 검증합니다.

아래 shell 함수를 정의하면 이후 예시를 그대로 사용할 수 있습니다.

```bash
claude_control() {
  python3 "$HOME/plugins/claude-control/scripts/claude_control_cli.py" "$@"
}
```

공개 운영 명령은 JSON을 출력합니다.

## Codex 앱에서 사용하기

새 Codex 작업에서 작업 내용과 스킬을 함께 말하면 됩니다.

> $claude-control을 사용해줘. Sonnet에게 범위가 고정된 구현을 맡기고,
> 별도 Fable 세션이 결과를 검증하게 해줘. 두 세션 ID를 보존하고 정확한
> 결과를 확인하기 전에는 승인하거나 적용하지 마.

0.11의 전체 흐름을 사용하려면 다음처럼 요청할 수 있습니다.

> 이 저장소에서 $claude-control composition을 사용해줘. Fable이 계획을
> 검토한 뒤 명시적 계획 승인을 기다리고, Sonnet은 허용된 파일만 수정하고
> 이름 있는 검사를 실행하게 해줘. 편집 결과를 동결한 뒤 별도 읽기 전용
> Fable 검토를 만들고, merge나 push는 자동으로 하지 마.

Codex는 세션 ID, 결과 해시, 승인 근거를 관리해야 합니다. Claude 프로세스가
정상 종료했다는 사실만으로 답의 정확성이 증명되지는 않습니다.

## 첫 CLI 실습: 대화 하나 시작하기

Claude에 전달할 내용을 `/tmp/claude-task.txt`에 작성합니다. `start`는 파일
권한을 주지 않으므로 필요한 코드나 근거는 프롬프트에 포함해야 합니다.

```bash
claude_control start \
  --name parser-review \
  --model sonnet \
  --effort medium \
  --role implementer \
  --project /srv/my-project \
  --prompt-file /tmp/claude-task.txt \
  --request-id parser-review-001 \
  --timeout 300
```

응답에서 다음 ID를 보관하세요.

| JSON 필드 | 의미 | 사용하는 명령 |
|---|---|---|
| `id` | 한 번의 실행 ID | `status`, `wait`, `logs`, `result`, `stop`, `reconcile` |
| `session_id` | 컨트롤러가 관리하는 대화 ID | `followup`, `resume`, `restart` |
| `backend_id` | Claude의 영속 대화 ID | 검증과 진단 전용 |

```bash
claude_control status --run <run-uuid>
claude_control wait --run <run-uuid> --seconds 30
claude_control logs --run <run-uuid> --stream stderr --bytes 8192
claude_control result --run <run-uuid>
```

세션이 idle이 된 뒤 정확한 대화를 이어갑니다.

```bash
claude_control followup \
  --session <managed-session-uuid> \
  --prompt-file /tmp/followup.txt \
  --request-id parser-review-002
```

`resume`은 `followup`의 별칭입니다. 세션의 모델과 effort는 고정됩니다.
각 논리 작업에는 고유한 request ID를 사용하세요. 같은 ID와 같은 입력을
다시 보내면 기존 결과를 반환하지만, 같은 ID에 다른 입력을 쓰면 거절합니다.

## 구조화된 단일 위임

역할 지침과 검증 가능한 보고서 형식이 필요하면 `delegate`를 사용합니다.
다음을 `/tmp/parser-review.json`으로 저장합니다.

```json
{
  "id": "parser-review",
  "name": "Parser review",
  "role": "critic",
  "model": "fable",
  "project": "/srv/my-project",
  "objective": "Review the supplied parser behavior for ambiguous inputs.",
  "context": "Paste the relevant requirements, source text and test evidence here.",
  "scope": ["Only the supplied parser and tests"],
  "acceptance_criteria": [
    "Identify concrete failure cases",
    "Separate verified facts from assumptions"
  ],
  "deliverable": "A concise review with regression-test proposals.",
  "effort": "high",
  "timeout": 300
}
```

```bash
claude_control roles
claude_control delegate \
  --assignment-file /tmp/parser-review.json \
  --request-id parser-review-delegate-001
claude_control wait --run <run-uuid> --seconds 30
claude_control report --run <run-uuid>
```

기본 역할 배치는 `executor`·`researcher`가 Sonnet,
`planner`·`architect`·`critic`·`verifier`가 Fable입니다. assignment에
`model`을 쓰면 해당 선택을 사용합니다. `report`는 ID와 형식을 검증하지만
내용을 승인하지는 않습니다.

## 전체 실습: 계획, 편집, 동결본 검토

중요한 저장소 변경에는 `composition` 흐름을 권장합니다.

### 1. 커밋된 트리 확인

Composition 생성 시 ref를 정확한 커밋으로 고정합니다. 원본 저장소의
미커밋 변경과 추적되지 않은 파일은 사본에 포함되지 않습니다.

```bash
git -C /srv/my-project status --short
git -C /srv/my-project rev-parse HEAD
mkdir -p /tmp/claude-composition
```

### 2. 계획 assignment 작성

`/tmp/claude-composition/plan.json`:

```json
{
  "id": "ledger-plan",
  "name": "Plan the ledger fix",
  "role": "planner",
  "model": "fable",
  "project": "/srv/my-project",
  "objective": "Produce a bounded implementation and test plan for the supplied ledger defect.",
  "context": "Paste the defect report, relevant source excerpts and constraints here. The planning stage cannot read repository paths directly.",
  "scope": ["src/ledger.py", "tests/test_ledger.py"],
  "acceptance_criteria": [
    "The plan names the exact files and behavior to change",
    "The plan defines a regression test and the named verification command"
  ],
  "deliverable": "A file-by-file implementation plan with risks and verification steps.",
  "effort": "high",
  "timeout": 600
}
```

계획 단계는 텍스트 위임입니다. `context`에 결함 설명, 관련 코드, 제약을
넣어야 하며 경로만 적는다고 파일을 읽지는 않습니다.

### 3. 편집 assignment 작성

`/tmp/claude-composition/editor.json`:

```json
{
  "id": "ledger-editor",
  "name": "Implement the ledger fix",
  "role": "executor",
  "model": "sonnet",
  "project": "/srv/my-project",
  "objective": "Implement the accepted plan in the controlled workspace.",
  "context": "Use only controller-mediated reads, full-file writes and named checks. The accepted plan provenance is appended by the composition.",
  "scope": ["src/ledger.py", "tests/test_ledger.py"],
  "acceptance_criteria": [
    "src/ledger.py implements the accepted behavior",
    "tests/test_ledger.py covers the regression",
    "The unit named check passes"
  ],
  "deliverable": "A complete workspace report with no pending operations.",
  "effort": "medium",
  "timeout": 600
}
```

### 4. 파일과 검사 권한 작성

`/tmp/claude-composition/editor-policy.json`:

```json
{
  "version": 1,
  "role": "executor",
  "read_paths": ["src/ledger.py", "tests/test_ledger.py"],
  "write_paths": ["src/ledger.py", "tests/test_ledger.py"],
  "checks": {
    "unit": {
      "argv": ["/usr/bin/python3", "-m", "unittest", "tests.test_ledger"],
      "timeout": 60
    }
  },
  "max_actions": 12,
  "max_calls": 6
}
```

쓰기 경로는 반드시 읽기 경로에도 있어야 합니다. 검사 실행 파일은
`/usr/bin/<이름>` 형태의 절대 경로여야 합니다. Claude는 `unit` 검사를
요청할 수 있지만 argv를 바꿀 수 없습니다.

### 5. 독립 검토 assignment 작성

`/tmp/claude-composition/reviewer.json`:

```json
{
  "id": "ledger-reviewer",
  "name": "Verify the frozen ledger fix",
  "role": "verifier",
  "model": "fable",
  "project": "/srv/my-project",
  "objective": "Independently assess the frozen editor result.",
  "context": "Review the frozen selected files and report evidence and limitations. Do not request writes or checks.",
  "scope": ["src/ledger.py", "tests/test_ledger.py"],
  "acceptance_criteria": [
    "src/ledger.py implements the accepted behavior",
    "tests/test_ledger.py covers the regression",
    "The unit named check passes"
  ],
  "deliverable": "A read-only verification report for Codex.",
  "effort": "high",
  "timeout": 600
}
```

검토자의 `acceptance_criteria`는 편집자의 기준과 정확히 같아야 합니다.
검토 정책은 컨트롤러가 자동으로 파생합니다. 읽기 경로만 같고 쓰기 경로와
검사는 비어 있습니다.

### 6. Composition 생성

```bash
claude_control composition create \
  --planning-assignment-file /tmp/claude-composition/plan.json \
  --editor-assignment-file /tmp/claude-composition/editor.json \
  --editor-policy-file /tmp/claude-composition/editor-policy.json \
  --reviewer-assignment-file /tmp/claude-composition/reviewer.json \
  --repo /srv/my-project \
  --ref HEAD \
  --operation-id ledger-composition-create-001 \
  --max-revisions 2 \
  --max-calls 6 \
  --dispatch-window-seconds 900 \
  --reviewer-effort high \
  --routing-metadata-file /tmp/claude-composition/routing.json
```

생성은 Git 커밋과 P4 workflow를 기록할 뿐 모델을 호출하지 않습니다.
반환된 composition UUID를 보관하세요.

리더가 직접 쓴 명세를 사용할 때는 `--planning-assignment-file` 대신
`--leader-spec-file`과 Fable `--critic-assignment-file`을 전달합니다. 선택적인
`--test-contract-file`은 editor binding 전 baseline 기대값을 확인하고, 동결한
테스트 경로가 그대로이며 모든 필수 검사가 최종 트리에서 통과했을 때만 reviewer를
엽니다. 전체 JSON 형식과 예제는 [composition 가이드](docs/compositions.ko.md)에
있습니다.

완료된 scout를 정확한 입력으로 재사용하려면 UUID 대신
`--scout-cache-assignment-file /tmp/claude-composition/scout.json`을 사용합니다.
일치하는 frozen 결과가 없으면 명시적으로 실패합니다. 여러 composition은 다음처럼
foreground dispatcher로 진행합니다.

```bash
claude_control composition dispatch --all --once
claude_control composition dispatch --all --until-idle --max-seconds 60
```

실제 기록의 성공률·비용과 라우팅별 차이는 다음과 같이 읽습니다.

```bash
claude_control composition evaluate --all
claude_control composition evaluate --composition <uuid> --composition <uuid>
claude_control composition evaluate --all --stratify risk_class --stratify editor_model
```

평가 명령은 원장을 변경하지 않으며, 누락된 비용이나 토큰을 추정하지 않습니다.

### 7. 계획 승인 지점까지 실행

```bash
claude_control composition run \
  --composition <composition-uuid> \
  --until-idle --max-seconds 60

claude_control composition status --composition <composition-uuid>
```

호출이 진행 중이었다면 상태를 확인한 뒤 같은 composition의 bounded run을
다시 실행합니다. 정상적인 첫 승인 지점은 다음과 같습니다.

```text
state:  awaiting_codex
reason: plan_acceptance_required
```

내부 workflow에는 `approve_recommended`가 표시되어야 합니다. 이는 검토
권고이며 승인 자체가 아닙니다.

### 8. 정확한 계획 결과 검토·승인

Composition status에 나온 계획 task와 run을 조회합니다.

```bash
claude_control task show --task <plan-task-uuid>
claude_control report --run <plan-run-uuid>
claude_control result --run <plan-run-uuid>
```

계획의 1부터 시작하는 모든 기준에 대해 근거를 기록한
`/tmp/claude-composition/plan-evidence.json`을 작성합니다.

```json
{
  "1": [{"type": "free_text", "text": "Codex verified the exact file and behavior boundaries."}],
  "2": [{"type": "free_text", "text": "Codex verified the proposed regression test and named check."}]
}
```

컨트롤러가 보여준 정확한 개정, run, 결과 해시를 사용합니다.

```bash
claude_control task accept \
  --task <plan-task-uuid> \
  --revision <plan-revision> \
  --run <plan-run-uuid> \
  --result-sha256 <plan-result-sha256> \
  --evidence-file /tmp/claude-composition/plan-evidence.json \
  --operation-id ledger-plan-accept-001
```

독립 계획 검토가 정확히 `approve_recommended` 상태가 아니면 composition이
이 승인을 거절합니다.

### 9. 편집과 동결본 검토 실행

```bash
claude_control composition run \
  --composition <composition-uuid> \
  --until-idle --max-seconds 60

claude_control composition status --composition <composition-uuid>
```

계획 승인 후, 생성 시 고정한 커밋으로 편집 workspace를 만듭니다. 편집이
끝나면 트리를 먼저 동결하고, 그 동결본으로 별도 읽기 전용 Fable 검토
workspace를 만듭니다. 다음 상태까지 bounded run을 이어갑니다.

```text
state:  awaiting_codex
reason: final_review_ready
```

여기까지 원본 저장소에는 어떤 파일도 자동 반영되지 않습니다.

### 10. 동결 증거 확인과 최종 승인

Composition status에 editor와 reviewer workspace UUID, 정확한 결과 ID와
해시가 들어 있습니다. 두 결과를 모두 확인합니다.

```bash
claude_control workspace status --workspace <editor-workspace-uuid>
claude_control workspace export --workspace <editor-workspace-uuid>
claude_control workspace status --workspace <reviewer-workspace-uuid>
claude_control workspace export --workspace <reviewer-workspace-uuid>
claude_control report --run <reviewer-run-uuid>
```

편집자의 모든 기준을 다루는
`/tmp/claude-composition/editor-evidence.json`을 작성합니다.

```json
{
  "1": [{"type": "free_text", "text": "Codex inspected the frozen implementation against the accepted plan."}],
  "2": [{"type": "free_text", "text": "Codex inspected the frozen regression test."}],
  "3": [{"type": "free_text", "text": "Codex verified the recorded named-check receipt and reviewer evidence."}]
}
```

```bash
claude_control task accept \
  --task <editor-task-uuid> \
  --revision <editor-revision> \
  --run <editor-run-uuid> \
  --result-sha256 <editor-result-sha256> \
  --evidence-file /tmp/claude-composition/editor-evidence.json \
  --operation-id ledger-editor-accept-001

claude_control composition status --composition <composition-uuid>
```

`accepted` 상태는 정확한 편집 결과에 대한 승인 기록과 손상되지 않은
editor/reviewer export에서 파생됩니다. 동결 patch나 파일을 원본에 적용하는
일은 이후 Codex 또는 사용자가 별도로 수행합니다.

## Composition 상태 읽기

| 상태/이유 | 의미 | 다음 행동 |
|---|---|---|
| `active` | 명시적으로 진행할 단계가 남아 있음 | bounded `composition run` 한 번 실행 |
| `awaiting_codex/plan_acceptance_required` | 검토된 계획이 준비됨 | 정확한 계획 결과를 확인하고 명시적으로 승인 |
| `awaiting_codex/final_review_ready` | 편집과 동결본 검토가 완료됨 | 두 export를 확인하고 정확한 편집 결과 승인 |
| `awaiting_codex/final_review_revise` | reviewer가 기준 실패를 확인함 | revision 지시를 확인하고 새 편집 작업 결정 |
| `awaiting_codex/final_review_blocked` | reviewer에게 필수 근거가 부족함 | 누락된 근거를 보강하기 전에는 승인하지 않음 |
| `awaiting_codex/<failure>` | 실패·잘못된 보고서·예산·불확실성으로 자동화가 멈춤 | 해당 child를 조사하고 자동 대체 작업을 만들지 않음 |
| `accepted` | 정확한 편집 승인과 온전한 증거가 존재함 | 원본 반영 여부를 별도로 결정 |
| `stopping` | 소유한 실행과 명령을 정리하는 중 | `stop` 또는 `run`으로 정리를 계속한 뒤 확인 |
| `stopped` | 신규 실행이 막혔고 정리가 끝남 | 기록을 보존하거나 의도적으로 새 composition 생성 |

증거를 지우지 않고 composition을 중단할 수 있습니다.

```bash
claude_control composition stop \
  --composition <composition-uuid> \
  --operation-id ledger-composition-stop-001
```

## 파일 작업이 없는 Task 승인 흐름

파일 작업이 필요 없다면 다음 명시적 흐름을 사용합니다.

```text
task create → task submit → report/result → task review → task accept
                                      ↘ task revise → task submit
```

`task review`의 `approve`, `revise`, `blocked`는 검토 권고입니다. 실제 승인은
`task accept`만 만들 수 있으며, 정확한 task 개정, run ID, 결과 SHA-256,
모든 기준의 근거가 필요합니다. 자세한 내용은 [작업 가이드](docs/tasks.ko.md)를
참고하세요.

## 병렬 실행, 대기열, 다음 턴 메시지

이미 시작한 여러 실행을 함께 관찰할 수 있습니다.

```bash
claude_control observe \
  --run <first-run-uuid> \
  --run <second-run-uuid> \
  --seconds 30
```

Task를 대기열에 넣고 준비된 작업을 명시적으로 배정합니다.

```bash
claude_control task enqueue \
  --task <task-uuid> --revision 1 \
  --operation-id enqueue-task-001

claude_control dispatch --until-idle --max-seconds 60
```

부모 task는 정확한 Codex 승인을 받아야 의존 조건을 만족합니다.
`message enqueue`로 이후 지시를 저장하고 `task revise`에서 message ID를
선택한 다음 새 개정을 별도로 제출합니다. [대기열](docs/queue.ko.md)과
[메시지·인수인계](docs/messages.ko.md)를 참고하세요.

## 중단과 복구

한 실행의 취소를 요청하고 실제 종료 상태를 확인합니다.

```bash
claude_control stop --run <run-uuid>
claude_control wait --run <run-uuid> --seconds 30
```

`unknown`은 실행이 아직 존재할 가능성을 뜻하므로 동시 실행 슬롯을 계속
차지합니다. 로그와 기록을 확인한 뒤, 원래 실행의 PID namespace에서 같은
run을 reconcile합니다.

```bash
claude_control status --run <run-uuid>
claude_control logs --run <run-uuid> --stream worker --bytes 8192
claude_control reconcile --run <run-uuid>
```

불확실성을 피하려고 DB를 지우거나 다른 state store를 초기화하거나 대체
세션을 만들면 안 됩니다. 실패·취소된 대화도 입력 일부를 읽었을 수 있으므로
확인 후 필요한 context acknowledgement를 명시해 후속 턴을 만드세요.

## 상태 저장소와 여러 호스트

기본 상태 경로는 `$XDG_STATE_HOME/claude-control`이며, `XDG_STATE_HOME`이
없으면 `$HOME/.local/state/claude-control`입니다. 설정, 프롬프트, 응답,
작업 이력, 프로세스 증거와 비공개 workspace를 사용자 전용 권한으로
저장합니다.

다른 초기화된 저장소를 선택하려면 `--state-dir`을 subcommand 앞에 둡니다.

```bash
claude_control --state-dir /srv/controller-state list
```

각 호스트는 별도 상태 저장소와 Claude 로그인·세션 이력을 가집니다. 이
버전은 컴퓨터 사이에 작업을 전달하지 않습니다.

## 업데이트와 스키마 이관

관리 중인 실행을 끝내거나 중단하고, `unknown` 실행을 해결한 뒤 갱신합니다.

```bash
git pull --ff-only
python3 install.py --update
```

재설치 후 새 Codex 작업을 여세요. 이전 CLI와 worker가 모두 종료된 상태에서
새 CLI로 기존 저장소를 확인하고 이관합니다.

```bash
claude_control migrate --status
claude_control migrate --offline
claude_control migrate --status
```

새 저장소는 schema 15를 사용합니다. 기존 schema 3–14는 각 단계마다 검증된
SQLite 백업과 영속 migration journal을 만들면서 순서대로 이관됩니다.
중단되었다면 원래 상태 디렉터리에서 같은 `migrate --offline`을 다시
실행합니다. 백업 DB로 작업을 실행하거나 원본 DB를 단일 SQLite 파일
복사본으로 교체하지 마세요.

## 문제 해결

| 증상 | 의미와 조치 |
|---|---|
| `doctor`가 Claude 옵션 부족을 보고함 | 표시된 옵션을 지원하는 Claude Code로 갱신 |
| `doctor --auth`가 ready가 아님 | 일반 Claude Code 로그인 후 다시 검사 |
| 지정 모델 호출 실패 | 계정의 모델 사용 가능 여부 확인; 자동 대체 없음 |
| `project_not_allowed` | 초기화한 `--allow-root` 내부 경로 사용; 새 state store로 우회하지 않음 |
| Linux에서 `workspace doctor` 실패 | Bubblewrap 또는 비특권 namespace를 복구; 비격리 실행은 하지 않음 |
| Windows에서 session control 비활성 | 아키텍처에 맞는 `ccc-win-supervisor.exe`와 설치 시 고정한 SHA-256을 확인 |
| Windows에서 workspace 비활성 | WSL2, WSL 내부 `bwrap`, private ext4 상태 경로를 확인; `/mnt` 아래 상태는 거절됨 |
| `capacity_full` 또는 session busy | 기존 실행을 확인하고 기다리거나 소유 실행을 의도적으로 중단 |
| `context_changed` | 최신 정확한 턴을 확인하고 요구된 acknowledgement와 함께 명시적 개정/후속 턴 생성 |
| `unknown` | 원래 실행을 조사하고 reconcile; 자동 재시도·대체 금지 |
| `invalid_report` | `result` 원문 확인 후 새 명시적 개정에서 수정; 자동 repair 호출 없음 |
| `migration_required` | client/run 중단, `migrate --status` 확인, `migrate --offline` 실행 |
| 설치 시 `yaml` import 실패 | PyYAML이 있는 Python 3.10+를 `--helper-python`으로 지정 |

## 문서 안내

- [CLI와 보고서 계약](docs/cli.ko.md)
- [Task 개정, 승인 근거와 migration](docs/tasks.ko.md)
- [대기열, 의존성, dispatch](docs/queue.ko.md)
- [다음 턴 메시지와 결과 인수인계](docs/messages.ko.md)
- [제한된 worker/reviewer workflow](docs/workflows.ko.md)
- [Workspace 정책과 격리](docs/workspaces.ko.md)
- [계획·편집·동결 검토 composition](docs/compositions.ko.md)
- [실시간 에이전트 모니터](docs/monitor.ko.md)
- [Effort와 실행 설정](docs/execution-settings.ko.md)
- [아키텍처와 신뢰 경계](docs/architecture.ko.md)
- [테스트와 선택적 live test](docs/testing.ko.md)
- [실제 프로젝트 파일럿 기록](docs/real-project-pilot.ko.md)
- [OMX에서 도입한 설계](docs/omx-adoption.ko.md)
- [Windows 설치와 운영](docs/windows.ko.md)
- [Windows 지원 설계 기록](docs/windows-support-plan.ko.md)

## 테스트

```bash
python3 -m unittest discover -s tests -v
ruff check .
ruff format --check .
```

일반 테스트는 가짜 Claude 프로세스를 사용하므로 모델을 호출하지 않습니다.
실제 Claude 계정을 쓰는 live test는 명시적으로 선택해야 하며,
[테스트 문서](docs/testing.ko.md)에 절차가 있습니다.

## 현재 범위

0.18 버전은 Linux와 Windows 10/11에서 텍스트 위임, 영속 세션,
검토·승인 가능한 task 개정, 유한 대기열과 workflow, 명시적
계획·편집·검토 composition, 다중 composition dispatch, 라우팅 출처와 평가,
검증된 scout 재사용, 역할별 모델·effort 설정, 계정 model catalog와 read-only
실시간 TUI를 지원합니다. Linux
workspace는 네이티브 Bubblewrap, Windows workspace는 WSL2 내부 Bubblewrap을
사용합니다. Claude의 직접 파일·shell 도구, 임의 기존 세션 인수, 대화 fork,
macOS, AppContainer, MCP adapter, 서버 간 전달, Codex 자동 깨우기는 현재
범위 밖입니다. Windows 네이티브 helper, Job Object와 도구 없는 실제 Sonnet
호출은 실기기에서 통과했습니다. WSL2/Bubblewrap 최종 검증은 Codex sandbox의
WSL 서비스 `E_ACCESSDENIED` 때문에 일반 Windows 터미널 실행이 남아 있으므로
Windows workspace 지원은 beta입니다.

제작: [kimstitute](https://github.com/kimstitute). OpenAI 또는 Anthropic의
공식 연동 제품이 아닌 독립 프로젝트입니다.
