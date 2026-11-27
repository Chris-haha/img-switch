"""failover-imagegen-tool 共享配置模块：加载/保存 config.json 与中转源管理。

config.json 结构：
{
  "version": 1,
  "sources": [
    {
      "name": "example-source",                 # 唯一名称
      "base_url": "https://relay.example.com/v1",   # OpenAI 兼容根地址，通常以 /v1 结尾
      "api_key": "sk-xxx",             # 只保存在本机，禁止提交/分享
      "model": "gpt-image-2.5-flare",  # 单模型；也可用 "models": ["a","b"] 配置站内级联
      "enabled": true,
      "timeout": 300                   # 可选，秒
    }
  ]
}
sources 数组顺序即故障切换优先级：排在前的先用；
源内 models 数组顺序即站内模型级联顺序（前面的模型失败先换本站下一个模型）。
"""
import json
import hashlib
import contextlib
import os
import re
import ssl
import sys
import tempfile
import urllib.parse

CONFIG_ENV = "FAILOVER_IMAGEGEN_TOOL_CONFIG"
CONFIG_FILENAME = "config.json"
DEFAULT_TIMEOUT = 300  # 生图（尤其 4K）耗时较长，默认放宽到 5 分钟
USER_AGENT = "IMG-Switch/1.0"

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9_\-.]{6,}", re.IGNORECASE),
]


def api_headers(api_key):
    """Identify the client consistently for every authenticated relay request."""
    return {"Authorization": "Bearer " + api_key, "User-Agent": USER_AGENT}


def configure_default_certificates():
    """Let macOS source installations use system roots when Python has none."""
    if sys.platform != "darwin" or "SSL_CERT_FILE" in os.environ or "SSL_CERT_DIR" in os.environ:
        return
    paths = ssl.get_default_verify_paths()
    if not paths.cafile and not paths.capath and os.path.isfile("/etc/ssl/cert.pem"):
        os.environ["SSL_CERT_FILE"] = "/etc/ssl/cert.pem"


configure_default_certificates()


