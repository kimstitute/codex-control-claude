# 실시간 에이전트 모니터

`monitor`의 관측·재생 경로는 현재 호스트의 claude-control 상태를 읽기 전용으로
보여주며 작업 시작, 재시도·중단·승인·apply를 수행하지 않습니다. Local Detail에서
명시적으로 ATTACH나 SHELL을 선택하면 viewer를 잠시 벗어나 사용자가 직접 운영하는
별도 terminal로 전환됩니다. 두 화면을 제공합니다.

- `monitor viewer`: Rust/Ratatui 기반 spatial graph와 과거 재생 화면
- `monitor tui`: Python 표준 라이브러리 기반 fallback과 provider quota 화면

## Graph/replay viewer 실행

```bash
claude_control monitor viewer
claude_control monitor viewer --inspect
claude_control monitor viewer --tree
tmux new -s claude-control-viewer 'claude_control monitor viewer'
```

기본 viewer는 계속 content-free Safe Observer입니다. 이 호스트의 로컬 전사까지
열람하려면 명시적으로 Local Detail을 켭니다.

```bash
claude_control monitor viewer --local-detail       # keyboard/mouse picker
claude_control monitor viewer --detail-current     # 현재 directory
claude_control monitor viewer --detail-dir /absolute/project
claude_control monitor viewer --detail-file /absolute/transcript.jsonl
claude_control monitor viewer --detail-id <provider-session-id>
claude_control monitor local sessions --source all
claude_control monitor viewer --detail-source '<selector>'
```

Local Detail은 관리 run artifact와 기본 `~/.claude/projects`, `~/.codex/sessions`
아래 JSONL만 bounded scan합니다. `--claude-root`와 `--codex-root`로 대체 root를
지정할 수 있습니다. picker를 `Esc`로 닫으면 전사를 열지 않고 Safe Observer로 진행합니다.
`monitor local stream`은 prompt와 응답을 JSONL stdout으로 그대로 내보내므로 민감한
로컬 출력으로 취급해야 합니다. 파일로 저장하거나 다른 프로세스에 전달할 때는 사용자가
명시적으로 선택해야 합니다.

viewer는 controller, composition, workflow, workspace, task와 Claude session을
카드와 방향 관계로 표시합니다. session 카드에는 역할, 실제/요청 모델, 현재 상태,
실행 수와 확정 output token을 집계합니다. run마다 카드를 무한히 늘리지 않으므로
장기간 원장도 agent graph의 크기에 가깝게 유지됩니다.
기본 `focus` scope는 최신 연결 작업만 따라갑니다. `a`를 누르면 `focus`, 제한된
`recent`, cursor 시점 전체인 `all` graph를 순환합니다.

viewer는 SSH와 tmux에서도 같은 의미가 유지되는 256색 팔레트를 사용합니다. 상위
Codex/Claude 프로세스가 기계 출력용 `NO_COLOR=1`을 설정해도 대화형 viewer는 금색
재생 구간, 녹색 live 경로, 주황색 대기, 빨간색 실패를 표시합니다. 단색 화면이
필요할 때만 `claude_control monitor viewer --no-color`를 사용하세요.

| 마우스 | 동작 |
|---|---|
| 카드 클릭 | 선택하고 30/70 Safe Inspector 열기 |
| 카드 드래그 | controller 상태를 바꾸지 않고 카드 위치 이동 |
| 빈 canvas 드래그 | graph를 manual camera로 이동 |
| canvas 위 휠 | pointer 중심 확대/축소 |
| inspector 위 휠 | 상세 이력을 세 줄씩 스크롤 |
| timeline 클릭·드래그 | event cursor scrub |
| `PLAY`, `LIVE` 클릭 | 과거 재생 전환 / 최신 cursor 복귀 |
| `FOCUS`, `RECENT`, `ALL` 클릭 | 다음 graph scope로 순환 |
| `ERA` 이전·다음 클릭 | 이전·다음 run 시작 구간으로 이동 |
| 속도 chip 클릭 | 0.25× → 0.5× → 1× → 2× → 4× → 8× 순환 |
| `GAP` chip 클릭 | 고정 간격과 기록 시각 기반 압축 재생 전환 |
| `M:*` chip 클릭 | all, prompt, tool, failure, agent marker filter 순환 |
| Inspector tab 클릭 | Overview, Provenance, Tools, Activity, Terminal 전환 |
| `ATTACH` 클릭 | 선택한 native background agent terminal에 단독 조작자로 연결 |
| `SHELL` 클릭 | 선택 소스의 허용된 project에 별도 사용자 shell 열기 |
| 우클릭 | inspector 닫기 |

