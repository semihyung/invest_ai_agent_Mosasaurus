# 모사사우르스 명세 v3.7.4 정밀 감사 보고서

- 대상: `mosasaurus_spec_v3.7.4.md` (1,780줄)
- 일자: 2026-11-24
- 방법: 5개 관점별 서브에이전트가 병렬로 분석했고, 이를 종합한 뒤 Blocker 항목은 명세 원문을 직접 Grep해 재확인했다. 줄 번호는 v3.7.4 원문 기준이며 ±1줄 오차가 있을 수 있다.
- 범위: 명세서 감사만 했다. 일부 항목은 현재 `src/`·`migrations/`·`scripts/` 구현과도 대조했다. 실제 KIS API는 호출하지 않았다. KIS 관련 사실 중 확정하지 못한 것은 **"검증 필요"**로 표시했다.

---

## 0. 요약

| 심각도 | 건수 | 핵심 주제 |
|---|---|---|
| **Blocker** | 9 | 정정 체인 DB 제약 충돌, 종결 불변 트리거 ↔ 늦은 체결 보정, 매도 의무 CHECK 위반, SAFE_HALT 중 손절 전면 정지, 체결 중 대사 오탐, 늦은 현금흐름 분류로 CRITICAL 오발동, 파킹 시계열 결함 → 전량 매도, `Cash_KIS` 미정의(D+2), `synchronous=NORMAL` 정전 시 write-ahead 유실 |
| **Major** | 20 | 상태 전이 누락, 취소 재시도 부재, 호출 한도 고갈, 소액 계좌 현금 사장, 분배 회계, 토큰 갱신 여유, 배포 신뢰 경계, 롤백 데이터 손실, UPS 재기동, 텔레그램 탈취 |
| **Minor** | 소수 | 외부 취소 전이, SUPERSEDED 순서 명문화 등 |

**공통 근본 원인 세 가지**

1. **"fail-closed(고장 나면 멈춤)"가 "보호 장치까지 멈춤"이 된다.** SAFE_HALT, `/ca_confirm` 대기, `/classify` 대기, 감시 데몬 오탐이 모두 트레일링 스탑을 끈다. 그런데 해제 수단은 운영자 수동 조작뿐이다. 운영자가 군 복무로 연락이 어려운 상황에서 가장 위험한 설계다.
2. **명세 내부의 이중 정의.** 본문, DDL(§9.2), rules.yaml이 같은 규칙을 다르게 적은 곳이 10곳 이상이다. 정정 인덱스, 종결 불변, `required_qty`, 마감 시각, 시크릿 경로 등이 그렇다. 이 중 일부는 **이미 코드가 명세와 다르게 구현돼 있다.**
3. **KIS 계좌 실측이 없는 회계·대사.** 대사의 기준 필드, 허용 오차, 대사 시점 조건이 `[UNVERIFIED]` 상태다. 이 상태에서 오탐은 곧 수동 해제 대기로 이어진다.

---

## 1. Blocker

### [심각도: Blocker] [§7.1 L815, §7.2 L863, §9.2 L1057–1058] 비상 매도 정정 체인이 Partial UNIQUE Index에 막혀 재가격이 사실상 0~1회로 줄어든다
- **발생 시나리오:**
  1. 원주문 O를 정정해 R1을 만든다. 같은 트랜잭션에서 O를 `CANCEL_PENDING`으로 바꾸고 R1을 INSERT한다.
  2. 20초 뒤 2회차 재가격에서 R1을 `CANCEL_PENDING`으로 바꾸고 R2(`replaces_order_id`=R1)를 INSERT한다.
  3. R1과 R2가 모두 "정정 attempt이면서 활성 상태"이므로 `ux_orders_one_active_replacement`에서 **IntegrityError**가 난다.
  4. §7.1 L815의 단일 인덱스(활성 6종, 정정 여부 구분 없음)를 그대로 구현하면, 1회차 INSERT부터 거부된다.
  5. write-ahead(주문 전 DB 기록) 커밋이 실패하므로 정정 요청이 아예 전송되지 않는다.
- **현재 명세의 한계:**
  - L1058의 "직전 정정 attempt가 종결된 뒤에만 다음 재가격"은 지킬 수 없다. R1은 R2의 정정 확인이 와야 `CANCELED`가 되므로 순환 대기다.
  - 직전 attempt의 `CANCELED` 확정에 "15초 간격 2회 연속 일치"(L895) 규칙을 쓰면 30초 이상 걸린다. 재가격 간격은 20초라서 일정이 맞지 않는다.
  - 정정 중에는 직전 행과 새 행이 동시에 활성이라, 매도 가능 수량이 "보유 − 2×잔량"으로 계산된다. 그러면 수량 게이트가 정정 attempt 자신을 거부한다.
  - 결과: `max_reprices: 5`(L852)와 시나리오 57은 달성할 수 없다.
- **수정안 (둘 중 택1):**
  - **(A, 권장) Phase 1에서는 비상 재가격 삭제.** 기본값이 이미 `execution.emergency.enabled: false`(L1382)다. 위험 축소 매도는 Bid1에 1회 제출하고, 미체결분은 `exit_obligations`로 넘긴다. `replaces_order_id`, 인덱스 2개, 시나리오 57 후반부를 제거한다.
  - **(B) 재가격 유지 시 인덱스 재정의:**
    ```sql
    DROP INDEX ux_orders_one_active_attempt;
    DROP INDEX ux_orders_one_active_replacement;
    -- CANCEL_PENDING 을 제외한 "살아 있는" attempt 는 의도당 1건
    CREATE UNIQUE INDEX ux_orders_one_live_attempt ON orders(parent_intent_id)
      WHERE status IN ('CREATED','SUBMITTING','PENDING_CONFIRMATION','ACCEPTED','PARTIALLY_FILLED');
    ```
    - 의도당 `CANCEL_PENDING` 1건 제한은 코드에서 검증한다.
    - 매도 가능 수량 = `holding − Σremaining(live attempts)`로 정의한다(정정 대상 원행은 제외).
    - 정정 수락 후 원주문은 KIS ccld의 `orgn_odno` 매칭으로 즉시 `CANCELED` 처리한다. 2회 연속 일치 규칙은 순수 취소에만 적용한다.

### [심각도: Blocker] [§7.3 L872, §9.2 L1062] `trg_terminal_immutable` 트리거가 명세가 요구하는 `LATE_FILL_CORRECTION`을 ABORT한다
- **발생 시나리오:** 15:14:30 일괄 취소로 주문이 `CANCELED`가 된다. 60초 안에 KIS 조회에 직전 체결분이 늦게 반영되어 `filled_qty`를 보정하려 하면 트리거가 `RAISE(ABORT)`한다. 보정이 실패하면 보유 수량 대조가 불일치하고, `ACCOUNT_MISMATCH`(수동 해제 전용)가 걸려 무인 운용이 정지된다.
- **현재 명세의 한계:**
  - L872는 "`filled_qty`·`trade_fills`만 보정"을 허용하고, L1062는 "`status`·`filled_qty` 변경 ABORT"로 정면 충돌한다.
  - `migrations/003_spec_v374_schema.sql` L569–574는 이미 "증가만 허용"으로 **명세와 다르게** 구현돼 있다.
  - `filled_qty`가 늘 때 `remaining_qty`를 어떻게 처리할지 정의가 없어 `filled+remaining<=requested` CHECK에 걸릴 수 있다.
- **수정안:**
  ```sql
  CREATE TRIGGER trg_terminal_immutable BEFORE UPDATE ON orders
  WHEN OLD.status IN ('FILLED','CANCELED','REJECTED','CONFIRMED_NOT_ACCEPTED','EXPIRED')
  BEGIN
    SELECT RAISE(ABORT,'terminal order is immutable')
    WHERE NEW.status <> OLD.status
       OR NEW.filled_qty < OLD.filled_qty
       OR NEW.remaining_qty <> 0;
  END;
  ```
  늦은 체결 보정 시 `remaining_qty=0`으로 고정하고, 같은 트랜잭션에서 `order_events`(LATE_FILL_CORRECTION)를 반드시 기록하도록 명문화한다. 이 경로는 fault_injection 테스트로 고정한다.

### [심각도: Blocker] [§7.3 L924–925, §9.2 L1104] `exit_obligations.required_qty` 갱신이 CHECK를 위반하고 체결분을 이중 차감한다
- **발생 시나리오:**
  1. 의무 `required=100`이 생긴다.
  2. 익일 60주가 체결되어 `filled=60`이 된다.
  3. 마감 때 L924대로 `required_qty`를 "현재 매도 필요 수량" 40으로 갱신하면, `filled_qty <= required_qty`(60 ≤ 40)가 거짓이라 **IntegrityError**가 난다.
  4. 갱신이 통과하더라도 다음 날 `required − filled` = 40 − 60이 음수가 되어, 체결분을 두 번 뺀 셈이 된다.
