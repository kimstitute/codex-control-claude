<p align="center">
  <img src="plugins/claude-control/assets/logo.png" alt="Codex Control Claude 로고" width="128">
</p>

<h1 align="center">Codex Control Claude</h1>

<p align="center">Codex에서 여러 로컬 Claude Code 세션을 관리하세요.</p>

<p align="center">Linux · Python 3.10+ · Python 표준 라이브러리 기반 실행</p>

<p align="center">
  <a href="README.md">English</a> · 한국어 · <a href="docs/cli.md">CLI 가이드</a> · <a href="docs/architecture.md">구조와 동작 범위</a>
</p>

Claude에게 구현안을 부탁하고, 별도 세션에 검토를 맡기고, 필요한 대화만 이어가고 싶을 때 사용하는 Codex 플러그인입니다. 세션 생성과 상태 확인, 결과 회수, 중단·재개를 로컬 컨트롤러와 스킬이 함께 처리합니다.

**0.1 버전은 텍스트를 전달하는 위임 작업을 지원합니다.** Claude가 분석·코드 제안·검토 의견을 반환하면 Codex가 확인하고 적용합니다. 이 실행 프로필에서는 Claude의 파일·셸 도구와 MCP를 비활성화합니다.

## 주요 기능

| 기능 | 설명 |
|---|---|
| 역할과 모델 지정 | 작업마다 `sonnet` 또는 `fable`과 역할을 명시합니다. |
| 병렬 실행 | 독립된 대화를 설정된 한도 안에서 실행합니다. 기본 한도는 2개입니다. |
| 문맥 유지 | 정확한 관리 세션 ID로 후속 지시를 보냅니다. |
| 진행 상황 확인 | 상태, 길이가 제한된 로그, JSON 결과를 조회합니다. |
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

## 데이터와 실행 범위

기본 상태 경로는 `$XDG_STATE_HOME/claude-control` 또는 `~/.local/state/claude-control`입니다. 프롬프트·응답·세션 ID·프로세스 정보·로그를 사용자 전용 권한으로 저장합니다. Claude 자체의 로컬 세션 이력도 별도로 유지됩니다.

**로컬 제어는 로컬 모델 추론을 뜻하지 않습니다.** 모델 요청은 기존 인증을 사용하는 Claude Code를 통해 Claude 서비스로 전송됩니다. 컨트롤러가 별도 텔레메트리 서비스를 추가하거나 서버 간 작업을 전달하지는 않습니다.

프로젝트 허용 경로는 실행 디렉터리 제한이며 OS 파일 sandbox가 아닙니다. 프로세스 정리는 소유한 process group을 대상으로 하고, 그 그룹을 이탈하는 프로세스까지 격리하지는 않습니다. Claude의 관리자 정책도 적용됩니다.

## 업데이트와 테스트

진행 중인 관리 작업을 완료하거나 중단한 뒤 갱신합니다.

```bash
git pull --ff-only
python3 install.py --update
```

갱신 후 새 Codex 작업을 열어주세요. 상태 저장소는 유지됩니다. 현재 상태 스키마는 3이며 초기 개발 스키마의 자동 이관은 지원하지 않습니다.

```bash
python3 -m unittest discover -s tests -v
```

자동 테스트는 가짜 Claude 프로세스를 사용하며 모델 호출을 하지 않습니다. 실제 Claude 호출을 사용하는 선택적 시험은 [테스트 안내](docs/testing.md)를 확인하세요. 실제 대화 원문이나 로컬 시험 기록은 이 저장소에 포함하지 않습니다.

## 현재 지원 범위

Linux, 텍스트 위임 작업, 이 컨트롤러가 만든 세션을 지원합니다. Claude의 파일·셸 직접 실행, 임의의 기존 세션 인수, fork, Windows/macOS, MCP 어댑터, 서버 간 전달, Codex 자동 깨우기는 후속 확장 항목입니다.

제작: [kimstitute](https://github.com/kimstitute). OpenAI 또는 Anthropic의 공식 연동 제품이 아닌 독립 프로젝트입니다.
