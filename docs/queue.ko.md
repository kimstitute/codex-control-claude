# 대기열과 의존성

버전 0.4는 기존 task/revision workflow에 영속적인 대기열을 추가합니다. Codex는 명시적으로 dispatcher를 호출합니다. enqueue만으로는 모델을 호출하지 않습니다.

## 등록과 dispatch

[CLI 가이드](cli.ko.md)의 `claude_control` 명령 헬퍼를 사용하세요. `task create`로 부모와 자식 task를 만들고 반환된 UUID를 저장하세요. 정확한 참조를 담은 dependencies 파일을 작성하세요.

```json
[{"task_id":"<parent-task-uuid>","revision":1}]
```

```bash
claude_control task enqueue --task <child-task-uuid> --revision 1 \
  --dependencies-file /absolute/path/dependencies.json --operation-id queue-child-1
claude_control task enqueue --task <parent-task-uuid> --revision 1 \
  --operation-id queue-parent-1
claude_control dispatch --once
```

부모가 시작되고 자식은 대기 상태를 유지합니다. 부모에 대해 `task show`, `wait`, `report`를 확인하세요. acceptance criteria를 독립적으로 검증한 뒤, [task 승인](tasks.ko.md)에 설명된 대로 정확한 revision, run, 결과 다이제스트, criterion evidence와 함께 `task accept`를 사용하세요. 권고성 `task review`만으로는 충분하지 않습니다.

```bash
claude_control dispatch --until-idle --max-seconds 60
claude_control task events --task <child-task-uuid> --after 0 --limit 100
```

두 번째 dispatch는 이제 자식을 시작할 수 있습니다. 완료되더라도 Codex의 검토와 명시적 승인이 여전히 필요합니다. 상태 확인과 승인 기록은 모델을 호출하지 않습니다. 이 릴리스는 검토를 예약하거나 결과를 자동으로 승인하지 않습니다.

## 순서와 한도

- 준비된 task는 FIFO 순서로 허용됩니다. 차단된 항목은 건너뛰어 독립적인 작업이 진행될 수 있게 합니다.
- `init --max-queued 100`은 `--max-parallel 2`와 별개로 대기 항목 한도를 설정합니다. 대기 항목에는 차단된 작업이 포함되며, 예약되거나 취소된 항목은 포함되지 않습니다. 지원되는 대기열 한도는 1~10000입니다.
- `dispatch --once`는 한 번의 허용 패스를 수행합니다. `--max-seconds`를 받지 않습니다.
- `dispatch --until-idle`은 0에서 3600 사이의 `--max-seconds`가 필요합니다. 이 한도는 새로운 허용에 적용되며, 이미 예약되어 시작된 worker는 계속 진행됩니다. 이는 파일 시스템 I/O나 이전에 커밋된 예약 실행에 대한 엄격한 실시간 프로세스 종료 마감이 아닙니다.
- 차단되거나 unknown 상태의 작업만 남아 있으면 루프는 `needs_attention`을 반환합니다. 실행이 진행될 수 있는 동안 일반적인 로컬 폴링 주기는 0.2초입니다. 백그라운드 dispatcher나 Codex를 깨우는 기능은 설치되어 있지 않습니다.

## 정확한 의존성과 입력 이력

각 자식 개정은 최대 64개의 고유한 부모 task/revision 참조를 고정합니다. 자기 자신에 대한 의존, 순환, 존재하지 않는 task, 현재가 아닌 부모 revision은 enqueue 전에 거부됩니다. 고정된 부모의 revision을 나중에 바꾸면 대기 중인 자식이 `dependency_changed`로 차단됩니다. 암묵적인 재타게팅은 없습니다.

허용 절차는 현재 부모 run, 손상되지 않은 결과 바이트, model/session 실행 증거, 유효한 완료 보고서, 정확한 Codex 승인을 확인합니다. 이 검사와 자식 예약은 하나의 쓰기 트랜잭션을 공유합니다. 직접적인 `task submit`과 `task retry`도 자식이 대기열에서 빠진 뒤에도 같은 게이트를 강제합니다.

