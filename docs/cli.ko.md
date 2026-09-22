# CLI 레퍼런스

모든 예시는 플러그인이 이미 설치되어 있다고 가정합니다. 현재 셸에서 더 짧은 명령을 사용하려면 다음과 같이 합니다.

```bash
claude_control() {
  python3 "$HOME/plugins/claude-control/scripts/claude_control_cli.py" "$@"
}
```

`--help`와 `--version`을 사용할 수 있습니다. 공개 운영 명령은 JSON을 반환하며, 0이 아닌 종료 상태는 오류를 나타냅니다. 예시의 경로와 UUID 자리표시자는 실제 값으로 바꾸세요.

## 상태 저장소 선택

기본값은 `$XDG_STATE_HOME/claude-control`이며, XDG_STATE_HOME이 설정되어 있지 않으면 `~/.local/state/claude-control`입니다. 다른 위치를 선택하려면 **모든 호출에서 전역 옵션을 하위 명령 앞에** 두세요.

```bash
claude_control --state-dir /absolute/path/to/private-state list
```

동시성 제한을 공유하고 기존 작업을 발견하려면 Codex 작업 전체에서 같은 저장소를 사용하세요. 점유된 슬롯이나 불확실한 실행을 우회하기 위해 두 번째 저장소를 만들지 마세요. `status`와 `list`는 영속 상태를 갱신하므로 데이터베이스 쓰기 권한도 필요합니다.

## 초기화와 진단

```bash
claude_control init \
  --claude-bin /absolute/path/to/claude \
  --allow-root /absolute/path/to/project \
  --max-parallel 2
claude_control doctor --auth
```

실행 파일과 프로젝트 경로는 명시적입니다. 저장소는 이를 만든 호스트와 사용자에게 귀속됩니다. 초기화는 파일 시스템 격리를 부여하거나 Claude 인증을 변경하지 않습니다.

## 실시간 모니터와 사용 기록

```bash
claude_control monitor tui --refresh-seconds 0.5 --history 100
claude_control monitor snapshot --history 100
claude_control monitor limits
```

TUI는 graph, agents, history, provider limits 화면을 제공하며 원장을 변경하거나
작업을 진행시키지 않습니다. 자동화용 `snapshot`은 관찰 정보를 JSON으로
반환하고, `limits`는 로그인된 Codex, Claude Code, Gemini CLI, Cursor 한도를
조회합니다.
자세한 키 조작과 실시간 추정치·완료 확정값의 차이는
[실시간 모니터 안내](monitor.ko.md)를 참고하세요.

## 개정과 승인이 있는 Task

`task create/list/show/submit/revise/retry/review/accept`와 `migrate --status/--offline`에 대해서는 [작업 생명주기 레퍼런스](tasks.ko.md)를 참고하세요. 새 task 턴은 구조화된 후속 작업을 포함해 명시적으로 기록된 v2 계약을 사용합니다. 기존 delegate와 비구조화 명령의 의미는 그대로 유지됩니다.

## 대화 시작

### 역할과 출력 계약으로 위임하기

`roles`는 초기화된 저장소 없이도 번들된 역할 지침과 해석된 기본 모델 목록을 보여줍니다.

```bash
claude_control roles
```

| 역할 | 기본 모델 | 제공 텍스트 작업 |
|---|---|---|
| `executor` | `sonnet` | 제한된 범위의 코드 또는 설정 변경 제안 |
| `researcher` | `sonnet` | 제공된 자료를 요약하고 누락된 근거 식별 |
| `planner` | `fable` | 연구, 실험 또는 구현 계획 수립 |
| `architect` | `fable` | 인터페이스 설계 및 기술적 트레이드오프 평가 |
| `critic` | `fable` | 약점과 근거 없는 주장 식별 |
| `verifier` | `fable` | 제공된 결과를 승인 기준에 따라 평가 |

구성된 루트 안의 기존 프로젝트를 사용하여 assignment JSON 파일을 작성하세요.

```json
{
  "id": "review-parser",
  "name": "parser-review",
  "role": "critic",
  "project": "/absolute/path/to/project",
  "objective": "Assess the supplied parser for ambiguous input handling.",
  "context": "Paste the relevant source code and existing test evidence here.",
  "scope": ["Only the supplied parser"],
  "acceptance_criteria": ["Identify concrete failure cases and distinguish assumptions"],
  "deliverable": "A concise review with proposed regression cases.",
  "effort": "high",
  "timeout": 300
}
```

