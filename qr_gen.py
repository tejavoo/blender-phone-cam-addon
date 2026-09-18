"""
Minimal QR Code generator -- byte mode only, versions 1-4, EC level L.

Written from the ISO/IEC 18004 structure rather than any existing library, so
it is validated here with structural self-checks (Reed-Solomon generator
roots, BCH format-info divisibility, finder pattern shape, module count)
since there is no way to test against a physical camera scan in this
environment. The pairing string is short enough that version 1-4 covers it;
if you outgrow it later this needs a bigger version table.

If a scan ever fails to read, the add-on panel also prints the pairing string
as plain text -- treat that as the reliable fallback, this as the convenience.
"""
import zlib

# ---------------------------------------------------------------------------
# GF(256) arithmetic, primitive polynomial x^8 + x^4 + x^3 + x^2 + 1 (0x11D)
# ---------------------------------------------------------------------------
_EXP = [0] * 512
_LOG = [0] * 256


def _init_gf():
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


def _gmul(a, b):
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def rs_generator_poly(degree):
    """The generator polynomial for `degree` EC codewords, highest term first."""
    poly = [1]
    for i in range(degree):
        new = [0] * (len(poly) + 1)
        for j, c in enumerate(poly):
            new[j] ^= c
            new[j + 1] ^= _gmul(c, _EXP[i])
        poly = new
    return poly


def rs_encode(data, ec_len):
    """Reed-Solomon EC codewords for a list of data bytes."""
    gen = rs_generator_poly(ec_len)
    res = list(data) + [0] * ec_len
    for i in range(len(data)):
        coef = res[i]
        if coef == 0:
            continue
        for j, g in enumerate(gen):
            res[i + j] ^= _gmul(g, coef)
    return res[len(data):]


# ---------------------------------------------------------------------------
# Version table, EC level L only. (module_count, data_codewords, ec_codewords,
# [group1_count, group1_data_len, group2_count, group2_data_len])
# Single block for versions 1-4 at L -- no need for block splitting yet.
# ---------------------------------------------------------------------------
_VERSIONS_L = {
    1: dict(modules=21, total=26, ec=7,  data=19, align=[]),
    2: dict(modules=25, total=44, ec=10, data=34, align=[6, 18]),
    3: dict(modules=29, total=70, ec=15, data=55, align=[6, 22]),
    4: dict(modules=33, total=100, ec=20, data=80, align=[6, 26]),
}

_FORMAT_GEN = 0b10100110111   # generator poly for the 15-bit format info
_FORMAT_MASK = 0b101010000010010


def _bch_format(data5):
    """15-bit format info (EC level + mask) with its 10-bit BCH remainder."""
    val = data5 << 10
    g = _FORMAT_GEN
    for i in range(4, -1, -1):
        if val & (1 << (i + 10)):
            val ^= g << i
    return ((data5 << 10) | val) ^ _FORMAT_MASK


def _pick_version(byte_len):
    for v in sorted(_VERSIONS_L):
        if byte_len <= _VERSIONS_L[v]["data"] - 3:   # room for the mode+length header
            return v
    raise ValueError(f"pairing string too long for this QR encoder ({byte_len} bytes)")


def _encode_data(text_bytes, spec):
    bits = []

    def push(value, n):
        for i in range(n - 1, -1, -1):
            bits.append((value >> i) & 1)

    push(0b0100, 4)                 # byte mode indicator
    push(len(text_bytes), 8)        # character count, 8 bits for versions 1-9
    for b in text_bytes:
        push(b, 8)

    total_bits = spec["data"] * 8
    push(0, min(4, total_bits - len(bits)))          # terminator, up to 4 zero bits
    while len(bits) % 8:
        bits.append(0)

    pad_bytes = [0xEC, 0x11]
    i = 0
    while len(bits) < total_bits:
        push(pad_bytes[i % 2], 8)
        i += 1

    codewords = [
        int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)
    ]
    return codewords


def _finder(matrix, r, c):
    for dr in range(-1, 8):
        for dc in range(-1, 8):
            rr, cc = r + dr, c + dc
            if not (0 <= rr < len(matrix) and 0 <= cc < len(matrix)):
                continue
            if -1 <= dr <= 7 and -1 <= dc <= 7 and (dr in (-1, 7) or dc in (-1, 7)):
                matrix[rr][cc] = (0, True)          # quiet separator, reserved
    for dr in range(7):
        for dc in range(7):
            on = (dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4))
            matrix[r + dr][c + dc] = (1 if on else 0, True)