- **현재 명세의 한계:**
  - L924는 "갱신"이라 하고 L925는 "`SUPERSEDED` 후 새 `OPEN`"이라 해서 서로 충돌한다.
  - 코드 `src/order_manager.py`의 `record_exit_obligation`은 수량이 다르면 거부하므로, 두 문장 어느 쪽과도 다르다.
  - 어떤 체결을 의무의 `filled_qty`로 셀지 FK가 없다(`cycle_id` 문자열 `X{id}`로만 연결).
- **수정안:** `required_qty`는 **불변**으로 둔다. 재계산은 항상 `SUPERSEDED` + 새 `OPEN`(required=잔량, filled=0)으로 처리한다.
  ```sql
  ALTER TABLE order_intents ADD COLUMN obligation_id TEXT REFERENCES exit_obligations(obligation_id);
  -- filled_qty 는 저장하지 않고 계산: SUM(trade_fills.qty) JOIN order_intents WHERE obligation_id = ?
  ```
  SUPERSEDED 처리 순서(기존 행 status UPDATE → 새 행 INSERT → 기존 행 superseded_by UPDATE)를 명세에 명문화한다. 티커당 `OPEN` 1건 유일 인덱스 때문에 이 순서만 가능하다.

### [심각도: Blocker] [§6.3 L704·L708·L719–721, §4.2 L393–397, §2.2 L192, §3.2 L268] 수동 해제 대기가 길어지면 보유 포지션의 손절이 몇 주씩 꺼진다
- **발생 시나리오:**
  - **배포 롤백:** 배포 롤백으로 `DEPLOY_ROLLBACK`이 걸린다. `allow_reduce_only_sell: false`(L1407) 때문에 트레일링 스탑과 120MA 매도가 모두 차단된다. 운영자가 훈련 중이면 `/clear_halt`를 보낼 수 없고, 이 동안 −30% 급락이 와도 포지션은 방치된다.
  - **일시 오류의 격상:** L708에 따르면 자동 해제 원인(`CREDENTIALS_UNAVAILABLE` 등)이 풀린 직후 대조가 KIS 5xx로 **한 번만** 실패해도 `ACCOUNT_MISMATCH`(수동 해제)로 격상된다.
  - **기업행위 대기:** `/ca_confirm` 대기 종목은 스탑과 120MA 판정이 무기한 보류된다(L396–397).
  - **현금흐름 분류 대기:** `/classify` 대기 중에는 낙폭 악화 전이가 동결된다(L677). 그래서 `CRITICAL→EMERGENCY`도 발동하지 않는다.
  - **감시 데몬:** `ups_monitor`나 `clock.json` 갱신이 멈추면 `UPS_LOW`·`CLOCK_UNSYNCED`로 SAFE_HALT가 걸리고, 역시 스탑이 꺼진다.
- **현재 명세의 한계:** "주문 상태가 불확실해서 멈추는 것(중복 주문 방지)"과 "보호성 청산(손절)"을 구분하지 않는다. 대기가 길어질 때 단계적으로 조치하는 규칙(에스컬레이션)도 없다.
- **수정안:**
  ```yaml
  risk:
    safe_halt:
      allow_reduce_only_sell: false           # 기본은 유지
      protective_exit:                        # 신설: 원인별 손절 예외
        allowed_reasons: [DEPLOY_ROLLBACK, ACCOUNT_MISMATCH, UPS_LOW, CONFIG_INVALID, CLOCK_UNSYNCED_MINOR]
        forbidden_reasons: [PENDING_CONFIRMATION, CREDENTIALS_UNAVAILABLE, DB_INTEGRITY]
        qty_source: KIS_ord_psbl_qty          # DB 대신 KIS 매도가능수량만 신뢰 → 과매도는 브로커가 차단
        peak_source: KIS_daily_candles        # DB peak 미사용, 일봉으로 재계산
        stop_pct: -0.10
  unattended:
    halt_unresolved_alert_every_hours: 24
    deploy_rollback_auto_clear: "boot_recovery 대조 2회 연속 일치 시"
    account_mismatch_auto_clear: "활성 주문 0건 상태에서 대조 3회 연속(≥5분 간격) 일치 시"
    reconcile_failure_vs_mismatch: "조회 실패는 ACCOUNT_DATA_STALE(매수만 차단), 수량 불일치 재현 시에만 ACCOUNT_MISMATCH"
    ca_unverified_stop: "raw_peak × min_cash_factor × (1 + stop_pct) 로 보수적 스탑 유지"
    unclassified_flow_dd: "미분류 유입은 0으로 간주(보수적)한 U로 낙폭 판정 지속"
  ```
  §0.6 진리표에 `SAFE_HALT(protective_exit 허용 원인)` 행을 추가한다.

### [심각도: Blocker] [§2.2 L195, §4.3 L410, §6.2 L677, §9.1 L1030, rules L1419–1422] 체결이 진행되는 중에 대사하면 오탐이 나고, 그 오탐이 수동 해제 대기로 고착된다
- **발생 시나리오:**
  - **수량 오탐:** 15:00–15:10 사이 부분체결이 계속 들어올 때, `trade_fills` 반영과 KIS 잔고·`prvs_rcdl_excc_amt` 반영 사이에 시차가 있다. 10초 주기 장중 대사에서 수량 차이가 잡히면 `ACCOUNT_MISMATCH`가 걸려 SAFE_HALT가 된다.
  - **현금 오탐:** 허용 오차가 10원이다. 그런데 KIS 수수료는 체결 건별로 원 미만을 절사하고 유관기관비가 포함되어 `fee_rate` 추정과 다르다. 분기마다 들어오는 예탁금 이용료도 있다. 그래서 10원 초과 차이가 상시 생기고 `UNCLASSIFIED`가 된다. 이후 `/classify`를 할 때까지 매수 차단과 HWM 동결이 이어진다.
  - **분류 수단 부족:** `/classify`의 선택지는 `DEPOSIT/WITHDRAWAL/DISTRIBUTION`뿐이다. 오탐을 무효화하는 값이 없고, 음수 차이는 CHECK(L1092) 때문에 `WITHDRAWAL`로만 분류할 수 있다. 그러면 단위 수(TWR 계산 단위)가 잘못 조정된다.
- **현재 명세의 한계:** 대사를 "주문·체결이 멈춘 상태(quiescent)"에서만 하라는 조건이 없다. 일시적 불일치를 확정 전 단계(PROVISIONAL)로 두거나 자동으로 없애는 경로도 없다. `cash_recon_learning_days: 14`(L1422)는 rules.yaml에만 있고 본문(L602, L677)과 충돌한다.
- **수정안:**
  ```
  recon_allowed = active_orders(account)==0 and now - last_fill_at >= late_fill_grace_seconds
  if not recon_allowed: skip
  diff 발견 → PROVISIONAL (매수만 차단)
  2회 연속(≥60s 간격) 재현 시에만 ACCOUNT_MISMATCH / UNCLASSIFIED 확정
  이후 대사가 차이를 설명하면 PROVISIONAL 자동 VOID
  ```
  ```yaml
  accounting:
    cash_tolerance_krw: "max(1000, 2 × 당일 체결건수)"   # 10 → 상향
    interest_credit_auto_class: true                     # 분기 이용료 일정과 일치하는 소액 유입 자동 분류
  ```
  `classification_status`에 `DISMISSED`를, `flow_type`에 `ADJUSTMENT`(단위 조정 없음, 부호 무관)를 추가한다.

### [심각도: Blocker] [§6.2 L667·L675·L677, rules L1422] 늦게 분류된 현금흐름이 오염된 단위가격으로 반영되어 CRITICAL이 거짓으로 발동한다
- **발생 시나리오 (학습기간 14일 이내):**
  1. 시작 상태는 V=3,000,000, N=30,000, U=100이다.
  2. 5일차에 2,000,000원이 입금되지만 UNCLASSIFIED로 남는다. L1422 학습기간에는 동결하지 않으므로 마감 U=166.67이 되고 HWM도 166.67로 오른다.
  3. 8일차에 `/classify DEPOSIT`을 하면 L667대로 "확정일 직전 스냅샷" U=166.67로 ΔN=12,000이 반영된다. N=42,000, U=119.05가 된다.
  4. DD=−28.6%이므로 **`EMERGENCY`가 자동 전환**된다.
- **발생 시나리오 (학습기간 이후, 동결이 정상 작동해도):**
  1. V=5,000,000에서 출금 1,000,000원이 미분류 상태라 스냅샷 U=80이 된다.
  2. 확정 시 ΔN=−12,500이 반영되어 U=106.67이 된다.
  3. **가짜 +6.67% 수익이 영구히 남고**, 동결이 풀리면 HWM이 106.67로 오른다.
  4. 입금은 자동 분류 경로가 없다(L195). 그래서 거의 모든 입금이 "늦은 확정" 경로를 탄다.
