# 계획, 편집 및 고정 검토 구성(compositions)

0.9 버전은 기존 P4 워크플로와 P5 워크스페이스 엔진 위에 하나의 유한한(finite)
구성(composition)을 추가합니다:

```text
경계가 정해진 계획 + 독립적인 Fable 비평
  -> 계획에 대한 Codex의 정확한 수락
  -> 통제된 편집자(editor) 워크스페이스와 지정된 검사
  -> 고정된(frozen) 편집자 익스포트
  -> 그 익스포트로부터 만들어진 독립적인 읽기 전용 Fable 워크스페이스
  -> 편집자 결과에 대한 Codex의 정확한 수락
```

조정자(coordinator)는 결과를 수락하거나, 실패했거나 알 수 없는 실행을
재시도하거나, 세션을 교체하거나, 브랜치를 병합하거나, 푸시하거나, Codex를
깨우지 않습니다. 각 `composition run` 호출은 명시적이며 범위가 한정되어 있습니다.
기존 `workflow`와 `workspace` 명령은 독립적인 동작을 그대로 유지합니다.

## 생성과 실행

계획(planning) 할당, 편집자 할당, 편집자 워크스페이스 정책, 검토자 할당 이렇게
네 개의 JSON 파일을 준비하세요. 검토자 정책은 편집자 정책에서 파생되며, 동일한
읽기 가능 경로(readable paths)를 가지되 쓰기 가능 경로와 검사(checks)는 없고
verifier 역할을 사용합니다. 검토자 할당은 반드시 Fable을 사용해야 하며 역할은
`critic` 또는 `verifier`여야 합니다.

```bash
claude_control composition create \
  --planning-assignment-file /absolute/path/plan.json \
  --editor-assignment-file /absolute/path/editor.json \
  --editor-policy-file /absolute/path/editor-policy.json \
  --reviewer-assignment-file /absolute/path/reviewer.json \
  --repo /absolute/path/repository --ref HEAD \
  --operation-id feature-composition-001 \
  --max-revisions 2 --max-calls 6 \
  --dispatch-window-seconds 900 --reviewer-effort high

claude_control composition run --composition <uuid> --once
claude_control composition run --composition <uuid> \
  --until-idle --max-seconds 60
claude_control composition status --composition <uuid>
```

생성 과정은 요청된 Git ref를 하나의 불변(immutable) 커밋으로 해석하고, 구성과
P4 워크플로를 기록하며, 모델을 호출하지 않습니다. 이후 저장소가 이동하더라도
편집자 스냅샷을 다시 타겟팅할 수 없습니다. 계획 워크플로는
`approve_recommended`에서 멈춥니다. 정확한 워커 작업, 리비전, 실행, 결과
digest를 확인한 뒤 `task accept`를 기록하세요. 이후의 경계 지정 구성 실행(bounded
composition run)은 그 검증된 계획 보고서와 출처(provenance)를 편집자 할당에
복사한 다음 편집자 워크스페이스를 생성합니다.

편집자가 최종 익스포트를 고정(freeze)하면, 다음 구성 단계는 그 고정된 트리로부터
별도의 검토자 워크스페이스를 자동으로 생성합니다. 이는 라이브 저장소나 변경 가능한
편집자 트리를 절대 읽지 않습니다. 검토자는 편집자의 각 기준에 대한 구조화
verdict와 `approve`, `revise`, `blocked` 권고를 반환합니다. 편집자 결과를 직접
승인하거나 변경할 수는 없지만, `revise`와 `blocked`는 편집자 승인을 거부합니다.

두 익스포트가 모두 존재하고 권고가 approve이면 상태는
`awaiting_codex/final_review_ready`를 보고합니다. 다른 권고는
`final_review_revise` 또는 `final_review_blocked`로 표시되며 승인 gate가 닫힙니다.
계획, 편집자 익스포트, 검토자 익스포트를 확인한 뒤, 정확한 편집자
작업 리비전, 실행, 결과 digest에 대해 `task accept`를 사용하세요. 구성 상태는
그 정확한 원장(ledger) 기록과 온전한 고정 증거로부터만 `accepted`를 도출합니다.

## 실패, 중지, 복구

형식이 잘못되었거나 차단된 보고서, 소진된 예산, 변경된 컨텍스트, 모델 불일치,
알 수 없는 실행, 중단된 워크스페이스 생성은 구성을 주의(attention) 상태에서
멈추게 합니다. 다시 실행한다고 해서 그 작업이 복구, 재시도, 대체되지 않습니다.
기존 run 또는 workspace 진단 도구를 사용하여 식별된 불확실한 하위 항목만
정리하세요.

```bash
claude_control composition stop --composition <uuid> --operation-id stop-001
```

중지는 현재 활성화된 워크플로 또는 워크스페이스에 위임되며, 모든 계획, 영수증,
스냅샷, 익스포트를 보존합니다. 동일한 중지 작업을 반복해도
멱등적(idempotent)으로 동작합니다.

## 출처(provenance)와 호환성

스키마 10은 구성(composition), 멤버(member), 정확한 결과(exact-result) 테이블만
추가합니다. 이는 기존 워크플로, 워크스페이스, 작업, 결정(decision), 세션, 실행
행(row)을 다시 작성하지 않습니다. 각 단계는 작업, 리비전, 실행, 결과 digest를
고정합니다. 워크스페이스 단계는 고정된 매니페스트와 트리 digest도 함께
고정합니다. 유휴 상태의 스키마 9 저장소는 일반적인 검증된 `migrate --offline`
절차로 업그레이드하세요.
