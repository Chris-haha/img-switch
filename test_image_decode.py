"""Strict scan completeness plus real native JPEG pixel decoding; no archives."""
from pathlib import Path
import sys
import unittest
from unittest import mock
import subprocess

sys.path.insert(0, str(Path(__file__).parent / "engine/scripts"))
import image_decode
import jpeg_entropy


def segment(code, body):
    return bytes((255, code)) + (len(body) + 2).to_bytes(2, 'big') + body


def entropy(bits):
    bits += '1' * (-len(bits) % 8)
    return bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)).replace(b'\xff', b'\xff\x00')


def header(size=(8, 8), sampling=((1, 1),), progressive=False, interval=0):
    """Independent tiny encoder for constant blocks and selected DCT coefficients.

    DC table: 0 -> category 0. AC codes (all length 3):
    000 EOB; 001 coefficient; 010 skip 1 then coefficient; 011 ZRL;
    100 EOBRUN 2..3; 101 EOBRUN 4..7. Values are positive/negative 1.
    """
    width, height = size
    frame = bytes((8,)) + height.to_bytes(2, 'big') + width.to_bytes(2, 'big') + bytes((len(sampling),))
    frame += b''.join(bytes((i + 1, h * 16 + v, 0)) for i, (h, v) in enumerate(sampling))
    dc = b'\x00' + bytes((1,)) + bytes(15) + b'\x00'
    ac = b'\x10' + bytes((0, 0, 6)) + bytes(13) + bytes((0, 1, 0x11, 0xf0, 0x10, 0x20))
    result = b'\xff\xd8' + segment(0xdb, b'\x00' + bytes((1,)) * 64)
    result += segment(0xc2 if progressive else 0xc0, frame) + segment(0xc4, dc + ac)
    return result + (segment(0xdd, interval.to_bytes(2, 'big')) if interval else b'')


def scan(components, bits, first=0, last=63, high=0, low=0):
    body = bytes((len(components),)) + b''.join(bytes((i, 0)) for i in components)
    return segment(0xda, body + bytes((first, last, high * 16 + low))) + (entropy(bits) if isinstance(bits, str) else bits)