```bash
claude_control delegate --assignment-file /absolute/path/to/assignment.json \
  --request-id parser-review-001
```

`model`과 `timeout`은 선택 사항이며, 기본값은 해당 역할의 모델과 300초입니다. 선택적 `effort`는 `low`, `medium`, `high`, `xhigh`, `max`를 받으며, 생략하면 CLI 동작이 유지되지만 JSON null은 유효하지 않습니다. [실행 설정](execution-settings.ko.md)을 참고하세요. 명시적으로 `"model": "sonnet"` 또는 `"model": "fable"`을 지정하면 프리셋을 재정의합니다. 해석된 모델은 항상 Claude에 명시적으로 전달됩니다. 자동 분류기나 모델 폴백은 없습니다. 나머지 모든 필드는 필수입니다. `context`는 비어 있을 수 있으며, scope와 acceptance criteria는 비어 있지 않은 문자열 목록이어야 합니다. ID는 `[a-z0-9][a-z0-9_-]{0,63}`와 일치해야 하며, 이름은 비어 있지 않고 120자 이하여야 합니다. Timeout은 1~3600초 사이의 유한한 숫자여야 합니다. 알 수 없는 필드, 중복된 JSON 키, null 옵션, 잘못된 타입은 거부됩니다.

각 delegate는 **새로운 명명된 세션**을 만듭니다. request ID와 입력이 일치하면 원래 run을 반환하며, 기존 이름에 다른 요청을 보내면 거부됩니다. context, timeout, model 또는 번들된 역할 지침의 변경은 기존 request ID와 충돌합니다. 절대 프로젝트 경로는 렌더링 전에 검사되고 정규화됩니다. assignment 파일과 렌더링된 프롬프트는 모두 1MiB 이내여야 하며, JSON 이스케이핑과 역할 지침은 후자에 포함됩니다. context는 제공된 텍스트일 뿐, 컨트롤러가 다른 파일을 읽도록 하는 지시가 아닙니다.

정확한 버전의 역할 지침과 task는 `prompt.txt`에 정규 JSON 형태로 저장되어 stdin으로 전송되며, run-intent 지문에 포함됩니다. 이는 Claude에 도구 접근 권한을 부여하지 않습니다.

### 비구조화된 프롬프트 사용하기

```bash
claude_control start \
  --name implementation \
  --model sonnet \
  --role implementer \
  --project /absolute/path/to/project \
  --prompt-file /absolute/path/to/task.txt \
  --request-id example-implementation-001 \
  --timeout 300
```

- 계정에서 사용 가능하다면, 제한된 일상 작업에는 `sonnet`을, 중요한 계획이나 비평에는 `fable`을 사용하세요.
- 도구 없이 작업을 해결할 수 있도록 프롬프트에 충분한 원문과 맥락을 제공하세요. 프롬프트는 1MiB로 제한됩니다.
- 모델, 역할, 프로젝트는 해당 관리 세션 동안 고정됩니다.
- `start --role`은 자유 형식 메타데이터 레이블입니다. 프리셋 역할 지침을 주입하는 것은 `delegate`뿐입니다.
- run 레코드가 반환되었다는 것은 요청이 수락되었다는 의미일 뿐, 완료되었다는 뜻은 아닙니다.
- 불확실한 제출을 재시도할 때는 같은 request ID와 입력을 유지하세요. 같은 ID에 다른 입력을 사용하면 거부됩니다.

| 필드 | 의미 | 사용 용도 |
|---|---|---|
| `id` | 단일 실행, 즉 run | status, logs, result, stop, wait, reconcile |
| `session_id` | 컨트롤러의 대화 기록 | follow-up, resume, restart |
| `backend_id` | Claude에 영속된 대화 UUID | 증거와 진단용; 컨트롤러가 선택 |

표시 이름은 세션 ID로 허용되지 않습니다. backend ID를 관리 세션 ID 대신 사용하지 마세요.

## 조회와 수집

```bash
claude_control list
claude_control status --run <run-uuid>
claude_control wait --run <run-uuid> --seconds 30
claude_control logs --run <run-uuid> --stream events --bytes 8192
claude_control result --run <run-uuid>
```

