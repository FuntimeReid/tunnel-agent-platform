"""Management API mounted under /retrieve/manage/v1 on the existing service."""
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import threading
import time
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, Header, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend import Backend, UncertainOperation
from store import ApiError, Store, identifier

PREFIX = '/retrieve/manage/v1'
ID = r'^[a-f0-9]{32}$'
REVIEW_LOCK = threading.Lock()


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Login(Input):
    identifier: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class Direction(Input):
    name: str = Field(min_length=1, max_length=128)

    @field_validator('name')
    @classmethod
    def clean_name(cls, value):
        if not value.strip():
            raise ValueError('名称不能为空。')
        return value.strip()


class LibraryCreate(Direction):
    direction_id: str = Field(pattern=ID)
    description: str = Field(default='', max_length=1024)
    visibility: Literal['public', 'private'] = 'private'


class LibraryUpdate(Input):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=1024)
    direction_id: str | None = Field(default=None, pattern=ID)
    visibility: Literal['public', 'private'] | None = None
    expected_revision: int = Field(ge=0)


class Decision(Input):
    decision: Literal['approve', 'reject']
    comment: str = Field(default='', max_length=1000)


class ModelKey(Input):
    name: str = Field(min_length=1, max_length=128)
    provider: str = Field(min_length=1, max_length=128)
    base_url: str = Field(min_length=1, max_length=512)
    model: str = Field(default='', max_length=128)
    api_key: str = Field(min_length=1, max_length=8192)
    enabled: bool = True

    @field_validator('name', 'provider', 'api_key')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('字段不能为空。')
        return value.strip()

    @field_validator('base_url')
    @classmethod
    def valid_url(cls, value):
        from urllib.parse import urlsplit
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('请填写无账号密码或查询参数的HTTP服务地址。')
        return value.rstrip('/')


class ModelKeyUpdate(Input):
    api_key: str | None = Field(default=None, min_length=1, max_length=8192)
    enabled: bool | None = None
    expected_revision: int = Field(ge=1)


class Search(Input):
    library_id: str = Field(pattern=ID)
    text: str = Field(min_length=1, max_length=8000)
    mode: Literal['standard', 'medium', 'high'] = 'standard'
    top_n: int = Field(default=12, ge=1, le=64)


def ok(data, status=200):
    return JSONResponse(status_code=status, content={'code': 0, 'request_id': identifier()[:16], 'data': data},
                        headers={'Cache-Control': 'no-store'})


def error(exc):
    return JSONResponse(status_code=exc.status, content={'code': exc.code, 'message': exc.message,
                                                        'request_id': identifier()[:16]}, headers={'Cache-Control': 'no-store'})


def review_view(row):
    result = dict(row)
    result['payload'] = json.loads(result['payload'])
    if result['action'] == 'document.upload':
        result['payload']['files'] = [{k: v for k, v in item.items() if k != 'stored'}
                                      for item in result['payload']['files']]
    result['result'] = json.loads(result['result']) if result['result'] else None
    result.pop('payload_hash', None)
    return result


