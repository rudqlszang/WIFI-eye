"""안드로이드 폰의 RSSI 를 adb 로 읽는다. 선택 기능이다.

왜 이런 우회로가 필요한가: **브라우저는 신호 세기를 못 읽는다.** 웹 표준에 그런
API 가 없고, 앞으로 생길 것 같지도 않다 (주변 AP 목록은 꽤 정확한 실내 위치가
되기 때문에 브라우저가 열어 줄 만한 정보가 아니다).

읽는 방법이 셋인데 이걸 골랐다.

    안드로이드 앱     제일 정확. 빌드·서명·설치가 붙고 폰에 앱이 남는다
    Termux + API      설치는 가볍지만 폰에 앱 두 개가 남는다
    adb (이것)        폰에 아무것도 안 남는다. 노트북에만 platform-tools

셋 다 안 되면 그냥 안 쓰면 된다. RSSI 없이도 서베이는 완결된다 — 오히려
"이 자리에서 실제로 얼마나 나오나"는 속도 쪽이 답한다. RSSI 는 그 값이
이상할 때 원인을 가르는 용도다.

    속도 느림 + RSSI 좋음   →  전파는 오는데 채널이 막혔다. 공유기 채널을 바꾼다
    속도 느림 + RSSI 나쁨   →  전파가 안 온다. 공유기를 옮기거나 중계기를 놓는다

이 구분이 RSSI 를 붙이는 유일한 이유다. 그게 아니면 숫자 하나 더 늘 뿐이다.
"""

from __future__ import annotations

import re
import subprocess
import sys

# adb 호출마다 콘솔 창이 깜빡이지 않게 한다. 서베이 한 번에 수십 번 부르므로
# 이게 없으면 검은 창이 계속 튄다.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

_RSSI_RE = re.compile(r"\bRSSI:\s*(-?\d+)", re.IGNORECASE)
_LINK_RE = re.compile(r"\bLink speed:\s*(\d+)\s*Mbps", re.IGNORECASE)
_SSID_RE = re.compile(r"\bSSID:\s*\"?([^\",]+)", re.IGNORECASE)


class AdbRssi:
    """`adb shell cmd wifi status` 를 파싱한다.

    Android 11 부터 `cmd wifi status` 가 한 줄에 RSSI·링크속도·SSID 를 다 준다.
    그 전 버전은 `dumpsys wifi` 로 폴백하는데, 출력이 훨씬 길고 기기마다
    달라서 실패할 수 있다. 실패하면 None 을 돌려주고 그걸로 끝이다 —
    RSSI 하나 때문에 서베이가 멈추면 안 된다.
    """

    def __init__(self, adb_path: str = "adb", timeout_s: float = 3.0):
        self.adb_path = adb_path
        self.timeout_s = timeout_s
        self.last_error: str | None = None
        self._checked = False
        self._present = False

    # --- 상태 -----------------------------------------------------------

    def available(self) -> bool:
        """adb 실행 파일이 있고 기기가 하나 이상 붙어 있나.

        결과를 캐시하지 않는다. 폰은 서베이 도중에도 붙었다 떨어졌다 하고
        (무선 디버깅은 특히 그렇다), 한 번 없다고 영영 없는 걸로 처리하면
        중간에 다시 붙여도 안 살아난다.
        """
        out = self._run(["devices"])
        if out is None:
            return False
        # 첫 줄은 "List of devices attached" 헤더다.
        for line in out.splitlines()[1:]:
            line = line.strip()
            if not line:
                continue
            if line.endswith("\tdevice"):
                return True
            if line.endswith("\tunauthorized"):
                self.last_error = "폰 화면의 USB 디버깅 허용 창을 눌러 주세요"
            elif line.endswith("\toffline"):
                self.last_error = "기기가 offline 입니다"
        if self.last_error is None:
            self.last_error = "연결된 기기가 없습니다"
        return False

    # --- 측정 -----------------------------------------------------------

    def read(self) -> dict | None:
        """{'rssi': -55, 'link_mbps': 72, 'ssid': '...'} 또는 None."""
        out = self._run(["shell", "cmd", "wifi", "status"])
        if out is None or _RSSI_RE.search(out) is None:
            # Android 10 이하 폴백. 출력이 수천 줄이라 느리다.
            out = self._run(["shell", "dumpsys", "wifi"])
        if out is None:
            return None

        m = _RSSI_RE.search(out)
        if m is None:
            self.last_error = "출력에서 RSSI 를 못 찾았습니다"
            return None

        rssi = int(m.group(1))
        # dumpsys 폴백은 연결이 끊긴 상태의 자리표시자로 -127 을 내놓는다.
        # 이걸 그대로 쓰면 히트맵에 "여기가 최악"인 점이 하나 박힌다.
        if rssi <= -127 or rssi >= 0:
            self.last_error = f"신호 값이 유효 범위 밖입니다 ({rssi})"
            return None

        link = _LINK_RE.search(out)
        ssid = _SSID_RE.search(out)
        self.last_error = None
        return {
            "rssi": rssi,
            "link_mbps": int(link.group(1)) if link else None,
            "ssid": ssid.group(1).strip() if ssid else None,
        }

    # --- 내부 -----------------------------------------------------------

    def _run(self, args: list[str]) -> str | None:
        try:
            proc = subprocess.run(
                [self.adb_path, *args],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=self.timeout_s,
                creationflags=_NO_WINDOW,
            )
        except FileNotFoundError:
            self.last_error = "adb 를 찾을 수 없습니다 (platform-tools 미설치)"
            return None
        except subprocess.TimeoutExpired:
            # 무선 디버깅에서 폰이 신호 약한 구석에 있으면 실제로 난다.
            # 정확히 그런 자리를 재려고 걸어간 것이라 드물지 않다.
            self.last_error = "adb 응답 시간 초과"
            return None
        except OSError as exc:
            self.last_error = f"adb 실행 실패: {exc}"
            return None

        if proc.returncode != 0:
            self.last_error = (proc.stderr or proc.stdout or "").strip()[:200]
            return None
        return proc.stdout
