# 다음 턴 메시지와 핸드오프

0.5 버전에서는 Codex가 관리형 Claude 작업이 실행되는 동안 지시사항을 저장해두었다가,
이후 리비전에서 명시적으로 선택할 수 있습니다. 등록(enqueue)과 선택(selection)은
모델을 호출하지 않으며 활성 Claude 프로세스에 기록되지도 않습니다.

[CLI 가이드](cli.md)의 `claude_control` 명령어 헬퍼를 사용하세요.

## 등록, 선택 및 제출

지시사항 텍스트를 UTF-8 파일에 작성하고 작업의 정확한 현재 리비전을 지정합니다:

```bash
claude_control message enqueue --task <task-uuid> --base-revision 1 \
  --content-file /absolute/path/instruction.txt --operation-id instruction-1
claude_control message list --task <task-uuid>
```

반환된 메시지 UUID를 저장해 두세요. `--session <managed-session-uuid>`는 선택적으로
작업의 세션을 검증(assert)합니다. 작업과 기존 백엔드 아이덴티티는 고정되어 있으므로,
대화를 재시작해도 메시지가 새 백엔드로 리다이렉트되지 않습니다. 작업에 아직 세션이
없다면, enqueue는 할당되지 않은 상태로 남겨두고 이후 검사에서 작업의 첫 번째
백엔드를 확인합니다.

현재 실행이 마무리된 후, 정확한 최신 부모 실행(run)과 선택한 메시지 ID들로 작업을
리비전합니다. 최대 64개의 고유 ID를 선택할 수 있으며, 입력 순서가 전달 순서를
바꾸지 않습니다 — 전달 순서는 등록된 순서를 따릅니다.

```bash
claude_control task revise --task <task-uuid> --revision 1 \
  --parent-run <latest-run-uuid> --assignment-file /absolute/path/revised.json \
  --message-id <message-uuid> --operation-id revision-2
claude_control task submit --task <task-uuid> --revision 2 --operation-id submit-2
```

세션이 없는 미제출 작업의 경우 `--parent-run`을 생략하세요. 부모 컨텍스트 실패 시
새 턴을 실행하기 전에 검사와 의도적인 `--acknowledge-context`가 필요합니다. 리비전
2를 직접 제출하는 대신 [작업 큐](queue.md)에 등록할 수도 있습니다. 두 경로 모두
동일한 메시지 검사를 적용합니다.

현재 리비전만 실행될 수 있습니다. 새 리비전은 선택된 메시지를 상속하지 않습니다.
기준(base)이 더 이상 일치하지 않는 바인딩되지 않은 메시지는 취소한 뒤 새
operation ID로 현재 기준에 맞춰 다시 등록해야 합니다 — 절대 암묵적으로 이동하지
않습니다. 동일한 operation ID를 반복하면 원래 결과가 반환되며, 입력이 변경되면
충돌이 발생합니다.

## 결과 핸드오프

Codex는 검증되고 고정된(frozen) 결과를 메시지에 첨부할 수 있습니다:

```bash
claude_control message enqueue --task <recipient-task-uuid> --base-revision 1 \
  --content-file /absolute/path/handoff-instruction.txt \
  --source-run <completed-source-run-uuid> --source-result-sha256 <exact-digest> \
  --operation-id handoff-1
```

소스는 반드시 완료되었고 결과와 유효한 보고서가 온전한 관리형 구조적 작업 실행
(v2, v3, v4 또는 v5)이어야 합니다. 완료(complete) 또는 차단(blocked) 보고서 모두
제공될 수 있으며, 이는 승인 결정 전에 비평가(critic)의 검토 결과를 전달하는 데
활용됩니다. 소스 run과 digest는 반드시 함께 제공되어야 합니다. 레거시 `delegate`와
비구조적 결과는 구조적 소스로 허용되지 않습니다.

이는 Codex의 명시적 선택입니다. 이것이 Claude에게 다른 에이전트를 호출하거나 작업을
생성할 권한을 부여하지는 않습니다. 또한 큐에 대기 중인 의존성의 소스를 수락하거나
별도의 부모 수락 게이트를 충족시키지도 않습니다. 복사된 보고서와 그 소스 run/digest는
이후 원본 아티팩트가 사라지더라도 고정된 상태로 유지됩니다. 자동 리비전에는 별도로
생성된 [경계 지정 워크플로](workflows.md)가 필요합니다.

## 상태와 이력

| 상태 | 의미 |
|---|---|
| `queued` | 어떤 실행도 이 메시지를 바인딩하지 않았습니다. `selected_for_revision`은 선택되었지만 아직 제출되지 않은 메시지를 나타냅니다. |
| `bound_to_run` | 예약(reservation)에 이 메시지가 포함되어 있으며, 실행은 대기 중이거나 진행 중입니다. |
| `run_completed` | 최근 바인딩된 실행이 온전한 결과와 함께 완료되었습니다. |
| `needs_attention` | 최근 전달이 실패했거나, 취소되었거나, 알 수 없는 상태가 되었거나, 무결성/읽기 문제가 있습니다. |
| `cancelled` | Codex가 바인딩되지 않은 메시지를 명시적으로 취소했습니다. |

