#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Minimal, dependency-free QR code generator (byte mode, EC level M, versions 1-10)."""

# RS block table for EC level M, versions 1..10.
# Each entry: list of (num_blocks, total_codewords, data_codewords)
_RS_M = {
    1: [(1, 26, 16)],
    2: [(1, 44, 28)],
    3: [(1, 70, 44)],
    4: [(2, 50, 32)],
    5: [(2, 67, 43)],
    6: [(4, 43, 27)],
    7: [(4, 49, 31)],
    8: [(2, 60, 38), (2, 61, 39)],
    9: [(3, 58, 36), (2, 59, 37)],
    10: [(4, 69, 43), (1, 70, 44)],
}

# Alignment pattern center coordinates per version.
_ALIGN = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
    6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
}

# ---- Galois field GF(256) ----
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _gmul(a, b):
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _rs_generator(ec_len):
    g = [1]
    for i in range(ec_len):
        g2 = [0] * (len(g) + 1)
        for j in range(len(g)):
            g2[j] ^= _gmul(g[j], 1)
            g2[j + 1] ^= _gmul(g[j], _EXP[i])
        g = g2
    return g


def _rs_ec(data, ec_len):
    gen = _rs_generator(ec_len)
    res = list(data) + [0] * ec_len
    for i in range(len(data)):
        coef = res[i]
        if coef != 0:
            for j in range(len(gen)):
                res[i + j] ^= _gmul(gen[j], coef)
    return res[len(data):]


def _char_count_bits(version):
    return 8 if version <= 9 else 16


def _pick_version(nbytes):
    for v in range(1, 11):
        data_cw = sum(n * d for (n, _t, d) in _RS_M[v])
        cap_bits = data_cw * 8
        need = 4 + _char_count_bits(v) + 8 * nbytes
        if need <= cap_bits:
            return v
    raise ValueError("data too long for supported versions (<=10)")


def _make_bitstream(data, version):
    data_cw = sum(n * d for (n, _t, d) in _RS_M[version])
    bits = []

    def push(val, length):
        for i in range(length - 1, -1, -1):
            bits.append((val >> i) & 1)

    push(0b0100, 4)                       # byte mode
    push(len(data), _char_count_bits(version))
    for b in data:
        push(b, 8)
    # terminator
    cap = data_cw * 8
    push(0, min(4, cap - len(bits)))
    # pad to byte boundary
    while len(bits) % 8:
        bits.append(0)
    # pad bytes
    pads = [0xEC, 0x11]
    k = 0
    codewords = []
    for i in range(0, len(bits), 8):
        codewords.append(int("".join(map(str, bits[i:i + 8])), 2))
    while len(codewords) < data_cw:
        codewords.append(pads[k % 2])
        k += 1
    return codewords


def _build_codewords(data):
    version = _pick_version(len(data))
    data_cw = _make_bitstream(data, version)

    # split into blocks
    blocks = []
    idx = 0
    ec_len = None
    for (n, total, dcount) in _RS_M[version]:
        ec_len = total - dcount
        for _ in range(n):
            blk = data_cw[idx:idx + dcount]
            idx += dcount
            blocks.append((blk, _rs_ec(blk, ec_len)))

    # interleave data
    result = []
    maxd = max(len(b[0]) for b in blocks)
    for i in range(maxd):
        for (d, _e) in blocks:
            if i < len(d):
                result.append(d[i])
    # interleave ec
    maxe = max(len(b[1]) for b in blocks)
    for i in range(maxe):
        for (_d, e) in blocks:
            if i < len(e):
                result.append(e[i])
    return version, result


# ---- matrix ----
def _new_matrix(size):
    return [[None] * size for _ in range(size)]


def _place_finder(m, r, c):
    for dr in range(-1, 8):
        for dc in range(-1, 8):
            rr, cc = r + dr, c + dc
            if 0 <= rr < len(m) and 0 <= cc < len(m):
                if 0 <= dr <= 6 and 0 <= dc <= 6:
                    on = (dr in (0, 6) or dc in (0, 6) or
                          (2 <= dr <= 4 and 2 <= dc <= 4))
                    m[rr][cc] = 1 if on else 0
                else:
                    m[rr][cc] = 0  # separator


def _place_alignment(m, version):
    coords = _ALIGN[version]
    size = len(m)
    for r in coords:
        for c in coords:
            # skip if overlapping finder
            if (r <= 8 and c <= 8) or (r <= 8 and c >= size - 9) or (c <= 8 and r >= size - 9):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    on = (abs(dr) == 2 or abs(dc) == 2 or (dr == 0 and dc == 0))
                    m[r + dr][c + dc] = 1 if on else 0


def _place_timing(m):
    size = len(m)
    for i in range(8, size - 8):
        v = 1 if i % 2 == 0 else 0
        if m[6][i] is None:
            m[6][i] = v
        if m[i][6] is None:
            m[i][6] = v


def _format_cells(size):
    """Exact cells written by format info (mirrors reference setup_type_info)."""
    vertical = []
    for i in range(15):
        if i < 6:
            vertical.append((i, 8))
        elif i < 8:
            vertical.append((i + 1, 8))
        else:
            vertical.append((size - 15 + i, 8))
    horizontal = []
    for i in range(15):
        if i < 8:
            horizontal.append((8, size - i - 1))
        elif i < 9:
            horizontal.append((8, 15 - i - 1 + 1))
        else:
            horizontal.append((8, 15 - i - 1))
    return vertical, horizontal


