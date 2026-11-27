"""Strict entropy completeness checks for 8-bit Huffman DCT JPEG.

This checks syntax and consumes every expected block; it does not reconstruct
pixels. Progressive scans retain only a bitmask of nonzero coefficients, which
is enough to consume refinement bits. Native decoding remains a separate step.
"""
from array import array
import time

FRAME_MARKERS = frozenset((0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7,
                           0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf))


class MalformedJpeg(ValueError):
    pass


class UnsupportedJpeg(RuntimeError):
    pass


def bad():
    raise MalformedJpeg('JPEG 熵数据不完整或编码结构无效')


def marker(data, pos):
    if pos >= len(data) or data[pos] != 255:
        bad()
    while pos < len(data) and data[pos] == 255:
        pos += 1
    if pos >= len(data) or data[pos] == 0:
        bad()
    return data[pos], pos + 1


def frame_mode(code, body):
    if code not in (0xc0, 0xc1, 0xc2):
        raise UnsupportedJpeg('此 JPEG 编码尚不支持严格检查；请改用 8 位 Huffman JPEG 或 PNG')
    if len(body) < 6:
        bad()
    if body[0] != 8:
        raise UnsupportedJpeg('此 JPEG 位深尚不支持严格检查；请使用 8 位 JPEG 或 PNG')
    if body[1:3] == b'\x00\x00':
        raise UnsupportedJpeg('此 JPEG 使用延迟高度定义，暂不支持严格检查')


def frame_size(data):
    """Classify coding before DHT semantics; unsupported modes must not retry.

    DHT is permitted before SOF, and a 12-bit stream can contain DC categories
    forbidden in 8-bit coding. Only segment boundaries are checked in this pass.
    """
    if not isinstance(data, bytes) or not data.startswith(b'\xff\xd8'):
        bad()
    pos = 2
    while pos < len(data):
        code, pos = marker(data, pos)
        if code in (0xd8, 0xd9, 0xda) or 0xd0 <= code <= 0xd7:
            bad()
        if code == 0x01:
            raise UnsupportedJpeg('不支持此 JPEG 临时编码标记')
        if pos + 2 > len(data):
            bad()
        length = int.from_bytes(data[pos:pos + 2], 'big')
        if length < 2 or pos + length > len(data):
            bad()
        if code in FRAME_MARKERS:
            body = data[pos + 2:pos + length]
            frame_mode(code, body)
            return int.from_bytes(body[3:5], 'big'), int.from_bytes(body[1:3], 'big')
        pos += length
    bad()


class Bits:
    def __init__(self, data, pos):
        self.data, self.pos = data, pos
        self.value = self.left = 0

    def read(self, count):
        while self.left < count:
            if self.pos >= len(self.data):
                bad()
            value = self.data[self.pos]
            self.pos += 1
            if value == 255:
                if self.pos >= len(self.data) or self.data[self.pos] != 0:
                    bad()  # Never pad a short scan with zero bits or gray blocks.
                self.pos += 1
            self.value = (self.value << 8) | value
            self.left += 8
        self.left -= count
        result = (self.value >> self.left) & ((1 << count) - 1)
        self.value &= (1 << self.left) - 1
        return result

    def finish(self):
        if self.left and self.value != (1 << self.left) - 1:
            bad()  # JPEG pads the final entropy byte with one bits.
        self.value = self.left = 0
        marker(self.data, self.pos)  # Extra entropy bytes are not permitted.
        return self.pos

    def restart(self, expected):
        self.finish()
        code, pos = marker(self.data, self.pos)
        if code != 0xd0 + expected:
            bad()
        self.pos = pos


def huffman(counts, symbols):
    table, code, index = {}, 0, 0
    for width, count in enumerate(counts, 1):
        if code + count >= 1 << width:
            bad()  # Oversubscribed tree or forbidden all-ones Huffman code.
        for _ in range(count):
            table[(width, code)] = symbols[index]
            code += 1
            index += 1
        code <<= 1
    return table


def symbol(bits, table):
    value = 0
    for width in range(1, 17):
        value = (value << 1) | bits.read(1)
        found = table.get((width, value))
        if found is not None:
            return found
    bad()


