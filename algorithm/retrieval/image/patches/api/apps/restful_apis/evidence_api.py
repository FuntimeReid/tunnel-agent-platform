"""Authenticated text-to-evidence tool using the existing Quart application."""
import asyncio
import logging
import time
import uuid
from quart import jsonify, request
from werkzeug.exceptions import BadRequest, UnsupportedMediaType
from api.apps import current_user, login_required
from api.apps.services.evidence_service import validate, allowed_documents, execute
from api.db.services.knowledgebase_service import KnowledgebaseService
from common.misc_utils import thread_pool_exec
from common.token_utils import token_usage_sink


@manager.route('/algorithm/evidence', methods=['POST'])  # noqa: F821
@login_required
async def evidence():
    request_id = uuid.uuid4().hex
    started = time.monotonic()
    usage = {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0, 'calls': 0}
    def error(status, code, message):
        return jsonify({'code': code, 'message': message, 'request_id': request_id,
                        'usage': dict(usage), 'elapsed_seconds': round(time.monotonic() - started, 3)}), status
    try:
        parsed = validate(await request.get_json())
    except (ValueError, TypeError, BadRequest, UnsupportedMediaType):
        return error(400, 'invalid_request', '参数不正确，请查看接口说明。')
    for kb_id in parsed['dataset_ids']:
        if not await thread_pool_exec(KnowledgebaseService.accessible, kb_id, current_user.id):
            return error(403, 'forbidden', '知识库不存在或无权访问。')
    try:
        docs = await thread_pool_exec(allowed_documents, current_user.id, parsed)
    except PermissionError:
        return error(403, 'forbidden', '文档不在允许访问的范围内。')
    except Exception as exc:
        logging.error('Evidence permission check %s failed: %s', request_id, type(exc).__name__)
        return error(502, 'scope_check_error', '无法确认文档权限，请按request_id查日志。')
    token = token_usage_sink.set(usage)
    try:
        async with asyncio.timeout(parsed['timeout_seconds']):
            data = await execute(current_user.id, parsed, docs, usage)
        return jsonify({'code': 0, 'request_id': request_id, 'data': data})
    except TimeoutError:
        return error(504, 'timeout', '请求超时。')
    except Exception as exc:
        logging.error('Evidence request %s failed: %s', request_id, type(exc).__name__)
        return error(502, 'evidence_error', '检索或模型执行失败，请按request_id查日志。')
    finally:
        token_usage_sink.reset(token)