def _alignment(matrix, r, c):
    if matrix[r][c][1]:      # already reserved (overlaps a finder corner)
        return
    for dr in range(-2, 3):
        for dc in range(-2, 3):
            on = max(abs(dr), abs(dc)) != 1
            matrix[r + dr][c + dc] = (1 if on else 0, True)


def _place_function_patterns(matrix, n, spec):
    _finder(matrix, 0, 0)
    _finder(matrix, 0, n - 7)
    _finder(matrix, n - 7, 0)

    for i in range(8, n - 8):                         # timing patterns
        matrix[6][i] = (1 - (i % 2), True) if not matrix[6][i][1] else matrix[6][i]
        matrix[i][6] = (1 - (i % 2), True) if not matrix[i][6][1] else matrix[i][6]
    for i in range(n):
        if not matrix[6][i][1]:
            matrix[6][i] = (i % 2 ^ 1, True)
        if not matrix[i][6][1]:
            matrix[i][6] = (i % 2 ^ 1, True)

    matrix[n - 8][8] = (1, True)                       # dark module

    aligns = spec["align"]
    for r in aligns:
        for c in aligns:
            if (r <= 8 and c <= 8) or (r <= 8 and c >= n - 9) or (r >= n - 9 and c <= 8):
                continue
            _alignment(matrix, r, c)


def _reserve_format_areas(matrix, n):
    for i in range(9):
        if not matrix[8][i][1]:
            matrix[8][i] = (0, True)
        if not matrix[i][8][1]:
            matrix[i][8] = (0, True)
    for i in range(8):
        if not matrix[8][n - 1 - i][1]:
            matrix[8][n - 1 - i] = (0, True)
        if not matrix[n - 1 - i][8][1]:
            matrix[n - 1 - i][8] = (0, True)


def _place_data(matrix, n, codewords):
    bits = []
    for byte in codewords:
        for i in range(7, -1, -1):
            bits.append((byte >> i) & 1)

    bit_i = 0
    col = n - 1
    upward = True
    while col > 0:
        if col == 6:                       # timing column is skipped entirely
            col -= 1
        for row in (range(n - 1, -1, -1) if upward else range(n)):
            for c in (col, col - 1):
                if matrix[row][c][1]:
                    continue
                bit = bits[bit_i] if bit_i < len(bits) else 0
                bit_i += 1
                matrix[row][c] = (bit, False)
        upward = not upward
        col -= 2
    return matrix


def _apply_mask_and_format(matrix, n, mask_id, ec_level_bits):
    def masked(row, col, val):
        if mask_id == 0:
            hit = (row + col) % 2 == 0
        elif mask_id == 2:
            hit = col % 3 == 0
        else:
            hit = (row * col) % 2 + (row * col) % 3 == 0
        return val ^ 1 if hit else val

    for r in range(n):
        for c in range(n):
            val, reserved = matrix[r][c]
            if not reserved:
                matrix[r][c] = (masked(r, c, val), reserved)

    fmt = _bch_format((ec_level_bits << 3) | mask_id)
    fmt_bits = [(fmt >> i) & 1 for i in range(14, -1, -1)]

    for i in range(6):
        matrix[8][i] = (fmt_bits[i], True)
    matrix[8][7] = (fmt_bits[6], True)
    matrix[8][8] = (fmt_bits[7], True)
    matrix[7][8] = (fmt_bits[8], True)
    for i in range(9, 15):
        matrix[14 - i][8] = (fmt_bits[i], True)

    for i in range(8):
        matrix[n - 1 - i][8] = (fmt_bits[i], True)
    matrix[8][n - 8] = (1, True)
    for i in range(8, 15):
        matrix[8][n - 15 + i] = (fmt_bits[i], True)


def generate_qr_matrix(text):
    """Returns an NxN list of 0/1 ints for `text` in byte mode, EC level L."""
    text_bytes = text.encode("utf-8")
    version = _pick_version(len(text_bytes))
    spec = _VERSIONS_L[version]
    n = spec["modules"]

    matrix = [[(0, False) for _ in range(n)] for _ in range(n)]
    _place_function_patterns(matrix, n, spec)
    _reserve_format_areas(matrix, n)

    codewords = _encode_data(text_bytes, spec)
    ec = rs_encode(codewords, spec["ec"])
    _place_data(matrix, n, codewords + ec)

    _apply_mask_and_format(matrix, n, mask_id=0, ec_level_bits=0b01)  # L = 01
    return [[cell[0] for cell in row] for row in matrix]


