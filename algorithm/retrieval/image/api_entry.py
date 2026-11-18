"""Start only the API, using the same initialization as the selected runtime."""
from pathlib import Path
import shutil

# Runtime configuration stays in a private Docker volume, outside image layers.
for filename in ("service_conf.yaml", "llm_factories.json"):
    shutil.copyfile(Path("/run/retrieval-config") / filename, Path("/ragflow/conf") / filename)

from api.db.runtime_config import RuntimeConfig

RuntimeConfig.init_env()
from api.apps import app

app.run(port=9380, host="0.0.0.0", use_reloader=False)
