"""Accounts, sessions, review requests and encrypted model credentials."""
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
import uuid

from cryptography.fernet import Fernet


class ApiError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def identifier():
    return uuid.uuid4().hex


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return f'scrypt${salt}${digest}'


def password_matches(password, encoded):
    try:
        _, salt, _ = encoded.split('$')
        return hmac.compare_digest(password_hash(password, salt), encoded)
    except (ValueError, TypeError):
        return False


class Store:
    def __init__(self, directory):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / 'management.sqlite3'
        self.uploads = self.root / 'uploads'
        self.uploads.mkdir(exist_ok=True, mode=0o700)
        key_file = self.root / 'encryption.key'
        try:
            descriptor = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(Fernet.generate_key())
        self.cipher = Fernet(key_file.read_bytes())
        with self.connect() as conn:
            conn.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS account (
                    id TEXT PRIMARY KEY, employee_id TEXT NOT NULL UNIQUE,
                    phone TEXT UNIQUE, display_name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('superadmin','admin','user')),
                    password_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS session (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES account(id),
                    expires_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS login_attempt (
                    identifier TEXT PRIMARY KEY, failures INTEGER NOT NULL, last_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS library_policy (
                    library_id TEXT PRIMARY KEY, owner_id TEXT REFERENCES account(id),
                    visibility TEXT NOT NULL CHECK(visibility IN ('public','private')),
                    revision INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS review (
                    id TEXT PRIMARY KEY, actor_id TEXT NOT NULL REFERENCES account(id),
                    action TEXT NOT NULL, library_id TEXT, payload TEXT NOT NULL,
                    state TEXT NOT NULL, reviewer_id TEXT REFERENCES account(id),
                    comment TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL, result TEXT, error_code TEXT,
                    idempotency_key TEXT, payload_hash TEXT NOT NULL,
                    UNIQUE(actor_id,idempotency_key));
                CREATE TABLE IF NOT EXISTS model_key (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, provider TEXT NOT NULL,
                    base_url TEXT NOT NULL, model TEXT NOT NULL, secret BLOB NOT NULL,
                    enabled INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                    updated_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id TEXT,
                    event TEXT NOT NULL, target TEXT NOT NULL,
                    outcome TEXT NOT NULL, details TEXT NOT NULL, at INTEGER NOT NULL);
            ''')
        if os.name == 'posix':
            os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self, write=False):
        conn = sqlite3.connect(self.path, timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        try:
            if write:
                conn.execute('BEGIN IMMEDIATE')
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def one(self, sql, args=()):
        with self.connect() as conn:
            row = conn.execute(sql, args).fetchone()
            return dict(row) if row else None

    def rows(self, sql, args=()):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, args)]

    def audit(self, conn, actor, event, target, outcome='ok', details=None):
        conn.execute('INSERT INTO audit(actor_id,event,target,outcome,details,at) VALUES (?,?,?,?,?,?)',
                     (actor, event, target, outcome, json.dumps(details or {}, ensure_ascii=False), int(time.time())))

    def add_account(self, employee_id, phone, display_name, role, password):
        """Database provisioning only; deliberately has no public HTTP route."""
        if role not in ('superadmin', 'admin', 'user') or not 10 <= len(password) <= 256:
            raise ValueError('角色不正确，或密码长度不在10至256字符内。')
        if not employee_id or (phone and (not phone.isdigit() or len(phone) != 11)):
            raise ValueError('工号或手机号格式不正确。')
        user_id = identifier()
        with self.connect(write=True) as conn:
            if conn.execute('SELECT 1 FROM account WHERE employee_id IN (?,?) OR phone IN (?,?)',
                            (employee_id, phone, employee_id, phone)).fetchone():
                raise ValueError('工号或手机号已使用。')
            conn.execute('INSERT INTO account VALUES (?,?,?,?,?,?,1,?)',
                         (user_id, employee_id, phone, display_name, role, password_hash(password), int(time.time())))
            self.audit(conn, None, 'account.provision', user_id)
        return user_id

    def authenticate(self, token):
        if not token or len(token) > 256:
            raise ApiError(401, 'unauthorized', '请先登录。')
        digest = hashlib.sha256(token.encode()).hexdigest()
        row = self.one('SELECT a.id,a.employee_id,a.phone,a.display_name,a.role FROM session s '
                       'JOIN account a ON a.id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND a.active=1',
                       (digest, int(time.time())))
        if not row:
            raise ApiError(401, 'unauthorized', '登录已失效，请重新登录。')
        return row

    def login(self, login_id, password):
        now = int(time.time())
        limited = self.one('SELECT * FROM login_attempt WHERE identifier=?', (login_id,))
        if limited and limited['failures'] >= 10 and now - limited['last_at'] < 300:
            raise ApiError(429, 'login_limited', '尝试次数过多，请5分钟后再试。')
        account = self.one('SELECT * FROM account WHERE employee_id=? OR phone=?', (login_id, login_id))
        encoded = account['password_hash'] if account else password_hash('unavailable-account')
        matches = password_matches(password, encoded)
        if not account or not account['active'] or not matches:
            with self.connect(write=True) as conn:
                previous = conn.execute('SELECT * FROM login_attempt WHERE identifier=?', (login_id,)).fetchone()
                failures = previous['failures'] + 1 if previous and now - previous['last_at'] < 300 else 1
                conn.execute('INSERT INTO login_attempt VALUES (?,?,?) ON CONFLICT(identifier) DO UPDATE '
                             'SET failures=excluded.failures,last_at=excluded.last_at', (login_id, failures, now))
                self.audit(conn, account['id'] if account else None, 'auth.login', '', 'denied')
            raise ApiError(401, 'invalid_credentials', '工号、手机号或密码不正确。')
        token = secrets.token_urlsafe(48)
        expires = now + 8 * 3600
        with self.connect(write=True) as conn:
            conn.execute('DELETE FROM session WHERE expires_at<=?', (now,))
            conn.execute('DELETE FROM login_attempt WHERE identifier=?', (login_id,))
            conn.execute('INSERT INTO session VALUES (?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), account['id'], expires))
            self.audit(conn, account['id'], 'auth.login', account['id'])
        return {'access_token': token, 'token_type': 'Bearer', 'expires_at': expires,
                'user': {key: account[key] for key in ('id', 'employee_id', 'phone', 'display_name', 'role')}}

    def policy(self, library_id):
        return self.one('SELECT * FROM library_policy WHERE library_id=?', (library_id,)) or {
            'library_id': library_id, 'owner_id': None, 'visibility': 'private', 'revision': 0}

    def can_read(self, actor, library_id):
        policy = self.policy(library_id)
        return actor['role'] != 'user' or policy['visibility'] == 'public' or policy['owner_id'] == actor['id']

    def can_write(self, actor, library_id):
        policy = self.policy(library_id)
        return actor['role'] != 'user' or (policy['visibility'] == 'private' and policy['owner_id'] == actor['id'])

    def require_library(self, actor, library_id, write=False):
        allowed = self.can_write(actor, library_id) if write else self.can_read(actor, library_id)
        if not allowed:
            raise ApiError(403, 'forbidden', '无权操作这个知识库。')

    def get_model_credential(self, key_id):
        """Server-side use only. No HTTP endpoint exposes this return value."""
        row = self.one('SELECT * FROM model_key WHERE id=? AND enabled=1', (key_id,))
        if not row:
            raise ApiError(404, 'model_key_not_found', '密钥不存在或已停用。')
        return {'provider': row['provider'], 'base_url': row['base_url'], 'model': row['model'],
                'api_key': self.cipher.decrypt(row['secret']).decode()}