class Management:
    def __init__(self, store, backend):
        self.store, self.backend = store, backend
        # Interrupted dispatches are retained for verification, never replayed automatically.
        with store.connect(write=True) as conn:
            conn.execute("UPDATE review SET state='uncertain',error_code='process_interrupted' WHERE state='applying'")

    def library(self, actor, library_id, write=False):
        self.store.require_library(actor, library_id, write)
        row = next((row for row in self.backend.libraries() if row['id'] == library_id), None)
        if not row:
            raise ApiError(404, 'library_not_found', '知识库不存在。')
        return dict(row, **{key: value for key, value in self.store.policy(library_id).items() if key != 'library_id'})

    def validate_operation(self, actor, action, payload):
        if action == 'direction.create':
            if actor['role'] == 'user':
                raise ApiError(403, 'forbidden', '只有管理员和超管可以新建方向。')
            return
        if action == 'library.create':
            if actor['role'] == 'user' and payload['visibility'] != 'private':
                raise ApiError(403, 'forbidden', '用户只能创建自己的私有知识库。')
            self.backend.require_direction(payload['direction_id'])
            if any(row['name'] == payload['name'] for row in self.backend.libraries()):
                raise ApiError(409, 'library_name_conflict', '库名已经使用，请换一个名称。')
            return
        library = self.library(actor, payload['library_id'], write=True)
        if payload['expected_revision'] != library['revision']:
            raise ApiError(409, 'revision_conflict', '知识库已发生变化，请刷新后重新提交。')
        if action == 'library.update':
            if actor['role'] == 'user' and payload.get('visibility', 'private') != 'private':
                raise ApiError(403, 'forbidden', '用户只能管理自己的私有知识库。')
            if 'direction_id' in payload:
                self.backend.require_direction(payload['direction_id'])
            if 'name' in payload and any(row['name'] == payload['name'] and row['id'] != library['id']
                                        for row in self.backend.libraries()):
                raise ApiError(409, 'library_name_conflict', '库名已经使用，请换一个名称。')
        if action == 'document.retry':
            record = self.backend.record(payload['record_id'])
            if record['library_id'] != payload['library_id']:
                raise ApiError(403, 'forbidden', '入库记录与知识库不一致。')
            if record['status'] != 'failed':
                raise ApiError(409, 'not_retryable', '只有失败记录可以重试。')

    def submit(self, actor, action, payload, idempotency_key=None):
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        # File location is per-upload. Content hashes determine request identity.
        fingerprint_payload = dict(payload)
        if 'files' in payload:
            fingerprint_payload['files'] = [{k: v for k, v in item.items() if k != 'stored'} for item in payload['files']]
        fingerprint = hashlib.sha256(json.dumps(fingerprint_payload, sort_keys=True).encode()).hexdigest()
        if idempotency_key is not None and not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', idempotency_key):
            raise ApiError(400, 'invalid_request', 'Idempotency-Key应为1至128位字母、数字或_.:-。')
        with REVIEW_LOCK:
            if idempotency_key:
                previous = self.store.one('SELECT * FROM review WHERE actor_id=? AND idempotency_key=?',
                                          (actor['id'], idempotency_key))
                if previous:
                    if previous['payload_hash'] != fingerprint or previous['action'] != action:
                        raise ApiError(409, 'idempotency_conflict', '同一请求编号已用于不同内容。')
                    return previous
            self.validate_operation(actor, action, payload)
            now, review_id = int(time.time()), identifier()
            with self.store.connect(write=True) as conn:
                conn.execute('INSERT INTO review(id,actor_id,action,library_id,payload,state,created_at,updated_at,'
                             'idempotency_key,payload_hash) VALUES (?,?,?,?,?,?,?,?,?,?)',
                             (review_id, actor['id'], action, payload.get('library_id'), encoded, 'pending',
                              now, now, idempotency_key, fingerprint))
                self.store.audit(conn, actor['id'], 'review.submit', review_id, details={'action': action})
        if actor['role'] == 'superadmin':
            return self.decide(actor, review_id, Decision(decision='approve', comment='超管直接执行'))
        return self.store.one('SELECT * FROM review WHERE id=?', (review_id,))

    def decide(self, reviewer, review_id, decision):
        if reviewer['role'] != 'superadmin':
            raise ApiError(403, 'forbidden', '只有超管可以审核。')
        with REVIEW_LOCK:
            row = self.store.one('SELECT * FROM review WHERE id=?', (review_id,))
            if not row:
                raise ApiError(404, 'review_not_found', '申请不存在。')
            if row['state'] != 'pending':
                raise ApiError(409, 'review_already_decided', '申请已处理，请查看当前状态。')
            now = int(time.time())
            if decision.decision == 'reject':
                with self.store.connect(write=True) as conn:
                    conn.execute("UPDATE review SET state='rejected',reviewer_id=?,comment=?,updated_at=? WHERE id=?",
                                 (reviewer['id'], decision.comment, now, review_id))
                    self.store.audit(conn, reviewer['id'], 'review.reject', review_id)
                self.cleanup(row)
                return self.store.one('SELECT * FROM review WHERE id=?', (review_id,))
            actor = self.store.one('SELECT id,role,active FROM account WHERE id=?', (row['actor_id'],))
            if not actor or not actor['active']:
                raise ApiError(409, 'applicant_inactive', '申请账号已经停用。')
            payload = json.loads(row['payload'])
            self.validate_operation(actor, row['action'], payload)
            with self.store.connect(write=True) as conn:
                conn.execute("UPDATE review SET state='applying',reviewer_id=?,comment=?,updated_at=? WHERE id=? AND state='pending'",
                             (reviewer['id'], decision.comment, now, review_id))
                self.store.audit(conn, reviewer['id'], 'review.approve', review_id)
            try:
                result = self.backend.execute(row['action'], payload, actor['id'], self.store.root)
                self.finish_policy(row, payload, result)
                with self.store.connect(write=True) as conn:
                    conn.execute("UPDATE review SET state='applied',result=?,updated_at=? WHERE id=?",
                                 (json.dumps(result, ensure_ascii=False), int(time.time()), review_id))
                    self.store.audit(conn, actor['id'], row['action'], review_id)
                self.cleanup(row)
            except ApiError as exc:
                with self.store.connect(write=True) as conn:
                    conn.execute("UPDATE review SET state='failed',error_code=?,updated_at=? WHERE id=?",
                                 (exc.code, int(time.time()), review_id))
                    self.store.audit(conn, actor['id'], row['action'], review_id, 'failed', {'code': exc.code})
            except Exception:
                with self.store.connect(write=True) as conn:
                    conn.execute("UPDATE review SET state='uncertain',error_code='dispatch_uncertain',updated_at=? WHERE id=?",
                                 (int(time.time()), review_id))
                    self.store.audit(conn, actor['id'], row['action'], review_id, 'uncertain')
            return self.store.one('SELECT * FROM review WHERE id=?', (review_id,))

    def finish_policy(self, review, payload, result):
        if review['action'] == 'library.create':
            if result.get('existed'):
                raise UncertainOperation()
            with self.store.connect(write=True) as conn:
                conn.execute('INSERT INTO library_policy(library_id,owner_id,visibility) VALUES (?,?,?)',
                             (result['id'], review['actor_id'], payload['visibility']))
        elif review['action'] == 'library.update':
            library_id = payload['library_id']
            old = self.store.policy(library_id)
            with self.store.connect(write=True) as conn:
                conn.execute('INSERT INTO library_policy VALUES (?,?,?,?) ON CONFLICT(library_id) DO UPDATE SET '
                             'visibility=excluded.visibility,revision=excluded.revision',
                             (library_id, old['owner_id'], payload.get('visibility', old['visibility']), old['revision'] + 1))

    def cleanup(self, row):
        for item in json.loads(row['payload']).get('files', []):
            path = self.store.root / item['stored']
            if path.parent.parent == self.store.uploads:
                shutil.rmtree(path.parent, ignore_errors=True)