def _reserve_format(m):
    size = len(m)
    vertical, horizontal = _format_cells(size)
    for (r, c) in vertical + horizontal:
        m[r][c] = 0
    m[size - 8][8] = 1  # dark module


def _data_positions(m):
    size = len(m)
    col = size - 1
    up = True
    while col > 0:
        if col == 6:
            col -= 1
        rng = range(size - 1, -1, -1) if up else range(size)
        for r in rng:
            for c in (col, col - 1):
                if m[r][c] is None:
                    yield r, c
        up = not up
        col -= 2


def _place_data(m, codewords):
    bits = []
    for cw in codewords:
        for i in range(7, -1, -1):
            bits.append((cw >> i) & 1)
    it = iter(bits)
    for (r, c) in _data_positions(m):
        try:
            m[r][c] = next(it)
        except StopIteration:
            m[r][c] = 0


def _mask_func(k):
    return [
        lambda r, c: (r + c) % 2 == 0,
        lambda r, c: r % 2 == 0,
        lambda r, c: c % 3 == 0,
        lambda r, c: (r + c) % 3 == 0,
        lambda r, c: (r // 2 + c // 3) % 2 == 0,
        lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
        lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
        lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
    ][k]


def _is_function(fm, r, c):
    return fm[r][c] is not None


def _apply_mask(m, fm, k):
    f = _mask_func(k)
    out = [row[:] for row in m]
    for r in range(len(m)):
        for c in range(len(m)):
            if not _is_function(fm, r, c) and f(r, c):
                out[r][c] ^= 1
    return out


def _penalty(m):
    size = len(m)
    score = 0
    # rule 1: runs
    for line in list(m) + [list(col) for col in zip(*m)]:
        run = 1
        for i in range(1, size):
            if line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run = 1
        if run >= 5:
            score += 3 + (run - 5)
    # rule 2: 2x2 blocks
    for r in range(size - 1):
        for c in range(size - 1):
            if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                score += 3
    # rule 3: finder-like pattern
    pat1 = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
    pat2 = [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1]
    for line in list(m) + [list(col) for col in zip(*m)]:
        for i in range(size - 11 + 1):
            seg = line[i:i + 11]
            if seg == pat1 or seg == pat2:
                score += 40
    # rule 4: dark ratio
    dark = sum(sum(row) for row in m)
    total = size * size
    percent = dark * 100.0 / total
    prev_mul = int(percent // 5) * 5
    next_mul = prev_mul + 5
    score += min(abs(prev_mul - 50), abs(next_mul - 50)) // 5 * 10
    return score


_FORMAT_MASK = 0b101010000010010


def _bch_format(fmt):
    d = fmt << 10
    g = 0b10100110111
    while d.bit_length() > 10:
        d ^= g << (d.bit_length() - 11)
    return ((fmt << 10) | d) ^ _FORMAT_MASK


def _place_format(m, mask_k):
    size = len(m)
    ec_bits = 0b00  # level M
    fmt = (ec_bits << 3) | mask_k
    bits = _bch_format(fmt)  # 15 bits, bit i placed at index i (LSB first)
    vertical, horizontal = _format_cells(size)
    for i, (r, c) in enumerate(vertical):
        m[r][c] = (bits >> i) & 1
    for i, (r, c) in enumerate(horizontal):
        m[r][c] = (bits >> i) & 1
    m[size - 8][8] = 1  # dark module


def generate_matrix(text):
    data = text.encode("utf-8")
    version, codewords = _build_codewords(data)
    size = version * 4 + 17
    m = _new_matrix(size)

    _place_finder(m, 0, 0)
    _place_finder(m, 0, size - 7)
    _place_finder(m, size - 7, 0)
    _place_alignment(m, version)
    _place_timing(m)
    _reserve_format(m)

    fm = [[m[r][c] for c in range(size)] for r in range(size)]  # function-module map
    _place_data(m, codewords)

    best = None
    for k in range(8):
        cand = _apply_mask(m, fm, k)
        _place_format(cand, k)
        sc = _penalty(cand)
        if best is None or sc < best[0]:
            best = (sc, cand)
    return best[1]


def matrix_to_svg(matrix, box=8, border=4, dark="#000", light="#fff"):
    n = len(matrix)
    dim = (n + border * 2) * box
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{dim}" height="{dim}" '
        f'viewBox="0 0 {dim} {dim}" shape-rendering="crispEdges">',
        f'<rect width="{dim}" height="{dim}" fill="{light}"/>',
    ]
    for r in range(n):
        for c in range(n):
            if matrix[r][c]:
                x = (c + border) * box
                y = (r + border) * box
                parts.append(f'<rect x="{x}" y="{y}" width="{box}" height="{box}" fill="{dark}"/>')
    parts.append("</svg>")
    return "".join(parts)


if __name__ == "__main__":
    import sys
    txt = sys.argv[1] if len(sys.argv) > 1 else "http://192.168.1.23:8000"
    mat = generate_matrix(txt)
    for row in mat:
        print("".join("#" if c else "." for c in row))