- **현재 명세의 한계:** L667이 쓰는 "직전 스냅샷"은 미분류 금액이 섞인 스냅샷이다. 또 L1422 학습기간 규칙과 L675 HWM 갱신 조건이 서로 충돌한다.
- **수정안:**
  ```
  미분류 흐름 존재 시 스냅샷:  U*_t = (Equity_t − Σ 미분류 F) / N_t   (오염 제거 U 저장)
  확정 시:                      ΔN = F / U*_{flow_date − 1}          (확정일이 아니라 발생일 직전)
  발생일~오늘 스냅샷은 restated=1 로 재계산, HWM도 재계산
  학습기간에도 HWM 동결은 유지 — 경보만 하는 대상은 '매수 차단'으로 한정
  ```

### [심각도: Blocker] [§5.1 L480, §5.3 L554·L584] 파킹 자산 시계열 하나의 결함으로 위험자산이 전량 매도된다
- **발생 시나리오:**
  1. 파킹 ETF 423160의 최근 121개 종가 중 하루가 누락되거나, 이 종목에 `CORPORATE_ACTION_UNVERIFIED`가 걸린다.
  2. L480(교집합 정렬)에 따라 **모든** RISK_ASSET이 `PRICE_SERIES_INVALID`가 된다.
  3. 5.2 필터에서 탈락하고 버퍼 자격도 잃어(L554) 최종 슬롯이 비게 된다.
  4. 보유 3종목에 전량 매도 Intent가 생성된다(L584).
  5. 매도 대금을 받을 파킹 자산도 차단 상태라 약 95%가 현금으로 방치된다.
- **현재 명세의 한계:** L584의 "`DATA_INVALID`는 매도 사유가 아니다"는 **보유 종목 자체**의 데이터 문제만 다룬다. 이 경우 보유 종목 데이터는 VALID라 그 보호에 걸리지 않는다.
- **수정안:**
  ```
  if parking_series_invalid:
      preview.status = 'RF_INVALID'; rotation = SKIP
      보유 종목은 트레일링 스탑·120MA 로만 판정 (전략 교체 매도 금지)
  데이터 원인(비교 기준 결측 등)으로 필터 탈락한 보유 종목 → HOLD_UNDETERMINED (매도 사유 아님)
  ```
  §11.2에 "파킹 1일 결측 → 교체 SKIP" 시나리오를 추가한다.

### [심각도: Blocker] [§5.4 L606·L618, §2.2 L195, rules L1420] `Cash_KIS`가 어떤 KIS 필드인지 정의되지 않아 D+2 정산과 충돌한다
- **발생 시나리오:**
  1. 15:00에 교체 매도 1,000,000원이 체결된다.
  2. 구현자가 `Cash_KIS`를 `dnca_tot_amt`(D+0 예수금)로 읽으면, 매도대금은 D+2에야 반영되므로 아직 들어오지 않은 상태다.
  3. 그 결과 세 가지가 일어난다.
     - ① V에서 1,000,000원이 빠진다(평가액은 사라졌는데 현금은 미반영).
     - ② `C_available` 가 과소 산출되어 같은 날 교체 매수가 0주가 되고, 슬롯이 1주일 동안 빈다.
     - ③ 15:40 스냅샷에 가짜 낙폭이 생긴다(규모에 따라 CRITICAL).
- **현재 명세의 한계:** 대사 필드는 `prvs_rcdl_excc_amt`로 고정했지만, 평가(V)와 매수가능금액에 쓰는 필드는 정하지 않았다. L28은 "확정"이라고 하는데 L1420은 `[UNVERIFIED]`다. 또 `nrcvb_buy_amt`에 당일 매도대금 재사용분이 포함되는지는 **검증 필요**다.
- **수정안:**
  ```yaml
  accounting:
    cash_recon_field:     "prvs_rcdl_excc_amt"   # 대사
    valuation_cash_field: "prvs_rcdl_excc_amt"   # V, C_available 에도 동일 필드만 사용
    valuation_total_field: "tot_evlu_amt"        # 교차검증용 (D+2 포함 여부 검증 필요)
  ```
  `V = prvs_rcdl_excc_amt + Σ evlu_amt`로 계산하고, `|V − tot_evlu_amt| > tol`이면 `DATA_INVALID`로 처리한다. D+0 필드는 표시용으로만 쓴다. **PAPER 계좌에서 매도→D+0/D+1/D+2 필드 변화를 실측하는 것을 Stage 2 완료 조건으로 둔다.**

### [심각도: Blocker] [§9.2 L1046] WAL 모드에 `synchronous=NORMAL`이면 정전 시 write-ahead 기록이 사라질 수 있다
- **발생 시나리오:**
  1. `SUBMITTING` 상태와 `idempotency_key`를 커밋한 뒤 KIS가 주문을 접수한다.
  2. 이때 정전이 나거나 UPS가 실패한다.
  3. WAL+NORMAL 조합은 마지막 커밋의 디스크 확정(fsync)을 보장하지 않으므로 해당 행이 사라질 수 있다.
  4. 재부팅 후 복구 Step 4에서 확인할 대상이 없다. 의도가 재계산되면 **같은 주문이 다시 전송**된다. 그렇지 않더라도 `ACCOUNT_MISMATCH` 수동 정지로 이어진다.
- **현재 명세의 한계:** "커밋했다"를 "디스크에 확정됐다"와 같은 뜻으로 전제한다. 참고로 현재 `src/db.py`는 `journal_mode`도 `synchronous`도 설정하지 않는다. 코드와 명세가 모두 미비하다.
- **수정안:**
  - `PRAGMA synchronous=FULL;`을 전역 적용한다. 쓰기량이 작아 비용은 무시할 만하다.
  - 토큰·상태 파일의 원자적 교체(rename) 뒤에는 디렉터리 `fsync`를 한다.
  - §7.3에 "DB에 없는 KIS 당일 주문은 고아 주문(`ORPHAN_ADOPTED`)으로 흡수하고, 해당 티커 신규 주문은 당일 차단"하는 규칙을 추가한다.

---

## 2. Major

### 2.1 상태 머신·주문 집행

**[심각도: Major] [§7.3 L878·L887–889·L912] `PENDING_CONFIRMATION → EXPIRED` 전이가 없다**
- **발생 시나리오:** 타임아웃 후 장 마감 완전 조회에서 "접수됐으나 0주 체결, 만료"가 확인된다. L923은 `EXPIRED`로 확정하라고 하지만, 전이표에는 이 행이 없고 "표에 없는 전이는 금지"다(L878).
- **영향:** `src/order_manager.py` L55–63의 전이 집합에도 없다. `PENDING_CONFIRMATION` 해제 조건이 영원히 충족되지 않아 SAFE_HALT가 고착된다.
- **수정안:** 전이표에 `PENDING_CONFIRMATION → EXPIRED`를 추가한다. 조건은 "장 마감 후 완전 조회에서 매칭 주문의 잔량 소멸 확인"이다.

**[심각도: Major] [§7.2 L863, §7.3 L897] 정정 응답이 타임아웃되면 직전 주문이 `CANCEL_PENDING`에 갇힌다**
- **발생 시나리오:** 새 attempt가 `CONFIRMED_NOT_ACCEPTED`로 확정돼도, 직전 attempt를 되돌릴 전이가 없다. 직전 주문이 장 마감까지 활성으로 남아 해당 티커를 막는다. 15:14:30 일괄 취소도 `CANCEL_PENDING` 상태에는 다시 요청하지 않는다.
- **수정안:**
  - `CANCEL_PENDING → ACCEPTED/PARTIALLY_FILLED` 조건에 "정정 attempt가 `CONFIRMED_NOT_ACCEPTED`이고, 원주문 잔량이 2회 연속 조회에서 변하지 않음"을 추가한다.
  - 일괄 취소는 `CANCEL_PENDING`에도 취소를 재요청할 수 있게 한다(상태는 유지하고 이벤트만 기록).

**[심각도: Major] [§0.6 L157, §7.3 L904·L914 vs §7.1 L782, §4.3 L411] 주문 1건 타임아웃이 계좌 전체 SAFE_HALT로 번진다**
- **발생 시나리오:** 15:05 매수 POST가 타임아웃되어 `PENDING_CONFIRMATION`이 되고, SAFE_HALT 원인이 활성화된다. BUY는 장중 확정이 금지돼 있어 15:40까지 이 상태가 유지된다. 그동안 다른 종목이 −10% 스탑에 도달해도 매도할 수 없다.
- **명세 불일치:** 게이트 3(L782·L411)은 "대기"라고 하고 L904는 "SAFE_HALT"라고 한다.
- **수정안:**
  - `PENDING_CONFIRMATION(BUY)`이면 계좌 BuyBlock만 건다.
  - `PENDING_CONFIRMATION(SELL)`이면 해당 티커만 SellBlock을 건다.
  - 다른 티커의 `RISK_REDUCTION` 매도는 KIS `ord_psbl_qty`를 기준으로 허용한다. 과매도는 브로커가 거부하므로 안전하다.

