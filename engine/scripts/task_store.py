"""Private durable image-task records. No credentials or provider configuration."""
import contextlib
import datetime
import json
import os
import uuid

import failovercfg

STATUSES = ('queued', 'running', 'succeeded', 'failed', 'unknown', 'cancelled', 'partial')


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds')


def default_jobs_dir():
    return os.path.abspath(os.environ.get('FAILOVER_IMAGEGEN_JOBS_DIR') or
                           os.path.join(os.path.dirname(failovercfg.config_path()), 'jobs'))


def _path(directory, task_id):
    try:
        normalized = str(uuid.UUID(str(task_id)))
    except (ValueError, AttributeError, TypeError):
        raise RuntimeError('任务 ID 必须是 UUID') from None
    return os.path.join(os.path.abspath(directory or default_jobs_dir()), normalized + '.json')


@contextlib.contextmanager
def task_lock(directory, task_id):
    """Exclusive execution lock, independent of the short JSON-write lock."""
    path = _path(directory, task_id)
    try:
        with failovercfg.config_lock(path + '.run'):
            yield
    except RuntimeError as exc:
        if '配置正在' in str(exc):
            raise RuntimeError('任务正在运行，不能同时重试；可请求取消') from None
        raise


def _validate(record, task_id):
    """Reject malformed but syntactically valid JSON before UI/retry use."""
    def invalid():
        raise RuntimeError('任务记录格式无效: ' + str(task_id))
    if not isinstance(record, dict) or record.get('id') != str(uuid.UUID(str(task_id))):
        invalid()
    if not isinstance(record.get('status'), str) or record['status'] not in STATUSES:
        invalid()
    if not isinstance(record.get('items'), list) or not isinstance(record.get('options', {}), dict):
        invalid()
    if not isinstance(record.get('sources', []), list) or any(not isinstance(v, str) for v in record.get('sources', [])):
        invalid()
    for field in ('name', 'kind', 'created_at', 'updated_at'):
        if field in record and not isinstance(record[field], str):
            invalid()
    if 'counts' in record and (not isinstance(record['counts'], dict) or any(type(v) is not int or v < 0 for v in record['counts'].values())):
        invalid()
    ids = set()
    for item in record['items']:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id'] or item['id'] in ids:
            invalid()
        ids.add(item['id'])
        if not isinstance(item.get('status'), str) or item['status'] not in STATUSES or not isinstance(item.get('prompt'), str):
            invalid()
        if item.get('output') is not None and not isinstance(item['output'], dict):
            invalid()
        if item.get('error') is not None and not isinstance(item['error'], str):
            invalid()
        if not isinstance(item.get('attempts', []), list) or any(not isinstance(v, dict) for v in item.get('attempts', [])):
            invalid()
    return record


def load_task(directory, task_id):
    path = _path(directory, task_id)
    try:
        with open(path, encoding='utf-8') as stream:
            record = json.load(stream)
    except (OSError, ValueError) as exc:
        raise RuntimeError('无法读取任务 %s: %s' % (task_id, type(exc).__name__)) from None
    return _validate(record, task_id)


def summarize(record):
    counts = {status: 0 for status in STATUSES}
    for item in record.get('items', []):
        counts[item.get('status', 'queued')] = counts.get(item.get('status', 'queued'), 0) + 1
    counts['total'] = len(record.get('items', []))
    record['counts'] = counts
    if counts['running']:
        record['status'] = 'running'
    elif counts['succeeded'] == counts['total'] and counts['total']:
        record['status'] = 'succeeded'
    elif counts['unknown']:
        record['status'] = 'partial' if counts['succeeded'] else 'unknown'
    elif counts['succeeded']:
        record['status'] = 'partial'
    elif counts['failed']:
        record['status'] = 'failed'
    elif counts['cancelled']:
        record['status'] = 'cancelled'
    else:
        record['status'] = 'queued'
    return record


def _write(path, record, secrets):
    clean = failovercfg.redact_data(record, secrets)
    # Do not serialize accidental credential/config fields supplied by callers.
    for field in ('api_key', 'authorization', 'base_url', 'config'):
        clean.pop(field, None)
    clean['sources'] = [s if isinstance(s, str) else s.get('name', '')
                        for s in clean.get('sources', [])]
    failovercfg.atomic_write(path, (json.dumps(clean, ensure_ascii=False, indent=2) + '\n').encode())
    return clean


def create_task(directory, record, secrets=()):
    record['id'] = str(uuid.UUID(str(record.get('id') or uuid.uuid4())))
    record['task_id'] = record['id']
    path = _path(directory, record['id'])
    with failovercfg.config_lock(path):
        if os.path.exists(path):
            raise RuntimeError('任务 ID 已存在，请使用 retry 或新的任务 ID')
        record.setdefault('created_at', now())
        record.setdefault('updated_at', record['created_at'])
        summarize(record)
        return _write(path, record, secrets)


def save_task(directory, record, secrets=(), reset_cancel=False):
    path = _path(directory, record['id'])
    with failovercfg.config_lock(path):
        current = load_task(directory, record['id'])
        if current.get('cancel_requested') and not reset_cancel:
            record['cancel_requested'] = True
        record['updated_at'] = now()
        summarize(record)
        return _write(path, record, secrets)


def cancel_task(directory, task_id):
    path = _path(directory, task_id)
    with failovercfg.config_lock(path):
        record = load_task(directory, task_id)
        record['cancel_requested'] = True
        record['updated_at'] = now()
        for item in record.get('items', []):
            if item.get('status') == 'queued':
                item.update(status='cancelled', error='用户已取消，未发送请求', updated_at=now())
        summarize(record)
        return _write(path, record, ())


def recover_task(directory, task_id):
    """Caller holds the execution lock. Never replay an interrupted request."""
    record = load_task(directory, task_id)
    changed = False
    for item in record.get('items', []):
        if item.get('status') != 'running':
            continue
        item.update(status='unknown', error='上次执行已中断；上游可能仍在生成，核对后才能确认重试',
                    updated_at=now(), finished_at=now())
        for attempt in item.get('attempts', []):
            if attempt.get('status') == 'running':
                attempt.update(status='unknown', ok=False, strategy='unknown',
                               error=item['error'], finished_at=now())
        changed = True
    if changed:
        record['finished_at'] = now()
        record = save_task(directory, record)
    return record


def list_tasks(directory=None):
    directory = directory or default_jobs_dir()
    if not os.path.isdir(directory):
        return []
    records = []
    for filename in os.listdir(directory):
        if not filename.endswith('.json'):
            continue
        task_id = filename[:-5]
        try:
            record = load_task(directory, task_id)
            if record.get('status') == 'running' or any(i.get('status') == 'running' for i in record.get('items', [])):
                try:
                    with task_lock(directory, task_id):
                        record = recover_task(directory, task_id)
                except RuntimeError:
                    pass  # Another process is still executing this task.
            records.append(record)
        except RuntimeError:
            # Keep corrupt files untouched and expose an actionable history row.
            records.append({'id': task_id, 'task_id': task_id, 'name': filename, 'kind': 'unknown',
                            'status': 'failed', 'created_at': '', 'updated_at': '', 'items': [],
                            'error': '任务记录损坏或无法读取，原文件已保留'})
    return sorted(records, key=lambda r: r.get('created_at', ''), reverse=True)