`wait`는 호출당 최대 60초로 제한됩니다. wait 타임아웃이 발생해도 run은 계속 실행됩니다. 로그 스트림은 `events`, `stderr`, `worker`이며, 각 응답은 최대 65,536바이트로 제한됩니다.

`completed` 결과는 유효한 응답 스트림, 일치하는 세션과 모델, 종료 코드 0, 검증된 결과 아티팩트를 필요로 합니다. 답이 정확함을 증명하지는 않습니다.

### 여러 실행 관찰하기

```bash
claude_control observe --run <first-run-uuid> --run <second-run-uuid> --seconds 30
```

서로 다른 관리 run을 1~128개 선택하세요. 중복 ID는 한 번만 계산됩니다. 이 호출은 선택된 모든 실행이 종료 상태이거나, failed/cancelled/interrupted/unknown 실행에 주의가 필요하거나, 제한된 대기(0~60초)가 끝나면 반환됩니다. `runs`, `counts`, `execution_done`, `needs_attention`(run ID), `wait_reason`을 반환합니다. `unknown` run은 주의가 필요하지만 종료 상태는 아닙니다. Observation은 작업을 중단, 재시도, 대기열에 넣거나 실행하지 않습니다. 영속 상태를 갱신하므로 데이터베이스 쓰기 권한이 필요합니다. `execution_done`은 프로세스 결과만을 설명하며, 보고서를 파싱하거나 정답 여부를 암시하지 않습니다.

### 위임된 보고서 조회하기

```bash
claude_control report --run <run-uuid>
```

응답은 `execution_status`, `contract_status`, `format_status`, `agent_status`, `acceptance`를 구분합니다. acceptance는 항상 `unreviewed`입니다. Codex가 내용을 직접 확인해야 합니다. 종료되지 않은 실행은 format status가 pending입니다. 일반 `start`나 비구조화 follow-up은 지원되지 않는 계약을 가지므로, 텍스트를 확인하려면 `result`를 사용하세요. resume/follow-up은 대화를 계속 보존하지만, 새 구조화된 assignment를 만들거나 이전 보고서 계약을 재사용하지는 않습니다.

완료된 위임 실행에 대해 컨트롤러는 원본 프롬프트를 저장된 run intent와 대조해 검증하고, 읽고 있는 정확한 결과 바이트를 검증합니다. 그런 다음 정확히 다음 필드를 포함하는 보고서를 검사합니다.

```json
{
  "task_id": "review-parser",
  "role": "critic",
  "status": "complete",
  "summary": "Assessment of the supplied text.",
  "deliverable": "Concrete findings and proposed tests go here.",
  "evidence": [{"claim": "A concrete finding", "basis": "supplied_context", "reference": "Relevant supplied excerpt"}],
  "limitations": ["No tests were executed by this reviewer."],
  "handoff": "Codex should run and assess the proposed tests."
}
```

`status`는 `complete` 또는 `blocked`입니다. evidence의 basis는 `supplied_context` 또는 `reasoning`입니다. blocked 보고서와 evidence가 없는 보고서는 비어 있지 않은 limitations가 필요합니다. 감싸는 코드 펜스 하나(맨 것 또는 `json`)는 허용되지만, 앞뒤에 다른 텍스트가 있거나, 키가 중복되거나, 식별자가 잘못되었거나, 필드가 누락·초과되거나, 응답이 1MiB를 초과하면 유효하지 않습니다. 구문적으로 유효한 evidence 주장이라도 실제로는 거짓일 수 있습니다. 역할 지침은 조작된 도구·테스트 주장을 금지하지만, 검증기가 기계적으로 준수 여부를 증명하거나 인용된 자료를 검증하지는 않습니다.

포맷 검사가 실패하면 `result`로 원본 출력을 확인하세요. 자동 복구 호출, 재시도, 승인 결정, 검증 모델 실행은 없습니다. `errors` 목록이 비어 있지 않은 사용 불가능한 보고서는 주의가 필요합니다. 결과가 단순히 없는 것이 아니라 저장된 계약이나 결과를 검증할 수 없었다는 뜻입니다.

## 계속하기 또는 재개하기

```bash
claude_control followup \
  --session <managed-session-uuid> \
  --prompt-file /absolute/path/to/followup.txt \
  --request-id example-implementation-002
```