**[심각도: Major] [§7.1 L790·L810·L816, §7.3 L883·L885] 일시적 거부나 게이트 실패 1회로 그날의 손절, 그 주의 슬롯이 사라진다**
- **발생 시나리오:**
  - ① 10:00 스탑 의도 `R{date}:STOP`을 만들었는데 호가 TTL이 초과되어 `CREATED → EXPIRED`가 된다. 재시도는 `CONFIRMED_NOT_ACCEPTED`일 때만 허용되고 같은 멱등 키(중복 주문 방지 키)도 재생성되지 않는다. 결과적으로 익일 09:10까지 손절이 실행되지 않는다.
  - ② 15:01 매수가 `EGW00201`(초당 호출 한도 초과)로 `REJECTED`가 된다. `W{주}` 키 때문에 슬롯이 1주일 동안 비어 있게 된다.
- **수정안:**
  ```yaml
  execution:
    transient_reject_codes: ["EGW00201", "EGW00123"]   # 코드표 검증 필요
    max_transient_reject_attempts: 2
  ```
  - 직전 attempt가 `EXPIRED`이고 `submitted_at IS NULL`(미전송 보장)이면, 같은 의도에 새 attempt를 허용한다.
  - 위험 매도의 게이트 실패나 거부는 즉시 `exit_obligations OPEN`에 등록하고, 당일 10분 간격으로 재확인한다.

**[심각도: Major] [§4.4 L426, §7.3 L919, §9.1 L1035] 일괄 취소가 1회뿐이고 취소 POST 재시도가 금지되어, 주문이 종가 단일가(15:20–15:30)로 넘어간다**
- **발생 시나리오:**
  - ① 15:14:30 취소 요청이 5초 타임아웃되면, 원주문이 종가 단일가에서 체결된다.
  - ② 15:08 `PENDING_CONFIRMATION` 주문이 15:15 poll에서 `ACCEPTED`로 확인되지만, 이미 sweep이 끝난 뒤다.
- **수정안:**
  ```yaml
  market:
    order_cancel_sweep_window: ["15:14:30", "15:19:30"]   # 15초 주기 반복
  ```
  - 취소(`RVSE_CNCL_DVSN_CD=02`, `QTY_ALL_ORD_YN=Y`)는 다시 보내도 "취소가능수량 없음"으로 거부될 뿐이므로 사실상 멱등이다. 따라서 취소만 재시도를 허용한다(검증 필요).
  - 15:19:30 이후에도 남은 활성 주문은 CRITICAL 경보를 보낸다.

**[심각도: Major] [§5.3 L589–592, §7.1 L820, §7.2 L842] 매도 선행 파이프라인에서 슬롯 불균형과 고아 포지션이 생긴다**
- **시나리오 A:** Bid1 교체 매도가 호가 이동으로 미체결된다. 15:06까지 종결되지 않으면 **매수 단계 전체**가 생략되어, 기존 현금으로 채울 수 있는 다른 슬롯까지 비게 된다. 부분 실행이므로 승계도 되지 않는다.
- **시나리오 B:** 스프레드가 0.3%를 넘어 매도가 제출되지 않는다. 미종결 주문이 없으므로 매수 단계는 진행된다. 그러면 구 종목이 슬롯 밖에 남아 위험자산이 4종목이 되고 지역 한도를 위반한다. 이월도 금지(L592)라 1주일 동안 방치된다.
- **시나리오 C:** Ask1 매수가 미체결되어 15:14:30에 취소된다. `W` 키 때문에 그 주에는 재시도할 수 없다.
- **수정안:**
  - 매수를 슬롯별로 판단한다. 의존하는 매도가 FILLED가 아니면 그 슬롯만 생략한다.
  - 제출되지 않은 교체 매도는 `exit_obligations(type=ROTATION_EXIT)`로 넘기고, 신규 매수는 구 종목 비중만큼 줄인다.
  - 다음 개장일 15:00에 같은 W cycle의 미충족 수량(요청 − 누적 체결)에 대해 새 attempt를 허용한다.

**[심각도: Major] [§4.4 L428, §9.1 L1030·L1033] PAPER 계좌 1 req/s 예산이 15:00 집행 시점에 고갈된다**
- **발생 시나리오:** WebSocket이 끊기면 보유 종목마다 10초 주기로 현재가와 호가를 REST로 폴링하고, 장중 대사도 돈다. 이것만으로 약 0.9 req/s다. 15:00에는 ccld 페이지 조회, psbl 조회, pending poll이 겹친다. 대기하는 동안 TTL(15초/30초)이 초과되어 게이트가 실패하고, 15:06 시한을 놓친다.
- **수정안:**
  - 15:00–15:15에는 `intraday_guard` REST 폴링을 30초에 1콜로 줄이고 장중 예수금 대사를 중지한다.
  - 주문 직전 조회 묶음(잔고·호가·psbl)은 토큰 버킷에서 예약을 확보한 뒤 연속으로 실행한다.
  - 모의투자 공식 한도는 **검증 필요**다(알려진 값은 초당 약 2건, 실전 20건).

### 2.2 퀀트·회계

**[심각도: Major] [§5.4 L608·L612·L614·L616, rules L1333] 소액 계좌에서 슬롯을 채울 수 없고 현금이 사장된다**
- **발생 시나리오 (V=500,000):**
  - S3 목표 100,000원이 최소 주문 150,000원보다 작아 영구히 미매수다.
  - 그런데 슬롯은 "배정됨" 상태라 `w_parking=0`이 되어(L608) 파킹으로도 가지 않는다.
  - 결과적으로 25%(100,000원 + 버퍼 25,000원)가 현금으로 사장된다.
- **Drift 기준의 무력화:** 실효 drift 기준은 `max(5%, 150,000/V)`다. V=1M이면 15%p다. 따라서 V<3,000,000에서는 `rebalance_drift_ratio`가 사실상 작동하지 않는다.
- **수정안:**
  ```
  if w_i * V < min_trade: slot_i = UNFILLABLE; w_parking += w_i
  parking 매수 판정: gap_f >= P_f (1주 기준, min_trade 미적용)
  min_trade_eff = min(150_000, max(50_000, 0.05 * V))
  ```
  REAL 전환 체크리스트(§11.3)에 "최소 운용 자금(예: 3,000,000원)"을 추가한다.

**[심각도: Major] [§5.1 L474, §5.4 L633] 파킹 현금화 필요액 N이 정의되지 않았고, 459580 전환 기준이 너무 낮다**
- **N 미정의 예:** V=1M, 423160 8주, 현금 120k 상태에서 S1 450k를 매수해야 한다. N을 `T−H`(450k)로 잡으면 5주(550k)를 판다. 정답은 `450k×(1+fee) + 0.05V − 120k`≈380k, 즉 4주다.
- **459580 문제:** V=10M일 때 459580 1주가 1,075,000원(10.75% V)이다. ceil 매도를 하면 큰 잔여 현금이 남는다.
- **수정안:**
  - `N = Σ_buy (T_i − H_i − P_i)(1+fee) + max(0, 0.05V − (C_available − Σ활성매수))`로 정의한다.
  - 매도 수량은 floor와 ceil 중 잔여가 작은 쪽을 쓴다.
  - 459580 전환 조건은 `P_f/V ≤ 0.025`로 한다.

**[심각도: Major] [§5.3 L573 vs L522·L580] TickerBlock이 걸린 보유 종목의 슬롯 처리가 정의되지 않았다**
- **발생 시나리오:** B(S2, 30%)에 CA 미검증이 걸린다. L573은 "현 슬롯 유지"라고 하지만, L522 필터는 B를 제외하고 L580 재배정은 남은 3칸을 다른 종목으로 채운다. 결과는 위험자산 4종목, 목표 비중 합계 125%다.
- **수정안:** 차단된 보유 종목을 기존 슬롯과 비중으로 **사전 배정**하고, 집중도 ①–④ 계산에 포함한다. 남은 칸만 알고리즘으로 채운다.

**[심각도: Major] [§5.3 L550 ④ vs L554] 슬롯 번호를 Score순으로 재배정한 뒤 지역 비중 상한을 다시 검사하지 않는다**
- **발생 시나리오:**
  - 배정 단계에서 US는 45+20=65%로 한도 안이다.
  - Score순 재배정으로 US_B가 S2(30%)로 올라가면 US=75% > 0.65가 된다. 이는 §11.2 #52(L1680)에 위반된다.