class Check:
    def __init__(self, data, expected_size):
        self.data, self.expected_size = data, expected_size
        self.components, self.tables, self.quant = {}, {}, set()
        self.frame, self.restart_interval = None, 0
        self.scan_count = 0
        self.deadline = time.monotonic() + 15

    def frame_header(self, code, body):
        frame_mode(code, body)
        if self.frame is not None or len(body) < 6:
            bad()
        height, width, count = int.from_bytes(body[1:3], 'big'), int.from_bytes(body[3:5], 'big'), body[5]
        if not width or (width, height) != self.expected_size or count not in (1, 2, 3, 4) or len(body) != 6 + count * 3:
            bad()
        for offset in range(6, len(body), 3):
            ident, sampling, quant = body[offset:offset + 3]
            h, v = sampling >> 4, sampling & 15
            if ident in self.components or not 1 <= h <= 4 or not 1 <= v <= 4 or quant > 3:
                bad()
            self.components[ident] = {'h': h, 'v': v, 'q': quant, 'levels': [-1] * 64}
        max_h = max(c['h'] for c in self.components.values())
        max_v = max(c['v'] for c in self.components.values())
        self.mcu_cols = (width + 8 * max_h - 1) // (8 * max_h)
        self.mcu_rows = (height + 8 * max_v - 1) // (8 * max_v)
        for c in self.components.values():
            c['cols'] = (width * c['h'] + 8 * max_h - 1) // (8 * max_h)
            c['rows'] = (height * c['v'] + 8 * max_v - 1) // (8 * max_v)
            c['stride'] = self.mcu_cols * c['h']
            blocks = c['stride'] * self.mcu_rows * c['v']
            if blocks > 2_000_000:
                raise UnsupportedJpeg('JPEG 严格检查超出资源限制，未继续生成请求')
            c['nonzero'] = array('Q', [0]) * blocks if code == 0xc2 else None
        self.frame = code

    def huffman_tables(self, body):
        pos = 0
        while pos < len(body):
            if pos + 17 > len(body):
                bad()
            kind, ident = body[pos] >> 4, body[pos] & 15
            counts = body[pos + 1:pos + 17]
            length = sum(counts)
            pos += 17
            if kind > 1 or ident > 3 or not 0 < length <= 256 or pos + length > len(body):
                bad()
            symbols = body[pos:pos + length]
            if kind == 0 and any(value > 11 for value in symbols):
                bad()
            self.tables[(kind, ident)] = huffman(counts, symbols)
            pos += length

    def quant_tables(self, body):
        pos = 0
        while pos < len(body):
            precision, ident = body[pos] >> 4, body[pos] & 15
            pos += 1
            width = precision + 1
            if precision > 1 or ident > 3 or pos + 64 * width > len(body):
                bad()
            if any(int.from_bytes(body[i:i + width], 'big') == 0 for i in range(pos, pos + 64 * width, width)):
                bad()
            self.quant.add(ident)
            pos += 64 * width

    def blocks(self, selected):
        if len(selected) == 1:
            c, dc, ac = selected[0]
            for y in range(c['rows']):
                for x in range(c['cols']):
                    yield ((c, y * c['stride'] + x, dc, ac),)
        else:
            if sum(c['h'] * c['v'] for c, _, _ in selected) > 10:
                bad()
            for y in range(self.mcu_rows):
                for x in range(self.mcu_cols):
                    yield tuple((c, (y * c['v'] + dy) * c['stride'] + x * c['h'] + dx, dc, ac)
                                for c, dc, ac in selected for dy in range(c['v']) for dx in range(c['h']))

    def scan(self, body, pos):
        if self.frame is None or not body:
            bad()
        count = body[0]
        if not 1 <= count <= 4 or len(body) != 1 + 2 * count + 3:
            bad()
        first, last, approximation = body[-3:]
        high, low = approximation >> 4, approximation & 15
        progressive = self.frame == 0xc2
        if progressive:
            if not 0 <= first <= last <= 63 or (first == 0 and last != 0) or (first > 0 and count != 1):
                bad()
            if low > 13 or (high and low != high - 1):
                bad()
        elif (first, last, approximation) != (0, 63, 0):
            bad()
        selected, ids = [], set()
        for offset in range(1, 1 + count * 2, 2):
            ident, tables = body[offset:offset + 2]
            if ident in ids or ident not in self.components or tables >> 4 > 3 or tables & 15 > 3:
                bad()
            ids.add(ident)
            c = self.components[ident]
            if c['q'] not in self.quant:
                bad()
            dc, ac = self.tables.get((0, tables >> 4)), self.tables.get((1, tables & 15))
            if (first == 0 and not high and dc is None) or (last > 0 and ac is None):
                bad()
            if first and c['levels'][0] < 0:
                bad()
            if any((c['levels'][k] != high if high else c['levels'][k] != -1) for k in range(first, last + 1)):
                bad()
            selected.append((c, dc, ac))
        bits, eob, restart_number = Bits(self.data, pos), 0, 0
        for index, blocks in enumerate(self.blocks(selected)):
            if index % 256 == 0 and time.monotonic() > self.deadline:
                raise UnsupportedJpeg('JPEG 严格检查超时；无法确认完整性，未继续生成请求')
            if index and self.restart_interval and index % self.restart_interval == 0:
                if eob:
                    bad()
                bits.restart(restart_number)
                restart_number = (restart_number + 1) % 8
            for c, block, dc, ac in blocks:
                if first == 0:
                    if high:
                        bits.read(1)
                    else:
                        length = symbol(bits, dc)
                        if length > 11:
                            bad()
                        bits.read(length)
                    if progressive:
                        continue
                mask = c['nonzero'][block] if progressive else 0
                k = max(1, first)
                if not eob:
                    while k <= last:
                        value = symbol(bits, ac)
                        zeros, length = value >> 4, value & 15
                        if not length and zeros != 15:
                            if not progressive and zeros:
                                bad()
                            eob = (1 << zeros) + bits.read(zeros) if progressive else 1
                            break
                        if high:
                            if length not in (0, 1):
                                bad()
                            if length:
                                bits.read(1)  # Sign of a newly nonzero coefficient.
                            remaining = zeros + (0 if length else 1)
                            while k <= last:
                                if mask & (1 << k):
                                    bits.read(1)
                                elif remaining:
                                    remaining -= 1
                                else:
                                    break
                                k += 1
                                if not length and remaining == 0:
                                    break
                            if remaining:
                                bad()
                            if length:
                                if k > last:
                                    bad()
                                mask |= 1 << k
                                k += 1
                        else:
                            if length > 10:
                                bad()
                            if not length:
                                k += 16
                                if k > last + 1:
                                    bad()
                            else:
                                k += zeros
                                if k > last:
                                    bad()
                                bits.read(length)
                                mask |= 1 << k
                                k += 1
                if eob:
                    if high:
                        for coefficient in range(k, last + 1):
                            if mask & (1 << coefficient):
                                bits.read(1)
                    eob -= 1
                if progressive:
                    c['nonzero'][block] = mask
        if eob:
            bad()
        for c, _, _ in selected:
            c['levels'][first:last + 1] = [low] * (last - first + 1)
        self.scan_count += 1
        if self.scan_count > 256:
            raise UnsupportedJpeg('JPEG 扫描次数超出严格检查限制')
        return bits.finish()

    def run(self):
        if not isinstance(self.data, bytes) or not self.data.startswith(b'\xff\xd8'):
            bad()
        pos = 2
        while pos < len(self.data):
            code, pos = marker(self.data, pos)
            if code == 0xd9:
                if not self.scan_count or any(c['levels'][0] < 0 for c in self.components.values()):
                    bad()
                if any(-1 in c['levels'] for c in self.components.values()):
                    raise UnsupportedJpeg('JPEG 仅包含部分渐进扫描，无法确认完整图像')
                return self.expected_size
            if code in (0xd8, 0x00) or 0xd0 <= code <= 0xd7:
                bad()
            if code == 0x01:
                raise UnsupportedJpeg('不支持此 JPEG 临时编码标记')
            if pos + 2 > len(self.data):
                bad()
            length = int.from_bytes(self.data[pos:pos + 2], 'big')
            if length < 2 or pos + length > len(self.data):
                bad()
            body = self.data[pos + 2:pos + length]
            pos += length
            if code in FRAME_MARKERS:
                self.frame_header(code, body)
            elif code == 0xc4:
                self.huffman_tables(body)
            elif code == 0xdb:
                self.quant_tables(body)
            elif code == 0xdd:
                if len(body) != 2:
                    bad()
                self.restart_interval = int.from_bytes(body, 'big')
            elif code == 0xda:
                pos = self.scan(body, pos)
            elif not (0xe0 <= code <= 0xef or code == 0xfe):
                raise UnsupportedJpeg('此 JPEG 扩展编码暂不支持严格检查')
        bad()


def validate_entropy(data, expected_size=None):
    dimensions = frame_size(data)
    if expected_size is not None and dimensions != expected_size:
        bad()
    width, height = dimensions
    if width <= 0 or height <= 0 or width * height * 4 > 256 * 1024 * 1024:
        raise MalformedJpeg('图片解码尺寸超过限制')
    return Check(data, dimensions).run()
