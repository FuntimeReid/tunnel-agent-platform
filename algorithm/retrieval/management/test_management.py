"""HTTP tests use temporary databases and a deterministic fake upstream."""
import concurrent.futures
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import UncertainOperation
from management_api import PREFIX, install_management
from store import ApiError, Store

DEFAULT = '0' * 32
LEGACY = 'a' * 32


class FakeBackend:
    def __init__(self):
        self.libs = [{'id': LEGACY, 'name': '原公共库', 'direction_id': DEFAULT}]
        self.dirs = [{'id': DEFAULT, 'name': '未分类'}]
        self.calls = []
        self.records = {}
        self.failure = None

    def libraries(self):
        return self.libs.copy()

    def directions(self):
        return self.dirs.copy()

    def require_direction(self, value):
        if not any(row['id'] == value for row in self.dirs):
            raise ApiError(404, 'direction_not_found', '方向不存在。')

    def documents(self, library_id, page=1, page_size=100):
        return {'docs': [{'id': 'doc', 'name': '原文.md'}], 'total': 1}

    def record(self, record_id):
        if record_id not in self.records:
            raise ApiError(404, 'record_not_found', '记录不存在。')
        return self.records[record_id]

    def execute(self, action, payload, actor_id, root):
        self.calls.append(action)
        if self.failure:
            raise self.failure
        if action == 'direction.create':
            row = {'id': 'b' * 32, 'name': payload['name']}
            self.dirs.append(row)
            return row
        if action == 'library.create':
            row = {'id': f'{len(self.libs) + 1:032x}', 'name': payload['name'],
                   'direction_id': payload['direction_id']}
            self.libs.append(row)
            return {'id': row['id'], 'name': row['name'], 'existed': False}
        if action == 'library.update':
            row = next(row for row in self.libs if row['id'] == payload['library_id'])
            row.update({key: value for key, value in payload.items() if key in ('name', 'description', 'direction_id')})
            return {'id': row['id']}
        if action == 'document.upload':
            self.last_bytes = (root / payload['files'][0]['stored']).read_bytes()
            row = {'record_id': 'c' * 32, 'library_id': payload['library_id'], 'status': 'queued'}
            self.records[row['record_id']] = row
            return {'items': [row]}
        return {'record_id': payload['record_id'], 'status': 'queued'}


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.backend = FakeBackend()
        self.app = FastAPI()
        self.manager = install_management(self.app, catalog=object(), root=self.temp.name, backend=self.backend)
        self.store = self.manager.store
        self.client = TestClient(self.app)
        self.users, self.headers = {}, {}
        for index, role in enumerate(('superadmin', 'admin', 'user', 'user2')):
            self.users[role] = self.store.add_account('E' + str(index), '1390000000' + str(index), role,
                                                    'user' if role == 'user2' else role, 'test-password-123')
            response = self.call('POST', '/auth/login', body={'identifier': 'E' + str(index), 'password': 'test-password-123'})
            self.headers[role] = {'Authorization': 'Bearer ' + response.json()['data']['access_token']}

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def call(self, method, path, role=None, body=None, **kw):
        headers = dict(self.headers.get(role, {}))
        headers.update(kw.pop('headers', {}))
        return self.client.request(method, PREFIX + path, json=body, headers=headers, **kw)

    def create_private(self, owner='user', name='私有库'):
        response = self.call('POST', '/libraries', owner, {'name': name, 'direction_id': DEFAULT})
        self.assertEqual(response.status_code, 202 if owner != 'superadmin' else 200)
        review = response.json()['data']
        if review['state'] == 'pending':
            response = self.call('POST', '/reviews/' + review['id'] + '/decision', 'superadmin', {'decision': 'approve'})
            self.assertEqual(response.status_code, 200)
        row = response.json()['data']
        self.assertEqual(row['state'], 'applied')
        return row['result']['id']

    def test_login_employee_phone_logout_and_invalid_token(self):
        login = self.call('POST', '/auth/login', body={'identifier': '13900000002', 'password': 'test-password-123'})
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.json()['data']['user']['id'], self.users['user'])
        self.assertEqual(self.call('GET', '/auth/me').status_code, 401)
        self.assertEqual(self.call('GET', '/auth/me', headers={'Authorization': 'Bearer invalid'}).status_code, 401)
        self.assertEqual(self.call('POST', '/auth/login', body={'identifier': 'E2', 'password': 'wrong'}).status_code, 401)
        self.assertEqual(self.call('POST', '/auth/logout', 'user').status_code, 200)
        self.assertEqual(self.call('GET', '/auth/me', 'user').status_code, 401)

    def test_account_disabled_expired_and_lockout(self):
        with self.store.connect(write=True) as conn:
            conn.execute('UPDATE account SET active=0 WHERE id=?', (self.users['user'],))
            conn.execute('UPDATE session SET expires_at=0 WHERE user_id=?', (self.users['admin'],))
        self.assertEqual(self.call('GET', '/auth/me', 'user').status_code, 401)
        self.assertEqual(self.call('GET', '/auth/me', 'admin').status_code, 401)
        for _ in range(10):
            self.assertEqual(self.call('POST', '/auth/login', body={'identifier': 'E3', 'password': 'wrong'}).status_code, 401)
        self.assertEqual(self.call('POST', '/auth/login', body={'identifier': 'E3', 'password': 'wrong'}).status_code, 429)

    def test_create_requires_approval_and_owned_private_isolation(self):
        response = self.call('POST', '/libraries', 'user', {'name': '待建库', 'direction_id': DEFAULT})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(len(self.backend.libs), 1)
        review = response.json()['data']
        self.assertEqual(self.call('POST', '/reviews/' + review['id'] + '/decision', 'admin', {'decision': 'approve'}).status_code, 403)
        self.assertEqual(self.call('GET', '/reviews/' + review['id'], 'user2').status_code, 403)
        applied = self.call('POST', '/reviews/' + review['id'] + '/decision', 'superadmin', {'decision': 'approve'}).json()['data']
        library_id = applied['result']['id']
        self.assertEqual(self.call('GET', '/libraries/' + library_id + '/documents', 'user').status_code, 200)
        self.assertEqual(self.call('GET', '/libraries/' + library_id + '/documents', 'user2').status_code, 403)
        for role in ('admin', 'superadmin'):
            self.assertEqual(self.call('GET', '/libraries/' + library_id + '/documents', role).status_code, 200)
        visible = self.call('GET', '/libraries', 'user2').json()['data']['libraries']
        self.assertNotIn(library_id, [row['id'] for row in visible])
        self.assertEqual(self.call('POST', '/libraries', 'user', {'name': '公有', 'direction_id': DEFAULT, 'visibility': 'public'}).status_code, 403)
        self.assertEqual(self.call('PATCH', '/libraries/' + LEGACY, 'user', {'name': '越权', 'expected_revision': 0}).status_code, 403)

    def test_super_direct_admin_pending_and_rejection(self):
        direct = self.create_private('superadmin')
        self.assertEqual(self.store.policy(direct)['owner_id'], self.users['superadmin'])
        result = self.call('PATCH', '/libraries/' + LEGACY, 'admin', {'name': '待改', 'expected_revision': 0}).json()['data']
        self.assertEqual(self.backend.libs[0]['name'], '原公共库')
        rejected = self.call('POST', '/reviews/' + result['id'] + '/decision', 'superadmin',
                             {'decision': 'reject', 'comment': '请补资料'}).json()['data']
        self.assertEqual(rejected['state'], 'rejected')
        self.assertEqual(self.backend.libs[0]['name'], '原公共库')
        self.assertEqual(self.call('POST', '/reviews/' + result['id'] + '/decision', 'superadmin', {'decision': 'approve'}).status_code, 409)

    def test_update_version_conflict_and_duplicate_names(self):
        library_id = self.create_private()
        self.assertEqual(self.call('POST', '/libraries', 'user2', {'name': '私有库', 'direction_id': DEFAULT}).status_code, 409)
        response = self.call('PATCH', '/libraries/' + library_id, 'user', {'description': '更新', 'expected_revision': 1})
        review = response.json()['data']
        with self.store.connect(write=True) as conn:
            conn.execute('UPDATE library_policy SET revision=2 WHERE library_id=?', (library_id,))
        self.assertEqual(self.call('POST', '/reviews/' + review['id'] + '/decision', 'superadmin', {'decision': 'approve'}).status_code, 409)
        self.assertEqual(self.call('PATCH', '/libraries/' + library_id, 'user', {'name': ' ', 'expected_revision': 2}).status_code, 400)
        self.assertEqual(self.call('PATCH', '/libraries/' + library_id, 'user', {'visibility': 'public', 'expected_revision': 2}).status_code, 403)

    def test_upload_is_staged_until_approval_and_records_are_scoped(self):
        library_id = self.create_private()
        response = self.call('POST', '/libraries/' + library_id + '/documents', 'user',
                             data={'category': 'general', 'expected_revision': '1'},
                             files={'file': ('test.md', b'# source\ncontent')})
        self.assertEqual(response.status_code, 202)
        row = response.json()['data']
        self.assertNotIn('stored', row['payload']['files'][0])
        self.assertNotIn('document.upload', self.backend.calls)
        approved = self.call('POST', '/reviews/' + row['id'] + '/decision', 'superadmin', {'decision': 'approve'}).json()['data']
        self.assertEqual(approved['state'], 'applied')
        self.assertEqual(self.backend.last_bytes, b'# source\ncontent')
        self.assertEqual(list(self.store.uploads.iterdir()), [])
        record_id = approved['result']['items'][0]['record_id']
        self.assertEqual(self.call('GET', '/records/' + record_id, 'user').status_code, 200)
        self.assertEqual(self.call('GET', '/records/' + record_id, 'user2').status_code, 403)
        self.assertEqual(self.call('POST', '/records/' + record_id + '/retry', 'user').status_code, 409)

    def test_upload_rejected_empty_and_idempotent(self):
        library_id = self.create_private()
        path = '/libraries/' + library_id + '/documents'
        empty = self.call('POST', path, 'user', data={'expected_revision': '1'}, files={'file': ('x.md', b'')})
        self.assertEqual(empty.status_code, 400)
        self.assertEqual(list(self.store.uploads.iterdir()), [])
        reviews = []
        for _ in range(2):
            reviews.append(self.call('POST', path, 'user', data={'expected_revision': '1'},
                                     files={'file': ('x.md', b'x')}, headers={'Idempotency-Key': 'same-upload'}).json()['data']['id'])
        self.assertEqual(reviews[0], reviews[1])
        self.assertEqual(len(list(self.store.uploads.iterdir())), 1)
        self.call('POST', '/reviews/' + reviews[0] + '/decision', 'superadmin', {'decision': 'reject'})
        self.assertEqual(list(self.store.uploads.iterdir()), [])

    def test_idempotency_and_concurrent_decisions(self):
        data = {'name': '一次建库', 'direction_id': DEFAULT}
        first = self.call('POST', '/libraries', 'admin', data, headers={'Idempotency-Key': 'create-1'})
        second = self.call('POST', '/libraries', 'admin', data, headers={'Idempotency-Key': 'create-1'})
        review_id = first.json()['data']['id']
        self.assertEqual(second.json()['data']['id'], review_id)
        conflict = self.call('POST', '/libraries', 'admin', dict(data, name='别的库'), headers={'Idempotency-Key': 'create-1'})
        self.assertEqual(conflict.status_code, 409)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            statuses = list(executor.map(lambda _: self.call('POST', '/reviews/' + review_id + '/decision',
                            'superadmin', {'decision': 'approve'}).status_code, range(2)))
        self.assertEqual(sorted(statuses), [200, 409])
        self.assertEqual(self.backend.calls.count('library.create'), 1)

    def test_model_key_encryption_rotation_disable_and_audit(self):
        data = {'name': '测试模型', 'provider': 'deepseek', 'base_url': 'https://example.com/v1',
                'model': 'chat', 'api_key': 'secret-value-not-in-logs'}
        self.assertEqual(self.call('POST', '/model-keys', 'user', data).status_code, 403)
        response = self.call('POST', '/model-keys', 'admin', data)
        self.assertEqual(response.status_code, 201)
        key_id = response.json()['data']['id']
        self.assertNotIn(data['api_key'], response.text)
        self.assertNotIn(data['api_key'].encode(), self.store.path.read_bytes())
        self.assertEqual(self.store.get_model_credential(key_id)['api_key'], data['api_key'])
        self.assertEqual(self.call('POST', '/model-keys', 'superadmin', data).status_code, 409)
        changed = self.call('PATCH', '/model-keys/' + key_id, 'superadmin',
                            {'api_key': 'new-secret', 'expected_revision': 1}).json()['data']
        self.assertEqual(changed['revision'], 2)
        self.assertEqual(self.call('PATCH', '/model-keys/' + key_id, 'admin', {'enabled': False, 'expected_revision': 1}).status_code, 409)
        self.call('PATCH', '/model-keys/' + key_id, 'admin', {'enabled': False, 'expected_revision': 2})
        with self.assertRaises(ApiError):
            self.store.get_model_credential(key_id)
        logs = self.call('GET', '/audit?event=model_key.update', 'admin')
        self.assertEqual(len(logs.json()['data']['events']), 2)
        self.assertNotIn('new-secret', logs.text)
        self.assertNotIn('password', logs.text)
        self.assertEqual(self.call('GET', '/audit', 'user').status_code, 403)

    def test_validation_pagination_and_no_user_management(self):
        self.assertEqual(self.call('POST', '/auth/login', body={'identifier': 'E1', 'password': 'dont-echo', 'role': 'superadmin'}).status_code, 400)
        self.assertNotIn('dont-echo', self.call('POST', '/auth/login', body={'password': 'dont-echo'}).text)
        self.assertEqual(self.call('GET', '/audit?limit=201', 'admin').status_code, 400)
        self.assertEqual(self.call('GET', '/reviews?state=unknown', 'user').status_code, 400)
        self.assertEqual(self.call('GET', '/libraries?direction_id=unknown', 'user').status_code, 404)
        self.assertEqual(self.call('GET', '/libraries/' + LEGACY + '/documents?page_size=101', 'user').status_code, 400)
        self.assertEqual(self.call('POST', '/users', 'superadmin', {}).status_code, 404)
        self.assertEqual(self.call('POST', '/libraries', 'user', {'name': 'x', 'direction_id': DEFAULT, 'owner_id': self.users['user2']}).status_code, 400)

    def test_failure_and_restart_do_not_repeat_external_mutations(self):
        self.backend.failure = UncertainOperation()
        result = self.call('POST', '/libraries', 'superadmin', {'name': '超时库', 'direction_id': DEFAULT}).json()['data']
        self.assertEqual(result['state'], 'uncertain')
        self.assertEqual(self.call('POST', '/reviews/' + result['id'] + '/decision', 'superadmin', {'decision': 'approve'}).status_code, 409)
        self.assertEqual(len(self.backend.calls), 1)
        with self.store.connect(write=True) as conn:
            conn.execute("UPDATE review SET state='applying' WHERE id=?", (result['id'],))
        from management_api import Management
        Management(Store(self.temp.name), self.backend)
        self.assertEqual(self.store.one('SELECT state FROM review WHERE id=?', (result['id'],))['state'], 'uncertain')

    def test_directions_require_admin_review(self):
        self.assertEqual(self.call('POST', '/directions', 'user', {'name': '方向'}).status_code, 403)
        pending = self.call('POST', '/directions', 'admin', {'name': '方向'}).json()['data']
        self.assertEqual(len(self.backend.dirs), 1)
        response = self.call('POST', '/reviews/' + pending['id'] + '/decision', 'superadmin', {'decision': 'approve'})
        self.assertEqual(response.json()['data']['state'], 'applied')
        self.assertEqual(len(self.backend.dirs), 2)

    def test_legacy_entry_checks_identity_and_blocks_unreviewed_changes(self):
        self.assertEqual(self.client.get('/retrieve/libraries').status_code, 401)
        library_id = self.create_private()
        headers = self.headers['user2']
        result = self.client.get('/retrieve/libraries', headers=headers)
        self.assertEqual(result.status_code, 200)
        self.assertNotIn(library_id, [row['id'] for row in result.json()['data']['libraries']])
        self.assertEqual(self.client.post('/retrieve/search', headers=headers,
                         json={'library_id': library_id, 'text': '越权查询'}).status_code, 403)
        self.assertEqual(self.client.post('/retrieve/libraries', headers=self.headers['admin'],
                         json={'name': '直接建库'}).status_code, 409)

    def test_disabled_applicant_cannot_be_approved_and_backend_failure_is_recorded(self):
        review = self.call('POST', '/libraries', 'user', {'name': '待审核', 'direction_id': DEFAULT}).json()['data']
        with self.store.connect(write=True) as conn:
            conn.execute('UPDATE account SET active=0 WHERE id=?', (self.users['user'],))
        self.assertEqual(self.call('POST', '/reviews/' + review['id'] + '/decision', 'superadmin', {'decision': 'approve'}).status_code, 409)
        self.backend.failure = ApiError(400, 'unsupported_format', '格式不支持。')
        failed = self.call('POST', '/libraries', 'superadmin', {'name': '服务拒绝', 'direction_id': DEFAULT}).json()['data']
        self.assertEqual(failed['state'], 'failed')
        self.assertEqual(failed['error_code'], 'unsupported_format')

    def test_persisted_sessions_and_encrypted_credential_survive_database_reopen(self):
        key = self.call('POST', '/model-keys', 'admin', {'name': '重开测试', 'provider': 'test',
            'base_url': 'http://localhost:9000/v1', 'api_key': 'test-reopen-secret'}).json()['data']
        reopened = Store(self.temp.name)
        token = self.headers['user']['Authorization'][7:]
        self.assertEqual(reopened.authenticate(token)['id'], self.users['user'])
        self.assertEqual(reopened.get_model_credential(key['id'])['api_key'], 'test-reopen-secret')


if __name__ == '__main__':
    unittest.main(verbosity=2)