`resume`은 `followup`의 별칭입니다. 두 명령 모두 정확한 영속 대화를 선택합니다. 추가 지시는 현재 턴이 끝난 뒤 새로운 턴으로 전송됩니다. 이 인터페이스는 활성 터미널에 입력을 넣는 방식이 아닙니다.

관리 세션에서는 한 번에 하나의 run만 활성화될 수 있습니다. 서로 다른 세션은 저장소 전체 제한 내에서 병렬로 실행될 수 있습니다.

## 소유한 run 중단하기

```bash
claude_control stop --run <run-uuid>
claude_control wait --run <run-uuid> --seconds 30
```

첫 응답은 취소 요청을 접수했다는 확인입니다. 종료 상태를 별도로 확인하세요. 접수 확인만으로는 프로세스가 실제로 멈췄다는 증거가 되지 않습니다. 실패하거나 취소된 대화를 계속하기 전에 부분 결과를 확인하세요.

```bash
claude_control followup \
  --session <managed-session-uuid> \
  --acknowledge-context \
  --prompt-file /absolute/path/to/recovery-context.txt \
  --request-id example-recovery-001
```

## 새로운 backend 대화 시작하기

Claude가 대화를 저장하기 전에 첫 턴이 종료되었다면, 일반적인 resume은 실패할 수 있습니다. 실패를 확인한 뒤 명시적으로 다시 시작하세요.

```bash
claude_control restart \
  --session <managed-session-uuid> \
  --acknowledge-context \
  --prompt-file /absolute/path/to/fresh-context.txt \
  --request-id example-restart-001
```

`restart`는 같은 관리 세션 아래에 새로운 Claude 대화를 생성합니다. **이전 대화의 맥락은 유지되지 않습니다.** 맥락을 다시 제공하세요. 과거 run ID, backend ID, 로그는 계속 조회할 수 있습니다. 일반 resume이 조용히 restart로 바뀌는 일은 없습니다.

## 상태와 복구

`unknown` run은 슬롯을 계속 점유하며 다른 턴을 막습니다. 컨트롤러에 실행이 멈췄음을 확인해 달라고 요청하기 전에 그 증거를 먼저 검토하세요.

```bash
claude_control reconcile --run <run-uuid>
```

같은 부팅 세션에서는 reconcile이 worker의 PID namespace 안에서 실행되어야 합니다. 기록된 worker와 프로세스 그룹이 더 이상 살아 있지 않거나, 호스트 재부팅으로 인해 이전 프로세스가 더 이상 실행 중일 수 없음이 증명된 경우에만 불확실성이 해소됩니다. 저장된 PID에 무작정 시그널을 보내지 않습니다. backend 세션이 일치하지 않는 대화는 계속 차단된 상태로 남습니다.