| 키 | 동작 |
|---|---|
| `[`, `]` | 과거 이벤트를 1개 이동 |
| `{`, `}` | 이전·다음 run 시작 구간으로 이동 |
| `p`, `P` | 이전·다음 prompt 구간으로 이동 |
| `/`, `n`, `N` | 검색 입력, 다음 결과, 이전 결과 |
| `m`, `v` | marker filter와 Inspector tab 순환 |
| `,`, `.` | 재생 속도를 0.25×–8× 범위에서 낮추거나 높이기 |
| `z` | 고정 간격(`GAP OFF`)과 시각 간격 압축(`GAP ON`) 전환 |
| `Home`, `End` | baseline 또는 최신 live cursor로 이동 |
| `Space` | 선택한 cursor부터 재생/일시정지 |
| `PageUp`, `PageDown` | 열린 inspector를 한 화면씩 스크롤 |
| `Tab`, `Shift-Tab`, 방향키 | 다음·이전·공간상 인접 카드 선택 |
| `Enter` | 선택이 없을 때 첫 카드 선택 |
| `o`, `f`, `r`, `c` | overview, 최신 작업 follow, 재배치, 선택 카드 중앙 정렬 |
| `a` | `focus` → `recent` → `all` graph scope 순환 |
| `HJKL`, `+`, `-`, `0` | manual pan, zoom, zoom 초기화 |
| `t`, `s` | 선택한 background agent에 attach / 별도 project shell 열기 |
| `x`, `i`, `?`, `q` | mouse capture 전환, session 정보, 도움말, 종료 |

viewer는 `focus` scope의 fitted overview로 시작합니다. Rataflow가 node scratch-buffer
clipping, step edge routing, semantic zoom, viewport interaction과 minimap을 한 좌표계에서
처리합니다. 상태만 바뀌면 기존 위치를 유지하고 `r`을 눌렀을 때만 전체를 다시
배치합니다. 카드를 선택하면 Safe Inspector가 열리고 연결된 run의 실제 모델,
effort, 시작·종료 시각, provider token·비용과 controller operation 상세 이력을
보여줍니다. 활성 작업은 새 행을 자동으로 따라가며, 수동으로 위로 스크롤하면 그
위치를 유지합니다. 카드의 `R/W/P/C` chip은 각각 read, write, patch, named check
횟수이고 열린 operation 수도 따로 표시됩니다. timeline은 event marker, 가중 2행
activity와 playhead를 분리하며 순서는 시각이 아니라 단조 증가 observation cursor로
결정됩니다.

Inspector와 operation projection은 content-free입니다. 프로젝트 경로, 명령 argv,
prompt, reasoning, request/response body, 결과, hash와 error text를 표시하거나 AG-UI
관측 이벤트에 추가하지 않습니다. operation kind도 `read`, `write`, `patch`,
`named_check`, `unknown`의 닫힌 집합으로 정규화합니다.

Local Detail을 켠 경우 Inspector는 **Overview**, **Provenance**, **Tools**,
**Activity**, **Terminal** 탭으로 나뉩니다. 앞의 네 탭은 safe run/operation 정보,
bounded prompt와 response/reasoning, tool 상태·duration·요약,
prompt/tool/spawn/failure 시각 이력을 보여줍니다. Terminal 탭은 `terminal start`로 만든
native background session의 bounded 최근 화면, 상태와 attach 가능 여부를 표시합니다.
`partial / bounded`는 malformed row 또는 byte/record 한도로 과거 일부가
생략됐다는 뜻입니다. 이 내용은 메모리에서만 합성하며 SQLite observation 원장,
AG-UI, OTLP, `--inspect`, `--tree`에 기록하지 않습니다.
Claude assistant row의 `(requestId, message.id)`가 같은 streaming block은 하나의
응답으로 취급합니다. input, cache creation/read, output, thinking token은 응답의 최신
확정값으로 집계하며 누락·충돌·bounded truncation이 있으면 `incomplete`를 표시합니다.
transcript가 비용을 직접 제공하지 않으면 `unavailable`로 남기고 단가표로 추정하지 않습니다.
처음 선택할 때 transcript가 없던 `terminal:` source는 같은 UUID의 유일한 transcript가
생기면 viewer 재시작 없이 `claude:` source로 승격됩니다. session 카드 identity는 유지됩니다.
부모·자식 agent, prompt/응답, tool 상태는 기록 시각에 맞춰 재생됩니다. 선택한
cursor 뒤에 끝난 tool은 `pending`으로 보이며, transcript 침묵만으로 agent 완료를
추정하지 않습니다.