def constant_baseline(size, sampling, interval=0):
    width, height = size
    max_h, max_v = max(h for h, v in sampling), max(v for h, v in sampling)
    if len(sampling) == 1:
        count, blocks = ((width + 7) // 8) * ((height + 7) // 8), 1
    else:
        count = ((width + max_h * 8 - 1) // (max_h * 8)) * ((height + max_v * 8 - 1) // (max_v * 8))
        blocks = sum(h * v for h, v in sampling)
    parts = []
    for start in range(0, count, interval or count):
        if start:
            parts.append(bytes((255, 0xd0 + (start // interval - 1) % 8)))
        parts.append(entropy('0000' * blocks * min(interval or count, count - start)))
    return header(size, sampling, interval=interval) + scan(range(1, len(sampling) + 1), b''.join(parts)) + b'\xff\xd9'


class FullDecodeTests(unittest.TestCase):
    def test_valid_jpeg_decodes_to_expected_size(self):
        raw = (Path(__file__).parent / "test-fixtures/valid-64.jpg").read_bytes()
        self.assertEqual(image_decode.validate_jpeg(raw, (64, 64)), (64, 64))

    def test_complete_framing_without_image_data_is_rejected(self):
        raw = bytes.fromhex("ffd8ffc0000b080040004001011100ffda0008010100003f0000ffd9")
        with self.assertRaises(image_decode.InvalidImage):
            image_decode.validate_jpeg(raw, (64, 64))

    def test_decoded_size_must_match_header(self):
        raw = (Path(__file__).parent / "test-fixtures/valid-64.jpg").read_bytes()
        with self.assertRaises(image_decode.InvalidImage):
            image_decode.validate_jpeg(raw, (65, 64))

    def test_huge_image_is_rejected_before_decoder(self):
        with self.assertRaises(image_decode.InvalidImage):
            image_decode.validate_jpeg(b"\xff\xd8\xff\xd9", (32768, 32768))

    def test_entropy_cut_in_half_with_valid_eoi_is_rejected(self):
        raw = (Path(__file__).parent / 'test-fixtures/valid-64.jpg').read_bytes()
        sos = raw.index(b'\xff\xda')
        start = sos + 2 + int.from_bytes(raw[sos + 2:sos + 4], 'big')
        end = raw.rindex(b'\xff\xd9')
        truncated = raw[:start + (end - start) // 2] + b'\xff\xd9'
        with self.assertRaises(image_decode.InvalidImage):
            image_decode.validate_jpeg(truncated, (64, 64))

    def test_system_decoder_timeout_stops_without_blame_on_image(self):
        raw = (Path(__file__).parent / 'test-fixtures/valid-64.jpg').read_bytes()
        with mock.patch.object(image_decode.sys, 'platform', 'darwin'), \
             mock.patch.object(image_decode.os.path, 'isfile', return_value=True), \
             mock.patch.object(image_decode.subprocess, 'run', side_effect=subprocess.TimeoutExpired('decoder', 20)):
            with self.assertRaises(image_decode.DecoderUnavailable):
                image_decode.validate_jpeg(raw, (64, 64))

    def test_independent_imageio_fixtures_decode_baseline_and_progressive(self):
        for color, size in (('color', (37, 29)), ('gray', (17, 19))):
            for kind, marker in (('baseline', b'\xff\xc0'), ('progressive', b'\xff\xc2')):
                with self.subTest(color=color, kind=kind):
                    raw = (Path(__file__).parent / f'test-fixtures/imageio-{color}-{kind}.jpg').read_bytes()
                    self.assertIn(marker, raw)
                    self.assertEqual(image_decode.validate_jpeg(raw, size), size)

    def test_non_aligned_sampling_and_restart_markers_decode(self):
        for sampling in (((1, 1),), ((1, 1),) * 3, ((2, 1), (1, 1), (1, 1)), ((2, 2), (1, 1), (1, 1))):
            for interval in (0, 1, 3):
                with self.subTest(sampling=sampling, interval=interval):
                    size = (73, 25)  # partial edge MCUs; interval=1 wraps RST7 to RST0
                    raw = constant_baseline(size, sampling, interval)
                    self.assertEqual(image_decode.validate_jpeg(raw, size), size)

    def test_progressive_refinement_reads_existing_and_new_nonzero_coefficients(self):
        # Initial AC coefficients at 1, 3 and 20, followed by refinement that
        # corrects them, inserts 2 and 21, and uses a ZRL over 16 zero entries.
        raw = header(progressive=True) + scan((1,), '0', 0, 0, low=1)
        raw += scan((1,), '0011' + '0100' + '011' + '0011' + '000', 1, 63, low=1)
        raw += scan((1,), '1', 0, 0, high=1)
        raw += scan((1,), '00111' + '0111' + '00101' + '000', 1, 63, high=1)
        raw += b'\xff\xd9'
        self.assertEqual(image_decode.validate_jpeg(raw, (8, 8)), (8, 8))

    def test_progressive_eob_runs_and_restart_intervals(self):
        for interval in (0, 1):
            raw = header((24, 8), progressive=True, interval=interval)
            if interval:
                dc = entropy('0') + b'\xff\xd0' + entropy('0') + b'\xff\xd1' + entropy('0')
                ac = entropy('000') + b'\xff\xd0' + entropy('000') + b'\xff\xd1' + entropy('000')
            else:
                dc, ac = entropy('000'), entropy('1001')  # EOBRUN=3
            raw += scan((1,), dc, 0, 0, low=1) + scan((1,), ac, 1, 63, low=1)
            raw += scan((1,), dc, 0, 0, high=1) + scan((1,), ac, 1, 63, high=1) + b'\xff\xd9'
            self.assertEqual(image_decode.validate_jpeg(raw, (24, 8)), (24, 8))


class EntropyFailureTests(unittest.TestCase):
    def test_each_native_progressive_scan_rejects_truncated_entropy(self):
        for color, size in (('color', (37, 29)), ('gray', (17, 19))):
            raw = (Path(__file__).parent / f'test-fixtures/imageio-{color}-progressive.jpg').read_bytes()
            pos, scans = 2, 0
            while pos < len(raw):
                code, pos = jpeg_entropy.marker(raw, pos)
                if code == 0xd9:
                    break
                length = int.from_bytes(raw[pos:pos + 2], 'big')
                pos += length
                if code == 0xda:
                    end = pos
                    while end + 1 < len(raw):
                        if raw[end] == 255:
                            if raw[end + 1] == 0:
                                end += 2
                                continue
                            break
                        end += 1
                    with self.subTest(color=color, scan=scans):
                        truncated = raw[:pos + (end - pos) // 2] + b'\xff\xd9'
                        with self.assertRaises(image_decode.InvalidImage):
                            image_decode.validate_jpeg(truncated, size)
                    scans += 1
                    pos = end
            self.assertGreater(scans, 3)

    def test_restart_missing_reordered_or_early_is_rejected(self):
        raw = constant_baseline((24, 8), ((1, 1),), interval=1)
        malformed = (raw.replace(b'\xff\xd0', b'', 1), raw.replace(b'\xff\xd0', b'\xff\xd1', 1),
                     raw.replace(b'\x0f\xff\xd0', b'\xff\xd0', 1))
        for value in malformed:
            with self.assertRaises(image_decode.InvalidImage):
                image_decode.validate_jpeg(value, (24, 8))

    def test_progressive_eob_run_cannot_cross_restart_or_scan_end(self):
        for interval in (0, 1):
            raw = header((24, 8), progressive=True, interval=interval)
            dc = entropy('000') if not interval else entropy('0') + b'\xff\xd0' + entropy('0') + b'\xff\xd1' + entropy('0')
            raw += scan((1,), dc, 0, 0) + scan((1,), '10100', 1, 63) + b'\xff\xd9'  # EOBRUN=4 for 3 blocks
            with self.assertRaises(image_decode.InvalidImage):
                image_decode.validate_jpeg(raw, (24, 8))

    def test_unconsumed_entropy_or_non_one_padding_is_rejected(self):
        raw = constant_baseline((8, 8), ((1, 1),))
        for tail in (b'\x00\xff\xd9', b'\x0f\x00\xff\xd9'):
            with self.assertRaises(image_decode.InvalidImage):
                image_decode.validate_jpeg(raw[:-3] + tail, (8, 8))

    def test_unsupported_precision_encoding_and_partial_scans_stop(self):
        raw = constant_baseline((8, 8), ((1, 1),))
        modes = [raw.replace(b'\xff\xc0', b'\xff\xc9', 1), raw.replace(b'\x08\x00\x08\x00\x08', b'\x0c\x00\x08\x00\x08', 1),
                 header(progressive=True) + scan((1,), '0', 0, 0) + b'\xff\xd9']
        for value in modes:
            with self.assertRaises(image_decode.DecoderUnavailable):
                image_decode.validate_jpeg(value, (8, 8))

    def test_strict_check_timeout_is_decoder_unavailable(self):
        raw = constant_baseline((8, 8), ((1, 1),))
        with mock.patch.object(jpeg_entropy.time, 'monotonic', side_effect=(1, 17)):
            with self.assertRaises(image_decode.DecoderUnavailable):
                image_decode.validate_jpeg(raw, (8, 8))

    def test_unsupported_frame_is_classified_before_huffman_semantics(self):
        raw = constant_baseline((8, 8), ((1, 1),))
        offset = raw.index(b'\xff\xc0')
        end = offset + 2 + int.from_bytes(raw[offset + 2:offset + 4], 'big')
        frame = bytearray(raw[offset:end])
        frame[4] = 12  # 12-bit coding can legitimately use DC category 12.
        without_frame = bytearray(raw[:offset] + raw[end:])
        dht = without_frame.index(b'\xff\xc4')
        without_frame[dht + 4 + 17] = 12
        sos = without_frame.index(b'\xff\xda')
        reordered = bytes(without_frame[:sos] + frame + without_frame[sos:])
        with self.assertRaises(image_decode.DecoderUnavailable):
            image_decode.validate_jpeg(reordered, (8, 8))


if __name__ == "__main__":
    unittest.main()
