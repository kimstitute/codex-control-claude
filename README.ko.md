<p align="center">
  <img src="plugins/claude-control/assets/logo.png" alt="Codex Control Claude 로고" width="128">
</p>

<h1 align="center">Codex Control Claude</h1>

<p align="center">Codex에서 여러 로컬 Claude Code 세션을 관리하세요.</p>

<p align="center">Linux · Python 3.10+ · Python 표준 라이브러리 기반 실행</p>

<p align="center">
  <a href="README.md">English</a> · 한국어 · <a href="docs/cli.md">CLI 가이드</a> · <a href="docs/architecture.md">구조와 동작 범위</a> · <a href="CHANGELOG.md">변경 기록</a>
</p>

Claude에게 구현안을 부탁하고, 별도 세션에 검토를 맡기고, 필요한 대화만 이어가고 싶을 때 사용하는 Codex 플러그인입니다. 세션 생성과 상태 확인, 결과 회수, 중단·재개를 로컬 컨트롤러와 스킬이 함께 처리합니다.

**0.7 버전은 통제된 작업 사본 수정과 시험을 지원합니다.** Codex가 Git 사본·수정 가능 파일·허용된 시험 명령을 지정하면, Claude가 구조화된 보고서로 작업을 요청하고 컨트롤러가 실행 결과를 돌려줍니다. Claude의 직접 파일·셸 도구와 MCP는 비활성 상태를 유지하며, 기존 텍스트 위임도 지원합니다.

실제 과제에서 확인한 시간 초과·보고 형식 실패, 검증된 입력 검사 수정과 후속 안정화
조건은 [실제 프로젝트 운용 기록](docs/real-project-pilot.md)에 정리했습니다.

## 주요 기능

| 기능 | 설명 |
|---|---|
| 역할별 위임 | 역할 지침과 구조화된 작업 지시를 실제 입력에 넣고 `sonnet` 또는 `fable`을 명시해 실행합니다. |
| 병렬 실행 | 독립된 대화를 설정된 한도 안에서 실행합니다. 기본 한도는 2개입니다. |
| 문맥 유지 | 정확한 관리 세션 ID로 후속 지시를 보냅니다. |
| 진행 상황 확인 | 여러 실행을 함께 관찰하거나 개별 상태·로그·결과를 조회합니다. |
| 구조화된 보고서 | 작업 ID와 보고 형식을 검증합니다. 내용의 정확성은 Codex가 별도로 확인합니다. |
| 제한된 검토·수정 흐름 | 작업자 제안 → 독립 Fable 검토 → 필요한 수정 → Codex 최종 승인 대기를 처리합니다. |
| 작업 사본 수정 | 파일별 정책을 고정하고 사본을 수정하며, 격리된 임시 사본에서 지정 시험을 실행합니다. |
| 고정 결과 검토 | 변경 사본·패치·해시를 보존하고 별도 Fable 세션에서 읽기 전용 검토를 수행합니다. |
| 재접속 요약 | `overview --attention`으로 검토 대기·차단·불명 실행과 남은 예산을 확인합니다. |
| 선택적 중단 | 해당 컨트롤러가 소유한 실행 하나를 중단합니다. |
| 명시적 복구 | 불확실한 실행을 확인하거나, 새 대화를 의도적으로 시작합니다. |
| 실행 검증 | 실제 응답 모델·세션 ID·종료 상태·결과 파일의 무결성을 확인합니다. |

각 설치는 **같은 호스트, 같은 사용자 계정의 Claude**를 관리합니다. 여러 컴퓨터에 설치해도 상태와 세션은 각각 독립적으로 유지됩니다.

## 준비 사항

- Linux와 Python 3.10 이상. 현재 Windows와 macOS는 지원하지 않습니다.
- 로컬 설치 및 로그인이 완료된 Claude Code. 기본 HOME 기반 인증을 사용하며, 별도의 `CLAUDE_CONFIG_DIR`나 API 키 환경 변수는 전달하지 않습니다.
- 플러그인 명령을 지원하는 Codex CLI와 함께 제공되는 `plugin-creator` 도구.
- Codex 플러그인 검증기를 실행할 수 있는, PyYAML이 이미 설치된 Python 3.10 이상. **컨트롤러 자체에는 외부 Python 의존성이 없습니다.** 설치기가 패키지를 자동 설치하지는 않습니다.

`sonnet`과 `fable`의 사용 가능 여부는 Claude 설치 및 계정에 따라 달라집니다. 사용할 수 없는 모델을 다른 모델로 자동 대체하지 않습니다. `doctor --auth`는 CLI 옵션과 로그인 상태를 확인하며, 특정 모델의 호출 가능 여부까지 확인하지는 않습니다.

## 빠르게 시작하기

### 1. 설치

```bash
git clone https://github.com/kimstitute/codex-control-claude.git
cd codex-control-claude
python3 install.py
```

소스를 `~/plugins/claude-control`에 복사하고, 개인 Codex marketplace에 등록한 뒤 플러그인을 설치합니다. 실행 기록은 패키지와 별도 경로에 저장합니다.

