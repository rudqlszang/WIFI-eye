"""QR 코드 인코더 — 표준 라이브러리만 쓴다.

왜 라이브러리를 안 쓰는가: 이 도구는 의존성이 표준 라이브러리뿐이라 받아서
바로 돌아간다. QR 하나 그리자고 pip 설치를 붙이면 "폰으로 주소 치기
귀찮다"를 "파이썬 패키지부터 깔아라"로 바꾸는 것이라, 편해지려고 넣은
기능이 오히려 진입 장벽이 된다.

인터넷 QR API 도 안 쓴다. 집 내부 IP 를 남의 서버에 보내는 것이고, 정작
와이파이가 시원찮을 때 쓰는 도구가 인터넷을 요구하면 곤란하다.

범위를 좁혀서 짧게 유지했다.

    바이트 모드      URL 이라 영숫자 모드의 제약(대문자만)을 안 맞춰도 된다
    오류정정 M       약 15% 복원. 화면을 카메라로 찍는 상황에 맞다
    버전 1~10       최대 213바이트. LAN 주소는 30자 안쪽이라 넘칠 일이 없다

구현은 ISO/IEC 18004 를 따랐고, 결과 행렬이 레퍼런스 구현(segno)과 비트 단위로
같은지 대조해서 확인했다 — 잘못된 QR 은 없는 것보다 나쁘다. 안 읽히면 그나마
낫고, 엉뚱한 주소로 읽히면 최악이다.
"""

from __future__ import annotations

import sys

# --- 갈루아 체 GF(256) ------------------------------------------------------
# 원시 다항식 0x11D. 리드-솔로몬이 도는 바닥.
_EXP = [0] * 512
_LOG = [0] * 256


def _init_gf() -> None:
    x = 1
    for i in range(255):
        _EXP[i] = x
        _LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        _EXP[i] = _EXP[i - 255]


_init_gf()


def _mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _gen_poly(n: int) -> list[int]:
    """n개의 오류정정 코드워드를 만드는 생성 다항식."""
    g = [1]
    for i in range(n):
        nxt = [0] * (len(g) + 1)
        for j, c in enumerate(g):
            nxt[j] ^= c                      # ×x
            nxt[j + 1] ^= _mul(c, _EXP[i])   # ×a^i
        g = nxt
    return g


def _rs_encode(data: list[int], n: int) -> list[int]:
    """데이터 코드워드 → 오류정정 코드워드 n개."""
    g = _gen_poly(n)
    rem = [0] * n
    for d in data:
        factor = d ^ rem[0]
        rem = rem[1:] + [0]
        if factor:
            lf = _LOG[factor]
            for i in range(n):
                if g[i + 1]:
                    rem[i] ^= _EXP[lf + _LOG[g[i + 1]]]
    return rem


# --- 버전별 표 (오류정정 M 고정) --------------------------------------------
# (오류정정 코드워드/블록, 1군 블록수, 1군 데이터코드워드, 2군 블록수, 2군 데이터코드워드)
_EC_M = {
    1:  (10, 1, 16, 0, 0),
    2:  (16, 1, 28, 0, 0),
    3:  (26, 1, 44, 0, 0),
    4:  (18, 2, 32, 0, 0),
    5:  (24, 2, 43, 0, 0),
    6:  (16, 4, 27, 0, 0),
    7:  (18, 4, 31, 0, 0),
    8:  (22, 2, 38, 2, 39),
    9:  (22, 3, 36, 2, 37),
    10: (26, 4, 43, 1, 44),
}

# 정렬 패턴 중심 좌표. 버전 1 은 없다.
_ALIGN = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
    6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
}

# 인터리브 뒤에 덧붙는 나머지 비트. 버전 2~6 만 7비트다.
_REMAINDER = {1: 0, 2: 7, 3: 7, 4: 7, 5: 7, 6: 7, 7: 0, 8: 0, 9: 0, 10: 0}

MAX_VERSION = 10


def _data_codewords(version: int) -> int:
    ec, g1, d1, g2, d2 = _EC_M[version]
    return g1 * d1 + g2 * d2


def _capacity_bytes(version: int) -> int:
    """바이트 모드로 담을 수 있는 글자수."""
    # 모드 지시자 4비트 + 글자수 지시자(버전 10부터 16비트)
    header = 4 + (16 if version >= 10 else 8)
    return (_data_codewords(version) * 8 - header) // 8