- **수정안:**
  - 3!=6개의 순열 중 "순위 정렬도가 가장 높으면서 ①–④를 만족하는" 배치를 선택한다.
  - 만족하는 배치가 없으면 하위 종목을 미배정하고, 그 비중은 파킹으로 보낸다.

**[심각도: Major] [§2.2 L195, §6.2 L671, §9.2 L1090–1094] 분배금 회계: 같은 날 합산되고, 지급일에 이중 계상된다**
- **발생 시나리오:**
  - ① 같은 날 입금 1,000,000원과 분배금 22,000원이 들어오면 `unexplained_cash_delta` 1,022,000원 1행이 된다. `/classify`는 한 유형만 지정할 수 있어 나눌 수 없다.
  - ② 미수금 기간이 "입금 확인일까지"라 포함 여부가 불명확하다. 그래서 지급일 스냅샷에 현금과 미수금이 함께 잡혀 U가 +0.6% 되고, HWM이 갱신된 뒤 가짜 DD가 영구히 남는다.
  - ③ 자동 검증이 ex_date 다음 날 아침에야 되므로, ex_date 마감 스냅샷에는 미수금이 빠진다.
- **수정안:**
  - `distribution_receivables(ca_id, amount, status OPEN/SETTLED, settled_flow_id)` 테이블을 둔다. DISTRIBUTION 확정과 SETTLED 전환을 같은 트랜잭션에서 처리한다.
  - UNCLASSIFIED 행을 만들기 전에 `pay_date=오늘`인 기대 분배금을 먼저 차감한다.
  - `/classify <id> SPLIT DEPOSIT:1000000 DISTRIBUTION:22000`처럼 자식 행으로 나누게 한다(`parent_flow_id` + 합계 CHECK).

**[심각도: Major] [§4.2 L389·L393–396, §6.1 L653–654] 분배락일에 손절이 거짓으로 발동하거나, 반대로 손절 감시가 꺼진다**
- **발생 시나리오:**
  - **일정 미등록:** −8.2% 하락과 분배락 200원이 겹치면 원주가 기준 −10.2%가 되어 거짓 손절이 난다.
  - **일정 등록:** ex_date 아침에 격리가 걸려 스탑이 정지된다.
  - **ETF 분배 미반영:** KIS가 분배를 수정주가에 반영하지 않으면 계수 f=1.0이다. 자동 검증 범위 `[0.95, 1.0)`에서 빠지므로 분배 때마다 `/ca_confirm`이 필요하다(**검증 필요**).
  - **반복 격리:** 규칙 2가 `verified`를 보지 않아 검증된 이벤트도 다시 격리한다(설정·해제 반복 루프).
- **수정안:**
  - 규칙 2를 `verified=0`인 이벤트로 한정한다.
  - 자동 검증 범위를 `[min_cash_factor, 1.0]`으로 넓힌다.
  - ex_date에는 `f_est = (Close_{d−1} − cash)/Close_{d−1}`를 잠정 적용하고 스탑은 계속 켜 둔다. 확정 후 f로 교체한다.
  - 단순화 대안: `PeakPrice`를 저장하지 않고 매일 `max(adjusted_close[진입일..어제])`로 다시 계산하면 재기준화 로직 자체가 사라진다.

**[심각도: Major] [§6.2 L688–690, §9.2 L1066] EMERGENCY 해제와 HWM 리베이스에 구멍이 있다**
- **문제점:**
  - `emergency_reason`이 단일 값이다. MANUAL EMERGENCY 중에 DD가 −16%가 되어도 reason이 MANUAL에 머문다. 그래서 해제해도 리베이스가 일어나지 않고, 다음 tick에서 CRITICAL이 재발동한다.
  - 리베이스에 쓰는 `U_now`의 시점이 정의되지 않았다(장중 값인지, 동결 중 오염된 U인지).
  - 리베이스 횟수에 제한이 없다. 3회 해제하면 누적 손실이 1−0.85³ = **38.6%**까지 가능하다.
- **수정안:**
  - `emergency_reasons`를 집합(JSON 또는 별도 테이블)으로 바꾼다.
  - 리베이스는 직전 VALID 마감 스냅샷의 U로만 하고, `hwm_frozen=1`이면 거부한다.
  - epoch 누적 낙폭 하한 `risk.epoch_max_drawdown: -0.25`를 신설하고, 이 하한은 리베이스 대상에서 제외한다.

### 2.3 보안·인프라·무인 운영

**[심각도: Major] [§3.3 L290–306] 토큰 갱신 여유가 사실상 없어 장중 만료가 가능하다**
- **문제점:**
  - 규칙 B는 "남은 시간 ≤1h"이면 갱신하고, 실행은 매시 한 번이다. 남은 시간이 1h00m30s일 때는 재사용하고, 다음 정각에는 30초만 남는다. 이때 1회만 실패해도 만료된다.
  - CRITICAL 경보는 3회 실패(약 3시간) 후에야 나간다.
  - 규칙 B가 08:25 발급보다 먼저 작동하면서 발급 시각이 매일 앞당겨진다. 그래서 L304의 "08:25 발급 보장"은 성립하지 않는다.
- **수정안:**
  ```
  remaining <= 4h → issue()
  실패 시 5분 간격 재시도(65초 쿨다운·flock 유지), remaining < 2h 이면 1회 실패부터 경보
  평일 08:05: expires_at < 당일 16:00 KST 이면 issue()
  앱: expires_at − 300s 이전부터 CREDENTIALS_UNAVAILABLE (주문 POST 도중 만료 방지)
  ```
  KIS가 새 토큰을 발급할 때 기존 토큰을 무효화하는지는 **검증 필요**다.

**[심각도: Major] [§0.2 L91, §3.3 L284 / 코드 `src/kis_client.py` L86–111·L706–712] 토큰 파일 권한 설계와 구현이 충돌해 컨테이너가 토큰을 읽지 못한다**
- **문제점:**
  - 명세는 rename **후**에 권한을 `0640`으로 바꾼다. 그 사이에 잠깐 읽을 수 없는 구간이 생기고, 중간에 크래시하면 그 상태가 영구화된다.
  - 구현은 디렉터리를 `0700`, 파일을 `0600`으로 강제한다. 그래서 uid 10001(컨테이너)은 디렉터리에 들어가지도 못한다.
- **수정안:**
  - 임시 파일에 `fchown(pi:mosasecret)` + `fchmod(0640)` → `fsync` → `rename` → 디렉터리 `fsync` 순서로 처리한다.
  - 디렉터리 권한은 `2750`으로 한 번만 설정하고, 코드에서는 바꾸지 않는다.
  - 배포 후 검사에 `docker exec -u 10001 mosasaurus-agent test -r /run/secrets/token.json`을 넣는다.

**[심각도: Major] [§3.4 L330–332, rules L1273 / 코드 `scripts/mosasaurus-deploy`, `.github/workflows/deploy.yml`] 커밋 서명 검증과 self-hosted runner의 신뢰 경계가 실제로는 작동하지 않는다**
- **문제점:**
  - **서명 검증:** GitHub 웹에서 PR을 머지하면 GitHub `web-flow` 키로 서명된다. 그러면 `verify-commit`이 항상 실패한다. 반대로 web-flow 키를 허용하면 GitHub 세션 탈취만으로 배포할 수 있다.
  - **서명자 목록 위치:** `allowed_signers`가 저장소 `rules.yaml`에 있으므로, 배포 대상 커밋이 서명자 목록을 스스로 수정할 수 있다.
  - **CI 확인:** 래퍼에서 CI 성공 확인이 선택 훅으로 되어 있다(명세는 필수).
  - **runner 실행 조건:** 워크플로의 `if:`와 `environment:`는 푸시된 YAML이 정한다. 브랜치에서 워크플로를 수정해 푸시하면 runner가 임의 코드를 실행한다.
- **수정안:**
  - 서명자 목록은 `/etc/mosasaurus/allowed_signers`(root 600) 하나만 쓴다. rules.yaml의 해당 키는 삭제한다.
  - 운영자 로컬의 `git merge -S` 서명 머지만 허용한다.
  - 래퍼에서 CI 확인을 필수로 바꾼다. 이때 쓰는 읽기 전용 토큰은 금고에 둔다.
  - runner group은 이 저장소와 `main` environment로 한정하고, ephemeral(1회용) runner를 쓴다. `chmod 750 /home/pi`로 홈 디렉터리를 닫는다.
  - **더 단순한 대안(권장, §3):** self-hosted runner를 제거하고 Tailscale SSH로 수동 `deploy.sh`를 실행한다.