바인딩은 예약 영수증일 뿐, Claude가 지시사항을 읽고 이해했다는 증거가 아닙니다.
`run_completed` 역시 의미적 수락(semantic acceptance)이 아닙니다. 보고서를 검증하고
기준 증거와 함께 별도로 `task accept`를 사용하세요.

`message list [--task <uuid>] --after <sequence> --limit <1..1000>`는 메시지 내용,
소스 출처, 선택 정보, 바인딩 이력 및 `next_cursor`를 반환합니다. 이 커서는 새로
등록된 메시지를 발견하는 데 사용됩니다. 이전 메시지의 변화하는 상태를 갱신하려면
해당 페이지를 다시 조회하세요. 바인딩은 자체적인 단조 증가(monotonic) 시퀀스를
가집니다. `redelivered` 플래그는 여러 번의 예약 시도가 있었음을 의미하며, 여기에는
Claude를 전혀 실행하지 못한 재시도가 포함될 수 있습니다. 실행 증거는 바인딩된 각
run을 직접 확인하세요.

변경 사항을 추적하려면 `task events --task <uuid> --after <cursor>`를 사용하세요.
순서는 벽시계 타임스탬프가 아니라 이벤트 ID로 결정됩니다. 새 리비전이 이전 것을
대체한 이후에도 과거 선택 내역은 계속 표시되지만, 실행 가능한 것은 현재 리비전뿐입니다.

## 취소와 의도적 재전달

```bash
claude_control message cancel --message <message-uuid> --operation-id cancel-1
```

취소는 최초 바인딩 이전에만 동작합니다. 선택되었지만 아직 제출되지 않은 메시지를
취소하면 해당 리비전의 제출이 무효화됩니다 — 의도한 선택으로 새 리비전을
생성하세요. 취소와 예약은 직렬화되어 있습니다: 취소가 먼저 이기면 어떤 실행도
바인딩되지 않으며, 바인딩이 먼저 이기면 취소가 거부됩니다. 바인딩된 메시지 이력은
절대 삭제되지 않습니다. 실행 자체를 취소하려면 `stop --run`을 사용하세요.

바인딩된 실행이 실패한 후, 그 결과와 프로세스 상태를 확인하세요. 동일한 메시지를
새 리비전에 의도적으로 포함하려면, `task revise`에 `--message-id`와
`--redeliver-messages`를 함께 전달하세요. 이렇게 하면 이전 전달의 run ID가
고정되고 이전 영수증이 보존됩니다. 완료된 전달과 해결되지 않은 `unknown` 실행은
재전달할 수 없습니다. 먼저 알 수 없는(unknown) 실행 상태를 정리하세요. Claude가
실패한 시도를 이미 읽었을 수도 있으므로, 의도적 재전달이 정확히 한 번의 이해나
외부 효과를 보장하지는 않습니다.

예약되지 않은 재전달 초안은 다른 재전달 리비전에 의해 명시적으로 대체될 수
있습니다. 이전 선택 내역은 과거 기록일 뿐이며 제출할 수 없습니다. 암묵적인
이월(carry-over)은 발생하지 않습니다.

`task retry`는 더 제한적입니다: 동일한 리비전의 모든 시도가 Claude를 전혀
시작하지 않았음이 증명된 경우에만 허용됩니다. 프롬프트는 바이트 단위로 동일해야
하며, 새 예약은 또 다른 메시지 영수증을 추가합니다. 충돌, 취소, 타임아웃 이후
자동 재전송은 없습니다.

## 제한과 마이그레이션

작업당 바인딩되지 않고 취소되지 않은 메시지는 오래된 것이나 선택된 것을 포함해
100개까지 허용됩니다. 공간을 확보하려면 오래된 항목을 취소하세요. 각 메시지와
그 복사된 소스, 그리고 각 리비전과 선택된 메시지는 1MiB 이내여야 합니다. 예약
시점에는 작업, 의존성 보고서, 메시지를 모두 합친 크기도 이 한도 내에 들어야
하며, 초과 시 실행이나 바인딩이 소비되지 않습니다. 대기 중인 작업 용량과 실행
슬롯은 별도의 한도로 유지됩니다.

새 저장소는 스키마 15를 사용합니다. 기존 저장소는 메시지에 대해 명시적인 오프라인
마이그레이션이 필요합니다. 5→6 단계는 P2 행(row)을 보존하며 검증된 백업과
복구 저널을 생성합니다. 먼저 클라이언트/워커를 중지하고 알 수 없는 실행을
해결하세요. 중단된 경우 `migrate --offline`을 반복하세요. [마이그레이션](tasks.md#schema-and-migration)을
참고하세요.
