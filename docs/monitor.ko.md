# 실시간 에이전트 모니터

`monitor`는 현재 호스트의 claude-control 상태를 읽기 전용으로 보여줍니다.
작업을 시작하거나 다음 단계로 진행시키지 않으며, 재시도·중단·승인·apply도
수행하지 않습니다.
Linux에서는 curses, Windows 10/11에서는 ANSI/VT console backend를 사용합니다.

## TUI 실행

```bash
claude_control monitor tui
claude_control monitor tui --refresh-seconds 1 --limits-refresh-seconds 60 --history 250
```

에이전트 화면은 기본 0.5초마다 갱신되며 0.1–60초로 설정할 수 있습니다.
계정 한도는 provider API를 과도하게 호출하지 않도록 별도로 60초마다 갱신되며
30–3600초로 설정할 수 있습니다. 기록 범위는 1–1000개 run입니다.

| 키 | 동작 |
|---|---|
| `1`, `2`, `3`, `4` | Graph, Agents, History, Limits 화면 선택 |
| `Tab` | 다음 화면 |
| `↑`, `↓`, `j`, `k` | 한 줄 이동 |
| `PageUp`, `PageDown` | 한 화면 이동 |
| `a` | Graph에서 진행·주의 대상만 보기/전체 보기 |
| `r` | 즉시 새로 읽기 |
| `q` | 종료 |

Graph 화면은 composition → workflow/workspace → task → agent 관계와 task
dependency를 표시합니다. Agents 화면은 역할, 요청 모델, 실제 응답 모델,
현재 작업과 상태를 보여줍니다. History 화면은 최근 run의 상태, 토큰, 시간,
비용을 표시합니다. Limits 화면은 이 호스트에 로그인된 Codex, Claude Code,
Gemini CLI, Cursor CLI의 기간별 사용률, 남은 비율, 초기화 시각, provider가
공개한 토큰·요청 수·초과 지출을 보여줍니다.

## 계정 한도와 남은 토큰

```bash
claude_control monitor limits
claude_control monitor snapshot --limits
claude_control monitor limits --local-only
```

각 provider가 실제로 공개한 단위를 그대로 보존합니다.

| Provider | 한도 출처 | 절대 토큰 수 |
|---|---|---|
| Codex | `account/rateLimits/read`의 기간별 사용률과 초기화 시각 | `account/usage/read`의 오늘·누적 사용 토큰을 별도로 표시. 구독 토큰 상한은 미공개라 남은 토큰 수는 계산하지 않음 |
| Claude Code | `~/.claude.json`의 service-reported usage cache | 5시간·주간 사용률은 정확하지만 토큰 상한이 없어 남은 토큰 수는 계산하지 않음. claude-control이 실행한 토큰 누계는 별도 표시 |
| Gemini CLI | Google Code Assist의 model별 quota bucket | 응답이 `remainingAmount`와 `tokenType`을 주면 사용·남은·전체 수를 표시. `REQUESTS`면 요청 수로 표시 |
| Cursor | Cursor dashboard의 결제 주기 사용률 | 절대 토큰 상한은 미공개. 포함 사용률과 초과 지출만 표시 |

따라서 `75% 남음`을 임의의 토큰 수로 환산하지 않습니다. provider가 절대
상한을 공개하지 않은 행에는 `absolute tokens unavailable`이 표시됩니다.
표시되는 인증 정보는 설치·로그인 여부뿐이며 access token, account ID, email,
project ID는 JSON과 화면에 포함되지 않습니다. Codex는 자체 app-server를,
Cursor와 Gemini는 각 CLI가 저장한 자격 증명을 해당 provider 호스트에만
전송합니다. Claude cache 조회와 `--local-only`는 네트워크를 사용하지 않습니다.

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
claude_control monitor snapshot --history 100 --limits
```

`snapshot`은 `claude-control.monitor.v1` JSON을 반환합니다. `nodes`와 `edges`는
그래프, `agents`는 현재 세션, `runs`는 최신순 기록, `summary.telemetry`는
append-only 원장에 기록된 확정 누계입니다. `--no-live`는 실행 stream을 읽지
않고 SQLite 원장만 조회합니다.