ATTACH는 viewer alternate screen을 잠시 닫고 Claude Code의 네이티브 화면을 그대로
연결합니다. 한 session에는 한 명의 로컬 조작자만 붙을 수 있으며 `Ctrl+Z`로 돌아오면
viewer가 복구됩니다. SHELL은 agent의 tool 권한을 늘리거나 명령을 주입하지 않고
허용된 project에서 별도 사용자 shell을 엽니다. 이 shell의 명령은 사용자의 직접 동작이며
controller workspace 격리와 승인 원장을 거치지 않습니다. 기존 headless `-p` run과
native background ID가 없는 interactive session은 관측할 수 있어도 attach할 수 없습니다.

headless `--inspect`는 기존 v1 contract를 유지하며 fidelity, cursor,
node/edge/agent 수와 확정 output token 합계를 JSON으로 출력합니다. schema 15에서
추가된 operation node는 이 기존 count에 넣지 않습니다. `--tree`는 controller부터
전체 composition/workflow/workspace/task/session, 화면 scope에서 숨은 run과 각 run의
operation 요약까지 결정적 ASCII tree로 출력합니다. 둘 다 TTY 없이 사용할 수 있고
content-free 필드만 포함합니다.

```bash
claude_control monitor agui stream > session.agui.jsonl
claude_control monitor viewer --stream-file session.agui.jsonl
claude_control monitor viewer --inspect --stream-file session.agui.jsonl
claude_control monitor viewer --tree --stream-file session.agui.jsonl
```

JSONL 파일은 content-free AG-UI event입니다. viewer는 SQLite를 직접 열지 않고,
실시간 모드에서 controller의 `monitor agui stream` child process 하나만 유지합니다.
cursor 중복은 무시하고 간격이 생기면 잘못된 과거 상태를 꾸미지 않고 중단합니다.

### Viewer 설치와 빌드

릴리스 바이너리 또는 직접 빌드한 바이너리의 SHA-256을 확인해 설치기에 함께 줍니다.
설치기는 네트워크에서 바이너리를 받지 않으며 hash 불일치 시 중단합니다.

```bash
cargo build --locked --release --manifest-path viewer/Cargo.toml
sha256sum viewer/target/release/ccc-viewer
python3 install.py --update \
  --viewer viewer/target/release/ccc-viewer \
  --viewer-sha256 <위에서 확인한 SHA-256>
```

Windows에서는 `ccc-viewer.exe`와 `Get-FileHash -Algorithm SHA256`을 사용합니다.
viewer가 번들되지 않았으면 `monitor viewer`는 `viewer_unavailable`로 실패하고 기존
`monitor tui`는 계속 사용할 수 있습니다. update는 이전 `viewer-manifest.json`과
바이너리가 모두 일치할 때만 기존 viewer를 보존합니다.

