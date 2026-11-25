"""Telegram 명령 콜백(Phase 1): `run_telegram_polling` 의 `command_actions` 를 DB·운영 동작에 연결합니다(명세 6.4절).

콜백 규격은 `(chat_id, 명령 전체 문자열) -> 답장 문자열` 입니다. 인증·화이트리스트·속도 제한·2단계 확인(`/confirm`)은
`telegram_bot.TelegramCommandHandler` 가 이미 통과시킨 뒤에만 호출되므로, 여기서는 **동작 자체**만 수행합니다.
2단계 명령은 `pending_actions` 의 `APPROVED` 이력(명령 해시 일치, 가장 최근 확인)을 찾아 `operator_actions` 에 넘깁니다.
답장에는 계좌·토큰을 넣지 않으며, 예외 내용은 노출하지 않고 고정 문구만 돌려줍니다(전송 시 한 번 더 마스킹됨).
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping

from src import operator_actions as ops
from src.config import load_config, strategy_params_hash
from src.ops_jobs import compose_briefing
from src.job_handlers import compare_account
from src.quant_engine import calculate_momentum_score
from src.safety_flags import MANUAL_HALT_CODES, active_halt_reasons, is_safe_halt, read_host_state

KST = timezone(timedelta(hours=9), name="KST")


class _Refused(Exception):
    """실행하지 않고 사유만 안내할 때 쓴다(승인은 소진하지 않는다)."""


FAIL = "처리하지 못했습니다. 상태와 인자를 확인하세요(자세한 원인은 로그에 기록됨)."


def _set_state(connection: sqlite3.Connection, key: str, value: str, now: datetime) -> None:
    """system_state 한 값을 UPSERT 한다."""
    with connection:
        connection.execute(
            'INSERT INTO system_state ("key", value, updated_at) VALUES (?, ?, ?) '
            'ON CONFLICT("key") DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at',
            (key, value, now.isoformat()))


def _state(connection: sqlite3.Connection, key: str) -> str | None:
    """system_state 한 값을 읽는다(없으면 None)."""
    row = connection.execute('SELECT value FROM system_state WHERE "key" = ?', (key,)).fetchone()
    return row[0] if row else None


APPROVAL_VALID_FOR = timedelta(minutes=10)  # 확인 직후 실행되는 것이 정상이므로 오래된 승인은 쓰지 않는다


def _approved_action_id(connection: sqlite3.Connection, command: str, now: datetime | None = None) -> str:
    """이 명령 문자열로 가장 최근에 `APPROVED` 된(그리고 아직 쓰이지 않은) 2단계 확인의 ID(없으면 예외)."""
    row = connection.execute(
        "SELECT id, confirmed_at FROM pending_actions WHERE approval_status = 'APPROVED' AND args_hash = ? "
        "ORDER BY confirmed_at DESC LIMIT 1", (hashlib.sha256(command.encode("utf-8")).hexdigest(),)).fetchone()
    if row is None:
        raise ops.ApprovalError("No approved confirmation for this command.")
    if now is not None and row["confirmed_at"]:
        confirmed = datetime.fromisoformat(row["confirmed_at"])
        confirmed = confirmed if confirmed.tzinfo else confirmed.replace(tzinfo=timezone.utc)
        if now - confirmed > APPROVAL_VALID_FOR:
            raise ops.ApprovalError("The approved confirmation is too old.")
    return row["id"]


def _consume_approval(connection: sqlite3.Connection, command: str) -> None:
    """성공한 명령의 승인을 소진한다(`EXPIRED`): 같은 확인으로 명령을 다시 실행하지 못하게 한다(감사 행은 유지)."""
    with connection:
        connection.execute(
            "UPDATE pending_actions SET approval_status = 'EXPIRED' WHERE approval_status = 'APPROVED' "
            "AND args_hash = ?", (hashlib.sha256(command.encode("utf-8")).hexdigest(),))


def build_summary_provider(
    connection_factory: Callable[[], sqlite3.Connection], approvals_dir: str | Path
) -> Callable[[str], str | None]:
    """2단계 확인 메시지에 붙일 "실행될 효과" 문장을 만드는 함수(명세 6.4절). 정보가 없으면 None."""

    def provider(command: str) -> str | None:
        """명령별로 즉시 스탑 도달 종목·해제될 원인·기업행위 정보·승인 파일 요약 등을 보여 준다."""
        parts = command.split()
        connection = connection_factory()
        try:
            if parts[0] == "/training" and parts[1] == "on":
                hits = []
                for r in connection.execute("SELECT ticker, peak_price_adj FROM positions WHERE tracking_status = 'MANAGED' "
                                            "AND quantity > 0 AND peak_price_adj IS NOT NULL"):
                    close = connection.execute("SELECT adjusted_close FROM daily_candles WHERE ticker = ? "
                                               "ORDER BY trade_date DESC LIMIT 1", (r["ticker"],)).fetchone()
                    if close and close[0] / r["peak_price_adj"] - 1.0 <= -0.07:
                        hits.append(f"{r['ticker']}({close[0] / r['peak_price_adj'] - 1.0:+.1%})")
                return ("훈련 모드(스탑 -7%, 위험자산 매수 차단). 즉시 스탑에 도달하는 종목(직전 종가 기준): "
                        + (", ".join(hits) if hits else "없음"))
            if parts[0] == "/clear_halt":
                active = [r[0] for r in connection.execute(
                    "SELECT code FROM safety_flags WHERE flag_type = 'HALT' AND cleared_at IS NULL ORDER BY code")]
                manual = [c for c in active if c in MANUAL_HALT_CODES]
                target = manual if parts[1].lower() == "all" else [c for c in manual if c.lower() == parts[1].lower()]
                return "해제될 원인: " + (", ".join(target) if target else "없음(자동 해제 원인은 강제 해제할 수 없음)")
            if parts[0] == "/ca_confirm":
                row = connection.execute("SELECT ticker, ex_date, detected_factor, cash_amount FROM corporate_actions WHERE id = ?",
                                         (int(parts[1]),)).fetchone()
                if row is None:
                    return "해당 기업행위 후보가 없습니다."
                close = connection.execute("SELECT adjusted_close FROM daily_candles WHERE ticker = ? ORDER BY trade_date DESC LIMIT 1",
                                           (row["ticker"],)).fetchone()
                qty = connection.execute("SELECT quantity FROM positions WHERE ticker = ?", (row["ticker"],)).fetchone()
                estimate = (1 - row["detected_factor"]) * close[0] * qty[0] if close and qty and row["detected_factor"] else None
                return (f"{row['ticker']} ex_date {row['ex_date']} / 조정계수 {row['detected_factor']} / 추정 분배금 "
                        + (f"약 {estimate:,.0f}원" if estimate is not None else "알 수 없음"))
            if parts[0] == "/resume":
                return "일시정지를 해제하면 다음 정상 배치부터 신규 매수가 재개됩니다(과거 배치 소급 실행 없음)."
            if parts[0] == "/emergency":
                return ("EMERGENCY 해제: 낙폭 원인이면 HWM 리베이스와 WARNING 하한이 적용됩니다." if len(parts) > 1
                        else "EMERGENCY: 신규 주문을 막고 미체결 매수를 취소합니다(위험 축소 매도만 허용).")
            if parts[0] == "/approve_real":
                approval = json.loads((Path(approvals_dir) / "real_activation.json").read_text(encoding="utf-8"))
                digest = hashlib.sha256((Path(approvals_dir) / "real_activation.json").read_bytes()).hexdigest()[:8]
                return (f"승인 파일 SHA {digest} / 만료 {approval.get('expires_at')} / 계좌 별칭 {approval.get('account_alias')}")
            if parts[0] == "/classify":
                row = connection.execute("SELECT amount, flow_date, classification_status FROM cash_flows WHERE id = ?",
                                         (int(parts[1]),)).fetchone()
                return f"현금흐름 {parts[1]}: {row['amount']:,}원 / {row['flow_date']} / {row['classification_status']}" if row else None
            return None
        finally:
            connection.close()

    return provider


def build_command_actions(
    connection_factory: Callable[[], sqlite3.Connection],
    *,
    approvals_dir: str | Path,
    backtest_dir: str | Path = "/run/backtests",
    broker: object | None = None,
    run_dir: str | None = None,
    parking_ticker: str | None = None,
    token_expiry: Callable[[], datetime | None] | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> Mapping[str, Callable[[str, str], str]]:
    """`/confirm` 을 제외한 Phase 1 명령 19종의 콜백 표를 만든다(`/add` 는 봇이 직접 처리)."""

    def guarded(fn: Callable[[sqlite3.Connection, list[str], datetime], str]) -> Callable[[str, str], str]:
        """연결 수명 관리와 예외 → 고정 문구 변환을 한 곳에서 한다."""

        def action(_chat_id: str, text: str) -> str:
            """연결 수명·예외 처리·승인 소진을 한 곳에서 처리하는 명령 실행기."""
            connection = connection_factory()
            try:
                reply = fn(connection, text.split(), clock())
                _consume_approval(connection, " ".join(text.split()))  # 승인이 없던 명령이면 아무 일도 없다
                return reply
            except _Refused as refusal:
                return str(refusal)
            except (ops.ApprovalError, ValueError, KeyError, sqlite3.Error, OSError) as error:
                with connection:
                    connection.execute(
                        "INSERT INTO system_events (component, event_type, severity, message, created_at) "
                        "VALUES ('telegram_actions', 'COMMAND_FAILED', 'WARN', ?, ?)",
                        (f"{text.split()[0]}: {type(error).__name__}", clock().isoformat()))
                return FAIL
            finally:
                connection.close()

        return action

    # ───── 조회 ─────
    def status(c, args, now):
        """/status: 운영 모드와 일일 브리핑을 보여 준다."""
        mode = ("SAFE_HALT" if is_safe_halt(c) else "EMERGENCY" if _state(c, "emergency_mode") == "true"
                else "PAUSED" if _state(c, "is_paused") == "true"
                else "TRAINING" if _state(c, "training_mode") == "true" else "NORMAL")
        return f"운영 모드: {mode}\n" + compose_briefing(c, now)

    def preview(c, args, now):
        """/preview: 최근 주간 프리뷰 요약."""
        row = c.execute("SELECT * FROM weekly_previews ORDER BY created_at DESC, preview_revision DESC LIMIT 1").fetchone()
        if row is None:
            return "저장된 주간 프리뷰가 없습니다."
        import json
        ranked = json.loads(row["ranked_candidates_json"])
        names = ("RF_INVALID: 교체 생략(" + ranked.get("reason", "") + ")") if isinstance(ranked, dict) else \
            ", ".join(item["ticker"] for item in ranked[:10]) or "후보 없음"
        return f"프리뷰 기준일 {row['base_candle_date']} / 상태 {row['status']}\n순위: {names}"

    def pool(c, args, now):
        """/pool: 활성 종목의 종가·120MA·모멘텀 점수와 점수순 순위."""
        parking = parking_ticker
        series = {}
        for r in c.execute("SELECT ticker FROM universe_assets WHERE status = 'ACTIVE'"):
            series[r["ticker"]] = {x["trade_date"]: x["adjusted_close"] for x in c.execute(
                "SELECT trade_date, adjusted_close FROM daily_candles WHERE ticker = ? ORDER BY trade_date DESC LIMIT 200",
                (r["ticker"],))}
        rows = []
        for r in c.execute("SELECT ticker, category, data_status FROM universe_assets WHERE status = 'ACTIVE' ORDER BY category, ticker"):
            data = series[r["ticker"]]
            dates = sorted(data)
            closes = [data[d] for d in dates]
            sma = f"{sum(closes[-120:]) / 120:,.0f}" if len(closes) >= 120 else "-"
            score = None
            if r["category"] == "RISK_ASSET" and parking in series:
                base = series[parking]
                common = [d for d in dates if d in base]
                if len(common) >= 121:
                    try:
                        score = calculate_momentum_score([data[d] for d in common], [base[d] for d in common])
                    except ValueError:
                        score = None
            rows.append((r["ticker"], r["category"], r["data_status"], closes[-1] if closes else None, sma, score))
        ranked = sorted((x for x in rows if x[5] is not None), key=lambda x: -x[5])
        rank = {x[0]: i + 1 for i, x in enumerate(ranked)}
        lines = [f"{t} {cat} {ds} 종가 {(f'{cl:,.0f}' if cl else '-')} / 120MA {sma} / 점수 "
                 + (f"{sc:.3f} (순위 {rank[t]})" if sc is not None else "-") for t, cat, ds, cl, sma, sc in rows]
        return "\n".join(lines) or "활성 종목이 없습니다."

    def trades(c, args, now):
        """/trades: 최근 주문·체결 내역."""
        limit = int(args[1]) if len(args) == 2 else 10
        rows = c.execute("SELECT ticker, side, requested_qty, filled_qty, status, order_price, submitted_at "
                         "FROM orders ORDER BY submitted_at DESC LIMIT ?", (limit,)).fetchall()
        return "\n".join(f"{r['submitted_at'][:16]} {r['ticker']} {r['side']} {r['filled_qty']}/{r['requested_qty']}주 "
                         f"@{r['order_price']:,} {r['status']}" for r in rows) or "주문 내역이 없습니다."

    def health(c, args, now):
        """/health: 대조·잠금·정지 원인·스탑 감시 주기와 CPU·온도·디스크·DB·NTP·UPS·KIS 토큰 상태."""
        import os
        import shutil

        last = _state(c, "last_reconciliation_at") or "없음"
        lines = [f"대조 상태: {_state(c, 'reconciliation_ok') or '미확인'} / 마지막 대조: {last}",
                 f"주문 잠금: {_state(c, 'scheduler_order_lock') or '미설정'} / "
                 f"SAFE_HALT 원인: {', '.join(active_halt_reasons(c)) or '없음'}",
                 f"스탑 감시 주기: {_state(c, 'guard_poll_interval_seconds') or '10'}초"
                 + (" (한도로 연장됨: 스탑 판정이 그만큼 지연될 수 있음)"
                    if int(_state(c, 'guard_poll_interval_seconds') or 10) > 10 else "")]
        try:
            load = f"{os.getloadavg()[0]:.2f}"
        except (AttributeError, OSError):
            load = "n/a"
        try:
            temp = f"{int(Path('/sys/class/thermal/thermal_zone0/temp').read_text()) / 1000:.1f}°C"
        except (OSError, ValueError):
            temp = "n/a"
        usage = shutil.disk_usage(Path(".").resolve().anchor or "/")
        lines.append(f"CPU 부하 {load} / 온도 {temp} / 디스크 {usage.used / usage.total:.0%} 사용")
        lines.append("DB 무결성: " + c.execute("PRAGMA quick_check").fetchone()[0])
        if run_dir:
            ups = read_host_state(Path(run_dir) / "ups.json", now=now, ttl_seconds=120)
            clock_state = read_host_state(Path(run_dir) / "clock.json", now=now, ttl_seconds=120)
            lines.append("UPS: " + (f"배터리 {ups.get('battery_ratio', '?')}" if ups else "상태 불명(감시 파일 없음·오래됨)")
                         + " / NTP: " + (f"오차 {clock_state.get('offset_seconds', '?')}초" if clock_state else "상태 불명"))
        expiry = token_expiry() if token_expiry else None
        lines.append("KIS 토큰 만료: " + (expiry.astimezone(KST).strftime("%m-%d %H:%M") if expiry else "알 수 없음"))
        return "\n".join(lines)

    def reconcile(c, args, now):
        """/reconcile: KIS 와 DB 를 즉시 비교해 차이를 보여 준다(읽기만, 상태 덮어쓰기 없음) 또는 KIS 보유 수량 채택(2단계)."""
        if args[1:] == ["adopt"]:
            if broker is None:
                raise _Refused("증권사 조회가 연결되지 않아 채택할 수 없습니다.")
            diff = ops.adopt_broker_positions(
                c, {p.ticker: p.quantity for p in broker.positions()},
                action_id=_approved_action_id(c, " ".join(args), now), now=now)
            return f"KIS 보유 수량을 채택했습니다(차이 {len(diff)}건). 대조는 다음 마감 후 잡에서 재확인됩니다."
        if broker is None:
            return (f"대조 상태: {_state(c, 'reconciliation_ok') or '미확인'} / 마지막 대조: "
                    f"{_state(c, 'last_reconciliation_at') or '없음'}\n(증권사 조회가 연결되지 않아 즉시 대조는 하지 않았습니다)")
        today = now.astimezone(KST).date()
        result = compare_account(c, broker.positions(), broker.orders(today), today)
        if result.reconciliation_ok:
            return "KIS 와 DB 가 일치합니다. 수동 채택은 /reconcile adopt (2단계 확인)."
        return ("불일치 — 보유 수량: " + (", ".join(result.mismatches) or "없음")
                + " / 주문: " + (", ".join(result.order_mismatches) or "없음")
                + (" / 고아 주문: " + ", ".join(result.orphan_orders) if result.orphan_orders else "")
                + "\n(읽기 전용 비교입니다. 채택은 /reconcile adopt)")

    # ───── 운영 모드 ─────
    def pause(c, args, now):
        """/pause·/stop: 신규 매수를 막는 일시정지."""
        _set_state(c, "is_paused", "true", now)
        return "일시정지: 신규 매수를 막고 보호 매도·취소만 허용합니다. 해제는 /resume (2단계 확인)."

    def resume(c, args, now):
        """/resume: 일시정지 해제(2단계 확인)."""
        if is_safe_halt(c):
            raise _Refused("SAFE_HALT 중에는 /resume 으로 풀 수 없습니다. /clear_halt 로 원인을 해제하세요.")
        _approved_action_id(c, " ".join(args), now)
        _set_state(c, "is_paused", "false", now)
        return "일시정지를 해제했습니다."

    def training(c, args, now):
        """/training: 훈련 모드 켜기·끄기(2단계 확인)."""
        _approved_action_id(c, " ".join(args), now)
        if args[1] == "off":
            _set_state(c, "training_mode", "false", now)
            _set_state(c, "training_until", "", now)
            return "훈련 모드를 해제했습니다(다음 평가부터 정상 규칙)."
        until = (now.astimezone(KST).date() + timedelta(days=int(args[2]))).isoformat() if len(args) == 3 else ""
        _set_state(c, "training_mode", "true", now)
        _set_state(c, "training_until", until, now)
        return f"훈련 모드 활성화(스탑 -7%, 위험자산 매수 차단){' / 종료일 ' + until if until else ' / 무기한'}"

    def emergency(c, args, now):
        """/emergency: EMERGENCY 켜기·끄기(2단계 확인)."""
        _approved_action_id(c, " ".join(args), now)
        if len(args) == 2:  # off
            rebase = ops.release_emergency(
                c, action_id=_approved_action_id(c, " ".join(args), now),
                floor_until_date=(now.astimezone(KST).date() + timedelta(days=7)).isoformat(), now=now)
            return "EMERGENCY 를 해제했습니다." + (" HWM 리베이스가 예약되었습니다." if rebase else "")
        _set_state(c, "emergency_mode", "true", now)
        _set_state(c, "emergency_reason", "MANUAL", now)
        return "EMERGENCY 활성화: 위험 축소 매도만 허용합니다."

    # ───── 운영자 승인·정정 ─────
    def clear_halt(c, args, now):
        """/clear_halt [원인|ALL]: 수동 해제 원인을 해제한다(2단계 확인, 대조 성공 필요, 자동 해제 원인은 강제 해제 불가)."""
        action_id = _approved_action_id(c, " ".join(args), now)
        reconciled = _state(c, "reconciliation_ok") == "true"
        if args[1].lower() == "all":
            codes = [code for code in active_halt_reasons(c) if code in MANUAL_HALT_CODES]
            if not codes:
                raise _Refused("해제할 수동 해제 원인이 없습니다(자동 해제 원인은 조건 충족 시 스스로 풀립니다).")
            cleared = [code for code in codes if ops.clear_halt(c, code=code, action_id=action_id,
                                                                reconciliation_ok=reconciled, now=now)]
            if not cleared:
                raise _Refused("해제 조건(대조 성공)을 만족하지 않아 유지됩니다.")
            return "해제했습니다: " + ", ".join(cleared)
        ok = ops.clear_halt(c, code=args[1].upper(), action_id=action_id, reconciliation_ok=reconciled, now=now)
        if not ok:
            raise _Refused(f"{args[1].upper()} 는 해제 조건(대조 성공·수동 해제 원인)을 만족하지 않아 유지됩니다.")
        return f"{args[1].upper()} 해제했습니다."

    def classify(c, args, now):
        """/classify: 미분류 현금흐름을 분류한다."""
        ops.classify_cash_flow(c, flow_id=int(args[1]), flow_type=args[2].upper(),
                               action_id=_approved_action_id(c, " ".join(args), now), now=now)
        return f"현금흐름 {args[1]} 을(를) {args[2].upper()} 로 처리했습니다."

    def ca_confirm(c, args, now):
        """/ca_confirm: 감지된 기업행위를 검증하고 최고가를 재기준화한다."""
        factor = ops.confirm_corporate_action(c, corporate_action_id=int(args[1]),
                                              action_id=_approved_action_id(c, " ".join(args), now), now=now)
        return f"기업행위 {args[1]} 검증 완료(조정계수 {factor:.4f}), PeakPrice 재기준화·격리 해제."

    def remove(c, args, now):
        """/remove: 유니버스에서 종목을 제거한다."""
        _approved_action_id(c, " ".join(args), now)
        with c:
            cur = c.execute("UPDATE universe_assets SET status = 'REMOVED', removed_at = ? WHERE ticker = ? "
                            "AND status = 'ACTIVE'", (now.isoformat(), args[1]))
        if cur.rowcount != 1:
            raise ValueError("Ticker is not an active universe asset.")
        return f"{args[1]} 을(를) 유니버스에서 제거했습니다. 보유 중이면 다음 15:00 판정에서 퇴출 매도됩니다."

    def approve_real(c, args, now):
        """/approve_real: REAL 활성화 승인 레코드를 만든다."""
        ops.approve_real_activation(c, approval_id=args[1], action_id=_approved_action_id(c, " ".join(args), now),
                                    approvals_dir=approvals_dir, now=now)
        return "REAL 활성화 승인 레코드를 만들었습니다(호스트 승인 파일과 일치 확인). 실계좌 연결은 별도 단계입니다."

    def approve_smoke(c, args, now):
        """/approve_smoke: 스모크 세션(최대 60분)을 승인한다."""
        ops.approve_smoke_session(c, approval_id=args[1], action_id=_approved_action_id(c, " ".join(args), now),
                                  approvals_dir=approvals_dir, now=now)
        return "스모크 세션(최대 60분)을 승인했습니다."

    def approve_backtest(c, args, now):
        """`/approve_backtest [run_id] [report_sha256]`: 보고서 파일의 정규화 해시가 인자와 같고, 보고서의 전략 해시가
        현재 설정과 같을 때만 승인한다(다른 전략의 백테스트로 승인되는 일을 막는다)."""
        action_id = _approved_action_id(c, " ".join(args), now)
        run_id, digest = args[1], args[2]
        report_path = Path(backtest_dir) / f"{run_id}.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        canonical = json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != digest:
            raise ValueError("Report hash does not match.")
        if report["strategy_params_hash"] != strategy_params_hash(load_config()):
            raise ValueError("Report was produced for a different strategy configuration.")
        start, end = report["data_range"]
        ops.approve_backtest(c, run_id=run_id, report_sha256=digest, code_commit_sha=report["code_commit_sha"],
                             strategy_params_hash=report["strategy_params_hash"], data_range=(start, end),
                             action_id=action_id, now=now)
        return f"백테스트 {run_id} 승인 완료(전략 해시 일치, 기간 {start}~{end})."

    return {
        "/status": guarded(status), "/preview": guarded(preview), "/pool": guarded(pool),
        "/trades": guarded(trades), "/health": guarded(health), "/reconcile": guarded(reconcile),
        "/pause": guarded(pause), "/stop": guarded(pause), "/resume": guarded(resume),
        "/training": guarded(training), "/emergency": guarded(emergency),
        "/remove": guarded(remove), "/clear_halt": guarded(clear_halt), "/classify": guarded(classify),
        "/ca_confirm": guarded(ca_confirm), "/approve_real": guarded(approve_real),
        "/approve_smoke": guarded(approve_smoke), "/approve_backtest": guarded(approve_backtest),
        "/add": lambda _chat, _text: "종목 추가는 공식 메타데이터 확인 후 봇이 직접 처리합니다.",
    }