**[심각도: Major] [§3.4 L344·L346·L354·L358 / 코드 `scripts/safe_deploy.sh`, `scripts/backup_db.sh`] 롤백 시 데이터 손실, 주문 잠금 고착, DB 소유권 손상**
- **롤백 데이터 손실:** 7단계에서 새 컨테이너가 기동해 boot_recovery를 수행하고 주문 잠금까지 해제한다. 그 뒤 헬스체크가 실패하면 9단계 `cp backup`이 그 사이에 기록된 주문 이벤트를 **덮어쓴다**. 따라서 "데이터 손실 없음" 주장은 거짓이다.
- **DB 소유권 손상:** 2단계는 호스트 root가 sqlite에 직접 쓴다. 이는 단일 writer 원칙 위반이다. root 소유 `-wal`/`-shm` 파일이 생기면 앱이 `SQLITE_READONLY` 오류를 낼 수 있다.
- **주문 잠금 고착:** 3단계에서 활성 주문이 있으면 잠금을 `true`로 둔 채 `exit 0`한다. 앱은 재기동되지 않으므로 잠금이 풀리지 않고, **손절 주문까지 막힌다**. 검사 대상도 3개 상태뿐이다(명세는 활성 6종).
- **백업 암호화 누락:** 백업이 평문이다. 명세 L1203은 암호화를 요구한다.
- **수정안:**
  ```sh
  # 검증은 MAINT_MODE(텔레그램·주문 Job 비활성) 로 기동, 잠금 해제는 8단계 확정 후에만
  # 호스트의 DB 작업은 모두 컨테이너 사용자로: docker compose run --rm --user 10001:10001 agent python -m src.ops <cmd>
  trap 'restore_lock_state' EXIT          # 모든 종료 경로에서 잠금 원복
  restore(): stop; rm -f db-wal db-shm; age -d -i backup.key b > tmp; sha256sum -c; mv tmp db; integrity_check
  ```

**[심각도: Major] [§10.2 L1232, §3.4 L338 / `docker-compose.yml` L29] `run/` 공유 폴더 마운트가 보안과 기능 중 하나를 깨뜨린다**
- **문제점:**
  - 명세(rw)를 따르면, 탈취된 컨테이너가 `deploy_queue`를 심볼릭 링크로 바꿔 root의 쓰기를 임의 파일로 유도할 수 있다. 또 `clock.json`·`ups.json`·`post_close_done`을 위조할 수 있다.
  - 구현(`:ro`)을 따르면, 앱이 `post_close_done`을 쓸 수 없다. 그러면 개장일 배포 금지 시간이 끝나지 않는다.
- **수정안:**
  - 폴더를 둘로 나눈다. `run/host/`는 호스트가 쓰고 컨테이너는 `:ro`로 읽는다. `run/app/`은 앱이 쓰고 호스트는 `O_NOFOLLOW`로 읽는다.
  - 배포 큐는 `/var/lib/mosasaurus/deploy_queue`(root 600)로 옮긴다.

**[심각도: Major] [§3.2 L268] UPS 방전 시 `shutdown -h` 후 자동으로 다시 켜지지 않는다**
- **발생 시나리오:** UPS가 계속 전원을 공급하므로 Pi 5는 정지(halt) 상태로 남는다. 외부 전원이 돌아와도 재기동되지 않아 몇 주간 정지될 수 있다.
- **수정안:**
  - 종료 전에 `echo +900 > /sys/class/rtc/rtc0/wakealarm`으로 RTC 알람 기상을 설정한다. EEPROM은 `POWER_OFF_ON_HALT=1`로 설정한다.
  - "전원 복귀 시 자동 기동(auto-power-on)"을 지원하는 UPS HAT을 하드웨어 필수 요건으로 정한다.
  - 런북에 "UPS 방전 → 전원 복귀 → 기동" 시험 절차를 추가한다.

**[심각도: Major] [§6.4 L727–747] 휴대폰 분실·Telegram 계정 탈취 시 대응 수단이 없고, `/emergency`의 단계 지정이 자기 규칙과 모순된다**
- **탈취 시나리오:** 2단계 확인 코드가 같은 채널로 오므로, 공격자는 확인 단계를 그대로 통과한다. 가능한 공격은 다음과 같다.
  - `/clear_halt ALL`, `/reconcile adopt`로 안전 정지를 무력화한다.
  - `/remove`를 반복해 전 종목을 강제 매도시킨다.
  - `/classify`로 출금을 입금으로 위장해 HWM과 낙폭 판정을 조작한다.
- **기존 대응책의 한계:** 명세의 대응은 "SSH로 사용자 ID 무효화"다. 군 복무 중에는 수행할 수 없고, 컨테이너 재기동도 필요하다.
- **`/emergency` 모순:** L733은 "위험을 줄이는 명령은 1단계"라고 정해 놓고, L747은 `/emergency`를 2단계로 지정했다. 확인 코드를 5회 틀리면 30분 잠금까지 걸린다.
- **수정안:**
  ```yaml
  telegram:
    away_mode: true          # 해제·위험 증가 명령 전부 거부; /status /pause /emergency 만 허용
    second_factor: totp_offline   # /clear_halt /reconcile adopt /classify /remove 에 TOTP(휴대폰과 별도 보관 카드)
    revocation: db_based          # 재기동 없이 사용자 차단; 보조 연락인 ID는 /emergency·/revoke 만
    max_remove_per_week: 1
  ```
  `/emergency`를 1단계로 바꾸고, 실패 잠금 대상에서도 제외한다.

**[심각도: Major] [§3.2 L268, §0.4 L127] 감시 데몬 오탐이 SAFE_HALT를 일으킨다**
- **문제점:** NTP 오차 1.0초, `clock.json` 120초 TTL 같은 기준은 15초 TTL·300초 매칭 창을 쓰는 시스템에는 과도하게 엄격하다. 데몬이 한 번만 죽어도 스탑이 꺼진다.
- **수정안:**
  - 시각 오차 임계값을 10초로 올린다.
  - 감시 파일이 오래된 경우는 SAFE_HALT가 아니라 BuyBlock + 경보로 처리한다.
  - UPS가 없는 구성이면 설정으로 비활성화할 수 있게 한다.

**[심각도: Major] [§9.2 L1066, §7.1 L780, L1108] "계좌가 정상인가"의 판단 출처가 셋이다**
- **문제점:** `system_state.reconciliation_ok`, `last_successful_reconcile_at`, `safety_flags(ACCOUNT_MISMATCH)`가 따로 있어 갱신이 하나만 빠져도 서로 다른 답을 낸다.
- **수정안:**
  - 판정 기준을 "`ACCOUNT_MISMATCH` 없음 AND 마지막 성공 대조 300초 이내" 하나로 통일하고 `reconciliation_ok`는 삭제한다.
  - 같은 이유로 `universe_assets.data_status`(TickerBlock 플래그와 중복)를 삭제한다.
  - `order_intents.status`는 저장하지 않고 `orders`에서 계산한다.

### 2.4 Minor (요약)
- [§7.3 L890–898] 운영자가 KIS 앱에서 직접 취소한 경우의 `ACCEPTED → CANCELED` 전이가 없다. 마감 후 `EXPIRED`로 잘못 분류된다. → "조회에서 외부 취소 확인" 전이 행을 추가한다.
- [§7.3 L913] 장중 미접수 확정(SELL 전량)은 조건이 복잡하고 거의 실행되지 않는 경로다. → 삭제하고 장 마감 후 확정만 남기는 것을 권장한다.
- [§0.6 L163, §6.1 L648] `TRAINING`과 `EMERGENCY`가 동시에 활성일 때 스탑 기준(−10%/−7%)이 정의되지 않았다. → 더 엄격한 값을 적용하도록 명시한다.

---

## 3. 과잉 설계 평가 (1인 개발·무인 운영 관점)

> 현재 `src/`는 약 10.3k줄(`order_manager.py` 1,445줄, `kis_client.py` 1,199줄), 마이그레이션 3개(912줄), `def test_` 395개다. 여기서 "간소화"는 **이미 있는 코드 경로를 지우자**는 뜻이다. 지운 경로는 무인 운용 중에 처음 실행되다가 터질 일이 없다.

