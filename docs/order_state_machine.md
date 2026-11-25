# Order State Machine (명세 v3.7.5 7장)

활성 6종: `CREATED`, `SUBMITTING`, `PENDING_CONFIRMATION`, `ACCEPTED`, `PARTIALLY_FILLED`, `CANCEL_PENDING`.
종결 5종: `FILLED`, `CANCELED`, `REJECTED`, `CONFIRMED_NOT_ACCEPTED`, `EXPIRED`. 종결 상태의 `status` 는 불변이며
DB 트리거(`trg_terminal_immutable`)가 변경을 거부합니다. 단, 늦게 반영된 체결 보정(`LATE_FILL_CORRECTION`)은
체결 수량 증가 + 잔량 0 의 형태로만 허용하고, 체결 감소·잔량 증가는 거부합니다. 재전송은 종결된 행을 되살리지 않고
**같은 의도의 새 `orders` 행**(새 `attempt_id`)을 만드는 것입니다.

## Write-ahead 규칙

`SUBMITTING`(취소 요청은 `CANCEL_PENDING`)과 `request_payload_hash` 를 DB 에 **커밋한 뒤에만** HTTP 요청을 보냅니다.
커밋은 `PRAGMA synchronous=FULL` 로 디스크 확정까지 기다립니다(v3.7.5: 정전 시 기록이 사라지지 않게).
그래도 DB 에 없는 KIS 당일 주문(고아 주문)이 발견되면 계좌 전체 신규 주문을 막고 취소만 요청합니다(재전송 금지).
따라서 `CREATED` 로 남은 주문은 전송된 적이 없음이 보장되며 `boot_recovery` 에서 `EXPIRED` 로 닫습니다.
`SUBMITTING` 으로 남은 주문(크래시)은 `PENDING_CONFIRMATION` 으로 전이합니다.

## 허용 전이표 (표에 없는 전이는 금지)

| 현재 | 다음 | 조건 |
| --- | --- | --- |
| CREATED | SUBMITTING | 공통 게이트 통과 후 write-ahead 커밋 |
| CREATED | EXPIRED | 전송 전 게이트 실패·`boot_recovery` 발견 |
| SUBMITTING | ACCEPTED / REJECTED | 응답 `rt_cd = 0` + 주문번호 / 명시적 거부 |
| SUBMITTING | PENDING_CONFIRMATION | 타임아웃·단절·응답 해석 불가·크래시 |
| PENDING_CONFIRMATION | ACCEPTED / PARTIALLY_FILLED / FILLED | 조회·체결통보에서 매칭 주문 발견 |
| PENDING_CONFIRMATION | REJECTED / CANCELED | 거부 또는 이미 취소된 주문으로 발견 |
| PENDING_CONFIRMATION | CONFIRMED_NOT_ACCEPTED | 아래 "미접수 확정 규칙" |
| ACCEPTED | PARTIALLY_FILLED / FILLED / CANCEL_PENDING / REJECTED / EXPIRED | 체결 증가·취소 요청·거부 통보·장 마감 후 소멸 |
| PARTIALLY_FILLED | FILLED / CANCEL_PENDING / EXPIRED | 잔량 0·취소 요청·장 마감 후 소멸 |
| CANCEL_PENDING | CANCELED | 취소 확인 + 연속 2회 일치 조회 또는 장 마감 후 완전 조회 |
| CANCEL_PENDING | FILLED / ACCEPTED / PARTIALLY_FILLED / EXPIRED | 전량 체결 / 취소 거부 / 장 마감 후 소멸 |

`PENDING_CONFIRMATION → PENDING_CONFIRMATION` 은 상태가 바뀌지 않는 "재조회 기록"입니다.

## 미접수 확정 규칙 (`resolve_pending_confirmation`)

연속 미발견 횟수만으로는 확정하지 않습니다.
1. **장 마감 후**: 주문·체결 조회를 모든 페이지 완전 수집해 매칭 주문이 없으면 `CONFIRMED_NOT_ACCEPTED`(모든 방향).
2. **장중, SELL 이면서 매도 가능 수량 전량인 의도만**: 120초 경과 + 체결통보 WebSocket 연속 연결 + 접수 통보 없음 +
   30초 이상 간격 연속 3회 미발견. 일부 수량 매도와 모든 BUY 는 장중 확정하지 않습니다(중복 주문을 브로커가 막아 주지 않음).