def install_management(app, catalog=None, root=None, backend=None):
    if catalog is None:
        import library_catalog as catalog
    store = Store(root or os.environ.get('MANAGEMENT_DATA', Path(__file__).with_name('private')))
    manager = Management(store, backend or Backend(catalog))
    # Existing registered libraries keep shared visibility. This one-time import
    # records exactly the legacy set, and never publishes a newly untracked library.
    with store.connect(write=True) as conn:
        imported = conn.execute("SELECT 1 FROM audit WHERE event='migration.legacy_libraries'").fetchone()
        if not imported:
            for row in manager.backend.libraries():
                conn.execute("INSERT OR IGNORE INTO library_policy VALUES (?,NULL,'public',0)", (row['id'],))
            store.audit(conn, None, 'migration.legacy_libraries', '')
    app.state.management = manager
    router = APIRouter(prefix=PREFIX)

    @app.exception_handler(ApiError)
    async def api_error(request, exc):
        return error(exc)

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        logging.getLogger('management').error('request failed type=%s path=%s', type(exc).__name__, request.url.path)
        return error(ApiError(500, 'internal_error', '请求暂时无法完成，请保存request_id排查。'))

    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def invalid_parameters(request, exc):
        return error(ApiError(400, 'invalid_request', '参数不正确，请检查字段、类型和长度。'))

    def current_user(authorization: str | None = Header(default=None)):
        token = authorization[7:] if authorization and authorization.startswith('Bearer ') else None
        return store.authenticate(token)

    def staff(actor=Depends(current_user)):
        if actor['role'] not in ('admin', 'superadmin'):
            raise ApiError(403, 'forbidden', '只有管理员和超管可以操作。')
        return actor

    @app.middleware('http')
    async def protect_legacy(request: Request, call_next):
        path = request.url.path
        if path.startswith('/retrieve/') and not path.startswith(PREFIX + '/') and path != '/retrieve/health':
            try:
                actor = current_user(request.headers.get('authorization'))
                if path == '/retrieve/search' and request.method == 'POST':
                    payload = await request.json()
                    if not isinstance(payload, dict) or not isinstance(payload.get('library_id'), str):
                        raise ApiError(400, 'invalid_request', '请填写library_id。')
                    manager.library(actor, payload['library_id'])
                elif request.method != 'GET':
                    raise ApiError(409, 'approval_required', '请通过管理接口提交审核申请。')
                elif path == '/retrieve/libraries':
                    return libraries(request.query_params.get('direction_id'), actor)
            except ApiError as exc:
                return error(exc)
            except (ValueError, TypeError):
                return error(ApiError(400, 'invalid_request', '请求JSON格式不正确。'))
        return await call_next(request)

    def response_review(row):
        return ok(review_view(row), 202 if row['state'] in ('pending', 'applying') else 200)

    @router.get('/health')
    def health():
        return ok({'status': 'ok', 'version': 'management-v1'})

    @router.post('/auth/login')
    def login(body: Login):
        return ok(store.login(body.identifier.strip(), body.password))

    @router.get('/auth/me')
    def me(actor=Depends(current_user)):
        return ok(actor)

    @router.post('/auth/logout')
    def logout(authorization: str = Header(), actor=Depends(current_user)):
        with store.connect(write=True) as conn:
            conn.execute('DELETE FROM session WHERE token_hash=?', (hashlib.sha256(authorization[7:].encode()).hexdigest(),))
            store.audit(conn, actor['id'], 'auth.logout', actor['id'])
        return ok({'logged_out': True})

    @router.get('/directions')
    def directions(actor=Depends(current_user)):
        return ok({'directions': manager.backend.directions()})

    @router.post('/directions')
    def create_direction(body: Direction, actor=Depends(staff), idempotency_key: str | None = Header(default=None)):
        return response_review(manager.submit(actor, 'direction.create', body.model_dump(), idempotency_key))

    @router.get('/libraries')
    def libraries(direction_id: str | None = None, actor=Depends(current_user)):
        if direction_id is not None:
            manager.backend.require_direction(direction_id)
        rows = [dict(row, **{k: v for k, v in store.policy(row['id']).items() if k != 'library_id'})
                for row in manager.backend.libraries() if store.can_read(actor, row['id'])
                and (direction_id is None or row.get('direction_id') == direction_id)]
        return ok({'libraries': rows})

    @router.post('/libraries')
    def create_library(body: LibraryCreate, actor=Depends(current_user), idempotency_key: str | None = Header(default=None)):
        return response_review(manager.submit(actor, 'library.create', body.model_dump(), idempotency_key))

    @router.patch('/libraries/{library_id}')
    def update_library(library_id: str, body: LibraryUpdate, actor=Depends(current_user),
                       idempotency_key: str | None = Header(default=None)):
        fields = body.model_dump(exclude_unset=True)
        if any(value is None for value in fields.values()) or (body.name is not None and not body.name.strip()):
            raise ApiError(400, 'invalid_request', '字段不能为空或null。')
        if len(fields) <= 1:
            raise ApiError(400, 'invalid_request', '请至少指定一个修改字段。')
        if 'name' in fields:
            fields['name'] = fields['name'].strip()
        return response_review(manager.submit(actor, 'library.update', dict(fields, library_id=library_id), idempotency_key))

    @router.get('/libraries/{library_id}/documents')
    def documents(library_id: str, page: int = 1, page_size: int = 100, actor=Depends(current_user)):
        if page < 1 or not 1 <= page_size <= 100:
            raise ApiError(400, 'invalid_request', 'page至少为1，page_size应为1至100。')
        manager.library(actor, library_id)
        return ok(manager.backend.documents(library_id, page, page_size))

    @router.post('/search')
    def search(body: Search, actor=Depends(current_user)):
        manager.library(actor, body.library_id)
        import retrieval_service
        return retrieval_service.search(retrieval_service.SearchInput(**body.model_dump()), actor['id'])

    @router.post('/libraries/{library_id}/documents')
    def upload(library_id: str, file: list[UploadFile] = File(...), category: str = Form(default='general'),
               expected_revision: int = Form(..., ge=0), actor=Depends(current_user),
               idempotency_key: str | None = Header(default=None)):
        manager.library(actor, library_id, write=True)
        if category not in ('paper', 'patent', 'standard', 'policy', 'manual', 'general') or not 1 <= len(file) <= 10:
            raise ApiError(400, 'invalid_request', '请检查类别，单次上传1至10份文件。')
        directory = store.uploads / identifier()
        directory.mkdir(mode=0o700)
        staged = []
        try:
            total = 0
            for index, item in enumerate(file):
                name = (item.filename or '').replace('\\', '/').rsplit('/', 1)[-1]
                if not name or len(name) > 255 or any(ord(c) < 32 for c in name):
                    raise ApiError(400, 'invalid_request', '文件名不正确。')
                path = directory / str(index)
                digest, size = hashlib.sha256(), 0
                with path.open('wb') as output:
                    while chunk := item.file.read(1024 * 1024):
                        size += len(chunk)
                        total += len(chunk)
                        if size > 120 * 1048576 or total > 256 * 1048576:
                            raise ApiError(413, 'file_too_large', '单份最多120 MiB，每批最多256 MiB。')
                        digest.update(chunk)
                        output.write(chunk)
                if not size:
                    raise ApiError(400, 'empty_file', '文件不能为空。')
                staged.append({'filename': name, 'size': size, 'sha256': digest.hexdigest(),
                               'stored': str(path.relative_to(store.root))})
            payload = {'library_id': library_id, 'expected_revision': expected_revision, 'category': category, 'files': staged}
            row = manager.submit(actor, 'document.upload', payload, idempotency_key)
            saved = {item['stored'] for item in json.loads(row['payload']).get('files', [])}
            if not saved.intersection(item['stored'] for item in staged):
                shutil.rmtree(directory, ignore_errors=True)
            return response_review(row)
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    @router.get('/records/{record_id}')
    def record(record_id: str, actor=Depends(current_user)):
        row = manager.backend.record(record_id)
        manager.library(actor, row['library_id'])
        return ok(row)

    @router.post('/records/{record_id}/retry')
    def retry(record_id: str, actor=Depends(current_user), idempotency_key: str | None = Header(default=None)):
        row = manager.backend.record(record_id)
        library = manager.library(actor, row['library_id'], write=True)
        return response_review(manager.submit(actor, 'document.retry', {'library_id': row['library_id'],
            'record_id': record_id, 'expected_revision': library['revision']}, idempotency_key))

    @router.get('/reviews')
    def reviews(state: str | None = None, limit: int = 50, offset: int = 0, actor=Depends(current_user)):
        if not 1 <= limit <= 200 or offset < 0:
            raise ApiError(400, 'invalid_request', '分页参数不正确。')
        clauses, args = [], []
        if actor['role'] != 'superadmin':
            clauses.append('actor_id=?')
            args.append(actor['id'])
        if state:
            if state not in ('pending', 'applying', 'applied', 'rejected', 'failed', 'uncertain'):
                raise ApiError(400, 'invalid_request', '申请状态不正确。')
            clauses.append('state=?')
            args.append(state)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        rows = store.rows('SELECT * FROM review' + where + ' ORDER BY created_at DESC,id LIMIT ? OFFSET ?', (*args, limit, offset))
        return ok({'reviews': [review_view(row) for row in rows]})

    @router.get('/reviews/{review_id}')
    def review(review_id: str, actor=Depends(current_user)):
        row = store.one('SELECT * FROM review WHERE id=?', (review_id,))
        if not row:
            raise ApiError(404, 'review_not_found', '申请不存在。')
        if actor['role'] != 'superadmin' and row['actor_id'] != actor['id']:
            raise ApiError(403, 'forbidden', '只能查看自己的申请。')
        return ok(review_view(row))

    @router.post('/reviews/{review_id}/decision')
    def decide(review_id: str, body: Decision, actor=Depends(current_user)):
        return response_review(manager.decide(actor, review_id, body))

    def key_view(row):
        # Do not expose any characters from the actual credential.
        return {**{k: v for k, v in row.items() if k != 'secret'}, 'masked_key': '********'}

    @router.get('/model-keys')
    def model_keys(actor=Depends(staff)):
        return ok({'keys': [key_view(row) for row in store.rows('SELECT * FROM model_key ORDER BY name')]})

    @router.post('/model-keys')
    def create_key(body: ModelKey, actor=Depends(staff)):
        import sqlite3
        key_id = identifier()
        try:
            with store.connect(write=True) as conn:
                conn.execute('INSERT INTO model_key VALUES (?,?,?,?,?,?,?,1,?)',
                             (key_id, body.name, body.provider, body.base_url, body.model,
                              store.cipher.encrypt(body.api_key.encode()), int(body.enabled), int(time.time())))
                store.audit(conn, actor['id'], 'model_key.create', key_id)
        except sqlite3.IntegrityError:
            raise ApiError(409, 'key_name_conflict', '密钥名称已经使用。') from None
        return ok(key_view(store.one('SELECT * FROM model_key WHERE id=?', (key_id,))), 201)

    @router.patch('/model-keys/{key_id}')
    def update_key(key_id: str, body: ModelKeyUpdate, actor=Depends(staff)):
        if body.api_key is None and body.enabled is None:
            raise ApiError(400, 'invalid_request', '请指定新密钥或启用状态。')
        if body.api_key is not None and not body.api_key.strip():
            raise ApiError(400, 'invalid_request', '密钥不能为空白。')
        with store.connect(write=True) as conn:
            row = conn.execute('SELECT * FROM model_key WHERE id=?', (key_id,)).fetchone()
            if not row:
                raise ApiError(404, 'model_key_not_found', '密钥不存在。')
            if row['revision'] != body.expected_revision:
                raise ApiError(409, 'revision_conflict', '密钥配置已更新，请刷新后重试。')
            encrypted = store.cipher.encrypt(body.api_key.strip().encode()) if body.api_key is not None else row['secret']
            enabled = int(body.enabled) if body.enabled is not None else row['enabled']
            conn.execute('UPDATE model_key SET secret=?,enabled=?,revision=revision+1,updated_at=? WHERE id=?',
                         (encrypted, enabled, int(time.time()), key_id))
            store.audit(conn, actor['id'], 'model_key.update', key_id, details={'enabled': bool(enabled)})
        return ok(key_view(store.one('SELECT * FROM model_key WHERE id=?', (key_id,))))

    @router.get('/audit')
    def audit(actor_id: str | None = None, event: str | None = None, start: int | None = None,
              end: int | None = None, limit: int = 50, offset: int = 0, actor=Depends(staff)):
        if not 1 <= limit <= 200 or offset < 0 or (start is not None and end is not None and start > end):
            raise ApiError(400, 'invalid_request', '分页或时间参数不正确。')
        clauses, args = [], []
        # Admins can inspect key audit and their own actions. Superadmins inspect all events.
        if actor['role'] == 'admin':
            clauses.append("(event LIKE 'model_key.%' OR actor_id=?)")
            args.append(actor['id'])
        for value, clause in ((actor_id, 'actor_id=?'), (event, 'event=?'), (start, 'at>=?'), (end, 'at<=?')):
            if value is not None:
                clauses.append(clause)
                args.append(value)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        rows = store.rows('SELECT * FROM audit' + where + ' ORDER BY id DESC LIMIT ? OFFSET ?', (*args, limit, offset))
        for row in rows:
            row['details'] = json.loads(row['details'])
        return ok({'events': rows})

    app.include_router(router)
    return manager
