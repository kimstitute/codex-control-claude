# Task 생명주기

이 문서는 `claude-control`에 추가된 P1 task workflow, 즉 위임 작업의 생성, 제출, 검토, 승인과 이를 활성화하기 위한 1회성 migration을 설명합니다. 예시는 [CLI 레퍼런스](cli.ko.md)의 `claude_control` 셸 함수를 사용합니다. Assignment 파일은 기존 [assignment 스키마](cli.ko.md#delegate-with-a-role-and-an-output-contract)를 사용합니다.

## Schema와 Migration

Task 생명주기는 schema 4 이상이 필요합니다. queue는 schema 5, message는 schema 6, workflow는 schema 7, workspace는 schema 8이 필요합니다. 명시적 effort 설정은 schema 9가, 계획·편집·검토 composition은 schema 10이, run telemetry는 schema 11이, guarded workspace apply는 schema 12가 필요합니다. 새로 설치(`claude_control init`)하면 schema 12로 시작합니다. 기존 schema-3 저장소는 `task` 하위 명령을 사용하기 전에 migration해야 하며, schema-3 저장소는 migration 전까지 task 명령을 거부합니다.

Migration은 백그라운드 서비스가 아니라 유지보수 작업입니다.

1. 관리 run을 완료하거나 중단하세요. unknown 실행은 기록된 PID namespace에서 조사·reconcile하세요. 해결되지 않은 실행은 migration을 막습니다.
2. 오래된 CLI 클라이언트와 worker를 중단하세요. migration 중에는 서로 다른 버전의 컨트롤러를 동시에 실행하지 마세요.
3. 새 코드를 설치하세요. schema-3 저장소는 레거시 진단/stop/reconcile 명령을 계속 사용할 수 있지만, task 명령은 migration이 필요합니다.
4. 상태를 확인한 뒤 같은 상태 디렉터리를 명시적으로 업그레이드하세요.

```shell
claude_control --state-dir /srv/project/.claude-control migrate --status
claude_control --state-dir /srv/project/.claude-control migrate --offline
```

Migration은 3→4→5→6→7→8 순서로 진행되며, 필요한 각 단계마다 SQLite의 backup API를 사용해 커밋된 WAL 데이터를 포함한 `schema-<source>-backup.sqlite3`를 만들고 검증합니다. 설정을 `migrating`으로 표시하고, 트랜잭션 안에서 데이터베이스를 변경한 뒤 설정과 journal을 마무리합니다. 일반 명령은 부분적으로 migration된 저장소를 거부합니다. `--offline`은 모든 오래된 클라이언트가 중단되었음을 확인하는 것이며, 활성 또는 unknown run에 대한 강제 옵션이 아닙니다. 새 버전의 데이터베이스 작업 역시 독점 migration 잠금에 참여합니다. DDL이 시작되기 전에 오래된 클라이언트가 유지보수 규칙을 위반하면 업그레이드는 중단되고, run 상태를 전혀 바꾸지 않은 채 schema-3 진단을 다시 엽니다. 해당 작업을 해결한 뒤 migration을 반복하세요. 이는 버전이 섞인 운영을 지원한다는 뜻이 아닙니다.

migration이 중단된 후에는 같은 `migrate --offline` 명령을 **원래 상태 디렉터리**에 대해 실행하세요. 저장된 journal, DB 버전, 비공개 백업 덕분에 안전하게 완료할 수 있습니다. 다른 저장소를 초기화하거나, 실 DB를 `.sqlite3` 파일 복사본으로 교체하거나, 백업에 대해 모델 작업을 실행하지 마세요. 업그레이드된 schema 운영이 시작된 뒤에는 자동 다운그레이드가 제공되지 않습니다. schema 1과 2는 지원되지 않습니다.

`migrate --status`는 현재 버전, 활성 run ID, migration journal을 읽으며 모델을 호출하지 않습니다. schema/버전 불일치는 일반 명령을 차단합니다.

## Task 생명주기

task는 하나 이상의 불변 **개정(revision)**에 걸쳐 assignment(role, model, project, prompt, acceptance criteria)를 보관합니다. 각 개정은 **제출(submit)**되어 하나 이상의 **run**을 만들 수 있으며, 완료된 run은 **검토(review)**된 뒤 명시적으로 **승인(accept)**되거나, task가 **개정(revise)**되어 다시 제출됩니다.

### 1. 생성

```shell
claude_control task create \
  --assignment-file /srv/project/assignments/refactor-auth.json \
  --operation-id create-refactor-auth-001
```

기존 관리 세션에 연결하려면 정확한 최신 완료/종료 턴을 식별하는 `--session <SESSION_UUID>`와 `--parent-run <RUN_UUID>`를 함께 제공하세요. role, model, canonical project가 일치해야 합니다. 부모 턴이 실패한 뒤에는 부분 대화를 확인하고 의도적으로 `--acknowledge-context`를 사용하세요. 이후의 레거시 follow-up이나 restart는 `context_changed`로 제출을 차단합니다. 이 컨트롤러 밖에서 Claude를 resume하여 만든 변경은 감지할 수 없습니다. 이 문서 전체에서 `<SESSION_UUID>`, `<RUN_UUID>` 등 꺾쇠괄호로 표시된 이름은 자리표시자이며, 그대로 입력할 문자 그대로의 ID가 아닙니다. 응답에는 새 task의 id와 첫 번째 개정이 포함됩니다.

### 2. 제출(및 재시도)

```shell
claude_control task submit \
  --task <TASK_ID> --revision <REVISION> \
  --operation-id submit-refactor-auth-001
```

`submit`은 개정에 대해 run을 예약하고 worker를 실행합니다. 같은 `--operation-id`를 반복하면 원래 run을 그대로 반환하며(`"deduplicated": true`), 두 번째 worker를 실행하지 않습니다. 제출 프로세스가 커밋 이후 실행 전에 죽었다면 그 예약은 만료됩니다. 이를 확인하고 새 operation ID로 명시적인 `retry`를 사용하세요. 오래된 operation을 재생해도 실행이 복구되지는 않습니다. `retry`는 같은 인수를 받지만, worker가 시작되기 전에 만료된 예약처럼 절대 시작되지 않았음이 증명된 시도에 대해서만 허용됩니다. 결과를 알 수 없는 시도나 실제로 실행되어 실패한 시도에는 절대 허용되지 않습니다.

```shell
claude_control task retry \
  --task <TASK_ID> --revision <REVISION> \
  --operation-id retry-refactor-auth-001
```

### 3. 조회

```shell
claude_control task list
claude_control task show --task <TASK_ID>
claude_control report --run <RUN_ID>
```

`task show`는 task의 상태, 상태 사유, 저장된 불변 prompt와 criteria를 포함한 모든 개정, 각 개정에 대한 run, 결정 이력을 반환합니다. `report`(기존의 최상위 명령)는 task run을 포함한 모든 run에 계속 사용할 수 있으며 v2 task 턴 보고서 형식을 인식합니다.

### 4. 검토

run이 끝나면 reviewer는 각 acceptance criterion에 대한 검토 결과를 evidence 파일에 기록합니다. evidence는 1부터 시작하는 모든 criterion id를 하나 이상의 타입 객체에 매핑합니다. 예: `/srv/project/evidence/refactor-auth-run1.json`.

```json
{
  "1": [{"type": "free_text", "text": "결과를 요구사항과 대조했습니다."}],
  "2": [{
    "type": "check_receipt",
    "run_id": "<workspace-run-uuid>",
    "seq": 0,
    "receipt_sha256": "<canonical-receipt-sha256>"
  }]
}
```

파일은 최대 1MiB여야 하며, `accept`의 경우 개정이 요구하는 모든 criterion을 다루어야 합니다. 지원 타입은 `free_text`, `check_receipt`, `diff_hunk`, `review_result`입니다. 뒤의 세 타입은 불변 workspace receipt, 동결 manifest, 구조화 review run과 대조됩니다. 기존 문자열 입력은 저장할 때 `free_text`로 정규화됩니다. 알 수 없는 필드, 빈 항목, 일치하지 않는 digest, 숫자가 아닌 criterion 키는 `invalid_evidence`로 거부됩니다.

```shell
claude_control task review \
  --task <TASK_ID> --revision <REVISION> --run <RUN_ID> \
  --result-sha256 <RESULT_SHA256> \
  --evidence-file /srv/project/evidence/refactor-auth-run1.json \
  --operation-id review-refactor-auth-run1 \
  --reviewer codex-reviewer \
  --recommendation approve
```

`--recommendation`은 `approve`, `revise`, `blocked` 중 하나입니다. review는 권고 사항을 기록할 뿐, 그 자체로는 task 상태를 바꾸지 않습니다.

### 5. 승인 또는 개정

승인은 별도의 명시적인 결정입니다. worker가 성공을 보고하는 것만으로는 결코 충분하지 않습니다.

```shell
claude_control task accept \
  --task <TASK_ID> --revision <REVISION> --run <RUN_ID> \
  --result-sha256 <RESULT_SHA256> \
  --evidence-file /srv/project/evidence/refactor-auth-run1.json \
  --operation-id accept-refactor-auth-run1
```

대신 작업에 변경이 필요하다면 같은 task에서 새로운 불변 개정을 만드세요. role, model, project는 고정된 채 prompt와 criteria만 변경됩니다.

```shell
claude_control task revise \
  --task <TASK_ID> --revision <REVISION> \
  --assignment-file /srv/project/assignments/refactor-auth-v2.json \
  --parent-run <RUN_ID> \
  --operation-id revise-refactor-auth-002
```

`revise`는 새 개정을 만들 뿐 제출하지는 않습니다. 준비가 되면 `task submit`으로 새 개정을 별도로 제출하세요. 예약된 backend에서 중단된 모든 run이 Claude가 한 번도 실행되지 않았음을 증명한다면, 제출은 존재하지 않는 기록을 resume하는 대신 같은 backend UUID를 `--session-id`와 함께 사용합니다. 일단 실행 증거가 있으면 대화를 조용히 재설정하는 일은 절대 없습니다.

## Operation id와 `--revision`

상태를 변경하는 모든 task 명령은 `--operation-id`를 받습니다. 같은 operation id를 같은 입력으로 재사용하면 원래 결과를 멱등적으로 반환하고, 다른 입력으로 재사용하면 충돌로 거부됩니다. `revise`, `submit`, `retry`도 `--revision`을 받아 호출자가 현재라고 믿는 개정을 지정합니다. 따라서 오래된 개정에 대한 변경은 이미 진행된 작업에 조용히 적용되는 대신 거부됩니다.

## 실패 처리 요약

- 시작에 실패한 run(예: 만료된 예약)은 `task retry`로 재시도할 수 있습니다.
- 결과를 알 수 없는 run이나 시작 후 실행 중 실패한 run은 자동으로 재시도해서는 안 되며 조사가 필요합니다. unknown 실행은 새 턴을 진행하기 전에 반드시 reconcile해야 합니다. 중단되거나 실패한 턴은 새 개정과 명시적 context acknowledgement가 필요합니다.
- `task review`는 evidence와 권고 사항을 기록할 뿐, 그 자체로는 task 상태를 바꾸지 않습니다.
- 개정의 작업을 승인된 것으로 표시하는 것은 오직 `task accept`, 즉 명시적인 사람/Codex의 결정뿐입니다.

일시적인 result 읽기 오류(예: 임시 디스크립터 고갈)는 완료된 run을 영구적으로 failed로 바꾸지 않고 `result_unreadable`/`needs_attention`을 보고합니다. 누락되거나 변경된 결과 아티팩트는 여전히 무결성 검사에 실패합니다.

## 아직 지원하지 않는 기능

Task 관리는 여전히 텍스트 전용이며 단일 호스트에 한정됩니다. 의존 작업과 제한된 dispatch는 [대기열 가이드](queue.ko.md)에서 다룹니다. 자율 검토 루프, worker 파일/셸 도구, 호스트 간 실행은 지원되지 않습니다. 기존 v1 명령(`start`, `followup`, `resume`, `restart`, `status`, `result`, `report`, `logs`, `stop`, `reconcile`, `wait`, `observe`, `delegate`)과 그 보고서는 이번 변경으로 바뀌지 않습니다.

## 다음 턴 지시

`task revise`는 반복 가능한 `--message-id`와 명시적인 `--redeliver-messages` 플래그도 받습니다. 선택, 취소, 소스 인수인계, 전달 영수증 의미에 대해서는 [메시지](messages.ko.md)를 참고하세요. 메시지를 선택하지 않으면 기존 v2/v3 계약은 그대로 유지됩니다.

### Schema 8에서 9로

오프라인 업그레이드는 session과 run에 null을 허용하는 `effort` 필드와 그 불변 값을 강제하는 제약 조건을 추가합니다. 기존 행은 rowid, 이전 필드, prompt 바이트, 해시, 결정을 그대로 유지합니다. NULL은 컨트롤러가 effort를 선택하지 않았다는 뜻이며, 알려진 모델 기본값이 아닙니다. 이 migration은 검증된 `schema-8-backup.sqlite3`와 재개 가능한 `migration-8-9.json` journal을 만듭니다. 대기열에 있는 개정은 그대로 유지되며 dispatch될 때 CLI 플래그를 생략합니다. 새 설정을 사용하기 전에 [실행 설정](execution-settings.ko.md)을 참고하세요.
