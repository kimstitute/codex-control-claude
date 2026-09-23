# OMX 기능 도입 계획

작성: 2026-09-20 · 갱신: 2026-09-23 · 기준: v0.17.0 · 상태: P1–P5 및 역할별 버전 모델·effort 설정 구현

이 문서는 도입 당시의 설계와 단계별 통과 조건을 보존한다. 단계별 구현 상태는 문서 끝의 진행 기록을 따른다. P5는 기존 Claude 도구 비활성화를 유지하는 컨트롤러 작업 요청 방식으로 구체화했다.

## 1. 목표와 범위

각 서버의 Codex가 **같은 서버의 여러 Claude를 팀처럼 운영**하게 한다.
Codex는 목표·작업 분할·최종 승인을 맡고, 컨트롤러는 지시·실행·결과·검토
기록과 중복 방지·복구를 맡는다. Sonnet은 단순 구현과 자료 정리,
Fable은 연구 계획·설계·중요 검증·비평을 담당한다. 명시적 모델 선택과 실제
응답 모델 확인을 유지하며, 사용자가 지정한 지원 모델이 우선한다.

우선 목표는 **작업을 여러 개 등록하고, 선행 결과를 확인해 다음 작업을
실행하며, 필요한 세션에 수정 지시를 보내고, 검토 결과를 추적하는 것**이다.
OMX 전체 런타임을 복제하거나 의존성으로 추가하지 않는다.

- 기본 범위: Linux, 동일 호스트·사용자, 기존 Claude 인증, 제공된 텍스트.
- Claude의 파일·셸·MCP 도구는 현재 프로필에서 계속 비활성화한다.
- 데이터베이스와 실행 엔진은 하나를 유지한다. 새 MCP·UI는 필요할 때 같은
  엔진을 감싸는 어댑터로 추가한다.
- 다른 서버로의 작업 전달·인증 공유·중앙 제어는 범위 밖이다. 기존의
  서버 간 전달 허용 여부 미정이라는 조건을 이 계획으로 변경하지 않는다.
- 대기열을 저장하는 것과 계속 처리하는 것은 다르다. 첫 조정자는 Codex가
  명시적으로 실행하는 유한한 로컬 루프이며, 자동 시작 서비스나 Codex를
  깨우는 기능은 포함하지 않는다.

## 2. 기준선: 이미 있는 기능

| 기능 | 현재 근거 | 이후 유지할 성질 |
|---|---|---|
| 역할 6종·기본 모델·입력 지침 보존 | [assignments.py](plugins/claude-control/scripts/claude_control/assignments.py), `ROLE_PRESETS`, `render_assignment` | 지침이 바뀌어도 과거 입력은 그대로 해석 |
| 세션과 개별 실행 기록 | [store.py](plugins/claude-control/scripts/claude_control/store.py), `SCHEMA` | 정확한 세션·backend·run 식별 |
| 요청 중복·세션 직렬화·동시 실행 제한 | 같은 파일 `Store.reserve` | 같은 요청을 두 번 실행하지 않음 |
| 선택적 취소·불명 실행 확인 | 같은 파일 `stop`, `refresh`, `reconcile` | `unknown`을 자동 재시도하지 않음 |
| 여러 실행 관찰·보고 형식 검증 | [orchestration.py](plugins/claude-control/scripts/claude_control/orchestration.py), `observe`, `report` | 실행 성공과 내용 승인을 분리 |
| 플러그인·스킬·설치 | [install.py](install.py), [SKILL.md](plugins/claude-control/skills/claude-control/SKILL.md) | 상태를 설치 파일과 분리 |

기준선 검증은 자동 시험 58개, Python 3.10/3.14, 실제 Sonnet·Fable 병렬
위임과 보고서 검사다. 이 검증을 아직 구현하지 않은 대기열·복구·도구 실행의
증거로 확대하지 않는다. OMX의 비교 근거는 [도입 분석](docs/omx-adoption.md)에 있다.

계획의 호환성 판단은 기준 커밋의 다음 위치에 고정한다.

