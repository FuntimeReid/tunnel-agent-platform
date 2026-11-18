"""Direction labels for registered libraries, hosted by the existing tool service."""
from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from urllib.parse import urlsplit
import uuid

from fastapi import Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_DIRECTION = '00000000000000000000000000000000'
STORE_PATH = Path(os.environ.get('LIBRARY_CATALOG_DB', Path(__file__).with_name('library_catalog.sqlite3')))
INGEST_BASE = os.environ.get('CATALOG_INGEST_BASE', 'http://127.0.0.1:30268').rstrip('/')
CREATE_LOCK = threading.Lock()


class CatalogError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def result(data, status=200):
    return JSONResponse(status_code=status, content={'code': 0, 'request_id': uuid.uuid4().hex[:16], 'data': data})


@contextmanager
def connection():
    with sqlite3.connect(STORE_PATH, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        yield conn


def initialize():
    with connection() as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS direction (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE)')
        conn.execute('CREATE TABLE IF NOT EXISTS library_direction (library_id TEXT PRIMARY KEY, direction_id TEXT NOT NULL REFERENCES direction(id))')
        conn.execute('INSERT OR IGNORE INTO direction (id,name) VALUES (?,?)', (DEFAULT_DIRECTION, '未分类'))


def require_direction(direction_id):
    with connection() as conn:
        row = conn.execute('SELECT id,name FROM direction WHERE id=?', (direction_id,)).fetchone()
    if row is None:
        raise CatalogError(404, 'direction_not_found', '方向不存在。')
    return dict(row)


def ingest_call(method, path, body=None, caller=None):
    target = urlsplit(INGEST_BASE)
    conn = http.client.HTTPConnection(target.hostname, target.port, timeout=60)
    headers = {'Content-Type': 'application/json', 'X-Caller-Id': caller or 'anonymous'}
    try:
        conn.request(method, target.path + path, json.dumps(body, ensure_ascii=False).encode() if body is not None else None, headers)
        response = conn.getresponse()
        payload = json.loads(response.read())
        if response.status >= 300 or payload.get('code') != 0:
            raise CatalogError(response.status, payload.get('code', 'upstream_error'), payload.get('message', '入库服务暂时不可用。'))
        return payload['data']
    except (OSError, http.client.HTTPException):
        raise CatalogError(503, 'upstream_unavailable', '暂时无法连接入库服务。')
    finally:
        conn.close()


def libraries():
    rows = ingest_call('GET', '/ingest/libraries')['libraries']
    with connection() as conn:
        mapping = {r['library_id']: r['direction_id'] for r in conn.execute('SELECT * FROM library_direction')}
        names = {r['id']: r['name'] for r in conn.execute('SELECT * FROM direction')}
    return [dict(row, direction_id=mapping.get(row['id'], DEFAULT_DIRECTION),
                 direction_name=names[mapping.get(row['id'], DEFAULT_DIRECTION)]) for row in rows]


def assign(library_id, direction_id):
    with connection() as conn:
        conn.execute('INSERT INTO library_direction(library_id,direction_id) VALUES (?,?) '
                     'ON CONFLICT(library_id) DO UPDATE SET direction_id=excluded.direction_id', (library_id, direction_id))


class DirectionInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    name: str = Field(min_length=1, max_length=128)


class LibraryInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    direction_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default='', max_length=1024)


class MoveInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    direction_id: str = Field(pattern=r'^[a-f0-9]{32}$')


def install_catalog(app):
    initialize()

    @app.exception_handler(CatalogError)
    async def catalog_error(request, exc):
        return JSONResponse(status_code=exc.status, content={'code': exc.code, 'message': exc.message,
                                                            'request_id': uuid.uuid4().hex[:16]})

    @app.get('/retrieve/directions')
    def list_directions():
        with connection() as conn:
            rows = list(conn.execute('SELECT id,name FROM direction ORDER BY rowid'))
        return result({'directions': [dict(r, is_default=r['id'] == DEFAULT_DIRECTION) for r in rows]})

    @app.post('/retrieve/directions')
    def create_direction(body: DirectionInput):
        name = body.name.strip()
        if not name:
            raise CatalogError(400, 'invalid_request', '方向名称不能为空。')
        with connection() as conn:
            inserted = conn.execute('INSERT OR IGNORE INTO direction(id,name) VALUES (?,?)', (uuid.uuid4().hex, name)).rowcount
            row = conn.execute('SELECT id,name FROM direction WHERE name=?', (name,)).fetchone()
        return result(dict(row, existed=not bool(inserted)), 201 if inserted else 200)

    @app.get('/retrieve/libraries')
    def list_libraries(direction_id: str | None = None):
        if direction_id is not None:
            require_direction(direction_id)
        rows = libraries()
        return result({'libraries': [r for r in rows if direction_id is None or r['direction_id'] == direction_id]})

    @app.post('/retrieve/libraries')
    def create_library(body: LibraryInput, x_caller_id: str | None = Header(default=None)):
        direction = require_direction(body.direction_id)
        name = body.name.strip()
        if not name:
            raise CatalogError(400, 'invalid_request', '库名称不能为空。')
        with CREATE_LOCK:
            existing = next((r for r in libraries() if r['name'] == name), None)
            if existing and existing['direction_id'] != direction['id']:
                raise CatalogError(409, 'library_name_conflict', '该库名已在其他方向使用，请换一个名称，或调整已有库的方向。')
            data = ingest_call('POST', '/ingest/libraries', {'name': name, 'description': body.description}, x_caller_id)
            assign(data['id'], direction['id'])
        return result(dict(data, direction_id=direction['id'], direction_name=direction['name']), 200 if data['existed'] else 201)

    @app.patch('/retrieve/libraries/{library_id}/direction')
    def move_library(library_id: str, body: MoveInput):
        if not re.fullmatch(r'[a-f0-9]{32}', library_id):
            raise CatalogError(400, 'invalid_request', '库ID格式不正确。')
        direction = require_direction(body.direction_id)
        if not any(r['id'] == library_id for r in libraries()):
            raise CatalogError(404, 'library_not_found', '知识库不存在或未登记。')
        assign(library_id, direction['id'])
        return result({'id': library_id, 'direction_id': direction['id'], 'direction_name': direction['name']})
