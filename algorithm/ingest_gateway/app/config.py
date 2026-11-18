"""Load config/ingest.yaml and the secret files it points to."""
import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

CONFIG_PATH = Path(os.environ.get("INGEST_CONFIG", Path(__file__).resolve().parent.parent / "config" / "ingest.yaml"))


@dataclass(frozen=True)
class Settings:
    raw: dict
    ragflow_api_key: str
    mysql_password: str
    mineru_model_id: str

    def __getitem__(self, key):
        return self.raw[key]


@lru_cache(maxsize=1)
def settings() -> Settings:
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    config_dir = CONFIG_PATH.resolve().parent
    account = json.loads((config_dir / raw["ragflow"]["account_file"]).read_text(encoding="utf-8"))
    raw["storage"]["staging_dir"] = str(config_dir / raw["storage"]["staging_dir"])
    return Settings(
        raw=raw,
        ragflow_api_key=(config_dir / raw["ragflow"]["api_key_file"]).read_text(encoding="utf-8").strip(),
        mysql_password=(config_dir / raw["mysql"]["password_file"]).read_text(encoding="utf-8").strip(),
        mineru_model_id=account["mineru_model_id"],
    )


def library_parser_config() -> dict:
    """parser_config for new libraries and for every per-document template change."""
    s = settings()
    return {**s["library_defaults"]["parser_config"], "layout_recognize": s.mineru_model_id}