- [스키마와 단일 active turn 제약: store.py:99](https://github.com/kimstitute/codex-control-claude/blob/b23e8e258c87d5dffad25031ee6412fadfc2ca68/plugins/claude-control/scripts/claude_control/store.py#L99)
- [원자적 admission: store.py:282](https://github.com/kimstitute/codex-control-claude/blob/b23e8e258c87d5dffad25031ee6412fadfc2ca68/plugins/claude-control/scripts/claude_control/store.py#L282)
- [pending 만료와 불명 상태: store.py:260](https://github.com/kimstitute/codex-control-claude/blob/b23e8e258c87d5dffad25031ee6412fadfc2ca68/plugins/claude-control/scripts/claude_control/store.py#L260)
- [보수적 복구: store.py:446](https://github.com/kimstitute/codex-control-claude/blob/b23e8e258c87d5dffad25031ee6412fadfc2ca68/plugins/claude-control/scripts/claude_control/store.py#L446)
- [v1 입력 사본: assignments.py:306](https://github.com/kimstitute/codex-control-claude/blob/b23e8e258c87d5dffad25031ee6412fadfc2ca68/plugins/claude-control/scripts/claude_control/assignments.py#L306)
- [최초 run에 한정된 보고: orchestration.py:13](https://github.com/kimstitute/codex-control-claude/blob/b23e8e258c87d5dffad25031ee6412fadfc2ca68/plugins/claude-control/scripts/claude_control/orchestration.py#L13)
- [기존 followup/restart 호환 시험: test_assignments.py:262](https://github.com/kimstitute/codex-control-claude/blob/b23e8e258c87d5dffad25031ee6412fadfc2ca68/tests/test_assignments.py#L262)

## 3. 도입할 기능과 우선순위

| 구분 | 기능 | 필요한 이유 | 완료 후 사용 경험 |
|---|---|---|---|
| 필수 P1 | 논리 작업·개정·실행 연결 | 재지시와 검증 결과를 하나의 작업으로 추적 | 같은 작업의 처음 제안부터 수정 결과까지 조회 |
| 필수 P1 | 구조화된 후속 턴 | 현재 structured report는 최초 위임에만 적용 | 같은 세션에서 수정해도 작업·보고 계약 유지 |
| 필수 P1 | 검토·승인 원장 | 현재 내용 승인은 항상 `unreviewed` | 누가 어떤 결과를 어떤 근거로 승인했는지 기록 |
| 필수 P2 | 대기열·의존 관계 | 동시 실행 한도 초과 시 현재는 거절 | 여러 작업을 등록하고 준비된 작업부터 실행 |
| 필수 P2 | 조정자 복구·작업 이벤트 | 중간 종료 후 중복 실행 방지 | 재접속 후 진행 위치와 필요한 조치 확인 |
| 필수 P3 | 다음 턴 지시함·인수인계 | 실행 중 들어온 지시를 잃지 않음 | 지시를 보관하고 허용된 다음 턴에 포함 |
| 필수 P4 | 제한된 검증→수정 흐름 | 반복 호출과 결과 전달을 매번 구성하는 부담 감소 | 정해진 횟수 안에서 수정한 뒤 Codex 승인 대기 |
| 조건부 P5 | 격리된 파일·명령 실행 | Claude가 직접 코드를 읽고 수정해야 할 때 필요 | 별도 작업 사본의 변경·시험 결과를 회수 |
| 후순위 | 로컬 MCP·상태 UI·OS 확장 | 운용 편의와 배포 범위 확대 | 같은 코어의 다른 접근 방식 |

처음부터 도입하지 않을 항목은 tmux 키 입력 조작, 에이전트의 재귀적 생성,
모델이 작업 그래프를 임의 변경하는 기능, 무제한 재시도, 범용 메시지 브로커,
자동 Git 병합·push, 서버 간 전달이다.

## 4. 작은 코어를 유지하는 설계

### 식별과 상태

- **session:** Claude의 지속 대화. 모델·역할·프로젝트는 고정이다.
- **task:** 사용자가 해결하려는 논리 작업. UUID와 표시용 이름을 갖는다.
- **revision:** 목표·자료·완료 조건의 불변 사본. `(task_id, revision)`으로 식별한다.
  생성 시 역할 지침·모델·실행 프로필도 고정하여 대기 중 패키지 갱신이 지시를 바꾸지 못하게 한다.
- **run:** 실제 Claude 한 턴. 기존 run을 실행 시도 ID로 재사용하고 별도
  `attempt_id`를 중복 도입하지 않는다.
- **decision:** 정확한 개정·run·결과 digest에 대한 검토 기록. 에이전트의
  권고와 Codex의 최종 승인을 구분한다.

작업 상태는 `queued → running → awaiting_review → accepted`를 기본으로
한다. 의존 미충족·문맥 불일치·불명 실행은 `blocked`와 구체적인 사유로
표시한다. 수정 요청은 원래 결과를 덮어쓰지 않고 새 revision을 만든다.
실패·취소도 이력으로 남기며, run의 기존 실행 상태를 승인 상태로 바꾸지 않는다.

항상 지켜야 할 조건:

1. 세션당 active run은 최대 하나다. `unknown`도 active로 취급한다.
2. 작업의 현재 revision에 대해 **non-terminal** 실행 예약은 최대 하나다.
   task의 active run 포인터를 같은 트랜잭션에서 조건부 갱신하고, 같은 revision의
   이전 실행이 unknown이면 새 예약을 거절한다.
3. `ready(task)`는 모든 선행 관계의 조건, 취소·차단 조건, 지정 세션 문맥,
   실행 예산 검사를 통과할 때만 참이다. 의존 종류는 기본 `accepted`와 제한된
   `reported` 두 가지로 고정한다.
4. `accepted`는 지정 revision·run·digest의 Codex 승인 기록을 요구한다.
   `reported`는 실행 completed, 결과 무결성, 실제 모델 검증, 유효한 보고 형식과
   agent status complete를 모두 요구하지만 최종 승인은 요구하지 않는다.
   `reported`는 Codex가 고정한 P4 workflow의 **검토·수정 전달 경로**에서만 만들 수
   있다. 일반 enqueue나 모델 출력이 이 조건을 선택하지 못한다.
5. 이전 revision의 늦은 응답이나 검토는 현재 revision의 상태를 바꾸지 못한다.
6. 실행 여부를 모르는 요청은 새 run으로 재전송하지 않는다.

### 상태 저장과 실행

기존 SQLite를 확장한다. 단계별로 `tasks`, `task_revisions`, `task_runs`,
`review_decisions`, `task_dependencies`, `task_events`, `messages`를 추가한다.
기존 sessions/runs 및 프로세스 제어를 복제하지 않는다.
P4에서는 workflow의 고정 정책과 예산·취소 상태를 같은 DB에 저장한다.

새 structured contract는 별도 버전으로 정의한다. run과 contract의 연결,
정확한 prompt digest를 명시적으로 저장하여 최초 run의 `rowid`로 계약 유무를
추정하는 방식을 새 계약에 사용하지 않는다. v1의 입력·fingerprint·보고 해석은
그대로 유지한다. 메시지·의존 결과를 합친 **최종 입력**도 저장하고 1 MiB를 넘으면
예약 전에 거절한다. 임의 요약이나 잘라내기로 제한을 우회하지 않는다.

작업 개정 확인, 의존 결과 확인, 세션·용량 확인, run 예약, task-run 연결,
입력에 포함할 메시지 확정은 **같은 SQLite 쓰기 트랜잭션**에서 처리한다.
커밋 후 worker를 실행한다. Claude 호출은 DB 트랜잭션 안에서 하지 않는다.
작업 완료와 검토 결정도 해당 revision 및 digest에 대한 조건부 갱신으로 처리한다.
현재 `Store.reserve`의 admission 부분을 같은 DB 연결을 받는 내부 함수로
분리하여 재사용한다. 별도 트랜잭션으로 run을 만든 뒤 task를 연결하지 않는다.

대기 작업은 `tasks`에 둔다. 기존 `runs.pending`은 곧 worker가 claim해야 하는
실행 예약이므로 장기 대기열로 재사용하지 않는다. 예약 뒤 시작되지 못한 실행은
기존 claim 기한과 프로세스 증거로 확인한다. 자동 재시도는 추가하지 않는다.

### 문맥·권한·완료 조건

기존 세션을 재사용하는 작업에는 `expected_parent_run_id`와 backend 식별을
고정한다. 그 사이 다른 followup/restart가 들어오면 `context_changed`로 막고,
대화를 조용히 교체하거나 최신 세션을 임의 선택하지 않는다.
이 검사는 컨트롤러에 기록된 턴에만 적용된다. 사용자가 컨트롤러 밖에서 같은
Claude backend를 직접 재개한 변경은 탐지한다고 보장하지 않는다.

Claude의 verifier/critic 출력은 **검토 권고**다. 최종 승인 기록은 Codex가
명시적 명령으로 남긴다. DB에 적은 reviewer 이름만으로 별도 보안 주체가
생기지는 않는다. 같은 OS 사용자에게서 악의적 DB 변조를 방어하는 시스템으로
설명하지 않는다.

## 5. 단계별 구현과 통과 조건

### P1 — 작업 이력과 구조화된 후속 턴 / v0.3 목표

**변경 대상:** `store.py`, `assignments.py`, `orchestration.py`, `cli.py`,
새 `tasks.py`, 새 migration 코드. 기존 스킬과 CLI 문서도 같은 단계에서 갱신한다.

- 작업·개정·run 연결과 append-only 검토 원장을 만든다.
- 계획 명령: `task create/list/show`, `task submit`, `task revise`,
  `task review`, `task accept`, 제한된 `task retry`. 기존 명령의 의미는 바꾸지 않는다.
- 새 작업은 새 세션 또는 명시적으로 지정한 호환 세션을 사용한다.
  수정은 동일 task의 새 revision과 새 run이며, 기존 대화의 명시적 재개가 기본이다.
- 최종 승인에는 정확한 revision, run ID, 결과 digest, 기준별 근거가 필요하다.
  과거 개정 승인·오염된 결과·blocked 결과는 승인을 거절한다.
- 스키마 3에서의 명시적 migration을 구현한다. 조용한 새 저장소 생성이나
  기존 이력 폐기로 우회하지 않는다.
- claim 기한 만료는 task를 `blocked/reserve_expired`로 옮긴다. `task retry`는
  이전 시도가 모두 terminal이고 Claude가 시작되지 않았다는 증거가 있는 경우에만,
  새 operation ID로 같은 revision의 새 run을 명시적으로 예약한다. 실행이 시작된
  뒤의 실패·취소는 문맥 확인과 필요한 acknowledge를 거쳐 `task revise`로 다룬다.
- 만료는 조회 시 계산한 표시만으로 처리하지 않는다. 재시도 트랜잭션 안에서
  기존 `pending` run을 조건부로 `launch_failed(reason=claim_deadline)`로 확정한 뒤
  새 예약을 만든다. 이미 terminal이면 그 상태와 미실행 증거를 다시 확인한다.
  만료된 run을 뒤늦게 claim하려는 worker가 거절되는 회귀 시험을 둔다.
- 실제 모델의 검증 실패는 기존 run의 실패 판정을 유지하고 task를
  `blocked/model_mismatch`로 표시한다. 승인·의존 해제·자동 수정 분기를 막는다.
  요청 별칭과 실제 모델 문자열의 단순 동일성 대신 기존 모델 계열 검증을 재사용한다.

**통과 조건:** 기존 58개 시험 유지; 같은 task의 두 턴에서 정확한 계약·모델·문맥
확인; 과거 v1 report 유지; 중복 수정 요청은 같은 run 반환; 변경된 요청은 충돌;
동시 submit은 하나만 예약; 승인 이후 새 revision은 다시 미승인 상태;
과거 결과를 재검토해도 현재 revision을 승인하지 못함.
동일 task/revision의 입력 digest 변경은 거절하고 `task revise`를 안내;
reserve 만료 뒤의 명시적 재시도 가능·unknown 재시도 불가; 모델 불일치 승인 거부도 시험한다.

### P2 — 대기열·의존 관계·유한 조정 루프 / v0.4 목표

**변경 대상:** `tasks.py`, `store.py`, `runner.py`, `cli.py`, 새 `scheduler.py`.

- 등록된 작업의 순서를 유지하는 FIFO 대기열과 명시적 의존 관계를 추가한다.
  순환 의존·자기 의존·존재하지 않는 참조는 등록 전에 거절한다.
- 계획 명령: `task enqueue`, `dispatch --once`,
  `dispatch --until-idle --max-seconds <N>`, `task events`.
- 준비된 작업은 건너뛰지 않고 FIFO로 배정하되, blocked 작업 때문에 독립된
  ready 작업까지 멈추지 않는다. 초기에 우선순위·선점·동적 그래프 수정은 넣지 않는다.
- 초기 대기 작업 상한은 저장소당 100개로 두고, 초과 등록은 `queue_full`로
  거절한다. 조용히 삭제·교체하지 않는다. 실행 슬롯과 대기열 상한은 별도 설정이다.
- 두 조정자가 동시에 실행되어도 DB의 단일 예약 규칙이 중복 dispatch를 막는다.
  조정자 사망이나 heartbeat 지연만으로 Claude를 새로 띄우지 않는다.
- 부모 실패·취소·unknown이면 종속 작업을 이유와 함께 차단한다. 이미 고정된
  부모 revision을 새 결과로 자동 바꿔치기하지 않는다.
- 의존 결과와 메시지를 확정한 **동일 트랜잭션 안에서** 최종 입력 크기를 검사한다.
  초과하면 run 생성·메시지 소비·호출 예산 소비 없이 거절한다.
- 조정 루프가 없는 동안은 queued 상태를 유지한다. 완료 확인에 모델 API를
  호출하지 않으며, 로컬 상태 확인의 대기 간격을 제한한다. 상주 서비스는 추가하지 않는다.
- P2의 `max-seconds`는 신규 배정 루프의 종료 기한이다. 이미 시작한 run은
  기존 worker가 계속 관리한다. 작업 취소가 필요하면 별도 stop을 사용한다.

**통과 조건:** 결정적 fake clock에서 다음 tick에 완료되는 작업 20개·동시 한도 2를
21 dispatch tick 안에 모두 실행 완료; 실제 프로세스 시험에서도 한도 유지;
부모 승인 전 자식 실행 0회; blocked와 무관한 작업 진행; 조정자 2개의 경쟁에서
task revision당 예약 1개; 커밋 직후·worker 시작 직후 조정자 종료에도 중복 호출 0회;
unknown의 슬롯 유지; 조정 루프 재시작 후 기존 run 추적; 종료 기한 뒤 새 dispatch 0회.
worker를 띄운 직후 조정자를 종료해도 worker가 독립적으로 완료하는 시험을 포함한다.
짧게 살아 있는 호스트 sandbox가 worker까지 종료하는 실행 환경에서는 이 보장을
선언하지 않고 지원되는 지속 실행 환경을 요구한다.

### P3 — 다음 턴 지시함과 인수인계 / v0.5 목표

**변경 대상:** `tasks.py`, `scheduler.py`, `assignments.py`, `cli.py`, 새 `messages.py`.

- 계획 명령: `message enqueue/list/cancel`. 대상은 관리 중인 정확한 세션·작업이다.
  메시지에는 operation ID, 순서, 대상 revision, 내용 digest를 기록한다.
- active Claude의 stdin이나 tmux 화면에 끼워 넣지 않는다. 다음 턴에 포함할
  메시지를 run 예약과 함께 확정하고, 어떤 지시가 어느 run에 포함됐는지 보여준다.
- 상태는 `queued`, `bound_to_run`, `run_completed`, `needs_attention`, `cancelled`로
  구분한다. `bound_to_run`을 Claude가 이해했거나 작업을 완료했다는 뜻으로 쓰지 않는다.
- 메시지를 저장하는 것만으로 새 턴을 자동 생성하지 않는다. Codex의
  `task revise` 또는 사전에 허용한 workflow 전이가 메시지 ID를 선택한다.
  메시지는 예상 기준 revision을 명시하고, 선택된 메시지를 포함하는 다음
  revision을 만든다. 기준이 이미 바뀌었으면 자동 이동하지 않고 차단한다.
- 처음에는 Codex→Claude 지시와 Codex가 선정한 결과 인수인계만 지원한다.
  Claude가 다른 Claude를 임의 호출하거나 작업을 재귀 생성하지 않는다.
- P4에서는 허용된 workflow 전이가 검토 결과의 수정 지시를 메시지로 만들 수 있다.
  이때 출처 run ID와 결과 digest를 기록한다. 다른 Claude-origin 메시지 경로는 추가하지 않는다.
- 실패·취소·unknown 턴에 묶인 메시지는 자동 재전송하지 않는다. 확인 후 명시적
  새 revision에 다시 포함할 때도 원래 전달 이력을 남긴다.

**통과 조건:** 실행 중 지시 두 개를 넣어도 병렬 followup 0회; 다음 턴의 입력과
메시지 순서 일치; 같은 operation ID 재전송으로 중복 지시 0개; enqueue/bind/실행
각 경계에서 종료·재시작해도 지시 누락이나 숨은 재전송 0회; 다른 revision 대상은 차단.

### P4 — 검증·수정 반복과 재접속 요약 / v0.6 목표

**변경 대상:** `scheduler.py`, `tasks.py`, 보고 계약, `cli.py`, 스킬,
새 `workflow.py`와 `tests/test_workflows.py`.

- 한 가지 흐름부터 지원한다: **Sonnet 제안 → 독립 Fable 검토 → 필요 시 Sonnet 수정
  → Fable 재검토 → Codex 최종 승인 대기**. 계획·설계 작업자는 Fable로 지정할 수 있다.
- 작업자와 검토자는 독립 세션을 사용한다. 검토자는 지정 revision의 정확한
  산출물과 완료 기준을 입력받고, 기준별 근거·미확인 사항·수정 권고를 반환한다.
- 검토용 새 보고 계약에는 고정된 기준 ID별 판단, 대상 result digest,
  `recommendation: approve | revise | blocked`, 수정 지시를 구조화한다.
  텍스트의 특정 단어를 검색해 자동 분기하지 않는다. `approve`도 최종 승인이 아니다.
- 자동 수정 분기는 workflow 생성 때 Codex가 고정한 정책으로만 허용한다.
  모델이 역할·모델·횟수·의존 관계·권한을 변경하지 못한다.
- 기본 한도 제안: 최초 제안 뒤 수정 최대 2회, 전체 모델 호출 최대 6회,
  신규 호출 허용 시간 `dispatch_window_seconds=900`. 값은 명시적 설정으로
  변경하되 유한값만 허용한다. 실행별 timeout은 기존 계약대로 별도 적용한다.
  예산은 dispatch 예약 시 영속 기록하고 unknown도 소비한 예약으로 유지한다.
- 시간 창은 workflow 생성 시점이 아니라 **최초 dispatch 예약이 성공한 시점**에
  열리고, 그 timestamp를 같은 트랜잭션에서 기록한다. run을 늦게 시작하거나
  조정자를 재시작해도 이 기준 시각을 재설정하지 않는다.
- 시간 창이 닫히면 신규 배정을 차단한다. 이미 시작한 run은 자기 timeout 안에서
  완료·결과 저장하게 두고 이후 Codex 판단 대기로 전환한다. 따라서 900초는
  전체 종료 시각의 보장이 아니다. 명시적 `workflow stop`은 기존 취소 경로를 사용한다.
- 검토 통과 권고가 나와도 자동으로 최종 승인하지 않는다. malformed/blocked/unknown,
  예산 초과, 문맥 불일치는 Codex 판단을 기다리는 상태로 종료한다.
- 계획 명령: `workflow create/run/status/stop`, `overview --attention`.
  새 Codex 작업은 요약을 조회해 queued·진행·차단·승인 대기를 구분한다.
  채팅을 자동 생성하거나 Codex를 자동으로 깨우지는 않는다.
- workflow는 별도 실행 엔진이 아니라 고정 템플릿이다. create가 task·의존 관계·
  메시지 연결·영속 정책/예산을 만들고, run은 P2의 dispatch를 호출한다.
  stop은 queued 멤버를 취소하고 owned run에 기존 stop을 적용한다.

**통과 조건:** 성공 경로는 제안 1회·검토 1회 후 승인 대기; 수정 경로의 참조 digest
일치; 무한 반려에도 설정 호출 수와 횟수 초과 0회; 루프 재시작이 예산을 초기화하지
않음; 검토자 문맥 분리; `workflow stop` 이후 신규 dispatch 0회와 기존 owned run의
종료 확인; `reported`도 unknown·잘못된 형식·모델 불일치·blocked 보고에서는
해제되지 않음; overview에 모든 blocked·awaiting_review·unknown 관련 작업 표시;
승인 결정이 없는 작업을 accepted로 표시한 건수 0회.

### P5 — 직접 파일 작업이 필요해질 때 별도 프로필

이 단계 전까지 코드 적용·테스트 실행은 Codex가 담당한다. 기존의 고정된
텍스트 프로필을 옵션 하나로 해제하지 않는다.

- 역할별 읽기·편집·명령 범위와 명시적 실행 정책부터 설계한다.
- 편집 작업자마다 독립 worktree/사본을 배정하고 기준 commit과 변경 목록을 남긴다.
  worktree는 충돌 격리이며 OS 보안 sandbox가 아니라는 점을 구분한다.
- Claude 권한 설정과 별개로 필요한 파일·프로세스 격리 경계를 시험한다.
- 검증자는 고정된 변경 사본을 검토하고, 통합은 Codex가 수행한다.
  자동 병합·push와 GPU·장시간 작업은 이 단계의 기본 동작에 넣지 않는다.
- 허용 범위 이탈, 동시 편집 충돌, 취소 후 자식 프로세스와 잔여 변경, 인증·로그
  노출을 검증한 뒤에만 해당 프로필을 지원한다고 선언한다.

## 6. migration·복구·호환성

첫 schema 확장은 모든 CLI·worker·조정자가 중지된 유지보수 상태에서 수행한다.
active/unknown run이 있으면 거절한다. 구버전과 신버전 CLI의 동시 운용은 지원하지 않는다.
P2–P4에서 추가 스키마 변경이 필요할 때도 같은 중지·백업·진행 표식·복구 절차를 적용한다.

순서는 기존 버전의 refresh/reconcile과 실행 종료 확인 → 모든 기존 CLI/worker
중지 → 새 코드 설치 → 명시적 migrate → 검사 후 운용 재개다. unknown의 종료가
확인되지 않으면 migration을 중단하고 기존 진단 경로를 유지한다. operator의
문장만으로 unknown을 terminal로 바꾸는 명령은 추가하지 않는다.

SQLite backup API로 검증된 사본을 만들고, 저장소 `config.json`의 schema를
`migrating`으로 원자적으로 표시하여 구버전과 신버전의 일반 명령을 차단한다.
진행 journal은 같은 private state root에 보존한다. 새 CLI는 DB user_version과
설정 schema가 다르면 일반 쓰기를 거절하고 migrate 상태/복구만 허용한다.
DB 변경과 버전 전환을 수행하며, DB commit과 설정 파일 교체 사이에
죽어도 진행 표식과 DB 버전으로 완료/복구를 판별하도록 각 경계를 시험한다.
실행 DB 파일만 복사해 WAL을 누락하거나, 실패했다고 상태를 삭제하지 않는다.
새 스키마에서 실행을 재개한 뒤에는 자동 downgrade하지 않는다.

| 위험 | 대응과 시험 |
|---|---|
| task를 runs.pending에 저장해 대기 중 만료 | 별도 task queue; 장기 queued 작업에 run 생성 0개 확인 |
| 예약과 task/message 연결 사이 종료 | 같은 트랜잭션; 강제 종료 지점별 DB 일관성 시험 |
| worker 생존 불명 상태를 재시도 | 기존 프로세스 식별·reconcile 유지; 중복 호출 0회 확인 |
| 오래된 결과를 승인하거나 다음 작업에 전달 | revision·run·digest 고정과 조건부 갱신 |
| legacy followup/restart가 문맥을 변경 | expected parent/backend 불일치에서 dispatch 거절 |
| schema 변경이 v1 보고서를 훼손 | 원본 legacy fixture의 fingerprint·보고 결과 비교 |
| 디스크 부족으로 DB/입력/결과 기록 실패 | ENOSPC 주입; 성공·승인 오판 금지, 상태 보존 후 명시적 복구 |
| 자동 루프의 호출 증가 | 영속 호출·수정·시간 예산; 재시작으로 한도 우회 불가 |

## 7. 검증과 구현 운영

각 단계는 해당 범위가 통과해야 다음 단계로 진행한다. 한 번에 P1–P5 전체를
구현하지 않는다. 먼저 P1의 migration·task 계약·후속 턴 호환성을 확정한다.

P1은 세 개의 검토 가능한 변경으로 나눈다: (A) migration·새 계약의 명시적
run 연결과 legacy fixture, (B) task/revision 생성·동시 예약·구조화된 후속 턴,
(C) 검토 원장·승인 조건·스킬과 설치본 확인. A의 데이터 계약을 확정한 뒤
B/C를 진행하며, DB 소유 코드는 동시에 서로 다른 작성자가 변경하지 않는다.

1. 순수 계약 시험: 입력 버전, revision, dependency, message, decision validation.
2. fake 프로세스 통합 시험: 동시 예약, 종료 경계, 취소, 복구, 예산, 디스크 실패.
3. 전체 회귀: 기존 58개와 신규 시험, 지원 Python, Ruff, 배포·스킬 검증.
4. 제한된 실제 모델 시험: 필요한 기능에만 명시적 Sonnet/Fable 호출을 사용하고
   요청/실제 모델, 실행 겹침, 문맥·작업 ID, 도구 사용을 확인한다.
5. 설치본 확인: 별도 경로 재배치, 기존 기록 조회, 업데이트·복구. 원문·개인 경로·
   인증·호스트·runtime DB는 공개 저장소에서 제외한다.

Codex가 통합을 맡고 Fable이 P1의 schema/상태 설계와 단계별 중요한 검토를 맡는다.
Sonnet에는 확정된 계약을 따르는 작은 구현 단위를 맡긴다. 변경 파일 소유권을
나누고, 작성자와 최종 검토자를 분리한다. 새 의존성·상시 서비스·외부 전송은
각각 필요성을 증명한 별도 설계로 다룬다.

## 8. 최소 완료 시나리오

Codex가 같은 호스트에서 작업 A와 B를 등록하고, A의 승인된 결과에 의존하는
검토 C를 등록한다. A/B는 한도 안에서 병렬 실행된다. A가 실행 중일 때 받은
수정 지시는 보존되며 현재 턴에 끼어들지 않는다. Codex가 결과를 확인하고
새 revision을 실행한 뒤 승인하면, C는 그 승인된 결과만 입력받는다.
중간에 조정자가 종료돼도 다시 조회해 기존 실행을 추적하고, 불명 실행을
중복 실행하지 않는다. 검토 결과와 최종 승인은 서로 구분된다.

이 시나리오를 fake 장애 주입과 제한된 실제 Claude 호출로 재현하는 것이
P1–P4의 통합 완료 기준이다. P5 없이도 텍스트 연구·설계·코드 제안 업무에 적용할 수 있다.

## 9. 계획 검토 기록

Fable의 독립 검토에서 승인 의존과 검토 전달의 충돌, 재시도 조건, migration 차단,
시간 예산의 의미를 보완했다. 재검토 결과는 APPROVE였으며, 메시지 출처·만료의
원자적 확정·시간 창 시작점·후속 migration 절차에 관한 추가 지적도 반영했다.
이 승인은 계획 검토 결과이며, 위 기능의 구현·시험·배포 완료를 의미하지 않는다.

## 10. P1 구현 완료 기록 — v0.3.0

- `task create/list/show/submit/revise/retry/review/accept` 구현. 생성과 수정은
  입력을 보존하며, 명시적 submit에서만 Claude를 호출한다.
- task/revision/run 연결과 operation 중복 방지를 단일 예약 트랜잭션으로 처리한다.
  v2 계약은 첫 턴·후속 턴 모두 명시적 run 연결을 사용하고 v1 보고 의미는 유지한다.
- 기준별 근거와 정확한 revision/run/result digest를 요구하는 append-only 검토·승인
  원장을 구현했다. 검토 권고와 Codex의 최종 승인은 구분된다.
- schema 3→4의 검증된 SQLite 백업·오프라인 이관·중단 후 재개를 구현했다.
  설치본 갱신과 기존 저장소 이관 후 모든 기존 session/run 행이 동일함을 확인했다.
- 실행 전 중단된 backend는 해당 backend의 전체 이력에서 실제 실행이 없다는
  증거가 있을 때만 같은 ID로 처음 시작한다. unknown 재시도와 자동 대화 교체는 없다.
- 일시적인 결과 읽기 실패는 재확인 대상으로 남기며 completed 기록을 영구 실패로
  바꾸지 않는다. 파일 소실·변조는 무결성 실패로 처리한다.
- 검증: Python 3.10·3.14 각각 87개 자동 시험, Ruff 검사·포맷, 플러그인·스킬 검증 통과.
  실제 Sonnet 두 턴에서 같은 backend의 정확한 문자열 회상, v2 보고 및 승인 기록을 확인했다.
  두 번째 입력에는 회상할 문자열을 넣지 않았으며 작업 폴더 변경은 없었다.
- Sonnet의 제한된 CLI 구현을 Codex가 검토·통합했다. Fable의 구현 검토와 보완 후
  최종 판정은 APPROVE다. 실제 응답 모델은 Sonnet `claude-sonnet-5`,
  Fable `claude-fable-5-1`로 확인했으며 원문·세션 ID는 비공개 기록에만 보관한다.

사용법은 [작업 관리 가이드](docs/tasks.md)를 따른다. P2의 대기열·의존 관계,
P3의 지시함, P4의 자동 검토 반복, P5의 파일·명령 실행은 P1 단계에 포함하지 않았다.

## 11. P2 구현 기록 — v0.4.0

- `task enqueue/dequeue/events`, `dispatch --once`,
  `dispatch --until-idle --max-seconds` 구현. FIFO 대기열은 기본 100개이며
  `max-parallel` 실행 슬롯과 별도 관리한다. blocked 작업은 독립 작업의 배정을 막지 않는다.
- 정확한 부모 task/revision을 고정하고 순환·자기·존재하지 않는 참조를 거부한다.
  부모의 현재 run과 결과 digest에 대한 Codex 승인이 있어야 자식을 배정한다.
  직접 submit·retry와 dequeue 후에도 의존 조건을 우회할 수 없다.
- v2 개정은 보존하고, v3 실행 입력에 부모 보고서·승인 출처를 복사한다.
  최종 1 MiB 검사와 실행 예약·대기열 연결은 한 트랜잭션에 포함한다.
  실패하면 run 생성이 없고, 과거 자식 보고서는 이후 부모 파일 변경과 독립적으로 검증된다.
- 커밋 후 조정자 사망은 claim 만료와 명시적 retry로 처리하며 자동 재호출하지 않는다.
  시작된 worker는 조정자와 독립적으로 완료한다. unknown은 슬롯을 유지하지만
  유한 루프는 조치 필요 상태를 반환한다. 이벤트는 DB 트랜잭션에서 기록한다.
- schema 4→5 이관을 추가했다. 기존 3→4 복구 경로를 유지하며, 완료된 과거 journal을
  현재 이관으로 오인하지 않도록 구분한다. 각 중단 지점의 복구와 P1 승인 이력을 검증했다.
- Sonnet이 CLI 초안을 작성했고 Codex가 통합·문서를 수정했다. Fable 설계 검토를
  반영한 뒤 최종 코드 검토는 APPROVE였다. 첫 코드 검토 호출은 시간 제한으로
  끝났고 같은 세션을 재개해 최종 판정을 받았다. 모델 자동 대체는 없었다.
- 실제 Sonnet 두 호출에서 부모 완료만으로는 자식 실행이 없고, 명시적 승인 뒤
  별도 세션의 자식이 복사된 결과의 문자열을 정확히 반환함을 확인했다. 프로젝트 변경과
  중복 호출은 없었다. 실제 모델은 `claude-sonnet-5`, 검토는 `claude-fable-5-1`이었다.

- 최종 검증: Python 3.10·3.14 각각 121개 자동 시험 통과. Ruff 검사·포맷,
  플러그인·스킬 검증 통과. 테스트는 결정적 20개 작업, 실제 프로세스 동시 한도,
  두 조정자 경쟁, 두 사망 지점, 입력 초과, 과거 오류 표시, 이관 복구를 포함한다.
- v0.4 설치본과 캐시를 갱신하고 기본 저장소를 백업 후 schema 5로 이관했다.
  기존 세션 1개·실행 3개의 모든 행이 변경 없이 보존됐고, 이관 시 active run은 없었다.
  설치본에서 실제 의존 작업의 승인·보고서 조회와 대기열 명령을 재확인했다.

대기열 계약·복구와 사용법은 [대기열 가이드](docs/queue.md)에 정리했다.
P2 완료 시점의 다음 단계는 P3 지시함이었다. P4 자동 검토 반복·P5 도구 실행·서버 간
전달은 P2에 포함하지 않았다.

## 12. P3 구현 기록 — v0.5.0

- `message enqueue/list/cancel`, `task revise --message-id`,
  `--redeliver-messages` 구현. 실행 중 지시를 저장할 수 있으며 등록·선택은
  Claude를 호출하지 않는다. 다음 run 예약과 전달 이력을 같은 트랜잭션으로 확정한다.
- 지시의 task·기준 revision·기존 session/backend를 고정하고 등록 순서로 전달한다.
  아직 세션이 없는 작업에 지시를 넣어도 세션을 미리 할당하지 않는다.
  기준이 바뀐 미전달 지시는 자동 이동하지 않으며, 취소 후 새 기준으로 등록한다.
- 선택됐지만 아직 실행에 묶이지 않은 지시는 취소할 수 있다. 이 경우 해당 개정의
  submit은 실패하며 새 개정에서 선택을 정리한다. 이미 묶인 지시는 이력을 보존하고,
  실행 중단이 확인된 실패에 한해 명시적으로 재전달한다. unknown·완료 턴은 재전달하지 않는다.
- 아직 예약하지 않은 재전달 개정을 다른 재전달 개정으로 교체할 수 있다.
  과거 선택은 이력으로 남고 현재 개정만 제출할 수 있다. 이전 대기열 항목의
  superseded 처리·과거 submit 거절·중복 요청의 전달 이력 불변을 재현 시험으로 확인했다.
- 인수인계는 완료된 v2/v3/v4 task run의 정확한 결과 digest와 유효한 보고서를
  확인해 사본으로 저장한다. complete·blocked 보고서 모두 전달할 수 있지만
  이는 Codex 승인이나 대기열의 부모 승인 조건을 대신하지 않는다.
- 선택 지시와 의존 결과를 v4 최종 입력에 고정한다. 등록·선택·예약 시 1 MiB를
  검사하며, 초과 시 실행·전달 이력이 생기지 않는다. 작업당 미전달 지시 상한은
  100개, 개정당 선택 상한은 64개다. 이벤트 순서는 시각 대신 단조 증가 ID를 따른다.
- schema 5→6의 백업·오프라인 이관·중단 후 복구를 추가했다. 기존 3→4→5 이관과
  v1/v2/v3 보고를 유지하며, 복합 외래 키와 불변 기록으로 작업·개정·run 연결을 검증한다.
- 검증: Python 3.10·3.14 각각 152개 자동 시험, Ruff 검사·포맷, 플러그인·스킬
  검증 통과. 취소 경쟁·프로세스 종료 경계·미실행 retry·명시적 재전달·입력 초과·
  결과 소실·이관 복구를 포함한다.
- 실제 Sonnet 두 턴에서 첫 턴 중 지시 2개 등록, 완료된 결과 1개 인수인계,
  같은 backend의 다음 턴에 등록 순서대로 전달되는 것을 확인했다. 정확한 응답,
  메시지별 전달 이력 1건, 중복 호출 없음, 작업 폴더 변경 없음을 검증했다.
- Sonnet의 CLI 초안을 Codex가 검토·통합했고, Fable 설계·코드 검토의 지적을
  반영하거나 코드·시험 근거로 해소했다. 같은 검토 세션의 최종 판정은 APPROVE다.
  실제 모델은 `claude-sonnet-5`, `claude-fable-5-1`이며 자동 대체하지 않았다.
- v0.5 설치본과 캐시를 갱신하고 기본 저장소를 백업 후 schema 6으로 이관했다.
  기존 session/run 및 P2 테이블의 모든 행이 동일함을 확인했다. 설치된 코드에서도
  실제 v4 보고·승인 상태와 지시 3개의 전달 이력을 재확인했다.

사용법은 [지시함·인수인계 가이드](docs/messages.md)를 따른다. P3 다음 단계인
P4의 제한된 검토·수정 반복과 재접속 요약은 아래 기록대로 구현했다.

## 13. P4 구현 기록 — v0.6.0

- `workflow create/run/status/stop`, `overview --attention` 구현. 생성은 모델을
  호출하지 않으며, 명시적으로 실행한 유한 조정 루프가 제안·검토·수정 단계를 진행한다.
  기존 task·queue·message·worker를 재사용하며 별도 실행 엔진이나 의존성을 추가하지 않았다.
- 작업자와 Fable critic은 독립된 지속 세션을 사용한다. 매 검토는 새 revision이며,
  task v5 계약이 정확한 대상 revision·run·결과 digest, 고정 기준별 판단·근거,
  미확인 사항과 수정 지시를 검증한다. 소스 보고서는 P3 메시지로 한 번 전달한다.
- 기본 한도는 수정 2회·전체 예약 6회·최초 예약 후 신규 실행 허용 시간 900초다.
  정책과 예약 이력은 DB에 고정하며 조정자 재시작으로 초기화하지 않는다.
  다음 작업자 턴에는 후속 검토까지 최소 2회분의 호출 여유가 필요하다.
  실패·미시작·unknown 예약도 소비한 호출로 남고 자동 재시도하지 않는다.
- 완료된 검토를 시간 창 이후에 관찰해도 권고는 기록하고 신규 실행은 차단한다.
  실행 중 시간 초과를 관찰한 흐름은 자동 진행을 종료하며, 이후 완료 결과는
  Codex가 조회·검토할 수 있다. 이미 시작한 run의 개별 timeout은 유지한다.
- `approve`는 검토 권고만 기록한다. Codex가 정확한 결과와 기준별 근거로
  명시적으로 승인해야 `accepted`가 된다. 잘못된 보고·모델·문맥·결과 무결성,
  unknown·예산 소진은 자동 진행을 종료하고 Codex 판단을 기다린다.
- 워크플로 소유 작업의 외부 제출·재시도·수정·대기열 변경과 지시 등록·취소를
  차단한다. stop은 신규 예약을 먼저 막고 기존 소유 프로세스의 종료를 확인한다.
  중단된 정리는 같은 stop/run으로 이어가며 unknown은 기존 reconcile로 해결한다.
- schema 6→7 백업·오프라인 이관·중단 복구를 추가했다. 3→7 전체 이관 뒤에도
  기존 v4 보고와 메시지를 보존하고 새 워크플로 생성·실행·종료가 동작함을 시험했다.
- 검증: Python 3.10·3.14 각각 전체 200개 시험 통과. P4 신규 48개는 정책·예산,
  독립 세션·정확한 검토, 수정·실패 경로, 경쟁·프로세스 종료·재시작,
  입력 초과·이관·CLI 경계를 다룬다. Ruff 검사·포맷 및 플러그인·스킬 검증도 통과했다.
- 실제 Sonnet 1회·Fable 1회로 제안과 검토를 실행했다. 독립 세션·정확한 검토 대상·
  자동 승인 없음·Codex 명시적 승인·재실행 시 추가 호출 없음·작업 폴더 불변을 확인했다.
  실제 모델은 `claude-sonnet-5`, `claude-fable-5-1`이며 모델 대체는 없었다.
- Sonnet의 CLI 제안을 Codex가 검토·수정해 통합했다. 해당 제안은 복수 코드 블록으로
  반환돼 structured report 검증에서 올바르게 거절됐으며, 자동 승인하지 않았다.
  Fable 설계·코드 검토 지적을 반영하거나 근거로 해소했다. 최종 판정은 전체 회귀 시험
  통과를 조건으로 APPROVE였고, 두 Python 버전에서 그 조건을 충족했다.
- v0.6 설치본·캐시를 갱신하고 유휴 기본 저장소를 백업 후 schema 7로 이관했다.
  기존 세션·실행·P3 테이블의 모든 행이 동일함을 확인했다. 설치본에서도 실제 v5
  보고와 승인 상태, 전달 이력을 모델 추가 호출 없이 검증했다.

사용법은 [워크플로 가이드](docs/workflows.md)를 따른다. 다음 후보는 P5의 격리된
파일·명령 실행 프로필이다. 현재 제공 텍스트 프로필의 Claude 도구·MCP 비활성화와
동일 호스트 범위는 유지하며, P5는 별도 경계 설계와 검증이 필요한 단계다.

## 14. P5 구현 기록 — v0.7.0

- `workspace doctor/create/task/run/status/list/export/stop/reconcile`을 추가했다. Git의 정확한 커밋에서 별도 사본을 만들며 원본의 미커밋·추적되지 않은 변경은 가져오지 않는다. 파일 읽기·전체 내용 쓰기·지정 시험 요청만 처리한다.
- 설계 검토에서 기존 safe-mode·OAuth 경계를 유지하는 방식을 선택했다. Claude의 직접 도구·MCP는 계속 비활성화하며 task v6 보고서의 `operations`를 컨트롤러가 검증·실행한 뒤 다음 개정에 영수증으로 전달한다.
- 파일별 정책·호출/작업 예산은 생성 시 고정한다. 검사 명령은 정확한 argv와 제한 시간을 등록하며 모델이 명령을 바꾸지 못한다. Bubblewrap 격리를 사전 시험하고 사용할 수 없으면 실행을 거절한다. 별도 설치 의존성을 자동 추가하지 않는다.
- 검사 사본은 사용자·PID·네트워크 네임스페이스로 격리하고 홈·인증 환경·컨트롤러 상태·GPU 장치를 제공하지 않는다. 실행 시간·출력·프로세스별 자원을 제한하지만 작업 전체의 cgroup 자원 할당량을 보장하지는 않는다.
- 생성 ID·작업 요청·명령 시작 식별자를 부작용 전에 저장한다. 동시 생성, stop 직후 종료, 명령 시작 직전 reconcile 경계를 회귀 시험으로 고정했다. 결과가 불명확한 작업은 재실행하지 않고 기록과 사본을 보존한다.
- 완료 시 사본·패치·manifest를 고정하고 해시를 다시 검사한다. 별도 Fable 읽기 전용 사본이 같은 결과를 검토할 수 있다. 최종 Codex 승인과 원본 반영은 별도이며 자동 병합·push는 수행하지 않는다.
- schema 7→8의 백업·오프라인 이관·중단 복구 및 기존 3→8 연속 이관을 추가했다. P4의 제안/검토 템플릿과 P5의 사본 실행 루프를 자동 결합하지 않는다.

- 검증: 기존 200개 회귀 시험과 최종 P5 58개 시험을 Python 3.10·3.14에서 확인했다. Python 3.10 전체 회귀 실행은 255개 통과했으며, 마지막 검토에서 추가된 3개를 포함한 P5 기능군도 별도 검증했다. Python 3.14 첫 전체 실행에서 비동기 취소 완료를 즉시 기대한 시험 1건이 실패해 제한 시간 내 종료 확인으로 수정했고, 최종 P5 기능군이 통과했다. 실제 OS 격리 6개는 격리 가능한 환경에서 별도 확인했다. Ruff 검사·포맷·플러그인·스킬 검증도 통과했다.
- 실제 Sonnet 2회와 별도 Fable 2회로 사본 수정·지정 unittest·고정 파일 검토·Codex 명시적 승인을 확인했다. 실제 모델은 `claude-sonnet-5`, `claude-fable-5-1`이며 직접 도구 호출은 0회다. 원본 불변과 반복 조회/실행 시 추가 호출 없음도 확인했다.
- 별도 실제 시험에서 Sonnet의 JSON 앞 설명문을 엄격한 보고 검증이 거절해 자동 진행을 중단했다. 실패 기록은 보존했으며, 출력 지시를 명확히 한 새 합성 사례에서 전체 흐름을 다시 검증했다. 잘못된 결과를 자동 수정·승인하지 않는다.
- Fable 설계·복구 경계 검토와 Codex 독립 검토의 지적을 반영했다. Sonnet 정책 초안 위임과 큰 입력의 Fable 코드 검토는 시간 초과로 끝났고, 해당 기록을 보존했다. Codex가 정책 구현을 완료했으며, Fable은 같은 검토 세션의 새 개정에서 핵심 복구 경계를 검토했다. 역할·모델을 조용히 대체하지 않았다.
- v0.7 설치본·캐시를 갱신하고 기본 저장소를 검증된 백업 후 schema 8로 이관했다. 기존 테이블의 모든 행이 동일함을 확인했다. 설치본에서 이전 v5 보고, 새 v6 보고·고정 사본·기존 명시적 승인 상태를 모델 추가 호출 없이 재검증했다.

사용법은 [워크스페이스 가이드](docs/workspaces.md)를 따른다. P5는 작은 Git 사본의 제한된 수정·시험·검토를 지원한다. P4 템플릿과의 자동 연결, 범용 Claude 도구 실행, 다중 서버 전달은 별도 후속 범위다.

## 15. v0.7.0 배포 검증 기록

- P1–P5 변경을 공개 버전 v0.7.0으로 묶고 [변경 기록](CHANGELOG.md)에 기능과 이관 절차를 정리했다.
- 최종 코드에서 Python 3.10·3.14 각각 전체 258개 시험이 한 번의 전체 실행으로 통과했다. 실제 Bubblewrap 격리 6개를 포함하며 생략된 시험은 없다. 시험 시작 이후 실행 코드와 테스트 코드는 변경하지 않았다.
- Ruff 검사·포맷, 플러그인·스킬 검증, 게시 대상 전체 diff의 공백 검사와 로컬 문서 링크 검사를 통과했다.
- 공개 파일의 개인 경로·호스트명·인증정보·실제 세션 기록·런타임 산출물을 별도로 검사했다. 작성자 표기는 GitHub 계정 이름과 noreply 이메일을 사용한다. 개인 실행 로그와 검증 원본은 저장소에 포함하지 않는다.

## 16. 실제 프로젝트 운용 검증과 후속 안정화

- 자체 저장소의 UTF-8 경로·검사 인자 검증 결함을 실제 과제로 선정했다. 잘못된
  surrogate 경로가 정책을 통과해 뒤늦은 인코딩 오류 또는 비 UTF-8 파일명을
  만들 수 있었다. 정상 유니코드와 기존 NUL·경로·길이 제한은 보존한다.
- 자동 편집 흐름은 이번 운용 검증을 **통과하지 못했다**. 첫 Sonnet 수정 호출은
  300초 제한으로 종료됐다. 종료 확인 후 명시적으로 만든 별도 900초 과제는
  약 431초에 응답했지만 JSON 키 중복으로 거절됐다. 두 경우 모두 자동 재시도·
  작업 쓰기·시험·승인은 없었으며 원본 저장소와 실패 기록을 보존했다.
- Codex는 거절된 응답을 코드 제안으로만 검토했다. 제안의 NUL 차단 회귀를
  기존 테스트로 검출하고 복원했으며, 유니코드 경계 시험을 보강했다. 수정본은
  별도 Git 커밋에서 읽기 전용 Fable 검토를 진행해 APPROVE를 받았다. 실패한 편집 작업을 성공이나
  승인으로 바꾸는 절차가 아니다.
- 관련 14개 단위 시험과 독립 6,177개 경계 검사를 통과했다. 새 시험을 이전
  구현에 적용하면 15개 하위 검사가 실패했다. Python 3.14 전체 263개 시험은
  실제 Bubblewrap 격리 6개를 포함해 생략 없이 통과했고, Python 3.10에서도 관련
  14개 시험을 통과했다. 정적 검사·플러그인·스킬 검증도 통과했다. Fable 검토
  산출물만 정확한 결과 해시로 승인했으며 실패한 편집 과제는 승인하지 않았다.
  [시험 문서](docs/testing.md)에 검증 범위를 구분해 기록했다.
- 입력 검사 수정과 운용 기록을 v0.7.1로 정리하고 설치본·캐시를 갱신했다.
  schema 8을 유지하며 데이터 이관 없이 기존 세션·실행 목록, 검토 승인과 고정
  결과가 그대로 보존됐음을 새 설치본에서 확인했다.
- 다음 구현 순서는 ① 추론 강도·시간 정책의 명시 및 실행 기록 고정,
  ② 도구 비활성 상태에서 CLI 구조화 출력 호환성 검증, ③ 정확한 실패 실행을
  지정하는 Codex 명시적 복구 경로, ④ 실제 편집→격리 시험→고정 결과 검토→승인
  재검증이다. P4/P5 자동 결합은 이 운용 조건을 충족한 뒤 검토한다.

세부 경위와 통과 조건은 [실제 프로젝트 운용 기록](docs/real-project-pilot.md)에 정리했다.


## 17. 명시적 실행 설정 — v0.8.0

- OMX 0.20.5의 실행 인자 정규화와 실행 전 상태 저장 패턴을 참고해 Claude의
  `effort`를 세션·실행·과제 스냅샷에 고정했다. OMX 런타임 의존성을 추가하지
  않고 기존 SQLite 예약·워커 경로에 통합했다.
- 새 과제는 Sonnet `medium`, 중요한 설계·검토는 Fable `high`를 스킬에서 명시적으로
  선택하도록 안내한다. 런타임은 `low/medium/high/xhigh/max`를 엄격히 검증한다.
  기존 미지정 값은 미지정으로 유지하며 재개·재시작으로 다른 값으로 바꾸지 않는다.
- 작업자와 검토자의 effort는 독립적이다. `workflow create --reviewer-effort`가
  검토 값을 고정하며 작업자 값을 복사하지 않는다. P5의 후속 턴도 원래 값을 유지한다.
- 기존 1–3600초 run timeout과 신규 실행 허용 시간·조회 시간을 구분했다.
  실제 요청 effort와 argv를 기록하지만 모델 내부 추론량을 관측했다고 주장하지 않는다.
- 지원 여부는 DB 쓰기 트랜잭션 밖에서 조회하며 워커가 새로 확인한다. exec 직전에는
  검증된 바이너리 식별자를 비교해 추가 조회 프로세스 없이 실행 경계를 확인한다.
  상위 환경의 effort 변수는 전달하지 않는다. 미지원과 일시 조회 실패를 구분한다.
- schema 8→9는 검증된 백업과 복구 저널을 사용한다. 이전 컬럼·rowid·프롬프트·
  fingerprint·보고·승인은 유지하며 기존 대기열의 입력을 다시 작성하지 않는다.
- 이후 순서는 여전히 구조화 출력 호환성, 정확한 실패 실행을 지정하는 명시적 복구,
  실제 프로젝트 편집→격리 시험→고정 결과 검토→승인 재검증이다. 이번 설정 기능만으로
  이전 실패 과제를 성공으로 처리하거나 P4/P5를 자동 결합하지 않는다.

검증 결과는 [시험 문서](docs/testing.md), 설정은 [실행 설정 안내](docs/execution-settings.md),
참고 범위는 [OMX 도입 기록](docs/omx-adoption.md)에 기록한다.

- 최종 검증: Python 3.14 전체 292개 시험이 실제 격리 6개를 포함해 생략 없이
  통과했다. Python 3.10에서 신규 29개를 확인했다. 동일 Fable 세션의 최종 검토는
  APPROVE다. 별도 합성 시험 저장소에서 실제 Sonnet medium 시작·같은 세션 재개와
  Fable high 호출을 확인했으며 도구 실행·모델 대체는 없었다.
- 설치본·캐시의 32개 파일을 원본과 대조했다. 기본 저장소를 백업 후 schema 9로
  이관해 기존 9개 세션·19개 실행과 전체 기존 컬럼·rowid를 보존했다. 이관 전후
  모든 과거 보고서가 v0.7.1 릴리스와 동일하며 데이터베이스 무결성을 확인했다.

## 18. P4/P5 자동 조합 — v0.9.0

- 기존 P4와 P5 엔진을 변경해 하나의 과제로 겹치지 않고, 상위 `composition`
  상태가 `계획·독립 검토 → 계획 승인 → 편집·격리 검사 → frozen 검토 → 최종 승인`
  순서를 연결한다.
- 계획의 `approve_recommended`는 편집 허가가 아니다. 현재 계획 task/revision/run/digest에
  대한 Codex accept가 있어야 editor workspace가 만들어진다.
- editor가 최종 export를 고정하면 같은 read path와 쓰기·검사 권한이 없는 별도 Fable
  reviewer workspace를 자동 생성한다. reviewer 결과는 승인 근거이며 editor를 승인하거나
  수정하지 않는다.
- reviewer export까지 고정되면 자동화는 멈춘다. Codex가 정확한 editor 결과를 승인해야
  조합 상태가 accepted로 파생된다. 자동 승인·재시도·세션 대체·merge·push는 없다.
- schema 9→10은 composition/member/result 테이블만 추가한다. 기존 session/run/task/workflow/
  workspace/decision row와 effort 기록은 다시 쓰지 않는다.
- 구조화 출력 호환성, 명시적 실패 복구, 실제 프로젝트 gate 재검증은 이번 범위에 포함하지 않는다.

## 19. 구조화 출력과 편집 신뢰성 — v0.10.0

- Claude Code JSON schema 출력을 structured task와 workspace에 적용했다.
- 전체 파일 JSON 반환 대신 base SHA-256이 일치하는 line hunk patch를 추가했다.
- 쓰기·검사 영수증이 없고 트리가 그대로인 형식 오류에만 같은 세션의 bounded repair
  1회를 허용했다. 실행 여부가 불명확하거나 부작용이 있으면 재시도하지 않는다.
- 최종 Fable reviewer의 `revise`와 `blocked` 권고에 거부권을 주고, 승인 근거를
  `check_receipt`, `diff_hunk`, `review_result`, `free_text`로 구분했다.

## 20. 실사용 증거와 반영 경계 — v0.11.0

- 실제 Sonnet 편집·검사·freeze와 별도 Fable 검토 파일럿을 통과했다.
- provider usage, model usage, 비용, API/전체 시간을 append-only telemetry에 저장했다.
- 원본 HEAD, clean worktree, frozen manifest와 patch를 모두 확인하는 `workspace apply`를
  추가했다. 적용은 명시적이고 commit·merge·push를 하지 않는다.
- 동일 커밋을 읽는 Sonnet scout 결과를 계획 context에 provenance와 함께 고정한다.

## 21. 관측과 quota — v0.12.0–v0.13.0

- composition, workflow, workspace, task, session 관계를 graph로 보여주는 read-only TUI와
  agent/history view를 추가했다.
- 진행 중 stream token 추정치와 완료 후 provider 확정 telemetry를 구분했다.
- Codex, Claude Code, Gemini CLI, Cursor의 provider-reported quota window를 표시한다.
  공개되지 않은 절대 token ceiling이나 남은 token 수는 추정하지 않는다.

## 22. Windows 포팅 — v0.14.0–v0.14.1

- native Windows 세션은 pinned supervisor와 kill-on-close Job Object를 사용한다.
- workspace는 WSL2 ext4와 기존 Bubblewrap 격리를 사용하며 unconfined fallback은 없다.
- 실제 Windows에서 helper handshake, suspended child identity/resume, Claude 인증과
  `claude-sonnet-5` 도구 없는 호출을 검증했다. Codex sandbox token의 WSL service
  `E_ACCESSDENIED` 때문에 WSL2/Bubblewrap 물리 검증은 일반 터미널에서 남아 있다.

## 23. 리더 명세·테스트 동결·실증 평가 구현 계획 — v0.15.0

이번 단계는 검수 의견의 1, 2, 3, 6번을 다음 순서로 구현한다.

1. **리더 명세 등록:** 엄격한 JSON leader spec을 composition 불변 policy에 digest와
   함께 저장한다. Fable은 `critic` 역할로만 비평하며 명세를 생성하거나 수정하지 않는다.
   기존 P4 독립 검토와 정확한 Codex accept 이후에만 editor를 연다.
2. **테스트 계약:** 동결 테스트 경로, named check, baseline pass/fail, post pass를
   고정한다. editor write와 동결 경로의 겹침을 생성 시 거절한다. controller가 editor
   binding 전에 baseline을 실행하고 기존 멱등 operation 원장에 영수증을 기록한다.
3. **최종 트리 게이트:** frozen export에서 동결 파일 해시를 다시 확인하고, export의
   tree SHA-256과 일치하는 모든 필수 check 통과 영수증이 있어야 reviewer를 만든다.
4. **실증 평가:** 기존 workflow/workspace/run/telemetry 관계만 SELECT하여 readiness,
   acceptance, unassisted success와 95% Wilson 구간을 계산한다. 모든 run의 provider 비용과
   네 token 필드가 완전한 composition만 비용·token 집계에 포함한다.
5. **문서와 호환성:** schema 13을 유지한다. 기존 generated-plan composition은 policy에
   새 필드가 없어도 기존 의미로 실행한다. 한국어/영문 가이드와 이 계획서를 갱신한다.

### 설계 검토에서 수용한 부분

- Fable과 Codex 모두 controller-owned baseline/post 검사, final-tree hash 결합,
  Wilson 구간, telemetry missing/partial 분리를 타당한 핵심으로 보았다.
- 별도 specification/critique/test table과 compositions 재구축은 채택하지 않았다.
  현재 불변 composition policy, append-only workspace receipt, idempotent task operation이
  같은 provenance와 replay 방지를 제공하므로 schema 14의 영구 이관 부채가 불필요했다.
- test contract는 기존 사용자를 깨지 않도록 선택 사항으로 둔다. 지정한 composition에는
  예외 없이 강제하며, 지정하지 않은 과거·신규 composition은 평가에서 `n/a`로 분리한다.
- 외부 event bridge, ACP, 정책 자동 승인, 추가 provider/platform은 이번 범위에서 제외한다.

### 구현 결과

- `composition create`는 generated plan과 leader spec 중 정확히 하나를 받는다.
- leader spec은 Fable critic workflow revision을 0으로 고정하고, editor 기준과 spec 기준의
  정확한 일치를 요구한다.
- baseline 불일치, 동결 파일 변경, 필수 check 누락·실패·stale tree receipt는 자동화가
  멈추는 명시적 사유다. 실패를 성공으로 바꾸거나 자동 재시도하지 않는다.
- `composition evaluate --all`과 반복 `--composition` 선택을 추가했다. zero denominator는
  0으로 꾸미지 않고 null이며, 비용과 token 누락도 추정하지 않는다.
- Python 3.14 전체 회귀 시험 411개가 통과했고 1개는 현재 호스트가 Bubblewrap
  namespace를 허용하지 않아 명시적으로 생략됐다. 변경 파일 Ruff 검사와 포맷 검사,
  `git diff --check`, 빈 실제 저장소에 대한 read-only 평가 smoke도 통과했다.

## 24. 라우팅 출처·다중 composition·scout 재사용 — v0.16.0

이번 단계는 실증 데이터의 해석 가능성과 여러 composition의 처리량을 높이되 기존
승인·재시도·격리 경계를 바꾸지 않는다.

1. **불변 라우팅 기록:** 선택적인 version 1 metadata에 task type, R0/R1/R2,
   routing policy version, model 선택 사유와 parent escalation을 저장한다. 부모
   composition, 연속 attempt, 이전·현재 editor model 일치를 생성 시 검증한다.
2. **층화 평가:** 기존 SELECT-only 평가에 routing, editor model/effort, 명세 origin,
   outcome별 결정적 그룹을 추가한다. metadata가 없는 과거 기록은 추정하지 않고
   `unrecorded`로 표시한다.
3. **다중 dispatcher:** 시작 시 선택한 composition 스냅샷을 FIFO sweep으로 진행한다.
   기존 `composition run --once`와 전역 `max_parallel`을 재사용해 worker가 겹쳐 실행될
   수 있게 한다. 전역 dispatcher lock, busy 격리, composition별 오류 분리를 적용한다.
4. **검증된 scout cache:** 완전한 정규화 assignment, source repo, 고정 commit,
   read paths, workspace task protocol을 cache identity로 사용한다. hit도 frozen export와
   digest를 다시 검증하며 miss는 `scout_cache_miss`로 멈춘다. 자동 scout 실행과
   암묵적 context 생략은 없다.
5. **호환성:** schema 13을 유지한다. routing은 불변 composition policy에 저장하고
   dispatcher와 평가·cache lookup은 기존 원장을 사용한다. 옵션을 생략한 동작은
   v0.15와 같다.

설계 단계는 로컬 Fable high가 schema 13 유지, 명시적 cache, foreground dispatcher를
검토했다. Sonnet medium은 지정한 소스 파일을 읽은 뒤 구현 호출이 600초 제한으로
종료되어 자동 재시도하지 않았고, Codex가 같은 승인 범위에서 구현·통합했다.

검증은 새 composition/evaluation 25개 focused 시험과 Python 3.14 전체 414개 시험을
통과했다. 전체 suite의 1개 생략은 현재 호스트가 Bubblewrap namespace를 허용하지 않는
기존 환경 경계다. 변경 파일 Ruff 검사·포맷, CLI help/version, JSON manifest와
`git diff --check`도 통과했다. 저장소 전체 Ruff에는 이번 diff 밖의 기존 import-order
3건이 남아 있어 변경 파일 검사와 구분해 기록한다.

## 25. 역할별 버전 모델·effort 설정 — v0.17.0

- executor, researcher, planner, architect, critic, verifier의 모델과 선택적 effort를
  호스트 전용 JSON 문서로 설정한다. 문서는 완전 교체 방식으로 검증 후 원자 저장한다.
- `sonnet`, `opus`, `haiku`, `fable` 별칭은 실제 응답의 모델 계열을 검증하고,
  `claude-...` 전체 ID는 정확히 일치해야 한다. 모델이나 effort 폴백은 없다.
- assignment의 명시적 값은 역할 기본값보다 우선한다. 해석된 값은 task/session 생성
  시점에 동결되어 이후 설정 변경이 기존 revision, retry, resume에 영향을 주지 않는다.
- scout, verifier, workflow reviewer, leader critic의 모델 계열 강제를 제거하고 역할과
  sandbox 경계는 유지한다. 내부 reviewer는 critic 기본값 또는 명시적 override를 쓴다.
- `models show/configure/reset`, 한국어·영문 설정 문서, exact-ID 검증과 호환 기본값
  회귀 시험을 추가한다. schema 13은 유지한다.

검증은 Python 3.14 전체 424개 시험을 통과했고, 1개는 현재 호스트의 Bubblewrap
namespace 제약으로 생략됐다. 변경 파일 Ruff, plugin-creator 검증, manifest JSON,
`git diff --check`도 통과했다.
