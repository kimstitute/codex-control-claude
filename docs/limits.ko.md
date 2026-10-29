# 실행 한도 설정 (0.29.0)

Claude Control에는 **기존 텍스트·workspace 컨트롤러**와 **Linux 자율 하네스**라는
별도 실행 경로가 있습니다. 두 경로의 동시 실행 한도는 독립적입니다. 아래 명령은
모델을 호출하지 않으며, 기존 상태 저장소를 지우거나 다시 초기화하지 않습니다.
예시는 설치 후 정의한 `claude_control` shell 함수를 사용합니다. 사용자 지정
`--state-dir`을 쓴다면 모든 명령에서 하위 명령 앞에 같은 경로를 전달하세요.

## 기존 컨트롤러의 호스트 한도

```bash
claude_control limits show
claude_control limits configure --max-parallel 4 --max-queued 250
claude_control limits configure --max-unbound-messages 300 --max-selected-messages 96
```

`limits configure`에는 바꿀 항목을 하나 이상 지정합니다. 지정하지 않은 값은
보존하고, `limits show`는 현재 값과 각 값의 허용 범위를 JSON으로 반환합니다.

| 값 | 기본값 | 허용 범위 | 적용 대상 |
|---|---:|---:|---|
| `max_parallel` | 2 | 1~32 | 실행 중·예약 중·결과 불명 run과 workspace 검사 명령이 공유하는 슬롯 |
| `max_queued` | 100 | 1~10,000 | `task enqueue` 대기 항목 |
| `max_unbound_messages` | 100 | 1~10,000 | task별 아직 개정에 묶이지 않은 메시지 |
| `max_selected_messages` | 64 | 1~512 | 한 task 개정에 선택할 수 있는 메시지 |

기존 저장소에서 `max_parallel`이 1이라면 설치 버전과 무관하게 한 번에 하나의
실행 슬롯만 사용합니다. `limits configure --max-parallel 2`로 **그 저장소의**
설정을 바꿀 수 있습니다. 새 저장소를 만들어 기록이나 불명 실행을 우회할
필요가 없습니다. `unknown` run도 슬롯을 차지합니다. 한도를 낮춰 현재 실행
수보다 작게 만들면 기존 실행을 강제로 중단하지 않고 새 예약만 기다립니다.
대기열·메시지 한도를 낮춰 기존 항목 수보다 작아져도 항목을 삭제하지 않으며,
새 항목만 제한합니다.

0.28 이하에서 만든 schema 15 저장소에는 새 메시지 한도 키가 없을 수 있습니다.
그 경우 위 기본값을 적용하고 첫 `limits configure`에서 값을 기록합니다.
한도 설정은 인증·세션·task 기록이나 모델 선택을 바꾸지 않습니다.

## Linux 하네스의 작업·턴 한도

```bash
claude_control harness limits show
claude_control harness limits configure --max-parallel 3 --max-turns 80

claude_control harness start \
  --repo /absolute/repo --spec-file /absolute/task.md \
  --model sonnet --request-id task-001 --max-turns 120

claude_control harness followup \
  --task TASK_UUID --spec-file /absolute/followup.md --max-turns 40
```

| 값 | 기본값 | 허용 범위 | 적용 대상 |
|---|---:|---:|---|
| `max_parallel` | 2 | 1~32 | 서로 다른 하네스 task에서 동시에 실행하는 Claude 턴 수 |
| `max_turns` | 30 | 1~1,000 | 새 Claude 호출에 전달하는 `--max-turns` 기본값 |

`harness init`에서도 `--max-parallel`·`--max-turns`를 지정할 수 있지만,
**이미 초기화된 하네스는 `harness limits configure`로 변경**합니다.
`start`·`followup`의 `--max-turns`는 해당 호출에만 적용됩니다. 생략하면 그때의
설정값을 사용하고, 실행 중인 턴은 설정 변경으로 중단하거나 연장하지
않습니다. 실제 값은 `harness show`의 `last_turn.max_turns`에 남습니다.

하네스는 task마다 잠금을 유지하고, 전역 수용 잠금으로 다른 task들의
`running`·`unknown` 상태를 계산합니다. 불명 실행은 재조정과 명시적 후속
지시 전까지 슬롯을 차지합니다. 같은 task의 `unknown` 턴을 검토하고
`--acknowledge-unknown`으로 잇는 경우에는 자신의 이전 슬롯을 중복 계산하지
않습니다. 새 하네스 한도를 낮춰도 기존 프로세스는 종료하지 않습니다.

`start`의 request ID는 저장소, 기준 SHA, 모델, 명세, 제한 시간과 **실제로
적용된 `max_turns`**에 묶입니다. 기본 턴 수를 바꾼 후 동일 ID로 이전 요청을
다시 보내면 입력 충돌이 날 수 있습니다. 이전 작업은 `harness show`로 확인하고,
다른 작업에만 새 request ID를 사용하세요.

## 이미 사용자가 지정할 수 있는 한도

| 위치 | 조정 방법 |
|---|---|
| 텍스트 실행 제한 시간 | `start`·`followup --timeout` 또는 assignment의 `timeout` |
| 하네스 실행·독립 검사 제한 시간 | `harness start/followup --timeout`, `harness check --timeout` |
| workflow 개정·호출·시간 예산 | `workflow create --max-revisions --max-calls --dispatch-window-seconds` |
| composition 계획·편집 예산 | `composition create --max-revisions --max-calls --dispatch-window-seconds` |
| workspace 행동·모델 호출 예산 | policy JSON의 `max_actions`·`max_calls` |
| workspace 검사 제한 시간 | policy JSON의 각 검사 `timeout` |
| 명시적 dispatcher 실행 시간 | `dispatch`·`workflow run`·`composition run`의 `--max-seconds` |

이 값들은 해당 작업의 생성·호출 시점에 지정합니다. 이미 동결된 assignment나
workspace 정책을 전역 한도 변경으로 소급 수정하지 않습니다.

## 사용자 설정으로 풀지 않은 안전 경계

프롬프트·assignment의 1MiB 입력 상한, 하네스 명세의 64KiB 상한,
이벤트 스트림의 16MiB 상한, 파일·JSON·로그 파서의 읽기 상한, 실행 제한
시간의 최대값, 제한된 도구 목록, 필수 샌드박스와 Team 조직 고정은
입력·메모리·인증·격리 경계입니다. 이들은 작업량 기본값과 달리 임의로
높이는 설정으로 제공하지 않습니다. 범위를 넘어야 한다면 해당 검증 경계를
코드·테스트와 함께 별도로 설계해야 합니다.
