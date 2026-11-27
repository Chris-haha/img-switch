#!/usr/bin/env python3
"""failover-imagegen-tool 主脚本：OpenAI 兼容中转站生图，多源×多模型级联故障切换。

用法示例：
  python failover_imagegen_tool.py generate --prompt-file p.txt --name test --size 4k
  python failover_imagegen_tool.py generate --prompt "..." --input-image ref.png   # 编辑模式
  python failover_imagegen_tool.py batch --prompts-file jobs.txt --name blog       # 批量生图
  python failover_imagegen_tool.py --models        # 发现各中转站支持的生图模型
  python failover_imagegen_tool.py --check         # 源健康体检 + 历史成功/耗时统计
  python failover_imagegen_tool.py --list-sources

故障切换规则（中转站优先级为外层，站内模型级联为内层）：
  - 换本站下一个模型：HTTP 404、响应无图、返回内容不是图片
  - 换下一个中转站：确定未发送的连接失败、401/403/429、明确不支持的尺寸
  - 状态未知并停止：发送后超时/断连、408/5xx、图片下载失败；须核对上游后确认重试
  - 致命（立即停止，不再尝试）：内容安全拒绝、未识别的 HTTP 4xx
  - 明确且结构化的模型/尺寸不支持错误可换兼容候选，不自动改变请求尺寸

输出：当前目录 generated/ 下 {name}-YYYYMMDD-HHMMSS-{尺寸标签}.png（或 .jpg）
      与同名 .meta.json（模型、尺寸、提示词快照、耗时、逐次尝试记录，分享前需检查敏感内容）。
"""
import argparse
import builtins
import errno
import multiprocessing
import math
import socket
import signal
import subprocess
import ssl
import base64
import binascii
import glob
import json
import http.client
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import failovercfg
import task_store
import image_decode

_SECRETS = ()
_REQUEST_EVENT = None


def _safe(value, secrets=()):
    return failovercfg.mask_secrets(value, tuple(_SECRETS) + tuple(secrets))


def print(*values, **kwargs):
    """Sanitize all command output, including names, prompts and provider text."""
    return builtins.print(*[_safe(v) for v in values], **kwargs)

# 尺寸别名（宽, 高）
SIZE_ALIASES = {
    "2k": (2560, 1440),
    "2k-landscape": (2560, 1440),
    "4k": (3840, 2160),
    "4k-landscape": (3840, 2160),
    "2k-portrait": (1440, 2560),
    "4k-portrait": (2160, 3840),
    "square": (2048, 2048),
    "2k-square": (2048, 2048),
}
DEFAULT_SIZE = (2560, 1440)

# 这些 HTTP 状态视为「站点级故障/限制」：直接换下一个中转站
FAILOVER_SOURCE_HTTP = {401, 403, 429}
# 模型发现时的生图模型关键字（大小写不敏感）
IMAGE_HINT = re.compile(
    r"image|dall|flux|midjourney|\bmj\b|stable|sdxl|sd3|seedream|ideogram|recraft"
    r"|kolors|hidream|pixart|imagen|wanx|imagine|banana|leo", re.IGNORECASE)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8"
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_RESPONSE_BYTES = 96 * 1024 * 1024


class AttemptError(Exception):
    """一次「站×模型」尝试失败。kind: fatal / next_model / next_source / unknown。"""

    def __init__(self, kind, message, request_id=None, disable_source=False):
        message = _safe(message)
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.request_id = request_id
        self.disable_source = disable_source


# ---------------------------------------------------------------- 尺寸与图片

def parse_size(text):
    """解析尺寸参数：别名或 WxH。返回 (w, h)。"""
    s = (text or "").strip().lower()
    if not s:
        return DEFAULT_SIZE
    if s in SIZE_ALIASES:
        return SIZE_ALIASES[s]
    m = re.fullmatch(r"(\d{2,5})\s*[x×*]\s*(\d{2,5})", s)
    if not m:
        raise ValueError(
            "无法识别的尺寸 %r；可用别名：%s，或直接写 3840x2160 这类 宽x高"
            % (text, "/".join(sorted(SIZE_ALIASES)))
        )
    w, h = int(m.group(1)), int(m.group(2))
    if w < 64 or h < 64:
        raise ValueError("尺寸过小（宽高均需 >= 64）：%dx%d" % (w, h))
    return (w, h)


def size_tag(w, h):
    """输出文件名里的尺寸标签。"""
    m = max(w, h)
    if m >= 3000:
        return "4k"
    if m >= 1500:
        return "2k"
    return "%dx%d" % (w, h)


def detect_image(data):
    """Check PNG CRC/data completeness or JPEG framing before declaring success."""
    if not isinstance(data, bytes) or len(data) > MAX_IMAGE_BYTES:
        return None
    if data[:8] == PNG_MAGIC:
        size = _png_size(data)
        return ("png", *size) if size else None
    if data[:2] == JPEG_MAGIC:
        size = _jpeg_size(data)
        if size:
            return ("jpg", size[0], size[1])
    return None