불변 기본 정의는 task contract v2를 사용합니다. 대기열에 들어간 실행은 승인된 보고서와 부모 task/revision/run/digest/decision ID를 복사한 v3을 사용합니다. 최종 입력은 1MiB 이내여야 하며, 초과 시 예약을 소비하지 않습니다. 과거의 자식 보고서는 이후 변경된 부모 파일을 다시 열지 않고 동결된 입력을 검증합니다. 중복 승인은 처음 일치하는 승인 기록을 선택하므로, 명시적 재시도는 동일한 prompt 바이트를 받습니다. P2에서는 승인을 철회하는 작업이 없으며, review 권고는 참고용입니다. 새 부모 revision은 자체 승인이 필요합니다.

## Dequeue, 개정, 복구

`task dequeue --queue-id <integer> --operation-id <id>`는 차단된 항목을 포함해 대기 중인 항목을 취소합니다. 예약된 run을 중단하지는 않습니다. 그럴 때는 `stop --run`을 사용하세요. 제출되지 않은 revision을 다시 enqueue하려면 같은 의존성 집합이 필요하며 새로운 FIFO 위치를 받습니다.

`task revise`는 `--parent-run` 없이 제출되지 않은 task를 개정할 수 있습니다. 세션이 존재한 이후에는 정확한 최신 부모 run이 필요합니다. 개정은 기존 대기 항목을 대체합니다. 새 개정은 새로운 의존성 선언과 명시적인 enqueue가 필요하며, 둘 다 자동으로 상속되지 않습니다.

예약 커밋 이후 실행 전에 크래시가 발생하면 pending 상태의 run이 남습니다. 이는 일반적인 청구 마감 시한에 따라 만료됩니다. 모든 시도가 Claude를 한 번도 실행하지 않았다는 증거가 있는 명시적인 `task retry`만이 같은 예약된 대기열 항목에 새 run을 바인딩할 수 있습니다. Retry는 의존성을 다시 확인하며 바이트 단위로 동일한 실행 입력을 요구합니다.

실행 이후 크래시가 발생하면 지원되는 영속 Linux 호스트에서 분리된 worker가 완료를 책임집니다. 다른 dispatcher를 시작해도 기존 예약을 읽을 뿐 중복 실행하지 않습니다. `unknown`은 용량 슬롯을 계속 점유합니다. 새 턴을 진행하기 전에 조사하고 `reconcile`하세요. unknown이거나 실행 후 실패한 턴은 `task retry`를 사용할 수 없습니다. reconciliation 이후 새 revision이 필요할 수 있습니다.

## Events와 Migration

`task events [--task <uuid>] --after <cursor> --limit <1..1000>`는 추가 전용(append-only)인 revision, reservation, run-state, queue-state, decision 이벤트를 반환합니다. 중복을 피하려면 `next_cursor`를 재사용하세요. 변경 없이 반복되는 차단 관찰은 이벤트를 생성하지 않습니다. migration 이전의 이벤트는 새로 만들어지거나 소급 생성되지 않습니다.

새 저장소는 schema 15를 사용하며, 대기열 자체는 schema 5 이상이 필요합니다. 기존 저장소는 대기열 기능을 사용하려면 명시적인 `migrate --offline`이 필요합니다. schema 3은 각 중간 schema를 거쳐 현재 버전까지 업그레이드되며, 각 단계는 검증된 SQLite 백업과 영속적인 journal을 만듭니다. 먼저 클라이언트/worker를 중단하고 unknown run을 해결하세요. 중단된 업그레이드는 같은 명령으로 재개됩니다. [migration과 복구](tasks.ko.md#schema-and-migration)를 참고하세요.

revision은 [다음 턴 메시지](messages.ko.md)를 명시적으로 선택할 수도 있습니다. dispatch는 run과 함께 해당 영수증을 예약하고 결합된 의존성과 메시지 입력 크기를 확인합니다. 메시지를 enqueue하는 것만으로는 새 task 턴이 대기열에 들어가지 않습니다.