| 구성요소 | 판정 | 대안 | 감수하는 리스크 |
|---|---|---|---|
| 지정가 전용, 시장가 상수 차단, 레버리지·인버스 CHECK | **유지** | — | — |
| `order_manager` 단일 주문 경로, write-ahead, POST 재시도 금지 | **유지** (`synchronous=FULL` 추가) | — | — |
| `PENDING_CONFIRMATION` + 장 마감 후 확정 | **유지** (차단 범위는 티커/BuyBlock으로 축소) | — | — |
| 멱등키 UNIQUE + 활성 attempt partial index | **유지** | 인덱스 1개로 통일 | — |
| 11상태 주문 머신 | 간소화 | 6상태(`SUBMITTING`, `UNKNOWN`, `OPEN`, `CANCEL_PENDING`, `FILLED`, `CLOSED`+`close_reason`) | 정보 손실 없음(사유가 컬럼으로 이동) |
| `IntentStatus` 5종 / `CONFLICT` | 삭제 | `orders`에서 계산. `CONFLICT`는 생성하는 명령이 없어 도달 불가 | 없음 |
| 비상 재가격 체인 | **삭제** (Blocker 1 해소) | Bid1 1회 + `exit_obligations` 이월 | 급락 시 미체결분은 익일로 넘어감 |
| 장중 미접수 확정 | 삭제 | 장 마감 후 확정만 | 해당 날짜 신규 주문 정지 |
| `exit_obligations` 4상태 | 간소화 | `OPEN`/`CLOSED`+reason | — |
| 운영 모드 5종 | 간소화 | `SAFE_HALT` + `PAUSED`(사유: MANUAL/DRAWDOWN/TRAINING) + `NORMAL` | 훈련 모드 자동 파킹 매수가 사라짐 |
| 낙폭 4단계 + 히스테리시스 + 5일 WARNING 하한 | 간소화 | `CRITICAL` 단일 차단기 + 리베이스. 종목별 −10% 스탑과 기능이 겹침 | −7~−15% 구간의 단계적 감속이 사라짐 |
| TWR epoch / 분배 미수금 | 간소화 | 단위가격(NAV) 방식만 유지. N≤0이면 `ACCOUNTING_INVALID` 처리 후 수동 대응 | 전액 출금 후 TWR 연속성 깨짐 |
| REAL 3중 잠금 + `/approve_*` 3종 | 간소화 | 저장소 밖 호스트 승인 파일 1개(만료일, 백테스트 보고서 해시, 전략 해시) | 휴대폰만으로 승인 불가(더 안전한 방향) |
| 텔레그램 21종 | 간소화 | 약 13종. `/add`는 어차피 무조건 거부(L757)하므로 삭제 | 조회 편의 감소 |
| 이벤트 테이블 5개 | 간소화 | `order_events` + `events(category,…)`. 26개 → 약 17개 | 쿼리가 조금 복잡해짐 |
| `weekly_previews` revision | 간소화 | 같은 주차는 덮어쓰기 + 감사 로그 | 프리뷰 이력 일부 손실 |
| 자동 배포(Runner·서명 래퍼·큐·DB 복원 롤백) | **삭제** | Tailscale SSH로 수동 `deploy.sh` + 하위 호환(추가만 하는) 마이그레이션만 허용 | 원격 배포에 SSH 필요. 대신 runner 공격면과 롤백 Major 3건이 사라짐 |
| 캘린더 이중 검증 → SAFE_HALT | 간소화 | KIS를 권위 소스로 사용, 불일치 시 BuyBlock + 경보 | 캘린더 라이브러리 오류에 덜 민감해짐 |
| 시나리오 75개 | 간소화 | 자동 테스트 약 40개 + 인프라 항목(32·33·35·38·39)은 §11.3 수동 체크리스트로 이동. 삭제된 기능 관련 항목 제거 | 인프라는 리허설로만 검증 |

**복잡도 자체가 위험이 되는 지점:**
- 자동 배포의 롤백 경로는 거의 실행되지 않는다. 그래서 실제로 필요할 때 버그가 날 가능성이 가장 높다.
- 배포 금지 시간의 종료 조건이 `post_close_done` 신호에 묶여 있다(L338). 그러면 `post_close_reconcile`에 버그가 있을 때, **그 버그를 고칠 배포가 영원히 큐에 남는다**.

---

## 4. 상호 참조 불일치 표

| # | 항목 | 위치 A | 위치 B | 불일치 내용 |
|---|---|---|---|---|
| 1 | 활성 attempt 인덱스 | §7.1 L815 (단일, 활성 6종) | §9.2 L1057–1058 (정정 여부로 2개 분리) | 개수와 조건이 다름. 어느 쪽이든 정정 체인이 막힘 |
| 2 | 종결 주문 `filled_qty` | §7.3 L872 (늦은 체결 보정 허용) | §9.2 L1062 (ABORT) / migration 003 L569–574 (증가 허용) | 명세 내부 모순. 코드는 명세와 다르게 구현됨 |
| 3 | 재가격 횟수 | §7.2 L852 `max_reprices: 5`, 시나리오 57 | §9.2 L1058 인덱스 | 실제로 1회 이하만 가능 |
| 4 | `required_qty` | §7.3 L924 (갱신) | §7.3 L925 (SUPERSEDED), §9.2 L1104 CHECK, `order_manager.record_exit_obligation` (수량이 다르면 거부) | 세 가지 방식이 충돌 |
| 5 | PENDING 만료 | §7.3 L923, 시나리오 45 | 전이표 L887–889, `order_manager.py` L55–63 | `PENDING_CONFIRMATION → EXPIRED` 행 없음 |
| 6 | 타임아웃 차단 강도 | §7.3 L904 (SAFE_HALT) | §7.1 L782, §4.3 L411 (게이트 3 "대기") | 같은 상황인데 차단 수준이 다름 |
| 7 | 대조 실패 처리 | §6.3 L708 (실패 = `ACCOUNT_MISMATCH`) | §4.3 L410 (수량 불일치만), §0.5 `ACCOUNT_DATA_STALE` | 조회 실패와 불일치를 구분하지 않음. 자동 해제 원인이 수동 해제 원인으로 격상됨 |
| 8 | 예수금 Shadow 기간 | rules L1422 (14일 경보만) | 본문 L602·L677 (즉시 동결·차단) | 본문에 정의 없음. HWM 갱신 허용 여부가 반대 |
| 9 | 현금 필드 | L195·L1420 `prvs_rcdl_excc_amt` | L606·L618 `Cash_KIS` (미정의) | 대사 필드와 평가·매수 필드가 분리됨 |
| 10 | 확정 표기 | L28 "D+2 필드 하나로 고정" (Blocker 해소) | L1420 `[UNVERIFIED]` | 확정과 미검증이 공존 |
| 11 | 교체 매도 잔량 | L592 "다음 개장일 자동 이월 안 함" | L924 "매도 필요 잔량 → `exit_obligations`" | 전략 교체 매도 잔량이 의무로 넘어가는지 불명 |
| 12 | 취소 재시도 | L426 "POST 재시도 절대 금지" | L919 "모든 활성 주문 취소" | 취소가 실패하면 주문이 종가 단일가까지 생존 |
| 13 | 정정 후 종결 시점 | L863 "정정 확인 시 `CANCELED`" | L895 "2회 연속 일치 필요" | 재가격 간격 20초와 맞지 않음 |
| 14 | 위험 축소 명령 단계 | L733 "위험을 줄이는 명령은 1단계" | L747 `/emergency`는 2단계 | 자기 규칙 위반 |
| 15 | 주차 ID 형식 | L808 `W2026-48` | L1076 `2026-W48` | 형식이 다름 |
| 16 | `halt_reasons` 성격 | L708 (`cleared_at` 기록), L723 "partial unique index" | L1108 (뷰. 인덱스는 `safety_flags`에 있음) | 뷰에는 쓸 수 없음 |
| 17 | `CONFLICT` 상태 | L935 (운영자가 폐기 시 설정) | L814 (표시 안 함), 해당 명령 없음 | 도달할 수 없는 상태 |
| 18 | 배포 금지 시간 종료 | L73 (15:45 고정) | L338 (15:45 이후 + `post_close_done`) | 신호가 없으면 끝나지 않음 |
| 19 | `run/` 마운트 | 명세 L1232 (rw) | `docker-compose.yml` L29 (`:ro`) | 쓰기 권한이 반대. 어느 쪽이든 보안 또는 `post_close_done` 중 하나가 깨짐 |
| 20 | 토큰 권한 | L91·L284 (`0640`, 디렉터리 750) | `kis_client.py` L86–111·L706–712 (0700/0600) | 컨테이너가 토큰을 읽을 수 없음 |
| 21 | 배포 잠금 수단 | L344 (컨테이너 소켓 API) | `safe_deploy.sh` 2단계 (호스트 root가 DB 직접 쓰기) | 단일 writer 위반 |
| 22 | 배포 전 활성 주문 검사 | L346 (활성 6종) | `safe_deploy.sh` 3단계 (3종) | `ACCEPTED`·`PARTIALLY_FILLED`·`CREATED` 누락 |
| 23 | 백업 암호화 | L1203 (암호화) | `backup_db.sh` (평문) | 미구현 |
| 24 | CI 성공 검증 | L332 (필수) | `mosasaurus-deploy` (선택 훅) | 강제력 없음 |
| 25 | 서명자 목록 출처 | rules L1273 (저장소) | 래퍼 `/etc/mosasaurus/allowed_signers` | 저장소 쪽은 배포 대상이 자기 서명자를 추가할 수 있음 |
| 26 | 시크릿 경로 | L215 `.secrets/mosasaurus/.env` | L91·L1127 `host.env`/`app.env` | 파일명이 다름 |
| 27 | 헬스체크 | L354·L1236 (`health_monitor --probe`, KIS read-only) | compose (DB 연결만) | KIS·토큰 검증 누락 |
| 28 | 토큰 발급 보장 | L304 "평일 08:25 발급, 장중 만료 없음" | L298–301 의사코드 | 발급 시각이 앞당겨지고 1h 여유뿐 |
| 29 | WAL/synchronous | §9.2 L1046 (WAL + NORMAL 강제) | `src/db.py` (journal_mode 미설정) | 명세와 코드 모두 미비(권장은 FULL) |
| 30 | 보유 유지 판정 | L570–572 ("아래 항목만") | L554 (버퍼는 5.2 필터 통과 필수, Score>0) | Score≤0이지만 120MA 위인 보유 종목의 처리가 모순 |
| 31 | TickerBlock 슬롯 | L573 (슬롯 유지) | L522·L580 (순위 제외 후 재배정) | 슬롯 점유 여부가 정의되지 않음 |
| 32 | 최소 주문 기준 | L612 (\|T−H\| ≥ 150k, "이상") | L616 ("초과"), §11.2 #27 L1626 ("15만 원 미만 생략") | gap 기준인지 Q×P 기준인지, 경계값 포함 여부 |
| 33 | 예약액 차감 | L618 (Cash − 활성매수, 보수적) | `src/portfolio_manager.py` L488 ("중복 차감하지 않음") | 코드가 명세와 반대 방향 |
| 34 | 수수료 산식 | L624 `fee_rate` 비율 | `src/portfolio_manager.py` L496 고정 `estimated_fee` | 산식이 다름 |
| 35 | emergency_reason | L690 (DRAWDOWN 조건) | L1066 (단일 값) | 사유 중첩을 표현할 수 없음 |
| 36 | PAPER 휴장일·iNAV | L439–440 PAPER 지원 `[UNVERIFIED]` | L377·L413 (미지원 시 `CALENDAR_MISMATCH` → SAFE_HALT), L826 (매수에 iNAV 필수) | PAPER에서 매일 정지되거나 매수가 전면 불가할 위험(검증 필요) |
| 37 | 테스트 누락 | §11.2 #5 L1580, #49 L1674 | L667, L690 | 지급일 이중 계상, 늦은 분류, MANUAL→CRITICAL, 파킹 결측 시나리오 없음 |

