# 실시간 에이전트 모니터

`monitor`는 현재 호스트의 claude-control 상태를 읽기 전용으로 보여줍니다.
작업을 시작하거나 다음 단계로 진행시키지 않으며, 재시도·중단·승인·apply도
수행하지 않습니다.

## TUI 실행

```bash
claude_control monitor tui
claude_control monitor tui --refresh-seconds 1 --history 250
```

기본 갱신 주기는 0.5초이며 0.1–60초로 설정할 수 있습니다. 기록 범위는
1–1000개 run입니다.

| 키 | 동작 |
|---|---|
| `1`, `2`, `3` | Graph, Agents, History 화면 선택 |
| `Tab` | 다음 화면 |
| `↑`, `↓`, `j`, `k` | 한 줄 이동 |
| `PageUp`, `PageDown` | 한 화면 이동 |
| `a` | Graph에서 진행·주의 대상만 보기/전체 보기 |
| `r` | 즉시 새로 읽기 |
| `q` | 종료 |

Graph 화면은 composition → workflow/workspace → task → agent 관계와 task
dependency를 표시합니다. Agents 화면은 역할, 요청 모델, 실제 응답 모델,
현재 작업과 상태를 보여줍니다. History 화면은 최근 run의 상태, 토큰, 시간,
비용을 표시합니다.

## 토큰과 비용의 의미

- 실행 중 `~1.2k`처럼 `~`가 붙은 수치는 Claude stream의
  `estimated_tokens`입니다. 응답 생성 도중 바뀌는 추정치입니다.
- 완료된 run은 provider가 반환한 input, cache-read, cache-creation, output,
  thinking token을 사용합니다.
- 비용은 provider가 `total_cost_usd`를 반환했을 때만 표시합니다. 컨트롤러가
  가격표로 추정하지 않습니다.
- schema 11 이전에 끝난 run은 원장 telemetry가 `null`입니다. 보존된 stream에
  완료 usage가 있으면 History에서 읽을 수 있지만 원장 누계에는 소급해 넣지 않습니다.

## JSON snapshot

```bash
claude_control monitor snapshot --history 100
claude_control monitor snapshot --history 100 --no-live
```

`snapshot`은 `claude-control.monitor.v1` JSON을 반환합니다. `nodes`와 `edges`는
그래프, `agents`는 현재 세션, `runs`는 최신순 기록, `summary.telemetry`는
append-only 원장에 기록된 확정 누계입니다. `--no-live`는 실행 stream을 읽지
않고 SQLite 원장만 조회합니다.