def skill_root():
    """skill 根目录（本文件位于 scripts/ 下）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def appdata_config_path():
    """App 统一配置路径（Windows AppData / 其他平台 ~/.config）。"""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.path.expanduser("~"), ".config")
    return os.path.join(base, "failover-imagegen-tool", "config.json")


def config_path():
    """配置文件路径优先级：
    1. 环境变量 FAILOVER_IMAGEGEN_TOOL_CONFIG
    2. skill 根目录下已存在的 config.json（手动安装/手动管理时向后兼容）
    3. AppData 统一配置（本机开关 App 管理，多 Agent 共享，不存在时作为写入目标）
    """
    env = os.environ.get(CONFIG_ENV)
    if env:
        return os.path.abspath(env)
    local = os.path.join(skill_root(), CONFIG_FILENAME)
    if os.path.exists(local):
        return local
    return appdata_config_path()


def default_config():
    cfg = Config({"version": 1, "sources": []})
    cfg.revision = None  # The reader observed that no file existed yet.
    return cfg


class Config(dict):
    """A loaded document carries its revision without serializing it to JSON."""


def atomic_write(path, data):
    """Replace a file using a private, unique temporary file in its directory."""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".img-switch-", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextlib.contextmanager
def config_lock(path):
    """Serialize cooperating writers; OS locks are released after crashes."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
    fd = os.open(path + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+b") as lock:
        if os.name == "nt":
            import msvcrt
            if os.fstat(lock.fileno()).st_size == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError("配置正在被其他进程保存，请稍后重试；原文件未改动") from None
        else:
            import fcntl
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError("配置正在被其他进程保存，请稍后重试；原文件未改动") from None
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def load_config(path=None):
    """读取配置；文件不存在返回 None（视为未配置），格式错误抛异常。"""
    path = path or config_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            raw = f.read()
        cfg = json.loads(raw)
    except (OSError, ValueError) as e:
        raise RuntimeError("配置文件解析失败 %s: %s" % (path, e))
    if not isinstance(cfg, dict) or not isinstance(cfg.get("sources"), list):
        raise RuntimeError(
            '配置文件格式不正确 %s：顶层应为 {"version":1,"sources":[...]}' % path
        )
    for i, src in enumerate(cfg["sources"]):
        _check_source(src, "sources[%d]" % i)
    names = [s["name"] for s in cfg["sources"]]
    if len(names) != len(set(names)):
        raise RuntimeError("配置中存在同名中转源，请先修复配置；原文件已保留")
    loaded = Config(cfg)
    loaded.revision = hashlib.sha256(raw).hexdigest()
    return loaded


def save_config(cfg, path=None):
    """Reject corrupt/stale documents, retain the last valid file, then replace."""
    path = path or config_path()
    with config_lock(path):
        _save_config_locked(cfg, path)


def _save_config_locked(cfg, path):
    current = load_config(path)  # Never replace an unreadable configuration.
    if hasattr(cfg, "revision") and cfg.revision != (current.revision if current is not None else None):
        raise RuntimeError("配置已被其他窗口修改，请重新打开后再保存；原文件已保留")
    data = (json.dumps(cfg, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if current is not None:
        with open(path, "rb") as f:
            previous = f.read()
        if hashlib.sha256(previous).hexdigest() != current.revision:
            raise RuntimeError("配置在保存前发生变化，请重新打开后再保存")
        atomic_write(path + ".backup", previous)
    atomic_write(path, data)
    if hasattr(cfg, "revision"):
        cfg.revision = hashlib.sha256(data).hexdigest()


def _check_source(src, where):
    if not isinstance(src, dict):
        raise RuntimeError("%s 不是对象" % where)
    for key in ("name", "base_url", "api_key"):
        if not isinstance(src.get(key), str) or not src[key].strip():
            raise RuntimeError("%s.%s 必须为非空字符串" % (where, key))
    if not source_models(src):
        raise RuntimeError("%s 需要至少配置一个模型（model 或 models）" % where)
    if "://" not in src["base_url"]:
        raise RuntimeError("%s.base_url 需要是完整的 http(s) 地址" % where)
    for field in ("supported_sizes", "supported_models"):
        if field not in src:
            continue
        values = src[field]
        if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v.strip() for v in values):
            raise RuntimeError("%s.%s 必须是非空字符串列表" % (where, field))
        if field == "supported_sizes":
            aliases = {"2k", "2k-landscape", "4k", "4k-landscape", "2k-portrait", "4k-portrait", "square", "2k-square"}
            for value in values:
                if value.strip().lower() in aliases:
                    continue
                match = re.fullmatch(r"(\d{2,5})\s*[x×*]\s*(\d{2,5})", value.strip().lower())
                if not match or any(int(n) < 64 for n in match.groups()):
                    raise RuntimeError("%s.supported_sizes 需要尺寸别名或至少 64×64 的宽高" % where)


def source_models(src):
    """源的模型级联列表：优先读 models 数组，兼容旧的单 model 字段。"""
    models = src.get("models")
    if isinstance(models, list):
        out = [str(m).strip() for m in models if str(m).strip()]
        if out:
            return out
    m = str(src.get("model") or "").strip()
    return [m] if m else []


def models_display(src):
    return " → ".join(source_models(src)) or "（未配置模型）"


def normalize_source(name, base_url, api_key, model, enabled=True, timeout=None):
    """构造/规范化一个中转源。model 可传单个字符串或列表（站内模型级联）。"""
    raw = model if isinstance(model, (list, tuple)) else [model]
    models = [str(m).strip() for m in raw if str(m).strip()]
    src = {
        "name": str(name).strip(),
        "base_url": str(base_url).strip().rstrip("/"),
        "api_key": str(api_key).strip(),
        "enabled": bool(enabled),
    }
    if len(models) == 1:
        src["model"] = models[0]
    elif len(models) > 1:
        src["models"] = models
    if timeout:
        src["timeout"] = int(timeout)
    _check_source(src, "source '%s'" % src["name"])
    return src


def find_source(cfg, name):
    for src in cfg["sources"]:
        if src["name"] == name:
            return src
    return None


def enabled_sources(cfg):
    """按优先级（数组顺序）返回已启用的源。"""
    return [s for s in cfg["sources"] if s.get("enabled", True)]


def failover_enabled(cfg):
    """故障转移开关：config.json 顶层 "failover": false 时关闭（默认开启）。
    关闭后生图只尝试首位已启用源的首个模型，失败不切换其他源/模型。"""
    if not isinstance(cfg, dict):
        return True
    return bool(cfg.get("failover", True))


def ordered_sources(cfg):
    """已启用源按生图尝试顺序返回：★首选（顶层 "prefer"=源名，须存在且启用）置顶，
    其余保持数组顺序（后备顺序）。无有效 prefer 时即数组顺序。"""
    srcs = enabled_sources(cfg)
    p = cfg.get("prefer")
    if p:
        first = [s for s in srcs if s.get("name") == p]
        if first:
            return first + [s for s in srcs if s.get("name") != p]
    return srcs


def prefer_index(cfg):
    """★首选源在 cfg["sources"] 里的下标：显式 prefer 且启用优先，否则第一个启用源；
    无启用源返回 None。"""
    srcs = cfg.get("sources") or []
    p = cfg.get("prefer")
    for i, s in enumerate(srcs):
        if p and s.get("name") == p and s.get("enabled", True):
            return i
    for i, s in enumerate(srcs):
        if s.get("enabled", True):
            return i
    return None


def mask_key(key):
    """脱敏展示密钥：sk-abc1234567 -> sk-ab****67。"""
    key = str(key)
    if len(key) <= 8:
        return "*" * len(key)
    return key[:5] + "****" + key[-2:]


def mask_secrets(text, secrets=()):
    """Remove configured credentials before truncating or displaying any text."""
    out = str(text)
    if isinstance(secrets, str):
        secrets = (secrets,)
    variants = set()
    for secret in secrets or ():
        if not isinstance(secret, str) or not secret:
            continue
        variants.update((value, False) for value in (
            secret, json.dumps(secret)[1:-1], json.dumps(secret, ensure_ascii=False)[1:-1]))
        variants.add((secret, True))
    if variants:
        # One pass prevents short secrets from matching the replacement marker.
        patterns = []
        for value, encoded in sorted(variants, key=lambda entry: len(entry[0]), reverse=True):
            pattern = re.escape(value)
            if encoded:
                # URL encoders may escape even unreserved characters, or mix
                # literals and escapes. Only escape hex digits ignore case.
                parts = []
                for char in value:
                    escaped = ''.join('%%%02X' % byte for byte in char.encode('utf-8'))
                    forms = [re.escape(char), '(?i:' + escaped + ')']
                    if char == ' ':
                        forms.append(r'\+')
                    parts.append('(?:' + '|'.join(forms) + ')')
                pattern = ''.join(parts)
            patterns.append(pattern)
        pattern = "|".join(patterns)
        out = re.sub(pattern, "<redacted>", out)
    for pat in SECRET_PATTERNS:
        out = pat.sub(lambda m: m.group(0)[:5] + "****", out)
    return out


def redact_data(value, secrets=()):
    """Return a sanitized copy suitable for a persisted task or diagnostic record."""
    if isinstance(value, str):
        return mask_secrets(value, secrets)
    if isinstance(value, dict):
        return {mask_secrets(key, secrets) if isinstance(key, str) else key:
                redact_data(item, secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_data(item, secrets) for item in value]
    return value


def endpoint_host(base_url):
    """只展示 URL 主机及端口，不包含用户信息、路径或查询参数。"""
    try:
        parsed = urllib.parse.urlsplit(base_url or "")
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            return "<无效地址>"
        host, port = parsed.hostname, parsed.port
        if ":" in host:
            host = "[%s]" % host
        elif not re.fullmatch(r"[A-Za-z0-9._-]+", host.encode("idna").decode("ascii")):
            return "<无效地址>"
        return "%s:%d" % (host, port) if port is not None else host
    except (ValueError, UnicodeError, TypeError):
        return "<无效地址>"