def _choose_version(length: int) -> int:
    for v in range(1, MAX_VERSION + 1):
        if length <= _capacity_bytes(v):
            return v
    raise ValueError(
        f"{length}바이트는 버전 {MAX_VERSION}(최대 {_capacity_bytes(MAX_VERSION)}바이트)에 안 들어갑니다"
    )


# --- 데이터 → 코드워드 ------------------------------------------------------
def _encode_data(payload: bytes, version: int) -> list[int]:
    total = _data_codewords(version)
    bits: list[int] = []

    def put(value: int, n: int) -> None:
        for i in range(n - 1, -1, -1):
            bits.append((value >> i) & 1)

    put(0b0100, 4)                                    # 바이트 모드
    put(len(payload), 16 if version >= 10 else 8)     # 글자수
    for b in payload:
        put(b, 8)

    # 종료자는 최대 4비트지만 남은 자리가 그보다 적으면 그만큼만 넣는다
    put(0, min(4, total * 8 - len(bits)))
    # 바이트 경계 맞추기
    while len(bits) % 8:
        bits.append(0)

    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    # 남는 자리는 규격이 정한 두 값을 0xEC 부터 번갈아 채운다
    for i in range(total - len(words)):
        words.append(0xEC if i % 2 == 0 else 0x11)
    return words


def _interleave(words: list[int], version: int) -> list[int]:
    ec_len, g1, d1, g2, d2 = _EC_M[version]

    blocks: list[list[int]] = []
    pos = 0
    for _ in range(g1):
        blocks.append(words[pos:pos + d1]); pos += d1
    for _ in range(g2):
        blocks.append(words[pos:pos + d2]); pos += d2

    ec_blocks = [_rs_encode(b, ec_len) for b in blocks]

    out: list[int] = []
    for i in range(max(len(b) for b in blocks)):
        for b in blocks:
            if i < len(b):
                out.append(b[i])
    for i in range(ec_len):
        for e in ec_blocks:
            out.append(e[i])
    return out


# --- 함수 패턴 --------------------------------------------------------------
def _blank(version: int):
    size = version * 4 + 17
    return [[None] * size for _ in range(size)], [[False] * size for _ in range(size)], size