def _png_size(data):
    offset, header, compressed, ended = 8, None, bytearray(), False
    palette = False
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset:offset + 4], "big")
        kind = data[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(data):
            return None
        payload = data[offset + 8:end - 4]
        if zlib.crc32(kind + payload) & 0xffffffff != int.from_bytes(data[end - 4:end], "big"):
            return None
        if header is None and kind != b"IHDR":
            return None
        if kind == b"IHDR":
            if header is not None or length != 13:
                return None
            header = payload
        elif kind == b"PLTE":
            if not length or length % 3 or length > 768:
                return None
            palette = True
        elif kind == b"IDAT":
            compressed.extend(payload)
        elif kind == b"IEND":
            if length:
                return None
            ended = True
            break
        elif not kind[0] & 32:
            return None  # Unknown critical chunk.
        offset = end
    if not ended or not header or not compressed:
        return None
    w, h = int.from_bytes(header[:4], "big"), int.from_bytes(header[4:8], "big")
    depth, color, compression, filtering, interlace = header[8:13]
    depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
    if (not 0 < w <= 32768 or not 0 < h <= 32768 or depth not in depths.get(color, ())
            or compression or filtering or interlace not in (0, 1) or (color == 3 and not palette)):
        return None
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    passes = ((0, 0, 1, 1),) if not interlace else (
        (0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
        (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
    rows = []
    for x, y, dx, dy in passes:
        pw, ph = max(0, (w - x + dx - 1) // dx), max(0, (h - y + dy - 1) // dy)
        if pw and ph:
            rows.append(((pw * channels * depth + 7) // 8 + 1, ph))
    expected = sum(length * count for length, count in rows)
    if expected > 256 * 1024 * 1024:
        return None
    try:
        decoder = zlib.decompressobj()
        pixels = decoder.decompress(compressed, expected + 1)
        if len(pixels) != expected or not decoder.eof or decoder.unused_data:
            return None
    except zlib.error:
        return None
    offset = 0
    for length, count in rows:
        for _ in range(count):
            if pixels[offset] > 4:
                return None
            offset += length
    return w, h


def _jpeg_size(data):
    """Validate common Huffman JPEG structure, not entropy-coded pixel values."""
    i, n, size, saw_scan = 2, len(data), None, False
    quant, huffman, components, scanned = set(), set(), {}, set()
    progressive = False
    while i + 1 < n:
        if data[i] != 0xff:
            return None
        marker = data[i + 1]
        if marker == 0xff:
            i += 1
            continue
        if marker == 0xd9:
            return size if saw_scan and scanned == set(components) else None
        if marker == 0x01 or 0xd0 <= marker <= 0xd7:
            i += 2
            continue
        if marker in (0x00, 0xd8) or i + 4 > n:
            return None
        seg_len = int.from_bytes(data[i + 2:i + 4], "big")
        if seg_len < 2 or i + 2 + seg_len > n:
            return None
        payload = data[i + 4:i + 2 + seg_len]
        if marker == 0xdb:  # Quantization tables.
            pos = 0
            while pos < len(payload):
                precision, table = payload[pos] >> 4, payload[pos] & 15
                length = 64 * (precision + 1)
                if precision > 1 or table > 3 or pos + 1 + length > len(payload):
                    return None
                values = payload[pos + 1:pos + 1 + length]
                step = precision + 1
                if any(int.from_bytes(values[j:j + step], "big") == 0 for j in range(0, length, step)):
                    return None
                quant.add(table)
                pos += 1 + length
        elif marker == 0xc4:  # Huffman code lengths and symbols.
            pos = 0
            while pos < len(payload):
                if pos + 17 > len(payload):
                    return None
                kind, table = payload[pos] >> 4, payload[pos] & 15
                counts = payload[pos + 1:pos + 17]
                total = sum(counts)
                if kind > 1 or table > 3 or not 0 < total <= 256 or pos + 17 + total > len(payload):
                    return None
                available = 1
                for count in counts:
                    available = 2 * available - count
                    if available < 0:
                        return None
                huffman.add((kind, table))
                pos += 17 + total
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if marker not in (0xc0, 0xc1, 0xc2) or size or seg_len < 8:
                return None
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            count = data[i + 9]
            if not 0 < w <= 32768 or not 0 < h <= 32768 or not 1 <= count <= 4 or seg_len != 8 + 3 * count:
                return None
            if payload[0] != 8:
                return None
            for j in range(6, len(payload), 3):
                component, sampling, table = payload[j:j + 3]
                if component in components or not 1 <= sampling >> 4 <= 4 or not 1 <= sampling & 15 <= 4 or table > 3:
                    return None
                components[component] = table
            progressive = marker == 0xc2
            size = (w, h)
        i += 2 + seg_len
        if marker == 0xda:
            if not size or seg_len < 6 or not 1 <= payload[0] <= len(components) or seg_len != 6 + 2 * payload[0]:
                return None
            first, last, refinement = payload[-3:]
            if not progressive and (first, last, refinement) != (0, 63, 0):
                return None
            if progressive and (not 0 <= first <= last <= 63 or (first == 0 and last != 0)
                                or (first > 0 and payload[0] != 1) or refinement >> 4 > 13 or refinement & 15 > 13):
                return None
            scan_components = set()
            for j in range(1, 1 + 2 * payload[0], 2):
                component, tables = payload[j:j + 2]
                if component not in components or component in scan_components or components[component] not in quant:
                    return None
                if (not progressive or (first == 0 and refinement >> 4 == 0)) and (0, tables >> 4) not in huffman:
                    return None
                if (not progressive or first > 0) and (1, tables & 15) not in huffman:
                    return None
                scan_components.add(component)
            scanned.update(scan_components)
            scan_start = i
            while i < n:
                if data[i] != 0xff:
                    i += 1
                elif i + 1 < n and (data[i + 1] == 0 or 0xd0 <= data[i + 1] <= 0xd7):
                    i += 2
                else:
                    break
            if i == scan_start:
                return None
            saw_scan = True
    return None


# ---------------------------------------------------------------- HTTP

def _http_json(req, timeout):
    """发请求并解析 JSON 响应。HTTPError 转成带上游消息的错误。"""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            request_id = resp.headers.get("x-request-id") or resp.headers.get("request-id")
            if _REQUEST_EVENT:
                _REQUEST_EVENT({"request_id": request_id, "response_at": task_store.now()})
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise AttemptError("next_source", "上游响应超过大小限制")
            return resp.status, _parse_json(raw), raw
    except urllib.error.HTTPError as e:
        request_id = e.headers.get("x-request-id") if e.headers else None
        if _REQUEST_EVENT:
            _REQUEST_EVENT({"request_id": request_id, "response_at": task_store.now()})
        raw = b""
        try:
            raw = e.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raw = b"upstream error body exceeds limit"
        except Exception:
            pass
        error = _http_error(e.code, raw)
        error.request_id = request_id
        raise error


def _http_error(code, raw):
    detail = ""
    parsed = _parse_json(raw)
    if isinstance(parsed, dict):
        err = parsed.get("error") or parsed
        if isinstance(err, dict):
            detail = str(err.get("message") or err.get("code") or "")
        elif err:
            detail = str(err)
    if not detail:
        detail = _safe(raw.decode("utf-8", "replace")).strip()[:200]
    error = urllib.error.HTTPError(
        "response", code,
        "HTTP %d: %s" % (code, _safe(detail)), None, None)
    upstream = parsed.get("error", parsed) if isinstance(parsed, dict) else {}
    if isinstance(upstream, dict):
        error.upstream_code = str(upstream.get("code") or "").lower()
        error.upstream_type = str(upstream.get("type") or "").lower()
        error.upstream_param = str(upstream.get("param") or "").lower()
    return error


def _parse_json(raw):
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def multipart_encode(fields, file_field, file_name, file_bytes, mime):
    boundary = "----failoverimagegentool" + uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(
            ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
             % (boundary, k, v)).encode("utf-8")
        )
    parts.append(
        ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
         "Content-Type: %s\r\n\r\n" % (boundary, file_field, file_name, mime)).encode("utf-8")
    )
    parts.append(file_bytes)
    parts.append(("\r\n--%s--\r\n" % boundary).encode("utf-8"))
    return b"".join(parts), "multipart/form-data; boundary=%s" % boundary


# ---------------------------------------------------------------- 单次尝试

def attempt_source(src, prompt, size, input_image, model, verbose=False):
    """对单个「站×模型」发起一次生图请求。

    成功返回 dict（含 image_bytes/ext/尺寸/耗时/命中的源与模型）；
    失败抛 AttemptError(kind, message)。
    """
    global _SECRETS
    _SECRETS = tuple(set(_SECRETS + (src["api_key"],)))
    base = src["base_url"].rstrip("/")
    model = (model or "").strip()
    if not model:
        raise AttemptError("fatal", "源 %r 未配置模型" % src["name"])
    timeout = int(src.get("timeout") or failovercfg.DEFAULT_TIMEOUT)
    editing = bool(input_image)
    endpoint = base + ("/images/edits" if editing else "/images/generations")
    headers = failovercfg.api_headers(src["api_key"])
    t0 = time.time()

    try:
        if editing:
            with open(input_image, "rb") as f:
                img_bytes = f.read()
            fields = {"model": model, "prompt": prompt, "size": "%dx%d" % size, "n": "1"}
            body, ctype = multipart_encode(
                fields, "image", os.path.basename(input_image), img_bytes,
                "image/png" if img_bytes[:8] == PNG_MAGIC else "image/jpeg",
            )
            headers["Content-Type"] = ctype
            req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        else:
            payload = {"model": model, "prompt": prompt, "size": "%dx%d" % size, "n": 1}
            headers["Content-Type"] = "application/json"
            req = urllib.request.Request(
                endpoint, data=json.dumps(payload).encode("utf-8"),
                headers=headers, method="POST",
            )
        if verbose:
            print("    POST %s model=%s size=%dx%d mode=%s"
                  % (failovercfg.endpoint_host(base), model, size[0], size[1], "edit" if editing else "gen"))
        status, data, _raw = _http_json(req, timeout)
    except urllib.error.HTTPError as e:
        code = e.code
        msg = str(e.reason)  # _http_error 构造时已带 "HTTP <code>: <detail>"
        request_id = getattr(e, "request_id", None)
        structured = {getattr(e, "upstream_code", ""), getattr(e, "upstream_type", "")}
        if (any("policy" in value or "safety" in value or "moderation" in value for value in structured)
                or any(term in msg.lower() for term in ("content policy", "content_policy", "safety filter", "safety system", "内容安全", "内容政策"))):
            raise AttemptError("fatal", msg, request_id)
        if code == 408 or 500 <= code < 600:
            # A gateway/server failure does not prove that the paid POST had no
            # side effects. No cross-provider idempotency/reconciliation exists.
            # RFC 9110 section 9.2.2 advises against retrying without such evidence.
            raise AttemptError("unknown", msg + "（上游可能已执行或计费；请核对请求记录，不会自动换源/换模型）", request_id)
        if code in FAILOVER_SOURCE_HTTP:
            raise AttemptError("next_source", msg, request_id, disable_source=True)
        unsupported = {"unsupported_size", "invalid_size", "size_not_supported", "unsupported_model",
                       "model_not_supported", "model_not_found", "invalid_model"}
        if code in (400, 422) and structured & unsupported:
            kind = "next_source" if structured & {"unsupported_size", "invalid_size", "size_not_supported"} else "next_model"
            raise AttemptError(kind, msg, request_id)
        if code == 404:
            raise AttemptError("next_model", msg, request_id)
        raise AttemptError("fatal", msg + "（请求被拒绝，检查模型 ID/尺寸/提示词，勿反复重试）", request_id)
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, http.client.HTTPException) as e:
        reason = e.reason if isinstance(e, urllib.error.URLError) else e
        safe = isinstance(reason, (ConnectionRefusedError, socket.gaierror, ssl.SSLCertVerificationError)) or getattr(reason, "errno", None) in (errno.ECONNREFUSED, errno.ENETUNREACH, errno.EHOSTUNREACH)
        kind = "next_source" if safe else "unknown"
        message = "连接失败: %s" % _safe(e)
        if not safe:
            message += "（请求可能已发送，状态未知；不会自动重试）"
        raise AttemptError(kind, message, disable_source=safe) from None

    image_bytes = _extract_image(data, timeout)
    if image_bytes.startswith(JPEG_MAGIC):
        try:
            # The decoder classifies unsupported coding before structural or
            # entropy checks, so local limitations never trigger another charge.
            dimensions = image_decode.validate_jpeg(image_bytes)
        except image_decode.DecoderUnavailable as error:
            raise AttemptError("fatal", str(error)) from None
        except image_decode.InvalidImage as error:
            raise AttemptError("next_source", str(error)) from None
        ext, (w, h) = "jpg", dimensions
    else:
        info = detect_image(image_bytes)
        if not info:
            raise AttemptError("next_source", "图片未通过 PNG/JPEG 完整性与结构检查")
        ext, w, h = info
    return {
        "image_bytes": image_bytes,
        "ext": ext,
        "width": w,
        "height": h,
        "model_returned": (data or {}).get("model") if isinstance(data, dict) else None,
        "endpoint": endpoint,
        "elapsed": round(time.time() - t0, 2),
        "source_name": src["name"],
        "source_host": failovercfg.endpoint_host(src["base_url"]),
        "model": model,
    }


def _extract_image(data, timeout):
    """从 OpenAI 兼容响应中取出图片字节：兼容 b64_json / url / images[]（URL 或 data:URI）。"""
    if not isinstance(data, dict):
        raise AttemptError("next_model", "响应不是 JSON 对象")
    items = data.get("data") if isinstance(data.get("data"), list) else None
    if not items and isinstance(data.get("images"), list):
        items = data["images"]
    if not items:
        raise AttemptError("next_model", "响应中没有 data/images 图片字段: %s"
                           % _safe(json.dumps(data, ensure_ascii=False))[:200])
    item = items[0]
    if isinstance(item, dict) and item.get("b64_json"):
        return _decode_image_base64(item["b64_json"])
    url = None
    if isinstance(item, dict):
        url = item.get("url") or item.get("image_url")
    elif isinstance(item, str):
        url = item
    if isinstance(url, dict):
        url = url.get("url")
    if not isinstance(url, str) or not url:
        raise AttemptError("next_model", "响应条目里没有 url/b64_json")
    if url.startswith("data:"):
        head, separator, b64 = url.partition(",")
        if not separator or not head.startswith("data:image/") or not head.endswith(";base64"):
            raise AttemptError("next_model", "图片 data URI 格式无效")
        return _decode_image_base64(b64)
    return _download(url, timeout)


def _decode_image_base64(value):
    try:
        if not isinstance(value, str) or len(value) > MAX_RESPONSE_BYTES:
            raise ValueError("invalid base64 value")
        data = base64.b64decode("".join(value.split()), validate=True)
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise ValueError("invalid image length")
        return data
    except (ValueError, binascii.Error):
        raise AttemptError("next_model", "上游图片 Base64 数据无效或超过大小限制") from None


def _download(url, timeout):
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("invalid download URL")
        req = urllib.request.Request(url, headers={"User-Agent": "failover-imagegen-tool/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(MAX_IMAGE_BYTES + 1)
            if len(data) > MAX_IMAGE_BYTES:
                raise ValueError("image too large")
            return data
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, ValueError, http.client.HTTPException) as e:
        raise AttemptError("unknown", "下载图片失败 %s（%s）；生成可能已完成，请先核对上游记录/费用，不自动重复生成"
                           % (failovercfg.endpoint_host(url), type(e).__name__)) from None


# ---------------------------------------------------------------- 级联与保存

def _attempt_plan_count(sources, model_override):
    return sum(len([model_override] if model_override else failovercfg.source_models(s))
               for s in sources)


def _attempt_worker(connection, arguments, secrets):
    global _SECRETS, _REQUEST_EVENT
    _SECRETS = secrets
    if os.name != "nt":
        os.setsid()
    _REQUEST_EVENT = lambda event: connection.send(("event", event))
    try:
        result = attempt_source(*arguments)
        connection.send(("result", result))
    except AttemptError as error:
        connection.send(("error", {"kind": error.kind, "message": error.message,
                                     "request_id": error.request_id,
                                     "disable_source": error.disable_source}))
    except BaseException as error:
        connection.send(("error", {"kind": "unknown", "message": _safe(error)}))
    finally:
        connection.close()


def _stop_worker(process):
    if process.is_alive():
        if os.name != "nt":
            import signal
            try:
                if os.getpgid(process.pid) == process.pid:
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
            except ProcessLookupError:
                pass
        else:
            try:
                # Windows does not terminate decoder descendants with terminate().
                if not isinstance(process.pid, int) or process.pid <= 0:
                    raise ValueError("invalid child process ID")
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except (OSError, ValueError, subprocess.TimeoutExpired):
                process.terminate()
        process.join(0.5)
        if process.is_alive():
            process.kill()
    process.join(0.5)


def _bounded_attempt(arguments, deadline_at, cancelled, on_event):
    """A killable child bounds connect, reads, downloads and image decoding."""
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_attempt_worker, args=(sender, arguments, tuple(_SECRETS)), daemon=True)
    process.start()
    sender.close()
    try:
        while True:
            if receiver.poll(0.05):
                try:
                    kind, value = receiver.recv()
                except EOFError:
                    raise AttemptError("unknown", "请求进程意外退出；上游结果未知") from None
                if kind == "event":
                    if on_event:
                        on_event(value)
                elif kind == "result":
                    return value
                else:
                    raise AttemptError(**value)
            if cancelled and cancelled():
                raise AttemptError("unknown", "已取消等待；请求可能已被接收，请核对上游结果后再重试")
            if time.monotonic() >= deadline_at:
                raise AttemptError("unknown", "达到任务总时限；请求可能已被接收，不会自动切换或重试")
            if not process.is_alive() and not receiver.poll():
                raise AttemptError("unknown", "请求进程意外退出；上游结果未知")
    except KeyboardInterrupt:
        if on_event:
            on_event({"cancel_requested": True})
        raise AttemptError("unknown", "用户中断等待；请求可能仍在上游执行") from None
    finally:
        _stop_worker(process)
        receiver.close()


def _capability_error(src, size, model):
    allowed = src.get("supported_sizes")
    if allowed is not None:
        if not isinstance(allowed, list) or not allowed or any(not isinstance(v, str) for v in allowed):
            return "fatal", "supported_sizes 必须是非空字符串数组"
        try:
            dimensions = [parse_size(value) for value in allowed]
        except ValueError:
            return "fatal", "supported_sizes 含无法识别的尺寸"
        if tuple(size) not in dimensions:
            return "next_source", "该源明确不支持请求尺寸 %dx%d，已跳过且不会自动调整尺寸" % size
    allowed = src.get("supported_models")
    if allowed is not None:
        if not isinstance(allowed, list) or not allowed or any(not isinstance(v, str) or not v.strip() for v in allowed):
            return "fatal", "supported_models 必须是非空字符串数组"
        if model not in allowed:
            return "next_model", "该源明确不支持模型 %s，已跳过" % model
    return None


def _run_attempts(sources, prompt, size, input_image, model_override,
                  verbose=False, failover=True, deadline_at=None, cancelled=None,
                  on_attempts=None, unavailable=None):
    """Run a cascade; unknown outcomes never move to another paid request."""
    global _SECRETS
    _SECRETS = tuple(set(_SECRETS + tuple(s["api_key"] for s in sources)))
    if not failover:
        sources = sources[:1]
    unavailable = unavailable if unavailable is not None else set()
    total = _attempt_plan_count(sources, model_override) if failover else 1
    attempts, idx = [], 0
    for si, src in enumerate(sources):
        if src["name"] in unavailable:
            continue
        models = [model_override] if model_override else failovercfg.source_models(src)
        if not failover:
            models = models[:1]
        for model in models:
            if (cancelled and cancelled()) or (deadline_at is not None and time.monotonic() >= deadline_at):
                return None, attempts
            idx += 1
            host = failovercfg.endpoint_host(src["base_url"])
            print("[%d/%d] 尝试源 %s（%s，模型 %s）..." % (idx, total, src["name"], host, model), flush=True)
            attempt = {"source": src["name"], "host": host, "model": model, "ok": False,
                       "status": "running", "started_at": task_store.now(), "request_id": None}
            attempts.append(attempt)
            if on_attempts:
                on_attempts(attempts)
            started = time.monotonic()
            try:
                incompatible = _capability_error(src, size, model)
                if incompatible:
                    attempt["sent"] = False
                    raise AttemptError(*incompatible)
                attempt["sent"] = True
                if on_attempts:
                    on_attempts(attempts)
                def event_received(event):
                    attempt.update(failovercfg.redact_data(event, _SECRETS))
                    if on_attempts:
                        on_attempts(attempts)
                arguments = (src, prompt, size, input_image, model, verbose)
                result = (_bounded_attempt(arguments, deadline_at, cancelled, event_received)
                          if deadline_at is not None else attempt_source(*arguments))
            except AttemptError as error:
                attempt.update(ok=False, status="unknown" if error.kind == "unknown" else "failed",
                               strategy=error.kind, error=_safe(error.message),
                               elapsed_seconds=round(time.monotonic() - started, 3), finished_at=task_store.now())
                if error.request_id:
                    attempt["request_id"] = _safe(error.request_id)
                if on_attempts:
                    on_attempts(attempts)
                if error.disable_source:
                    unavailable.add(src["name"])
                if error.kind in ("fatal", "unknown"):
                    label = "状态未知" if error.kind == "unknown" else "致命错误"
                    print("    ✗ %s，停止尝试：%s" % (label, error.message), flush=True)
                    return None, attempts
                if not failover:
                    print("    ✗ %s —— 故障转移已关闭，不切换其他源/模型" % error.message)
                    return None, attempts
                if error.kind == "next_source" or model == models[-1]:
                    print("    ✗ %s —— 切换下一个中转站" % error.message)
                    break
                print("    ✗ %s —— 换本站下一个模型" % error.message)
                continue
            attempt.update(ok=True, status="succeeded", elapsed_seconds=result["elapsed"],
                           finished_at=task_store.now())
            if on_attempts:
                on_attempts(attempts)
            return result, attempts
    return None, attempts


def unique_path(path):
    """已存在则追加 -2/-3 后缀，避免覆盖。"""
    if not os.path.exists(path) and not os.path.exists(os.path.splitext(path)[0] + ".meta.json"):
        return path
    stem, ext = os.path.splitext(path)
    for i in range(2, 1000):
        cand = "%s-%d%s" % (stem, i, ext)
        if not os.path.exists(cand) and not os.path.exists(os.path.splitext(cand)[0] + ".meta.json"):
            return cand
    raise RuntimeError("无法生成唯一文件名: " + path)


def save_outputs(outdir, name, force, size, result, prompt, prompt_file,
                 input_image, attempts, started_at, secrets=()):
    """落盘图片 + meta.json，返回 (img_path, meta_path, meta)。"""
    os.makedirs(outdir, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    base_name = "%s-%s-%s" % (name, ts, size_tag(*size))
    img_path = os.path.join(outdir, base_name + "." + result["ext"])
    if force:
        failovercfg.atomic_write(img_path, result["image_bytes"])
    else:
        original_path = img_path
        while True:
            img_path = unique_path(original_path)
            try:
                stream = open(img_path, "xb")  # Reserve atomically across tasks/processes.
            except FileExistsError:
                continue
            try:
                with stream:
                    stream.write(result["image_bytes"])
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                # Only this exclusively-created file belongs to this failed write.
                try:
                    os.unlink(img_path)
                except FileNotFoundError:
                    pass
                raise
            break

    meta = {
        "name": name,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "source": {
            "name": result["source_name"],
            "host": result["source_host"],
            "model": result["model"],
        },
        "model_requested": result["model"],
        "model_returned": result.get("model_returned"),
        "mode": "edit" if input_image else "generate",
        "size_requested": list(size),
        "size_actual": {"width": result["width"], "height": result["height"]},
        "size_mismatch": [result["width"], result["height"]] != list(size),
        "prompt": prompt,
        "prompt_file": os.path.abspath(prompt_file) if prompt_file else None,
        "input_image": os.path.abspath(input_image) if input_image else None,
        "output_file": os.path.abspath(img_path),
        "output_bytes": len(result["image_bytes"]),
        "duration_seconds": round(time.time() - started_at, 2),
        "attempts": attempts,  # 错误文本会尝试脱敏，仍需在分享前检查
    }
    meta = failovercfg.redact_data(meta, tuple(_SECRETS) + tuple(secrets))
    meta_path = os.path.splitext(img_path)[0] + ".meta.json"
    failovercfg.atomic_write(meta_path, (json.dumps(meta, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return img_path, meta_path, meta


def _print_success(result, img_path, meta_path, meta, attempts):
    print("    ✓ 成功（%.1fs）" % result["elapsed"])
    if meta["size_mismatch"]:
        print("      注意：实际尺寸 %dx%d 与请求 %dx%d 不一致，以输出为准（详见 meta.json）。"
              % (result["width"], result["height"],
                 meta["size_requested"][0], meta["size_requested"][1]))
    print("图片: %s（%dx%d, %d 字节）" % (img_path, result["width"], result["height"],
                                         len(result["image_bytes"])))
    print("记录: %s" % meta_path)
    if len(attempts) > 1:
        fail_names = "、".join("%s/%s" % (a["source"], a.get("model", "?"))
                               for a in attempts if not a["ok"])
        print("本次共尝试 %d 次（%s 失败后切换），最终由 %s/%s 产出。"
              % (len(attempts), fail_names, result["source_name"], result["model"]))


def _fail_summary(attempts):
    print("\n全部 %d 次尝试（站×模型）失败：" % len(attempts))
    _print_attempts(attempts)
    print("排查建议：确认中转站网络与额度；429/超时多为限流或服务过载；"
          "若超时发生在服务端已接受请求之后，切换可能造成重复生成/计费，先检查输出目录与中转站记录。")


def _print_attempts(attempts):
    for a in attempts:
        if a["ok"]:
            print("  - %s/%s: 成功（%.1fs）" % (a["source"], a.get("model", "?"),
                                                a.get("elapsed_seconds", 0)))
        else:
            print("  - %s/%s: 失败[%s] %s" % (a["source"], a.get("model", "?"),
                                               a.get("strategy", "?"), a["error"]))


# ---------------------------------------------------------------- 命令公共

def read_prompt(args):
    if getattr(args, "prompt_file", None):
        with open(args.prompt_file, "r", encoding="utf-8-sig") as f:
            p = f.read().strip()
    else:
        p = (getattr(args, "prompt", None) or "").strip()
    if not p:
        raise SystemExit("错误：提示词为空。用 --prompt 或 --prompt-file 提供（推荐后者，避免提示词进入终端历史）。")
    if len(p) > 32000:
        raise SystemExit("错误：提示词过长（%d 字符，上限 32000）。" % len(p))
    return p


def _load_cfg_or_exit():
    global _SECRETS
    try:
        cfg = failovercfg.load_config()
    except RuntimeError as e:
        raise SystemExit("错误：%s" % e)
    if cfg is None:
        raise SystemExit(
            "错误：No config found（%s）。先运行 python scripts/setup.py add --name <名> "
            "--base-url <地址> --api-key <密钥> --model <模型> 保存至少一个已启用的中转源，"
            "再用 --list-sources 检查。" % failovercfg.config_path()
        )
    _SECRETS = tuple(s["api_key"] for s in cfg["sources"])
    return cfg


def pick_sources(cfg, only_name, model_override=None):
    if only_name:
        src = failovercfg.find_source(cfg, only_name)
        if not src:
            names = ", ".join(s["name"] for s in cfg["sources"]) or "（无）"
            raise SystemExit("错误：配置中不存在源 %r。可用源：%s" % (only_name, names))
        if not src.get("enabled", True):
            raise SystemExit("错误：源 %r 当前处于禁用状态，先用 setup.py enable 启用。" % only_name)
        sources = [src]
    else:
        sources = failovercfg.ordered_sources(cfg)  # ★首选置顶，其余按数组顺序
        if not sources:
            raise SystemExit(
                "错误：没有已启用的中转源。运行 python scripts/setup.py add ...（或 setup.py gui）配置，"
                "再跑 --list-sources 检查。"
            )
    if not model_override:
        for s in sources:
            if not failovercfg.source_models(s):
                raise SystemExit("错误：源 %r 未配置模型，用 setup.py 重新添加并填模型。" % s["name"])
    return sources


def _prepare_generate(args):
    prompt = read_prompt(args)
    try:
        size = parse_size(getattr(args, "size", None))
    except ValueError as e:
        raise SystemExit("错误：%s" % e)
    input_image = getattr(args, "input_image", None)
    if input_image and not os.path.isfile(input_image):
        raise SystemExit("错误：输入图片不存在：%s" % input_image)
    cfg = _load_cfg_or_exit()
    sources = pick_sources(cfg, getattr(args, "source", None),
                           getattr(args, "model", None))
    return prompt, size, input_image, sources, failovercfg.failover_enabled(cfg)


# ---------------------------------------------------------------- generate

def _task_record(args, kind, prompts, size, sources, failover):
    created = task_store.now()
    return {
        "schema_version": 1, "id": getattr(args, "task_id", None) or str(uuid.uuid4()),
        "name": args.name, "kind": kind, "status": "queued", "created_at": created,
        "updated_at": created, "started_at": None, "finished_at": None,
        "cancel_requested": False, "duration_seconds": 0,
        "items": [{"id": str(index), "prompt": prompt, "status": "queued", "error": None,
                   "output": None, "attempts": [], "created_at": created, "updated_at": created,
                   "started_at": None, "finished_at": None}
                  for index, prompt in enumerate(prompts, 1)],
        "sources": [source["name"] for source in sources], "size": list(size),
        "options": {"model": getattr(args, "model", None),
                    "input_image": os.path.abspath(args.input_image) if getattr(args, "input_image", None) else None,
                    "outdir": os.path.abspath(getattr(args, "outdir", None) or "generated"),
                    "force": getattr(args, "force", False), "failover": failover,
                    "prompt_file": os.path.abspath(getattr(args, "prompt_file", None) or args.prompts_file)
                                   if getattr(args, "prompt_file", None) or getattr(args, "prompts_file", None) else None,
                    "deadline": getattr(args, "deadline", 600) if math.isfinite(float(getattr(args, "deadline", 600))) else 600, "verbose": getattr(args, "verbose", False)},
    }


def _execute_task(directory, record, sources, selected, raw_prompts=None):
    started = time.time()
    deadline_at = time.monotonic() + float(record["options"].get("deadline", 600))
    options, size = record["options"], tuple(record["size"])
    record.update(started_at=task_store.now(), finished_at=None)
    unavailable = set()
    def persist():
        record["duration_seconds"] = round(time.time() - started, 3)
        task_store.save_task(directory, record, _SECRETS)
    def cancelled():
        return task_store.load_task(directory, record["id"]).get("cancel_requested", False)
    print("任务: %s" % record["id"], flush=True)
    for item in record["items"]:
        if item["id"] not in selected:
            continue
        if cancelled() or time.monotonic() >= deadline_at:
            item.update(status="cancelled", error="已取消或达到总时限，尚未发送此条请求",
                        updated_at=task_store.now(), finished_at=task_store.now())
            persist()
            continue
        prompt = raw_prompts.get(item["id"], item["prompt"]) if raw_prompts else item["prompt"]
        item.update(status="running", error=None, started_at=task_store.now(), finished_at=None,
                    updated_at=task_store.now())
        old_attempts = list(item.get("attempts", []))
        persist()
        print("\n===== [%s/%d] %s =====" % (item["id"], len(record["items"]), _safe(prompt)[:40]), flush=True)
        def attempts_changed(attempts):
            item["attempts"] = old_attempts + attempts
            item["updated_at"] = task_store.now()
            persist()
        try:
            result, attempts = _run_attempts(
                sources, prompt, size, options.get("input_image"), options.get("model"),
                options.get("verbose", False), failover=options.get("failover", True),
                deadline_at=deadline_at, cancelled=cancelled, on_attempts=attempts_changed,
                unavailable=unavailable)
            if result:
                name = record["name"] if record["kind"] == "generate" else "%s-%02d" % (record["name"], int(item["id"]))
                try:
                    img_path, meta_path, meta = save_outputs(
                        options["outdir"], name, options.get("force", False), size, result, prompt,
                        options.get("prompt_file"), options.get("input_image"), item["attempts"], started, _SECRETS)
                except (OSError, ValueError) as error:
                    raise AttemptError("unknown", "上游已返回图片但本地保存失败；请检查输出目录，勿直接重复生成: " + _safe(error)) from None
                item.update(status="succeeded", output={"image": os.path.abspath(img_path),
                            "metadata": os.path.abspath(meta_path), "width": result["width"],
                            "height": result["height"], "source": result["source_name"], "model": result["model"]})
                _print_success(result, img_path, meta_path, meta, attempts)
            else:
                last = attempts[-1] if attempts else {}
                if last.get("cancel_requested"):
                    record["cancel_requested"] = True
                item["status"] = "unknown" if last.get("strategy") == "unknown" else "failed"
                item["error"] = last.get("error") or "没有可用候选源，或任务已取消/达到总时限"
        except (KeyboardInterrupt, AttemptError, OSError, RuntimeError) as error:
            if isinstance(error, KeyboardInterrupt):
                record["cancel_requested"] = True
            item["status"] = "unknown"
            item["error"] = _safe(error) or "执行已中断，结果未知"
            for attempt in item.get("attempts", []):
                if attempt.get("status") == "running":
                    attempt.update(status="unknown", strategy="unknown", ok=False, error=item["error"], finished_at=task_store.now())
        item.update(updated_at=task_store.now(), finished_at=task_store.now())
        persist()
    record["finished_at"] = task_store.now()
    persist()
    counts = record["counts"]
    if record["kind"] == "batch":
        print("\n===== 批量汇总：成功 %d / 失败 %d / 共 %d（总耗时 %.1fs）=====" %
              (counts["succeeded"], counts["failed"] + counts["unknown"], counts["total"], record["duration_seconds"]))
    print("任务状态: %s；记录: %s" % (record["status"], os.path.join(directory, record["id"] + ".json")))
    return 0 if record["status"] == "succeeded" else 1


def _create_and_run(args, kind):
    directory = getattr(args, "jobs_dir", None) or task_store.default_jobs_dir()
    prompts, size, sources, failover = [], DEFAULT_SIZE, [], True
    try:
        cfg = _load_cfg_or_exit()
        if kind == "generate":
            prompts = [read_prompt(args)]
        else:
            with open(args.prompts_file, encoding="utf-8-sig") as stream:
                prompts = [line.strip() for line in stream if line.strip() and not line.strip().startswith("#")]
            if not prompts:
                raise ValueError("提示词列表里没有有效提示词（一行一图）")
            if any(len(prompt) > 32000 for prompt in prompts):
                raise ValueError("提示词过长（每条上限 32000 字符）")
        size = parse_size(args.size)
        if getattr(args, "input_image", None) and not os.path.isfile(args.input_image):
            raise ValueError("输入图片不存在：" + args.input_image)
        if not math.isfinite(float(getattr(args, "deadline", 600))) or float(getattr(args, "deadline", 600)) <= 0:
            raise ValueError("总时限必须是大于 0 的有限秒数")
        sources = pick_sources(cfg, getattr(args, "source", None), getattr(args, "model", None))
        failover = failovercfg.failover_enabled(cfg)
    except (OSError, ValueError, SystemExit) as error:
        prompts = prompts or [getattr(args, "prompt", None) or ""]
        record = _task_record(args, kind, prompts, size, sources, failover)
        for item in record["items"]:
            item.update(status="failed", error=_safe(error), finished_at=task_store.now())
        record["finished_at"] = task_store.now()
        task_store.create_task(directory, record, _SECRETS)
        print("错误：%s" % error)
        return 1
    record = _task_record(args, kind, prompts, size, sources, failover)
    with task_store.task_lock(directory, record["id"]):
        task_store.create_task(directory, record, _SECRETS)
        if not failover:
            print("故障转移已关闭：仅尝试首位源首个模型")
        return _execute_task(directory, record, sources, {item["id"] for item in record["items"]},
                             {str(index): prompt for index, prompt in enumerate(prompts, 1)})


def cmd_generate(args):
    return _create_and_run(args, "generate")


def cmd_batch(args):
    return _create_and_run(args, "batch")


def cmd_history(args):
    records = task_store.list_tasks(args.jobs_dir)
    if args.json:
        builtins.print(json.dumps(records, ensure_ascii=False))
    else:
        for record in records:
            print("%s  %s  %s" % (record["id"], record["status"], record.get("name", "")))
    return 0


def cmd_cancel(args):
    record = task_store.cancel_task(args.jobs_dir, args.task_id)
    print("已请求取消任务 %s；已发送的请求须核对上游结果" % record["id"])
    return 0


def cmd_retry(args):
    directory = args.jobs_dir or task_store.default_jobs_dir()
    with task_store.task_lock(directory, args.task_id):
        record = task_store.recover_task(directory, args.task_id)
        eligible = {"queued", "failed", "cancelled"}
        if args.confirm_unknown:
            eligible.add("unknown")
        selected = {item["id"] for item in record["items"] if item["status"] in eligible}
        if not selected:
            print("没有可安全重试的条目；状态未知的条目须先核对上游，再使用 --confirm-unknown 确认")
            return 1
        cfg = _load_cfg_or_exit()
        sources = [source for name in record.get("sources", []) for source in failovercfg.enabled_sources(cfg) if source["name"] == name]
        if not sources:
            raise RuntimeError("原任务的源均已移除或禁用；请先恢复配置")
        record["cancel_requested"] = False
        if args.deadline is not None:
            if not math.isfinite(args.deadline) or args.deadline <= 0:
                raise RuntimeError("总时限必须是大于 0 的有限秒数")
            record["options"]["deadline"] = args.deadline
        for item in record["items"]:
            if item["id"] in selected:
                item["status"] = "queued"
        task_store.save_task(directory, record, _SECRETS, reset_cancel=True)
        return _execute_task(directory, record, sources, selected)


# ---------------------------------------------------------------- 模型发现

def cmd_models(args):
    cfg = _load_cfg_or_exit()
    sources = pick_sources(cfg, getattr(args, "source", None))
    any_ok = False
    for src in sources:
        base = src["base_url"].rstrip("/")
        host = failovercfg.endpoint_host(base)
        print("源 %s（%s）：" % (src["name"], host))
        req = urllib.request.Request(
            base + "/models", headers=failovercfg.api_headers(src["api_key"]))
        timeout = min(20, int(src.get("timeout") or 20))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = _parse_json(resp.read()) or {}
        except urllib.error.HTTPError as e:
            print("  ✗ HTTP %d：%s（检查密钥/地址）" % (e.code, e.reason))
            continue
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            print("  ✗ 不可达：%s" % _safe(e))
            continue
        ids = []
        items = data.get("data") if isinstance(data, dict) else None
        if isinstance(items, list):
            for it in items:
                mid = it.get("id") if isinstance(it, dict) else it
                if mid:
                    ids.append(str(mid))
        if not ids:
            print("  △ 接口正常但未返回模型列表（该站可能不开放 /models，以服务商文档为准）")
            any_ok = True
            continue
        hits = [i for i in ids if IMAGE_HINT.search(i)]
        if hits:
            print("  生图相关模型（%d 个）：" % len(hits))
            for mid in hits:
                print("    - %s" % mid)
        else:
            print("  未匹配到常见生图模型，该站全部模型（前 15 个）：")
            for mid in ids[:15]:
                print("    - %s" % mid)
        any_ok = True
    if not any_ok:
        print("\n所有源都不可用，先跑 --check 体检。")
        return 1
    print("\n提示：把上面任一模型 ID 填进配置（setup.py add --model，或 GUI 里逗号分隔多个）即可使用。")
    return 0


# ---------------------------------------------------------------- 健康体检

def cmd_check(_args):
    cfg = _load_cfg_or_exit()
    sources = failovercfg.enabled_sources(cfg)
    if not sources:
        raise SystemExit("错误：没有已启用的中转源。")

    print("== 源健康体检 ==")
    ok_count = 0
    for src in sources:
        base = src["base_url"].rstrip("/")
        host = failovercfg.endpoint_host(base)
        req = urllib.request.Request(
            base + "/models", headers=failovercfg.api_headers(src["api_key"]))
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                resp.read()
            latency = int((time.time() - t0) * 1000)
            print("  ✓ %s（%s）可达，%dms，/models 接口正常" % (src["name"], host, latency))
            ok_count += 1
        except urllib.error.HTTPError as e:
            latency = int((time.time() - t0) * 1000)
            if e.code == 401:
                print("  △ %s（%s）可达但密钥无效（HTTP %d），生图会失败"
                      % (src["name"], host, e.code))
            elif e.code == 403:
                print("  △ %s（%s）请求被拒绝（HTTP 403）；请检查权限或服务商安全策略"
                      % (src["name"], host))
            else:
                print("  △ %s（%s）可达（%dms，HTTP %d 无 /models 接口，生图仍可能可用）"
                      % (src["name"], host, latency, e.code))
                ok_count += 1
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            print("  ✗ %s（%s）不可达：%s" % (src["name"], host, _safe(e)))

    print("\n== 历史统计（%s）==" % os.path.join("generated", "*.meta.json"))
    stats = {}
    for mp in glob.glob(os.path.join("generated", "*.meta.json")):
        try:
            with open(mp, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        name = (meta.get("source") or {}).get("name", "?")
        st = stats.setdefault(name, {"count": 0, "total_sec": 0.0, "switched": 0})
        st["count"] += 1
        st["total_sec"] += float(meta.get("duration_seconds") or 0)
        if len(meta.get("attempts") or []) > 1:
            st["switched"] += 1
    if not stats:
        print("  暂无记录（在当前目录生图后自动积累，可据此用 setup.py up/down 调整源优先级）。")
    else:
        for name, st in sorted(stats.items(), key=lambda kv: -kv[1]["count"]):
            avg = st["total_sec"] / st["count"] if st["count"] else 0
            extra = ("，%d 张靠切换源/模型救回" % st["switched"]) if st["switched"] else ""
            print("  %s：%d 张，平均 %.1fs%s" % (name, st["count"], avg, extra))
    return 0 if ok_count else 1


# ---------------------------------------------------------------- list / main

def cmd_list_sources(_args):
    cfg = _load_cfg_or_exit()
    sources = cfg["sources"]
    if not sources:
        print("配置为空（%s）。用 setup.py add 添加中转源。" % failovercfg.config_path())
        return 1
    print("配置文件: %s（★=生图首选，先于列表顺序尝试；其余按列表顺序后备；"
          "站内模型按顺序级联；仅启用状态的源参与）" % failovercfg.config_path())
    pi = failovercfg.prefer_index(cfg)
    for i, s in enumerate(sources, 1):
        state = "启用" if s.get("enabled", True) else "禁用"
        timeout = s.get("timeout") or failovercfg.DEFAULT_TIMEOUT
        star = "★" if (pi is not None and i - 1 == pi) else " "
        print("%s%d. [%s] %-12s %s  %s  timeout=%ds key=%s"
              % (star, i, state, s["name"], failovercfg.models_display(s), failovercfg.endpoint_host(s["base_url"]),
                 timeout, failovercfg.mask_key(s["api_key"])))
    return 0


def _termination_requested(_signum, _frame):
    # GUI shutdown uses SIGTERM after a cancellation write cannot complete.
    # Unwind the bounded-request finally block before the parent engine exits.
    raise KeyboardInterrupt("进程收到终止请求")


def main(argv=None):
    try:
        signal.signal(signal.SIGTERM, _termination_requested)
    except ValueError:
        pass  # Embedded callers outside the main thread cannot install handlers.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser(
        prog="failover_imagegen_tool.py",
        description="OpenAI 兼容中转站生图（多源×多模型级联故障切换，输出到 generated/）",
    )
    parser.add_argument("--list-sources", action="store_true", help="列出已配置的中转源")
    parser.add_argument("--models", action="store_true",
                        help="发现各中转站支持的生图模型（查 /v1/models 并过滤）")
    parser.add_argument("--check", action="store_true",
                        help="源健康体检 + 历史成功/耗时统计")
    sub = parser.add_subparsers(dest="command")

    g = sub.add_parser("generate", help="生成/编辑单张图片")
    g.add_argument("--prompt", help="提示词（推荐改用 --prompt-file，避免进入终端历史）")
    g.add_argument("--prompt-file", help="提示词文件路径（整文件为一条提示词）")
    g.add_argument("--name", default="image", help="输出文件基础名（默认 image）")
    g.add_argument("--source", help="指定使用某个中转源（默认按配置顺序自动故障切换）")
    g.add_argument("--model", help="临时覆盖模型（默认用各源配置的模型级联）")
    g.add_argument("--size", help="尺寸：别名 2k/4k/2k-portrait/4k-portrait/square，或 3840x2160（默认 2560x1440）")
    g.add_argument("--input-image", help="参考图路径，提供后走 /images/edits 编辑模式")
    g.add_argument("--outdir", help="输出目录（默认 ./generated）")
    g.add_argument("--force", action="store_true", help="同名时直接覆盖（默认追加 -2 后缀）")
    g.add_argument("--verbose", action="store_true", help="打印请求细节（不含密钥）")
    g.set_defaults(func=cmd_generate)

    b = sub.add_parser("batch", help="批量生图（一行一图的提示词文件）")
    b.add_argument("--prompts-file", required=True, help="提示词列表文件：一行一条，# 开头为注释")
    b.add_argument("--name", default="batch", help="输出文件基础名（默认 batch，实际为 batch-01/02…）")
    b.add_argument("--source", help="指定使用某个中转源")
    b.add_argument("--model", help="临时覆盖模型")
    b.add_argument("--size", help="尺寸（同 generate）")
    b.add_argument("--outdir", help="输出目录（默认 ./generated）")
    b.add_argument("--force", action="store_true", help="同名时直接覆盖")
    b.add_argument("--verbose", action="store_true", help="打印请求细节（不含密钥）")
    b.set_defaults(func=cmd_batch)

    for command in (g, b):
        command.add_argument("--jobs-dir", default=None, help="私有任务记录目录")
        command.add_argument("--task-id", help="预先分配的任务 UUID")
        command.add_argument("--deadline", type=float, default=600, help="任务总时限（秒，默认 600）")

    history = sub.add_parser("history", help="读取任务历史与恢复中断状态")
    history.add_argument("--jobs-dir", default=None)
    history.add_argument("--json", action="store_true")
    history.set_defaults(func=cmd_history)
    retry = sub.add_parser("retry", help="仅重试失败或未发送的任务条目")
    retry.add_argument("--jobs-dir", default=None)
    retry.add_argument("--task-id", required=True)
    retry.add_argument("--confirm-unknown", action="store_true", help="已核对上游，明确确认未知状态可重试")
    retry.add_argument("--deadline", type=float, default=None)
    retry.set_defaults(func=cmd_retry)
    cancel = sub.add_parser("cancel", help="协作取消任务")
    cancel.add_argument("--jobs-dir", default=None)
    cancel.add_argument("--task-id", required=True)
    cancel.set_defaults(func=cmd_cancel)

    m = sub.add_parser("models", help="发现各中转站支持的生图模型")
    m.add_argument("--source", help="只查某个源")
    m.set_defaults(func=cmd_models)

    c = sub.add_parser("check", help="源健康体检 + 历史统计")
    c.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    if args.list_sources:
        return cmd_list_sources(args)
    if args.models:
        return cmd_models(args)
    if args.check:
        return cmd_check(args)
    if getattr(args, "func", None):
        try:
            return args.func(args)
        except (RuntimeError, OSError, ValueError) as error:
            print("错误：%s" % error, file=sys.stderr)
            return 1
    parser.print_help()
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