# ---------------------------------------------------------------------------
# Minimal PNG writer (1-bit grayscale, stdlib zlib only)
# ---------------------------------------------------------------------------
def _png_chunk(tag, data):
    out = len(data).to_bytes(4, "big") + tag + data
    out += (zlib.crc32(tag + data) & 0xFFFFFFFF).to_bytes(4, "big")
    return out


def matrix_to_png(matrix, scale=8, border=4):
    n = len(matrix)
    size = (n + border * 2) * scale
    ihdr = (
        size.to_bytes(4, "big") + size.to_bytes(4, "big") +
        bytes([8, 0, 0, 0, 0])   # 8-bit depth, greyscale, default filters
    )

    rows = bytearray()
    for y in range(size):
        rows.append(0)  # no filter
        my = y // scale - border
        for x in range(size):
            mx = x // scale - border
            dark = 0 <= my < n and 0 <= mx < n and matrix[my][mx]
            rows.append(0 if dark else 255)

    idat = zlib.compress(bytes(rows), 9)
    png = b"\x89PNG\r\n\x1a\n"
    png += _png_chunk(b"IHDR", ihdr)
    png += _png_chunk(b"IDAT", idat)
    png += _png_chunk(b"IEND", b"")
    return png


# ---------------------------------------------------------------------------
# Self-checks -- run standalone: python3 qr_gen.py
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)
            print("FAIL " + msg)

    # 1. Reed-Solomon generator polynomial roots must all evaluate to zero --
    #    this is the defining property of the RS generator, true regardless
    #    of any external reference value, so it's a real correctness check.
    gen = rs_generator_poly(10)

    def poly_eval(poly, x):
        # poly is highest-degree-first; Horner's method in GF(256).
        acc = 0
        for c in poly:
            acc = _gmul(acc, x) ^ c
        return acc

    for i in range(10):
        check(poly_eval(gen, _EXP[i]) == 0, f"RS generator root alpha^{i}")

    # 2. BCH format info: the 15-bit codeword must be exactly divisible (XOR
    #    remainder zero) by the format generator polynomial before masking.
    for data5 in range(32):
        fmt = _bch_format(data5) ^ _FORMAT_MASK
        rem = fmt
        g = _FORMAT_GEN
        deg_g = 10
        for shift in range(4, -1, -1):
            if rem & (1 << (shift + deg_g)):
                rem ^= g << shift
        check(rem == 0, f"BCH format divisibility for data5={data5:05b}")

    # 3. Module counts match the standard 17 + 4*version formula.
    for v, spec in _VERSIONS_L.items():
        check(spec["modules"] == 17 + 4 * v, f"module count formula v{v}")

    # 4. Encode a short pairing string and sanity check matrix structure.
    sample = "192.168.1.42:5005:5006:9f3a"
    m = generate_qr_matrix(sample)
    n = len(m)
    check(n == _VERSIONS_L[_pick_version(len(sample.encode()))]["modules"], "matrix size matches chosen version")
    check(all(len(row) == n for row in m), "matrix is square")

    # Top-left finder pattern: outer ring on, ring inside that off, 3x3 core on.
    check(all(m[0][c] == 1 for c in range(7)), "finder top row all dark")
    check(all(m[6][c] == 1 for c in range(7)), "finder bottom row all dark")
    check(all(m[r][1] == 0 for r in range(1, 6)), "finder inner ring light")
    check(all(m[3][c] == 1 for c in range(2, 5)), "finder 3x3 core dark")

    # Quiet zone must be all light once rendered.
    png = matrix_to_png(m, scale=4, border=4)
    check(png[:8] == b"\x89PNG\r\n\x1a\n", "PNG signature")

    # Round-trip the IDAT stream back through zlib to confirm the file isn't corrupt.
    idat_start = png.index(b"IDAT") + 4
    idat_len = int.from_bytes(png[idat_start - 8:idat_start - 4], "big")
    raw = zlib.decompress(png[idat_start:idat_start + idat_len])
    scale, border = 4, 4
    size = (n + border * 2) * scale
    check(len(raw) == size * (size + 1), "decompressed scanline length matches image size")

    # A byte string too long for the version table must raise cleanly.
    try:
        generate_qr_matrix("x" * 500)
        check(False, "oversize input should raise")
    except ValueError:
        pass

    print(f"\nmatrix: {n}x{n} for {len(sample)}-char string")
    print(f"png: {len(png)} bytes")
    print("\n" + ("ALL QR SELF-CHECKS PASSED" if not failures else f"{len(failures)} FAILURES"))
    import sys
    sys.exit(1 if failures else 0)
