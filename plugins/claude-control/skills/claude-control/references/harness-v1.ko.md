# Claude Code 하네스: Linux 단일 호스트 v1

`harness`는 Codex가 작업 목표를 한 번 전달하고 Claude Code가 스스로 저장소를 읽고 고치는 실험적 경로입니다. 기존 0.27 계열의 도구 없는 `task`·`workspace`·`composition` 명령은 계속 별도로 동작합니다.

## 계정과 실행 경계

- **Claude Agent SDK나 Anthropic API 키를 사용하지 않습니다.** 로컬 Claude Code CLI의 `claude.ai` Team 구독만 허용합니다. `harness init`은 현재 로그인된 조직 ID의 해시와 실행 파일 신원을 고정합니다. `harness start`와 `followup`은 실행 직전에 이를 다시 검사합니다. API 키와 타 제공자 선택 환경 변수는 자식 프로세스에 전달하지 않습니다.
- Team 조직에서 사용량 크레딧을 켜 두었다면 포함된 좌석 한도를 넘은 사용은 **조직 크레딧**으로 과금될 수 있습니다. 이 프로그램은 관리자 과금 설정을 읽거나 바꾸지 않습니다. 추가 지출도 금지하려면 랩실 관리자가 사용량 크레딧을 끄거나 해당 구성원의 한도를 0으로 설정해야 합니다. [Anthropic Team 사용량 크레딧 안내](https://support.claude.com/en/articles/12005970-manage-usage-credits-for-team-and-seat-based-enterprise-plans)
- `init` 전에 운영자가 로그인된 Team 조직이 랩실 제공 조직인지 확인해야 합니다. 프로그램은 조직의 이름을 출력하거나, 그 조직이 랩실 소유인지 스스로 증명하지 않습니다. 다른 조직으로 로그인하면 실행이 거부됩니다.
- Linux, Claude Code 2.1.248 이상, `bubblewrap`과 `socat`이 필요합니다. 시스템에 `socat`을 설치할 권한이 없으면 이미 확보한 신뢰할 수 있는 실행 파일을 `harness init --socat-bin /absolute/path/to/socat`으로 지정할 수 있습니다. 실행 파일 신원도 고정합니다. `harness doctor`가 실행 가능 상태를 확인합니다. 샌드박스가 없으면 파일 작업을 시작하지 않습니다.
- Claude는 원본 저장소와 Git 메타데이터를 공유하지 않는 작업별 독립 clone에서 실행됩니다. clone의 `origin`을 제거합니다. `--restricted`가 내장 파일 도구를 작업 디렉터리로 제한하고, Bash는 Claude Code의 필수 샌드박스 안에서 실행됩니다. 샌드박스 밖 재시도는 막고, 읽기 범위는 작업 디렉터리로 제한합니다. 등록되지 않은 MCP·네이티브 도구는 제공하지 않습니다.
- `--settings`와 조직 관리 정책은 Claude Code가 적용합니다. 이 경로는 호스트 OS 또는 조직 관리 정책보다 높은 보안 경계를 주장하지 않습니다. 외부 경로를 가리키는 심볼릭 링크와 clone에 다시 추가된 원격은 완료 상태로 인정하지 않습니다.

## 사용 순서

전역 `--state-dir`은 모든 호출에서 같은 값을 써야 합니다. 생략하면 기존 컨트롤러 기본 상태 디렉터리 아래 `harness/`를 사용합니다.

```sh
python3 plugins/claude-control/scripts/claude_control_cli.py harness init --claude-bin claude
python3 plugins/claude-control/scripts/claude_control_cli.py harness doctor
python3 plugins/claude-control/scripts/claude_control_cli.py harness start \
  --repo /absolute/path/to/repo --spec-file /absolute/path/to/task.md \
  --model sonnet --request-id stable-request-1
```

시스템 `socat`이 없다면 첫 줄에 `--socat-bin /absolute/path/to/socat`을 추가합니다. 플러그인은 시스템 패키지를 자동 설치하지 않습니다.

`start`는 동기 호출입니다. 같은 request ID와 같은 입력으로 다시 호출하면 기존 결과를 돌려주고, 입력이 바뀌면 거부합니다. Claude는 탐색·수정·가능한 자체 검증을 수행합니다. 성공한 턴 뒤에는 **컨트롤러가** 변경을 커밋합니다. Claude에게 Git 설정을 바꾸거나 `git commit`을 승인할 필요가 없습니다.

```sh
python3 plugins/claude-control/scripts/claude_control_cli.py harness show --task TASK_UUID
python3 plugins/claude-control/scripts/claude_control_cli.py harness followup \
  --task TASK_UUID --spec-file /absolute/path/to/followup.md
```

`show`는 base/head SHA, 변경 파일, 작업 트리 상태와 짧은 보고를 보여 줍니다. `followup`은 같은 Claude Code 세션과 clone을 이어 갑니다. 첫 턴이 세션 생성 전에 실패했다면 `--restart-session`으로 새 대화를 명시적으로 열 수 있습니다. 호출 프로세스가 사라진 뒤 상태가 `running`으로 남은 경우 `reconcile`로 `unknown`을 표시하고, 현재 파일·로그를 확인한 후 `--acknowledge-unknown`을 붙여 후속 지시를 보냅니다. 불명 실행을 자동 재시도하지 않습니다.

실행 중인 워커의 임무·모델·토큰·경과 시간은 읽기 전용 조회로 한 번에 확인할 수 있습니다.

```sh
python3 plugins/claude-control/scripts/claude_control_cli.py harness agents
python3 plugins/claude-control/scripts/claude_control_cli.py harness agents --all
```

첫 명령은 실행 중이거나 프로세스가 사라져 상태가 불명인 워커를, `--all`은 완료된 작업도 보여 줍니다. `mission`은 명세 첫 줄의 짧은 표제이며 기존 기록에는 없을 수 있습니다. `actual_models`는 Claude 이벤트에서 확인된 모델입니다. `tokens.total`은 입력·캐시 생성 입력·캐시 읽기 입력·출력 토큰의 합계입니다. 실행 중에는 중복 메시지를 제거한 관측치만 합산하고 `token_scope: provisional`로 표시합니다. 최종 `result` 사용량이 모든 턴에 있으면 `final`입니다. 이 조회는 Claude를 호출하지 않습니다. 현재 하네스는 Claude 내부 `Agent` 도구를 제공하지 않으므로 여기서 워커는 Codex가 관리하는 최상위 Claude 작업을 뜻합니다. 플러그인의 `claude-worker-status` 스킬은 이 결과에 **현재 Codex 대화의 서브에이전트 목록**을 더해 한 표로 정리합니다. Codex 조회에서 모델·토큰·시작 시각이 제공되지 않으면 값을 추측하지 않고 `—`로 표시합니다. 다른 Codex 대화의 서브에이전트는 이 스킬의 조회 범위 밖입니다.

승인 전에 컨트롤러가 별도 Bubblewrap에서 검사를 돌립니다. 검사 파일은 실행 파일의 **절대 경로**와 문자 그대로의 인수를 담은 JSON 배열입니다. 예: `["/usr/bin/python3", "-m", "unittest", "discover", "-s", "tests"]`. 검사 중 작업 트리나 head가 바뀌면 통과하지 않습니다.

```sh
python3 plugins/claude-control/scripts/claude_control_cli.py harness check \
  --task TASK_UUID --argv-json /absolute/path/to/check.json
python3 plugins/claude-control/scripts/claude_control_cli.py harness approve \
  --task TASK_UUID --sha EXACT_HEAD_SHA
```

검사가 적용되지 않는 문서 작업은 검토 후 `approve --accept-unchecked`를 명시할 수 있습니다. 승인은 기록만 남깁니다. 원본 브랜치로 merge·apply·push하지 않습니다. `provider_cost_usd`는 Claude가 보고한 사용량 추정치이며 개인 청구액을 뜻하지 않습니다.

## 현재 범위

이 버전은 Linux 단일 호스트에서 **작업별 clone → Claude의 자율 편집 → 컨트롤러 커밋 → 격리 검사 → 정확한 SHA 승인**과 읽기 전용 워커 현황 조회까지 제공합니다. 여러 호스트의 공통 큐, Codex 워커 백엔드, Agent SDK 감독자, GPU 예약·장시간 잡, 자동 통합, 실시간 그래픽 대시보드와 사람 승인 채널은 구현 범위에서 제외했습니다. 기존 0.27 명령의 보장을 새 경로가 대체했다고 간주하지 않습니다.

작업별 clone과 이벤트 기록은 자동 삭제하지 않습니다. 보존할 SHA·검사 기록을 검토한 뒤 상태 디렉터리의 수명 주기를 운영자가 관리합니다.

검사 실행은 기존 `workspace_sandbox`의 네트워크 없는 Bubblewrap 경계와 자원 상한을 사용합니다. 대형 학습·평가 작업이나 외부 서비스가 필요한 검사는 이 경로의 대상이 아닙니다.
