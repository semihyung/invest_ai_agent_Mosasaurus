"""호스트 상태 기록기: UPS·NTP 상태를 `run/ups.json`, `run/clock.json` 에 원자적으로 기록합니다.

컨테이너는 장치(I2C/USB)에 직접 접근하지 않고 이 파일만 읽기 전용으로 참조합니다(명세 3.2절).
UPS HAT 마다 인터페이스가 다르므로 읽기 방식은 환경변수 `UPS_STATUS_COMMAND`(JSON 한 줄을
출력하는 명령: {"battery_ratio": 0.0~1.0, "on_external_power": true|false})로 주입합니다.
명령이 없거나 실패하면 파일을 갱신하지 않으며, 파일이 TTL(120초)을 넘기면 앱이 `UPS_LOW`·
`CLOCK_UNSYNCED` 를 활성화합니다(fail-closed). systemd 서비스가 10초마다 실행합니다.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def parse_chrony_tracking(output: str) -> dict:
    """`chronyc tracking` 출력에서 동기화 여부와 오차(초)를 추출합니다."""
    system_time = re.search(
        r"System time\s*:\s*([0-9.eE+-]+) seconds (fast|slow) of NTP time", output
    )
    leap = re.search(r"Leap status\s*:\s*(\S+)", output)
    if system_time is None or leap is None:
        return {"synchronized": False, "offset_seconds": None}
    offset = float(system_time.group(1)) * (1 if system_time.group(2) == "fast" else -1)
    return {"synchronized": leap.group(1) == "Normal", "offset_seconds": offset}


def parse_ups_status(output: str) -> dict | None:
    """UPS 상태 명령의 JSON 출력을 검증합니다(범위를 벗어나면 None)."""
    try:
        payload = json.loads(output)
        ratio = payload["battery_ratio"]
        power = payload["on_external_power"]
    except (ValueError, KeyError, TypeError):
        return None
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0 <= ratio <= 1:
        return None
    if not isinstance(power, bool):
        return None
    return {"battery_ratio": float(ratio), "on_external_power": power}


def write_atomic(path: Path, payload: dict, *, now: datetime) -> None:
    """같은 디렉터리의 임시 파일에 쓰고 fsync 후 원자적으로 교체합니다."""
    data = json.dumps({**payload, "updated_at": now.astimezone(timezone.utc).isoformat()})
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # mkstemp 는 0600 으로 만든다: 컨테이너(uid 10001)가 읽기 전용으로 읽어야 하므로 열어 둔다(비밀값 없음).
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        os.unlink(temporary)
        raise


def main() -> int:
    """UPS·NTP 상태를 한 번 읽어 기록합니다(둘 중 읽기에 성공한 것만 갱신)."""
    run_dir = Path(os.environ.get("HOST_RUN_DIR", "/home/pi/mosasaurus/run"))
    run_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    command = os.environ.get("UPS_STATUS_COMMAND")
    if command:
        try:
            result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=5, check=True)  # noqa: S602
            ups = parse_ups_status(result.stdout)
            if ups is not None:
                write_atomic(run_dir / "ups.json", ups, now=now)
        except (subprocess.SubprocessError, OSError):
            pass
    try:
        tracking = subprocess.run(["chronyc", "tracking"], capture_output=True, text=True, timeout=5, check=True)
        write_atomic(run_dir / "clock.json", parse_chrony_tracking(tracking.stdout), now=now)
    except (subprocess.SubprocessError, OSError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