3. 증명이 불가능하면 `PENDING_CONFIRMATION` 을 유지하고 `CRITICAL` 경보를 보냅니다.

### 불확실 주문의 영향 범위 (v3.7.5 D3)

`PENDING_CONFIRMATION` 은 `SAFE_HALT` 를 만들지 않습니다. 매수 주문이 불확실하면 **계좌의 신규 매수**만, 매도 주문이 불확실하면 **그 티커의 신규 주문**만 대기합니다. 다른 티커의 위험 축소 매도는 KIS 매도가능수량 이내에서 계속 허용됩니다. 어떤 주문이 있는지 모르는 고아 주문은 `BOOT_RECOVERY_FAILED` 로 계좌 전체를 멈춥니다. 불확실 주문은 어떤 경우에도 재전송하지 않습니다.

## 체결 기록과 지연 체결 보정

REST 조회는 주문 단위 누적값만 주므로 `trade_fills` 는 **누적 체결 수량의 증분**으로 기록합니다
(`kis_execution_id = "{주문번호}:{누적수량}"`, WS 통보는 `"{주문번호}:WS:{누적수량}"`; 같은 누적 수량이면 병합).
누적 수량이 줄면 `ACCOUNT_MISMATCH` 로 처리합니다. 종결 후 늦게 반영된 체결은 `LATE_FILL_CORRECTION` 이벤트로
`filled_qty`·`trade_fills` 만 보정하며 `status` 는 바뀌지 않습니다(수동 해제가 필요한 정지를 만들지 않음).

## 의도 상태 매핑

`CREATED`(attempt 없음) → `IN_PROGRESS`(활성 attempt) → `COMPLETED`(누적 체결 = 요청) / `CLOSED`(모두 종결·재시도 불가) / `CONFLICT`(운영자 폐기).
같은 의도의 활성 attempt 는 partial UNIQUE index(`ux_orders_one_active_attempt`) 하나로 1건만 허용합니다(v3.7.5: 정정 체인 삭제로 인덱스 1개).

## 취소·마감·매도 의무

`15:14:30` 에 주문번호가 있는 모든 활성 주문의 취소를 요청합니다(주문번호가 없는 `PENDING_CONFIRMATION` 은 마감 후 확정).
취소 요청은 취소 확정이 아닙니다. 미해결 매도 잔량은 `exit_obligations`(`OPEN`, 티커당 1건)로 보존하고 익일 `09:10` 검문소에서
계좌·주문·호가·조정계수가 모두 유효할 때만 `EXIT_OBLIGATION_RETRY` 의도를 만듭니다. 수량이 바뀌면 `SUPERSEDED`,
보유가 0이면 `VOIDED`, 체결로 해소되면 `RESOLVED` 입니다. `required_qty` 는 생성 후 바꿀 수 없고(DB 트리거),
의무의 체결 수량은 `order_intents.obligation_id` 로 연결된 `trade_fills` 합계입니다(같은 트랜잭션에서 갱신).
대체는 항상 기존 `UPDATE`(SUPERSEDED) → 새 `INSERT`(OPEN) → 기존 `superseded_by` 순서이며, 연결된 활성 주문이 있으면 대체하지 않습니다.

## 위험 축소 매도 가격 (`calculate_limit_price(reduce_only=True)`)

v3.7.5 에서 비상 재가격(정정 체인)을 삭제했습니다(2회차 정정부터 DB 부분 UNIQUE 인덱스와 충돌해 동작할 수 없었음).
위험 축소 매도는 유효 `Bid1`(호가 단위 내림)으로 **1회만** 제출하며, `iNAV` 가 유효하고 `Bid1 < iNAV × (1 − 2%)` 이면
`MarketAnomalyError`(이상 시장)로 당일 제출하지 않습니다. 미체결 잔량은 15:14:30 취소 후 `exit_obligations` 로 넘기고
익일 09:10 검문소가 같은 방식으로 1회 재주문합니다. 시장가로 전환하지 않습니다.
ETF 호가 단위: 2,000원 미만 1원, 이상 5원 `[UNVERIFIED]`.

## KIS 주문 계약

TR ID 는 환경별 표에서만 선택하며(`_kis_order_adapter.TR_IDS`) 접두사 변환을 하지 않습니다. 본문은 지정가(`ORD_DVSN="00"`)만,
`ORD_UNPR` 은 양의 정수만 허용합니다. POST 는 자동 재시도하지 않고 모호한 실패는 `TimeoutError` → `PENDING_CONFIRMATION` 입니다.