def _place_function_patterns(m, fixed, size, version):
    def put(r, c, v):
        m[r][c] = v
        fixed[r][c] = True

    # 위치 검출 패턴 3개 + 분리자
    for br, bc in ((0, 0), (0, size - 7), (size - 7, 0)):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = br + dr, bc + dc
                if not (0 <= r < size and 0 <= c < size):
                    continue
                inner = 0 <= dr <= 6 and 0 <= dc <= 6
                if inner:
                    ring = dr in (0, 6) or dc in (0, 6)
                    core = 2 <= dr <= 4 and 2 <= dc <= 4
                    put(r, c, 1 if (ring or core) else 0)
                else:
                    put(r, c, 0)          # 분리자

    # 타이밍 패턴
    for i in range(8, size - 8):
        put(6, i, 1 if i % 2 == 0 else 0)
        put(i, 6, 1 if i % 2 == 0 else 0)

    # 정렬 패턴 — 위치 검출 패턴과 겹치는 자리는 뺀다
    centers = _ALIGN[version]
    for r in centers:
        for c in centers:
            if (r <= 8 and c <= 8) or (r <= 8 and c >= size - 9) or (r >= size - 9 and c <= 8):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    edge = max(abs(dr), abs(dc))
                    put(r + dr, c + dc, 1 if edge != 1 else 0)

    # '항상 검은 모듈'은 여기서 안 찍는다. 규격상 그 모듈은 형식 정보에
    # 딸린 것이고(7.9.1), 형식 정보는 마스크를 고른 뒤에 들어간다. 미리
    # 찍어 두면 마스크 점수가 그 한 칸 때문에 달라져서 다른 마스크가 뽑힌다.
    # 아래 reserve() 가 0 으로 두고, _place_format() 이 1 로 바꾼다.

    # 형식·버전 정보 자리를 예약한다. 값은 마스크를 고른 **뒤에** 쓰고,
    # 지금은 밝은 모듈(0)로 둔다.
    #
    # 이 0 이 중요하다. 마스크 점수는 형식 정보가 아직 없는 상태에서 매겨야
    # 한다 (ISO/IEC 18004 7.8). 형식 비트는 마스크마다 다른데 하필 위치
    # 검출 패턴 바로 옆에 붙어 있어서, 그걸 넣고 점수를 매기면 규칙 1·3 이
    # 마스크 자신의 형식 비트에 반응한다 — 무엇을 고르든 그 선택이 점수를
    # 바꾸는 순환이 된다.
    #
    # 이미 값이 있는 자리(타이밍 패턴, 항상 검은 모듈)는 건드리지 않는다.
    def reserve(r, c):
        if m[r][c] is None:
            m[r][c] = 0
        fixed[r][c] = True

    for i in range(9):
        reserve(8, i)
        reserve(i, 8)
    for i in range(8):
        reserve(8, size - 1 - i)
        reserve(size - 1 - i, 8)

    if version >= 7:
        for i in range(18):
            reserve(i // 3, size - 11 + i % 3)
            reserve(size - 11 + i % 3, i // 3)


def _place_data(m, fixed, size, codewords, version):
    bits: list[int] = []
    for w in codewords:
        for i in range(7, -1, -1):
            bits.append((w >> i) & 1)
    bits.extend([0] * _REMAINDER[version])

    idx = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:      # 세로 타이밍 패턴 열은 건너뛴다
            col -= 1
        for i in range(size):
            row = (size - 1 - i) if upward else i
            for c in (col, col - 1):
                if not fixed[row][c] and m[row][c] is None:
                    m[row][c] = bits[idx] if idx < len(bits) else 0
                    idx += 1
        upward = not upward
        col -= 2


# --- 마스크 -----------------------------------------------------------------
_MASKS = (
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
)


_N3 = bytes((1, 0, 1, 1, 1, 0, 1))


def _n3_line(seq: bytes) -> int:
    """규칙 3 — 위치 검출 패턴(1:1:3:1:1)을 닮은 무늬를 센다.

    한쪽에 밝은 모듈 4칸이 붙어 있어야 세는데, **심볼 가장자리에서는 그
    4칸이 여백(quiet zone)이다.** 여백은 정의상 밝으므로 가장자리에 걸친
    무늬도 세어야 한다.

    처음엔 11칸짜리 무늬를 줄 안에서 통째로 찾는 방식으로 짰다가 여기서
    틀렸다. 그 방식은 가장자리를 구조적으로 못 본다 — 앞이나 뒤로 4칸이
    없으니까. 그래서 마스크 점수가 실제보다 낮게 나오고, 스캐너가 위치
    검출 패턴으로 착각할 만한 무늬를 가진 마스크가 '제일 좋은 것'으로
    뽑혔다. 실제로 그렇게 만든 QR 두 개가 디코더에서 안 읽혔다.

    잘린 구간에 대한 `any()` 가 False 가 되는 성질이 그대로 여백 규칙이 된다.
    """
    count = 0
    idx = seq.find(_N3)
    while idx != -1:
        offset = idx + 7
        # 앞 4칸이 모두 밝거나(줄 밖이면 여백), 뒤 4칸이 모두 밝으면
        if not any(seq[max(idx - 4, 0):idx]) or not any(seq[offset:offset + 4]):
            count += 40
        else:
            # 무늬는 자기 자신과 겹칠 수 있다 (1011101011101).
            # 못 센 경우엔 겹치는 다음 후보부터 다시 찾는다.
            offset = idx + 4
        idx = seq.find(_N3, offset)
    return count


def _penalty(m, size) -> int:
    rows = [bytes(r) for r in m]
    cols = [bytes(c) for c in zip(*m)]
    score = 0

    for line in rows + cols:
        # 규칙 1 — 같은 색이 5칸 이상 이어지면 3 + (길이-5) = 길이-2
        run = 1
        for i in range(1, size):
            if line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    score += run - 2
                run = 1
        if run >= 5:
            score += run - 2
        # 규칙 3
        score += _n3_line(line)

    # 규칙 2 — 2×2 같은 색 덩어리
    for r in range(size - 1):
        row, nxt = m[r], m[r + 1]
        for c in range(size - 1):
            v = row[c]
            if v == row[c + 1] == nxt[c] == nxt[c + 1]:
                score += 3

    # 규칙 4 — 검은 모듈 비율이 50% 에서 멀수록.
    # |비율-50|/5 를 내림한 값. 정수만으로 같은 값을 낸다.
    total = size * size
    dark = sum(sum(row) for row in m)
    score += 10 * (abs(dark * 100 - 50 * total) // (5 * total))
    return score


# --- 형식/버전 정보 ---------------------------------------------------------
def _format_bits(mask: int) -> int:
    # 오류정정 M = 0b00
    data = (0b00 << 3) | mask
    rem = data << 10
    for i in range(4, -1, -1):
        if rem & (1 << (i + 10)):
            rem ^= 0x537 << i
    return ((data << 10) | rem) ^ 0x5412


def _version_bits(version: int) -> int:
    rem = version << 12
    for i in range(5, -1, -1):
        if rem & (1 << (i + 12)):
            rem ^= 0x1F25 << i
    return (version << 12) | rem


def _place_format(m, size, mask):
    m[size - 8][8] = 1          # 형식 정보에 딸린 '항상 검은 모듈'
    fmt = _format_bits(mask)
    for i in range(15):
        bit = (fmt >> i) & 1
        if i < 6:
            m[i][8] = bit
        elif i == 6:
            m[7][8] = bit
        elif i == 7:
            m[8][8] = bit
        elif i == 8:
            m[8][7] = bit
        else:
            m[8][14 - i] = bit
        if i < 8:
            m[8][size - 1 - i] = bit
        else:
            m[size - 15 + i][8] = bit


def _place_version(m, size, version):
    if version < 7:
        return
    bits = _version_bits(version)
    for i in range(18):
        bit = (bits >> i) & 1
        m[i // 3][size - 11 + i % 3] = bit
        m[size - 11 + i % 3][i // 3] = bit


# --- 공개 API ---------------------------------------------------------------
def encode(text: str) -> list[list[int]]:
    """문자열 → 모듈 행렬. 1 이 검은 모듈."""
    payload = text.encode("utf-8")
    version = _choose_version(len(payload))
    words = _interleave(_encode_data(payload, version), version)

    best = None
    for mask in range(8):
        m, fixed, size = _blank(version)
        _place_function_patterns(m, fixed, size, version)
        _place_data(m, fixed, size, words, version)
        for r in range(size):
            for c in range(size):
                if not fixed[r][c] and _MASKS[mask](r, c):
                    m[r][c] ^= 1
        score = _penalty(m, size)          # 형식·버전 정보는 아직 넣지 않는다
        if best is None or score < best[0]:
            best = (score, mask, m, size)

    _, mask, m, size = best
    _place_format(m, size, mask)
    _place_version(m, size, version)
    return m


# --- 터미널 출력 ------------------------------------------------------------
def _enable_vt() -> bool:
    """윈도우 콘솔에서 ANSI 이스케이프를 켠다. 콘솔이 아니면 False."""
    if sys.platform != "win32":
        return sys.stdout.isatty()
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False          # 파이프로 나가는 중 — 사람이 카메라로 볼 화면이 아니다
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def to_terminal(matrix: list[list[int]], quiet: int = 4) -> str:
    """반칸 문자로 그린다. 배경색을 직접 지정한다.

    반칸(▀)을 쓰는 이유는 세로로 절반이 되어서다. 콘솔 글자는 가로:세로가
    대략 1:2 라, 한 모듈을 한 글자로 그리면 QR 이 세로로 두 배 늘어난다.
    늘어난 QR 도 규격상 읽히기는 하지만 카메라가 훨씬 까다로워진다.

    배경색을 ANSI 로 못 박는 것도 이유가 있다. 색을 안 주고 블록 문자만
    찍으면 밝은 터미널에서는 제대로, 어두운 터미널에서는 흑백이 뒤집혀
    나온다. 반전된 QR 은 읽는 스캐너도 있고 아닌 스캐너도 있다.
    """
    size = len(matrix)
    n = size + quiet * 2
    grid = [[0] * n for _ in range(n)]
    for r in range(size):
        for c in range(size):
            grid[r + quiet][c + quiet] = matrix[r][c]
    if n % 2:
        grid.append([0] * n)       # 반칸이 짝을 이루도록

    WHITE_FG, BLACK_FG = "97", "30"
    WHITE_BG, BLACK_BG = "107", "40"
    lines = []
    for r in range(0, len(grid), 2):
        out = []
        for c in range(n):
            fg = BLACK_FG if grid[r][c] else WHITE_FG
            bg = BLACK_BG if grid[r + 1][c] else WHITE_BG
            out.append(f"\x1b[{fg};{bg}m▀")
        lines.append("".join(out) + "\x1b[0m")
    return "\n".join(lines)


def render(text: str, indent: str = "  ") -> str | None:
    """터미널에 찍을 QR. 못 그리는 상황이면 None."""
    if not _enable_vt():
        return None
    try:
        art = to_terminal(encode(text))
    except (ValueError, KeyError):
        return None
    return "\n".join(indent + line for line in art.splitlines())


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "http://192.168.1.99:5010"
    out = render(url)
    print(out if out else f"(QR 을 그릴 수 없는 터미널입니다) {url}")