---

## 5. 구현 착수 전 반드시 확정해야 할 최우선 의사결정 Top 5

1. **SAFE_HALT 중 보호성 손절을 허용할지, 무인 에스컬레이션을 어떻게 할지**
   - 지금 명세는 "불확실하면 아무것도 안 한다"이다. 군 복무 중 무인 운영에서는 이것이 곧 "급락장에서 손절이 안 된다"가 된다.
   - 원인별로 `protective_exit` 허용 여부, 자동 해제 조건, 24시간 재경보 정책을 정해야 한다.
   - 이 결정이 §0.6 진리표, §6.3, rules.yaml, 시나리오 24·41의 기대값을 모두 바꾼다.

2. **비상 재가격(정정 체인)을 Phase 1에서 삭제할지**
   - 유지한다면 인덱스·종결 불변 트리거·`required_qty`·매도 가능 수량을 한 번에 재설계해야 한다(Blocker 1~3).
   - 삭제를 권장한다. 기본값이 이미 `enabled: false`이고, 삭제하면 Blocker 1과 Major 2건이 함께 사라진다.

3. **KIS 계좌 현금 기준 필드와 대사 방식을 실측으로 확정**
   - 확정 대상은 `prvs_rcdl_excc_amt`·`nrcvb_buy_amt`·`tot_evlu_amt`의 D+0/D+2 의미, 허용 오차(10원 → ≥1,000원), 체결이 멈춘 시점에만 대사하는 조건, PROVISIONAL 2회 재현 규칙이다.
   - PAPER 계좌에서 매도 → D+2 필드 변화를 측정하는 것을 Stage 2 완료 조건으로 둔다.
   - 이 결정이 늦은 분류 회계식(`U*`)도 함께 고정한다.

4. **배포 방식: self-hosted runner 자동 배포 유지 vs SSH 수동 배포**
   - 자동 배포를 유지하면 서명 신뢰 경계, runner 격리, 롤백 데이터 손실, 잠금 고착, `run/` 분리를 모두 고쳐야 한다(Major 4건).
   - 수동 배포로 바꾸면 이 4건이 사라진다. 운영자가 원격으로 직접 배포할 수 있는 환경인지가 판단 기준이다.

5. **최소 운용 자금과 소액 계좌 정책**
   - `min_trade_krw` 150,000원 기준으로 V<3,000,000원이면 슬롯 사장과 drift 무력화가 구조적으로 생긴다.
   - 선택지는 둘이다. ① REAL 최소 자금 하한을 정한다. ② `min_trade_eff`, `UNFILLABLE` 슬롯 → 파킹, 파킹 1주 기준 정책을 채택한다.
   - 시작 금액을 함께 정해야 백테스트 기준도 확정된다.

---

## 6. 확정된 의사결정 (2026-11-24, 운영자 승인)

| # | 결정 | 내용 | 해소되는 항목 | 남은 미결 |
|---|---|---|---|---|
| D1 | **SAFE_HALT 중 원인별 보호성 손절 허용** (Top 5 #1, 선택지 A) | `DEPLOY_ROLLBACK`·`ACCOUNT_MISMATCH`·`UPS_LOW`·`CONFIG_INVALID` 등 중복 주문 위험이 낮은 원인에서는 KIS 매도가능수량 기준 `REDUCE_ONLY` 손절(−10%)을 허용한다. `PENDING_CONFIRMATION`·`CREDENTIALS_UNAVAILABLE`·`DB_INTEGRITY` 는 계속 전면 차단한다. 미해결 SAFE_HALT는 24시간마다 재경보한다. | Blocker 4, Major "타임아웃 → 계좌 전체 SAFE_HALT"(일부) | 원인별 허용 목록 최종 확정, 자동 해제 조건(대조 N회 일치) 수치 |
| D2 | **비상 재가격(정정 체인) Phase 1에서 삭제** (Top 5 #2) | 위험 축소 매도는 Bid1에 1회 제출하고, 미체결분은 `exit_obligations` 로 넘겨 익일 09:10에 재시도한다. `replaces_order_id`, 정정 인덱스 2개, `execution.emergency` 설정, 시나리오 57 정정 부분을 제거하고, 활성 attempt 인덱스는 1개로 통일한다. | Blocker 1, Major "정정 타임아웃 시 CANCEL_PENDING 고착", 불일치표 #1·#3·#13 | 없음 |

**적용 현황 (2026-11-24):** D1·D2와 Blocker 9건 전부를 명세 `mosasaurus_spec_v3.7.5.md` 에 반영했다(변경 이력 표 D1, D2, V-B2~V-B9). **코드·마이그레이션·`config/rules.yaml` 에는 아직 반영하지 않았다.** 현재 `config/rules.yaml` 에 `execution.emergency` 가 남아 있으면 v3.7.5 10.5절 폐기 키 검사에 걸리므로, 코드 정렬 작업에서 함께 처리해야 한다. Major 20건은 v3.7.5 범위 밖이며 후속 개정 대상이다.

---

## 부록: 감사 한계
- 실제 KIS API는 호출하지 않았다. TR ID(`TTTC0012U/0011U/0013U`, `8434R`, `8908R`, `0081R` 등)와 ETF 호가단위는 알려진 현행 값과 일치하지만 최종 확인은 **검증 필요**다.
- 확인하지 못한 KIS 사항: `nrcvb_buy_amt`에 매도대금 재사용분 포함 여부, `inquire-daily-ccld` PAPER 응답에 거부 주문 포함 여부, WebSocket 체결통보 AES 복호화 요구사항, 새 토큰 발급 시 기존 토큰 무효화 여부.
- 확인하지 못한 저장소 사항: 저장소 공개 여부, GitHub runner group 설정, `src.deployment check` 내부 로직.
- 코드 대조는 전이 집합·트리거·의무 함수·토큰 저장·배포 스크립트에 한정했다. 마이그레이션 001/002와 테스트 본문은 깊이 대조하지 않았다.