viewer는 [Zoetrope](https://github.com/furkankly/zoetrope)의 spatial graph,
camera와 timeline이라는 제품 방식을 참고했습니다. Zoetrope 소스와 asset은 포함하지
않습니다. 공개 MIT [Rataflow](https://github.com/furkankly/rataflow) crate를 사용하며
고지는 [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md)에 보존했습니다. event fold,
Claude Control projection, scope, card, timeline과 launcher는 이 저장소에서 구현했습니다.

## Python TUI 실행

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
| `1`, `2`, `3`, `4`, `5` | Graph, Agents, History, Limits, Replay 화면 선택 |
| `Tab` | 다음 화면 |
| `↑`, `↓`, `j`, `k` | 한 줄 이동 |
| `PageUp`, `PageDown` | 한 화면 이동 |
| `a` | Graph에서 진행·주의 대상만 보기/전체 보기 |
| `←`, `→` | Replay cursor를 이벤트 한 개만큼 이동 |
| `[`, `]` | Replay cursor를 이벤트 열 개만큼 이동 |
| `Home`, `End` | 첫 이벤트로 이동하거나 최신 기록을 실시간 추적 |
| `Space`, `p` | TUI 갱신 주기로 과거 기록 재생/일시정지 |
| `r` | 즉시 새로 읽기 |
| `q` | 종료 |

Graph 화면은 composition → workflow/workspace → task → agent 관계와 task
dependency를 표시합니다. Agents 화면은 역할, 요청 모델, 실제 응답 모델,
현재 작업과 상태를 보여줍니다. History 화면은 최근 run의 상태, 토큰, 시간,
비용을 표시합니다. Limits 화면은 이 호스트에 로그인된 Codex, Claude Code,
Gemini CLI, Cursor CLI의 기간별 사용률, 남은 비율, 초기화 시각, provider가
공개한 토큰·요청 수·초과 지출을 보여줍니다.
Replay 화면은 영속 cursor 시점의 그래프를 복원합니다. `LIVE`는 최신 이벤트를
따라가고, `PAUSED`는 선택한 cursor를 유지하며, `PLAYING`은 기록의 상한에
도달할 때까지 갱신 주기마다 이벤트 한 개씩 진행합니다.

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

## 영속 관측 기록과 과거 재생

Schema 14부터 append-only `claude-control.observation.v1` 이벤트 원장을
사용합니다. Schema 15는 controller operation의 시작·종료 메타데이터를 같은 원장에
추가합니다. 이 원장은 그래프 뷰어와 과거 시점 재생의 기준입니다. 시각은
설명용 정보이고, 실제 순서는 단조 증가 cursor로 결정합니다.

```bash
claude_control monitor events --after 0 --limit 100
claude_control monitor events --after 100 --limit 100 --through 500
claude_control monitor replay
claude_control monitor replay --through 500
claude_control monitor agui snapshot --through 500
claude_control monitor agui events --after 100 --limit 100 --through 500
```

`events`의 `--after`는 배타 cursor이며 `--through`는 선택적인 포괄 상한입니다.
`replay`는 baseline과 이후 이벤트를 접어 해당 포괄 cursor 시점의 그래프를
복원합니다. 기존 저장소를 schema 14 이상으로 이관할 때 이관 전 상태 변화는 복구할
수 없으므로 fidelity가 `baseline_only`이며, baseline 이후 변화는 정확히 기록됩니다.
schema 14→15 이관은 기존 request마다 최종 operation 요약 하나만 만들며 존재하지
않았던 시작·종료 전환을 꾸며내지 않습니다.

관측 계약에는 안정적인 식별자, 생명주기 상태, 관계, 시각, 숫자 telemetry만
들어갑니다. prompt, result, message 본문, policy, 프로젝트 경로, 작업 receipt,
계정 식별 정보, 자격 증명은 제외합니다. Provider 원본 JSONL은 controller 내부
증거이며 관측 export에 포함되지 않습니다.

## OpenTelemetry와 AG-UI 연동

```bash
claude_control monitor export --format otlp-json
claude_control monitor export --format otlp-json --through 500
claude_control monitor export --format otlp-json --metadata
```

기본 export는 최상위에 `resourceSpans`만 있는 OTLP/HTTP JSON
`ExportTraceServiceRequest`입니다. 종료된 Claude run마다 INTERNAL
`invoke_agent` span을 만들고 OpenTelemetry GenAI와 OpenInference 속성을 함께
기록합니다. trace/span ID는 결정적이며, provider의 token/cache/reasoning 의미를
보존합니다. 진행 중이거나 종료 시각이 없는 run은 값을 꾸며내지 않고 생략합니다.
`--metadata`는 표준 문서에 로컬 profile과 생략 내역을 명시적으로 감싼 형태이므로,
OTLP collector로 보낼 때는 기본 형식을 사용하세요.

OpenTelemetry GenAI agent semantic convention은 현재 Development 상태입니다.
따라서 export는 고정한 로컬 profile
`claude-control.otel-genai-openinference.v1-development`을 기록하며 안정화된 표준
버전을 따르는 것처럼 표시하지 않습니다. Prompt와 completion 본문은 기본적으로
내보내지 않습니다.

Python adapter `claude_control.observation_agui`는 replay snapshot과 해석된 원장
이벤트를 AG-UI 1.0의 `STATE_SNAPSHOT`, `RUN_STARTED`, `RUN_FINISHED`,
`RUN_ERROR`, `CUSTOM` 이벤트로 바꿉니다. 그래프·과거 재생 화면의 실시간
경계이며, 영속 기록은 계속 SQLite와 observation cursor가 담당합니다.
`monitor agui snapshot`은 표준 이벤트 하나를 반환합니다. `monitor agui events`는
표준 이벤트 배열을 `after`, `through`, `next_cursor`, `has_more`가 있는 로컬
페이지 envelope에 담습니다. 소비자는 숨은 상태 없이 `next_cursor`부터 이어서
읽을 수 있습니다. 이 adapter는 text message, reasoning 본문, tool-call 본문
이벤트를 만들지 않습니다.
