"""와이파이 서베이 서버.

노트북에서 이걸 켜고, 폰 브라우저로 접속해서 집을 한 바퀴 돈다.

    python survey.py
    → 터미널에 뜬 QR 을 폰 카메라로 찍는다

노트북은 **측정의 상대편**이지 측정 대상이 아니다. 폰이 지연과 속도를 재는데,
그 상대가 이 서버다. 그래서 노트북은 한자리에 두고 안 움직여야 한다 — 움직이면
폰 쪽 변화인지 노트북 쪽 변화인지 구분이 안 된다.

**노트북을 공유기에 유선으로 꽂으면 제일 좋다.** 둘 다 무선이면 두 무선 구간이
직렬로 붙어서, 노트북 구간이 병목일 때 폰이 어디로 가든 같은 값이 나온다.
유선이 안 되면 노트북을 공유기 바로 옆에 두는 것으로도 충분하다.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qr  # noqa: E402
from config import WifiConfig  # noqa: E402
from probe import AdbRssi  # noqa: E402

HERE = Path(__file__).resolve().parent
WEB_DIR = HERE / "web"

# 윈도우 콘솔이 기본 코드페이지면 한글이 깨진다. 앞의 두 트래커와 같은 처리다.
#
# line_buffering 은 여기서만 추가로 켠다. 이 프로그램은 화면에 뭘 그리지 않고
# **기동할 때 딱 한 번 주소를 찍는 게 전부**인데, 콘솔이 아닌 곳으로 출력이
# 가면(IDE 터미널, 파일로 리다이렉트) 파이썬이 블록 버퍼링으로 바뀌어서 그
# 주소가 안 나온다. 폰에 칠 주소를 모르면 이 프로그램은 아무짝에 쓸모가 없다.
try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass


def lan_addresses() -> list[str]:
    """폰에서 칠 수 있는 이 노트북의 주소들.

    기본 경로로 나가는 주소를 먼저 놓는다. 가상 어댑터(VMware, WSL, Hyper-V)가
    깔린 컴퓨터는 IPv4 주소가 대여섯 개씩 나오는데, 폰에서 닿는 건 보통 그중
    하나뿐이라 순서가 곧 사용성이다.
    """
    found: list[str] = []
    # 실제로 패킷을 보내지는 않는다. UDP connect 는 라우팅 테이블만 참조한다.
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        found.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addr = info[4][0]
            if addr not in found and not addr.startswith("127."):
                found.append(addr)
    except socket.gaierror:
        pass
    return found


class SurveyServer(ThreadingHTTPServer):
    daemon_threads = True
    # 서버를 껐다 켤 때 "주소가 이미 사용 중"으로 안 막히게. 튜닝하면서
    # 수십 번 재시작하게 된다.
    allow_reuse_address = True

    def __init__(self, cfg: WifiConfig):
        super().__init__((cfg.host, cfg.port), SurveyHandler)
        self.cfg = cfg
        self.save_path = HERE / cfg.save_path
        self.rssi = AdbRssi(cfg.adb_path, cfg.adb_timeout_s) if cfg.enable_adb_rssi else None
        # 속도 측정용 버퍼를 한 번만 만든다. 청크마다 새로 난수를 만들면
        # 파이썬이 병목이 되어서, 재는 게 와이파이가 아니라 CPU 가 된다.
        self.blob = os.urandom(cfg.speed_chunk_bytes)

    def handle_error(self, request, client_address) -> None:
        """끊긴 연결로는 트레이스백을 안 찍는다.

        **속도 측정은 매번 클라이언트가 먼저 끊는 것으로 끝난다.** 그게
        설계다 — 정해진 시간만큼 받고 손을 뗀다. 그런데 socketserver 의
        기본 동작은 그 끊김을 처리되지 않은 예외로 보고 화면에 20줄짜리
        트레이스백을 찍는다.

        지점 하나 잴 때마다 그게 한 번씩 쌓이면, 정작 봐야 할 '접속 —' 과
        '저장됨 —' 이 트레이스백 사이에 파묻힌다. 폰이 안 붙을 때 그 두 줄이
        유일한 단서라서, 묻히면 진단할 방법이 없어진다.
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class SurveyHandler(BaseHTTPRequestHandler):
    # keep-alive 를 쓰려면 1.1 이어야 한다. 지연 측정에서 이게 핵심이다 —
    # 매번 TCP 를 새로 열면 핸드셰이크 왕복이 값에 섞여서, 재려는 것보다
    # 큰 상수가 얹힌다.
    protocol_version = "HTTP/1.1"
    server_version = "WifiSurvey/1.0"

    # 접속한 적 있는 클라이언트 IP. 폰이 닿았는지를 아는 유일한 방법이다.
    _seen: set = set()

    def note_client(self) -> None:
        """새 기기가 처음 붙었을 때만 한 줄 찍는다.

        요청 단위 로그는 꺼 두었다 — 지점 하나 잴 때 /api/ping 이 15번씩
        오므로 콘솔이 핑으로 덮인다. 하지만 '폰이 아예 못 닿는' 상황에서는
        그 침묵이 곧 정보 부족이 된다. 안 열리는 이유가 방화벽인지, 다른
        네트워크에 붙어 있어서인지, 주소를 잘못 친 것인지 구분할 수가 없다.

        기기당 한 줄이면 그 구분이 된다. 줄이 뜨면 네트워크는 통한 것이고,
        안 뜨면 패킷이 여기까지 오지도 못한 것이다.
        """
        ip = self.client_address[0]
        if ip not in SurveyHandler._seen:
            SurveyHandler._seen.add(ip)
            print(f"  접속 — {ip}")

    # --- 라우팅 ---------------------------------------------------------

    def do_GET(self) -> None:
        self.note_client()
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self.serve_page()
        elif path == "/api/config":
            self.serve_config()
        elif path == "/api/ping":
            self.serve_ping()
        elif path == "/api/blob":
            self.serve_blob()
        elif path == "/api/rssi":
            self.serve_rssi()
        elif path == "/api/points":
            self.serve_points()
        else:
            self.send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:
        self.note_client()
        if urlparse(self.path).path == "/api/points":
            self.save_points()
        else:
            self.send_json({"error": "not found"}, status=404)

    # --- 페이지 ---------------------------------------------------------

    def serve_page(self) -> None:
        page = WEB_DIR / "survey.html"
        try:
            body = page.read_bytes()
        except OSError:
            self.send_text(f"survey.html 을 찾을 수 없습니다: {page}", status=500)
            return
        self.send_bytes(body, "text/html; charset=utf-8")

    # --- 측정 -----------------------------------------------------------

    def serve_config(self) -> None:
        """측정 파라미터를 클라이언트에 넘긴다.

        폰 쪽에 같은 숫자를 복사해 두지 않는 이유: 튜닝은 config.py 에서
        한다고 문서에 써 놨는데 실제로는 HTML 의 상수가 이기면, 값을 바꿔도
        아무 일이 안 일어나는 상황이 된다. 그 증상은 '튜닝이 안 먹는다'로
        나타나서 원인을 찾기가 유난히 어렵다.
        """
        cfg = self.server.cfg
        self.send_json({
            "ping_count": cfg.ping_count,
            "ping_warmup": cfg.ping_warmup,
            "speed_window_s": cfg.speed_window_s,
            "speed_rampup_s": cfg.speed_rampup_s,
            "idw_power": cfg.idw_power,
            "idw_radius_frac": cfg.idw_radius_frac,
            "heatmap_cell_px": cfg.heatmap_cell_px,
        })

    def serve_ping(self) -> None:
        """왕복시간 측정용. 본문이 작을수록 좋다.

        재려는 게 왕복 지연이지 대역폭이 아니다. 본문이 커지면 전송 시간이
        섞여서 둘의 합을 재게 된다.
        """
        self.send_bytes(b"1", "text/plain", cache=False)

    def serve_blob(self) -> None:
        """정해진 시간 동안 계속 밀어 넣는다. 클라이언트가 끊으면 끝난다.

        Content-Length 를 안 보낸다. 몇 바이트를 보낼지 서버가 모르기
        때문이다 — 끝나는 시점을 정하는 건 클라이언트다. HTTP/1.1 에서
        Connection: close 면 본문은 EOF 까지고, 그게 정확히 이 상황이다.
        """
        cfg = self.server.cfg
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

        blob = self.server.blob
        deadline = time.monotonic() + cfg.speed_max_seconds
        try:
            while time.monotonic() < deadline:
                self.wfile.write(blob)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            # 정상 종료 경로다. 클라이언트가 창을 닫아서 끊는 것과, 측정을
            # 마쳐서 끊는 것이 여기서는 구분되지 않고 구분할 필요도 없다.
            pass

    def serve_rssi(self) -> None:
        probe = self.server.rssi
        if probe is None:
            self.send_json({"available": False, "reason": "설정에서 꺼져 있습니다"})
            return
        if not probe.available():
            self.send_json({"available": False, "reason": probe.last_error or "기기 없음"})
            return
        reading = probe.read()
        if reading is None:
            self.send_json({"available": False, "reason": probe.last_error or "읽기 실패"})
            return
        self.send_json({"available": True, **reading})

    # --- 저장 -----------------------------------------------------------

    def serve_points(self) -> None:
        try:
            body = self.server.save_path.read_text(encoding="utf-8")
        except OSError:
            body = '{"points": [], "plan": null}'
        self.send_bytes(body.encode("utf-8"), "application/json", cache=False)

    def save_points(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        # 평면도 이미지가 dataURL 로 같이 온다. 폰 카메라 사진이면 몇 MB 다.
        if length > 32 * 1024 * 1024:
            self.send_json({"error": "본문이 너무 큽니다"}, status=413)
            return
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.send_json({"error": f"JSON 파싱 실패: {exc}"}, status=400)
            return

        path = self.server.save_path
        # 임시 파일에 쓰고 바꿔치운다. 그냥 덮어쓰면 쓰는 도중에 끊겼을 때
        # 반쯤 쓰인 파일이 남아서, 다음에 열면 서베이 전체가 날아간다.
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            self.send_json({"error": f"저장 실패: {exc}"}, status=500)
            return

        n = len(data.get("points", []))
        print(f"  저장됨 — 측정점 {n}개 → {path.name}")
        self.send_json({"ok": True, "points": n})

    # --- 응답 유틸 ------------------------------------------------------

    def send_bytes(self, body: bytes, ctype: str, cache: bool = True) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if not cache:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def send_json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def send_text(self, text: str, status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def log_message(self, fmt: str, *args) -> None:
        """/api/ping 이 한 지점당 열여덟 번 찍힌다. 그대로 두면 콘솔이
        핑 로그로 가득 차서 정작 봐야 할 줄이 안 보인다."""
        return


def main() -> int:
    cfg = WifiConfig()
    ap = argparse.ArgumentParser(description="와이파이 서베이 서버")
    ap.add_argument("--port", type=int, default=cfg.port)
    ap.add_argument("--no-adb", action="store_true", help="RSSI 읽기를 끈다")
    args = ap.parse_args()

    cfg.port = args.port
    if args.no_adb:
        cfg.enable_adb_rssi = False

    try:
        server = SurveyServer(cfg)
    except OSError as exc:
        print(f"[wifi] 포트 {cfg.port} 를 열 수 없습니다: {exc}")
        print(f"[wifi] 이미 켜져 있거나, 다른 프로그램이 쓰고 있습니다.")
        return 1

    addrs = lan_addresses()
    print()
    print("  와이파이 서베이 서버가 떴습니다.")
    print()

    if addrs:
        url = f"http://{addrs[0]}:{cfg.port}"
        # QR 은 첫 주소 하나만 만든다. 여러 개를 찍으면 어느 걸 찍어야 하는지를
        # 사용자가 판단해야 하는데, 그 판단이 바로 QR 로 없애려던 일이다.
        # 첫 주소는 기본 경로로 나가는 것이라 폰에서 닿을 확률이 제일 높다.
        art = qr.render(url)
        if art:
            print("  폰 카메라로 이 QR 을 찍으세요.")
            print()
            print(art)
            print()
        else:
            print("  폰 브라우저에서 아래 주소를 엽니다:")
        print(f"      {url}")
        for addr in addrs[1:]:
            print(f"      (또는 http://{addr}:{cfg.port})")
    else:
        print("  LAN 주소를 못 찾았습니다. 와이파이가 연결돼 있나요?")
    print()
    print("  폰과 노트북이 **같은 공유기**에 붙어 있어야 합니다.")
    print("  안 열리면 방화벽입니다 — 관리자 권한 PowerShell 에서:")
    print(f'      New-NetFirewallRule -DisplayName "WIFI-eye" -Direction Inbound '
          f'-LocalPort {cfg.port} -Protocol TCP -Action Allow')
    print()

    if cfg.enable_adb_rssi:
        if server.rssi.available():
            reading = server.rssi.read()
            if reading:
                print(f"  adb 연결됨 — RSSI {reading['rssi']} dBm ({reading.get('ssid')})")
            else:
                print(f"  adb 는 있는데 값을 못 읽습니다: {server.rssi.last_error}")
        else:
            print(f"  RSSI 없이 진행합니다 ({server.rssi.last_error}).")
            print("  지연과 속도만으로도 서베이는 됩니다. README 참고.")
        print()

    print("  Ctrl+C 로 종료.")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  종료합니다.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
