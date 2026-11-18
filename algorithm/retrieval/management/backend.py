"""Reuse the running catalog, ingestion API and RAGFlow document readers."""
import httpx

from store import ApiError


class UncertainOperation(Exception):
    pass


class Backend:
    def __init__(self, catalog):
        self.catalog = catalog

    def libraries(self):
        return self.catalog.libraries()

    def directions(self):
        with self.catalog.connection() as conn:
            return [dict(row) for row in conn.execute('SELECT id,name FROM direction ORDER BY rowid')]

    def require_direction(self, direction_id):
        try:
            return self.catalog.require_direction(direction_id)
        except self.catalog.CatalogError as exc:
            raise ApiError(exc.status, exc.code, exc.message) from None

    def record(self, record_id):
        from app import db
        from app.main import record_view
        row = db.one('SELECT * FROM ingest_record WHERE id=%s', [record_id])
        if not row:
            raise ApiError(404, 'record_not_found', '入库记录不存在。')
        return record_view(row)

    def documents(self, library_id, page=1, page_size=100):
        from app import ragflow
        try:
            return ragflow._call('GET', f'/datasets/{library_id}/documents', params={'page': page, 'page_size': page_size})
        except ragflow.RagflowError as exc:
            raise ApiError(503 if exc.transient else 502, 'upstream_error', '暂时无法读取文档列表。') from None

    def forward(self, method, path, actor_id, body=None, files=None, form=None):
        try:
            with httpx.Client(base_url='http://127.0.0.1:30269', timeout=180, trust_env=False) as client:
                response = client.request(method, path, json=body, files=files, data=form,
                                          headers={'X-Caller-Id': actor_id})
        except httpx.TransportError as exc:
            raise UncertainOperation() from exc
        try:
            payload = response.json()
        except ValueError:
            raise UncertainOperation() from None
        if response.status_code >= 500:
            raise UncertainOperation()
        if response.status_code >= 300 or payload.get('code') != 0:
            raise ApiError(response.status_code, payload.get('code', 'upstream_error'),
                           payload.get('message', '入库服务未完成请求。'))
        return payload['data']

    def execute(self, action, payload, actor_id, root):
        import uuid
        if action == 'direction.create':
            with self.catalog.connection() as conn:
                conn.execute('INSERT OR IGNORE INTO direction(id,name) VALUES (?,?)',
                             (uuid.uuid4().hex, payload['name']))
                return dict(conn.execute('SELECT id,name FROM direction WHERE name=?', (payload['name'],)).fetchone())
        if action == 'library.create':
            result = self.forward('POST', '/ingest/libraries', actor_id,
                                  {'name': payload['name'], 'description': payload['description']})
            self.catalog.assign(result['id'], payload['direction_id'])
            return result
        library_id = payload['library_id']
        if action == 'library.update':
            from app import db, ragflow
            values = {key: payload[key] for key in ('name', 'description') if key in payload}
            if values:
                try:
                    ragflow._call('PUT', '/datasets/' + library_id, json=values)
                except Exception as exc:
                    raise UncertainOperation() from exc
                columns = ','.join(key + '=%s' for key in values)
                try:
                    db.execute('UPDATE library SET ' + columns + ' WHERE id=%s', [*values.values(), library_id])
                except Exception as exc:
                    raise UncertainOperation() from exc
            if 'direction_id' in payload:
                self.catalog.assign(library_id, payload['direction_id'])
            return {'id': library_id, **values}
        if action == 'document.upload':
            from contextlib import ExitStack
            with ExitStack() as stack:
                files = [('file', (item['filename'], stack.enter_context((root / item['stored']).open('rb')),
                                   'application/octet-stream')) for item in payload['files']]
                return self.forward('POST', '/ingest/documents', actor_id, files=files,
                                    form={'library_id': library_id, 'category': payload['category']})
        if action == 'document.retry':
            return self.forward('POST', '/ingest/documents/' + payload['record_id'] + '/retry', actor_id)
        raise ApiError(400, 'invalid_action', '不支持这个操作。')
