"""Export the management API contract for Apifox without runtime data."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

root = Path(__file__).resolve().parent
sys.path.insert(0, str(root.parent))

with TemporaryDirectory() as data_dir, patch.dict("os.environ", {
    "MANAGEMENT_DATA": str(Path(data_dir) / "management"),
    "LIBRARY_CATALOG_DB": str(Path(data_dir) / "catalog.sqlite3"),
}):
    import library_catalog

    with patch.object(library_catalog, "libraries", return_value=[]):
        import retrieval_service

        schema = retrieval_service.app.openapi()
schema['info'] = {'title': '账号权限审核与模型密钥管理', 'version': '1.0.0'}
schema['servers'] = [{'url': 'http://127.0.0.1:30271'}]
schema['paths'] = {path: operations for path, operations in schema['paths'].items()
                   if path.startswith('/retrieve/manage/v1/')}
schema.setdefault('components', {})['securitySchemes'] = {'SessionBearer': {'type': 'http', 'scheme': 'bearer'}}
for path, operations in schema['paths'].items():
    for operation in operations.values():
        if path.endswith(('/auth/login', '/health')):
            operation['security'] = []
        else:
            operation['security'] = [{'SessionBearer': []}]
(root / 'openapi.json').write_text(
    json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding='utf-8', newline='\n')
print('已导出管理接口定义。')