새 저장소는 schema 13를 사용합니다. 기존 schema 3–11은 새 기능을 사용하려면 [명시적 오프라인 migration](tasks.ko.md#schema-and-migration)이 필요합니다. migration 전에도 레거시 진단과 stop/reconcile은 계속 사용할 수 있습니다. schema 1과 2는 지원되지 않습니다. 활성 또는 unknown 실행을 우회하기 위해 상태를 삭제하거나 교체하지 마세요.

## 문제 해결

| 증상 | 다음 조치 |
|---|---|
| `doctor`가 옵션 누락을 보고함 | 보고된 플래그를 지원하는 Claude Code 버전을 사용하세요. |
| 로그인이 준비되지 않음 | 일반적인 로컬 Claude Code 로그인 절차로 로그인한 뒤 `doctor --auth`를 다시 실행하세요. |
| 모델 요청 실패 | run의 result와 stderr를 확인하고, 지정한 모델 별칭을 사용할 수 있는지 확인하세요. |
| 상태 데이터베이스를 열 수 없음 | 소유권과 쓰기 권한을 확인하세요. Codex에서 상태 경로가 샌드박스 밖에 있다면 일반적인 승인 메커니즘을 사용하세요. |
| `start` 후 worker가 사라짐 | 수명이 짧은 PID 샌드박스가 분리된 worker를 종료시켰을 수 있습니다. 지원되는 영속 호스트 실행 환경을 사용하고 원래 run을 조사·reconcile하세요. |
| 세션이 사용 중이거나 용량이 가득 참 | 기존 작업을 확인하세요. 대기하거나, 해당 소유 run을 의도적으로 취소하세요. |
| 설치기가 validator를 찾지 못함 | `--plugin-creator-root`와 `--helper-python`을 기존의 호환 가능한 도구로 지정하세요. |

Claude 도구를 활성화하거나, 자격 증명을 전달하거나, 호스트의 승인 정책을 우회하기 위해 고정된 프로필을 수정하지 마세요.

## 대기열, dispatch, events

- `init`은 `--max-parallel`과 별개로 `--max-queued <1..10000>`(기본값 100)을 받습니다.
- `task enqueue --task <UUID> --revision <N> --operation-id <id> [--dependencies-file <JSON>]`.
- `task dequeue --queue-id <integer> --operation-id <id>`는 대기 중인 작업을 취소합니다.
- `dispatch --once` 또는 `dispatch --until-idle --max-seconds <0..3600>`.
- `task events [--task <UUID>] [--after <cursor>] [--limit <1..1000>]`.

Dependency JSON은 정확한 `task_id`/`revision` 참조의 배열입니다. 승인 게이트, FIFO 순서, 마감 시한 의미와 복구에 대해서는 [대기열 가이드](queue.ko.md)를 참고하세요.

## 다음 턴 메시지

- `message enqueue --task <UUID> --base-revision <N> --content-file <UTF-8 file> --operation-id <id> [--session <UUID>] [--source-run <UUID> --source-result-sha256 <digest>]`.
- `message list [--task <UUID>] [--after <sequence>] [--limit <1..1000>]`.
- `message cancel --message <UUID> --operation-id <id>`.
- `task revise`는 반복 가능한 `--message-id <UUID>`와 명시적인 `--redeliver-messages`를 추가로 받습니다.

[메시지와 인수인계 의미](messages.ko.md)를 참고하세요. 등록과 개정 선택은 모델을 호출하지 않습니다. 전달 영수증은 명시적인 submit 또는 dispatch 시점에만 바인딩됩니다.

## 제한된 workflow와 주의 개요

`workflow create/run/status/stop`과 `overview --attention`은 [workflow 가이드](workflows.ko.md)에 문서화되어 있습니다. 생성은 모델을 호출하지 않습니다. `run --until-idle`은 명시적이고 유한한 `--max-seconds`가 필요합니다. 소유한 구성원 task에는 workflow 명령을 사용하세요. 일반 task 변경으로는 그 정책을 바꿀 수 없습니다.

## 통제된 Workspace

`workspace doctor/create/task/run/status/list/export/apply/stop/reconcile`은 [workspace 가이드](workspaces.ko.md)에 문서화되어 있습니다. 생성 시 복사하기 전에 하나의 불변 workspace identity를 예약합니다. schema 12는 원본 HEAD와 clean worktree를 검사하는 명시적 apply를 추가합니다. Linux workspace는 Bubblewrap을, Windows workspace는 고정된 네이티브 supervisor와 WSL2 내부 Bubblewrap을 필요로 합니다.

## 명시적 실행 설정

`start`, `followup`, `resume`, `restart`는 `--effort <level>`을 받습니다. 기존 세션은 생략을 포함해 원래 설정을 고정합니다. 다른 명시적 값을 지정하면 거부됩니다. 구조화된 assignment는 선택적 `effort` 키를 사용합니다. `workflow create --reviewer-effort high`는 독립 reviewer 설정을 고정합니다. `doctor`는 `effort_supported`를 보고하지만, 이는 CLI 플래그 사용 가능 여부만 확인할 뿐 모든 effort 값에 대한 모델 지원 여부를 확인하지는 않습니다. 명시적 effort는 schema 9가 필요합니다.

[실행 설정과 호환성](execution-settings.ko.md)을 참고하세요.

Schema 11의 run 객체에는 `telemetry`가 포함됩니다. Claude가 반환한 원본
`usage`, 모델별 `model_usage`, 보고된 경우의 `provider_cost_usd`,
`duration_api_ms`, 컨트롤러가 관측한 `duration_ms`를 저장합니다. 이관 전의
과거 run은 `null`이며, 컨트롤러는 누락된 가격을 추정하지 않습니다.

## Composition

`composition create/run/status/stop`은 기존의 제한된 계획 workflow를 통제된 editor와 동결된 reviewer workspace에 연결합니다. schema 10이 필요하며, 계획과 최종 결과 승인을 명시적으로 유지합니다. 파일, 순서, 복구 동작에 대해서는 [composition 가이드](compositions.ko.md)를 참고하세요.