검증 도구의 위치가 다르면 다음 옵션을 사용하세요.

```bash
python3 install.py \
  --plugin-creator-root /absolute/path/to/plugin-creator \
  --helper-python /absolute/path/to/python-with-pyyaml
```

### 2. 이 컴퓨터의 실행 범위 설정

아래 예시 경로를 실제 Claude 실행 파일과 프로젝트 경로로 바꾸세요. Claude의 경로는 `command -v claude`로 확인할 수 있습니다.

```bash
python3 ~/plugins/claude-control/scripts/claude_control_cli.py init \
  --claude-bin /absolute/path/to/claude \
  --allow-root /absolute/path/to/your/project \
  --max-parallel 2

python3 ~/plugins/claude-control/scripts/claude_control_cli.py doctor --auth
```

여러 프로젝트를 허용하려면 초기화 명령에서 `--allow-root`를 반복해 지정합니다. 진단 결과의 `"ready": true`를 확인하세요.

### 3. Codex에 위임 요청

설치된 스킬을 발견할 수 있도록 새 Codex 작업을 열고 다음과 같이 요청합니다.

> $claude-control을 사용해서 Sonnet에게 작은 리팩터링안을, Fable에게 독립적인 비평을 맡겨줘. 필요한 코드는 텍스트로 전달하고, 각 세션을 따로 유지하면서 결과를 검증한 뒤 적용해줘.

기술적인 호출 성공과 답변의 정확성은 별개입니다. Codex가 내용을 검토하고 실제 변경을 확인하는 과정은 그대로 필요합니다.

## CLI로 직접 사용하기

프롬프트를 파일에 작성하고 실행합니다.

```bash
python3 ~/plugins/claude-control/scripts/claude_control_cli.py start \
  --name implementation --model sonnet --role implementer \
  --project /absolute/path/to/your/project \
  --prompt-file /absolute/path/to/task.txt \
  --request-id implement-example-001 --timeout 300
```

반환되는 `id`는 이번 **실행 ID**, `session_id`는 **관리 대화 ID**입니다. 응답을 받지 못해 같은 요청을 다시 제출한다면 기존 요청 ID를 유지하세요.

```bash
python3 ~/plugins/claude-control/scripts/claude_control_cli.py list
python3 ~/plugins/claude-control/scripts/claude_control_cli.py wait --run <run-uuid> --seconds 30
python3 ~/plugins/claude-control/scripts/claude_control_cli.py result --run <run-uuid>
```

후속 지시·중단·재개·복구의 자세한 사용법은 [CLI 가이드](docs/cli.md)에 있습니다.

`roles`는 역할 지침을 조회하고, `delegate --assignment-file <파일>
--request-id <ID>`는 역할 지침과 완료 조건을 포함한 작업을 전달합니다.
`observe --run <ID> --run <ID> --seconds 30`으로 여러 실행을 관찰하고,
`report --run <ID>`로 구조화된 답변을 확인합니다. 기존 `start --role`은
기록용 이름이며 역할 지침을 자동으로 넣지 않습니다.

기본 모델은 `executor`·`researcher`가 Sonnet,
`planner`·`architect`·`critic`·`verifier`가 Fable입니다. 명시적으로 다른
지원 모델을 지정할 수 있지만 자동 대체는 하지 않습니다. 실행 종료, 보고서
형식 적합성, 내용 승인은 별개입니다. `delegate`의 v1 보고는 검토 전 상태를 유지하며, 새 `task` 흐름에서는 Codex의 명시적 승인 기록을 남길 수 있습니다.

OMX의 역할·작업 관리 구조에서 도입한 부분과 이후 확장 계획은
[OMX 분석 문서](docs/omx-adoption.md)에 정리했습니다. 대기열·의존 작업은
명시적으로 호출하는 유한 조정 루프에서 실행합니다. 다음 턴 지시함과 명시적 결과 인수인계도 지원합니다.

## 데이터와 실행 범위

기본 상태 경로는 `$XDG_STATE_HOME/claude-control` 또는 `~/.local/state/claude-control`입니다. 프롬프트·응답·세션 ID·프로세스 정보·로그를 사용자 전용 권한으로 저장합니다. Claude 자체의 로컬 세션 이력도 별도로 유지됩니다.

**로컬 제어는 로컬 모델 추론을 뜻하지 않습니다.** 모델 요청은 기존 인증을 사용하는 Claude Code를 통해 Claude 서비스로 전송됩니다. 컨트롤러가 별도 텔레메트리 서비스를 추가하거나 서버 간 작업을 전달하지는 않습니다.

프로젝트 허용 경로는 Claude의 실행 디렉터리를 제한합니다. Workspace 시험 명령은 별도 Bubblewrap 네임스페이스에서 실행하며 호스트 홈·컨트롤러 상태·외부 네트워크·GPU 장치를 제공하지 않습니다. 자원 제한은 프로세스별 제한이며 전체 작업의 cgroup 할당량은 아닙니다. Claude의 관리자 정책도 적용됩니다.

## 업데이트와 테스트

진행 중인 관리 작업을 완료하거나 중단한 뒤 갱신합니다.

```bash
git pull --ff-only
python3 install.py --update
```

갱신 후 새 Codex 작업을 열어주세요. 상태 저장소는 유지됩니다. 새 저장소는 스키마 8을 사용합니다. 기존 스키마 3·4·5·6·7은 실행 종료 확인 후 `migrate --offline`으로 명시적으로 이관합니다. 검증된 SQLite 백업과 중단 후 재개를 지원하며, 실행 중 자동 이관하지 않습니다.

```bash
python3 -m unittest discover -s tests -v
```

자동 테스트는 가짜 Claude 프로세스를 사용하며 모델 호출을 하지 않습니다. 실제 Claude 호출을 사용하는 선택적 시험은 [테스트 안내](docs/testing.md)를 확인하세요. 실제 대화 원문이나 로컬 시험 기록은 이 저장소에 포함하지 않습니다.

## 작업 이력과 승인

0.3에서는 `task create → task submit → report → task review / task accept`로 작업을 관리합니다.
`task revise`는 입력과 완료 조건을 새 개정으로 보존하고, 이어서 `task submit`하면 같은
Claude 대화를 재개합니다. 작업·개정·실행 ID는 별개이며 결과 해시와 기준별 근거를 지정해야
승인할 수 있습니다. 검토 의견만으로 승인되지는 않습니다.

0.4에서는 `task enqueue`로 대기열에 등록하고 `dispatch --once` 또는
`dispatch --until-idle --max-seconds 60`으로 준비된 작업을 실행합니다.
부모의 정확한 개정·결과에 대한 Codex 승인이 있어야 자식 작업을 배정합니다.
`task events`로 상태 변경을 이어서 조회할 수 있습니다. 상주 조정자와 자동 검토 반복은 제공하지 않습니다.
자세한 사용법은 [대기열 가이드](docs/queue.md)를 참고하세요.
사용법과 이관 절차는 [작업 관리 가이드](docs/tasks.md)를 참고하세요.

## 현재 지원 범위

Linux, 텍스트 위임, 컨트롤러를 통한 제한된 사본 작업, 이 컨트롤러가 만든 세션을 지원합니다. Claude의 파일·셸 직접 실행, 임의의 기존 세션 인수, fork, Windows/macOS, MCP 어댑터, 서버 간 전달, Codex 자동 깨우기는 후속 확장 항목입니다.

제작: [kimstitute](https://github.com/kimstitute). OpenAI 또는 Anthropic의 공식 연동 제품이 아닌 독립 프로젝트입니다.

## 다음 턴 지시와 인수인계

0.5에서는 `message enqueue`로 실행 중에도 지시를 보관할 수 있습니다.
`task revise --message-id`로 포함할 지시를 선택한 뒤 제출하면 등록 순서대로
다음 턴에 전달합니다. 실행 중인 Claude에 입력을 끼워 넣거나 자동으로 새 턴을 만들지 않습니다.
검증된 결과를 출처 run·digest와 함께 복사하는 인수인계도 지원합니다.
전달 이력과 취소·명시적 재전달 규칙은 [지시함 가이드](docs/messages.md)에 정리했습니다.

## 제한된 검토·수정 흐름

`workflow create/run/status/stop`은 호출 횟수·수정 횟수·신규 실행 허용 시간을 보존하는 고정 흐름입니다. 기본값은 수정 2회, 호출 6회, 최초 예약 후 900초입니다. 검토 통과 권고는 Codex의 최종 승인을 대신하지 않습니다. [워크플로 가이드](docs/workflows.md)를 참고하세요.

## 통제된 사본에서 구현·시험하기

`workspace create/task/run/status/export/stop`으로 파일 수정과 시험을 진행합니다. Git의 특정 커밋을 사본으로 만들며, 원본의 미커밋·추적되지 않은 파일은 포함하지 않습니다. 결과를 고정한 뒤 `workspace create --from-snapshot`으로 별도 Fable 읽기 전용 검토를 만들 수 있습니다. 최종 승인과 원본 반영은 Codex가 담당합니다.

`composition create/run/status/stop`은 검토된 계획, 정확한 계획 승인, 제한된
편집·검사, frozen 결과의 별도 Fable 검토, 정확한 최종 승인을 한 흐름으로
연결합니다. 생성 시 Git ref를 한 커밋으로 고정하며, 자동 승인·재시도·병합·push는
하지 않습니다. 자세한 순서와 복구 경계는 [조합 가이드](docs/compositions.md)를
참고하세요.

Git과 Bubblewrap, 사용 가능한 Linux 사용자·PID·네트워크 네임스페이스가 필요합니다. `workspace doctor`로 확인하며 격리를 사용할 수 없으면 실행을 거절합니다. 정책·실행·복구 예시는 [워크스페이스 가이드](docs/workspaces.md)를 참고하세요.
