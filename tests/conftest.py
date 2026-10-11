import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bili.store import Store  # noqa: E402


@pytest.fixture
async def store(tmp_path):
    value = Store(tmp_path / "bilibili.sqlite3")
    await value.open()
    yield value
    await value.close()


@pytest.fixture(scope="session")
def plugin_module():
    # Match the real MaiBot loader's package isolation and relative-import behavior.
    spec = importlib.util.spec_from_file_location(
        "test_bilibili_plugin", ROOT / "plugin.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def dynamic(did="100", uid="42", ts=1000, kind="DYNAMIC_TYPE_WORD", text="更新内容"):
    return {
        "id_str": did,
        "type": kind,
        "modules": {
            "module_author": {"mid": uid, "name": "测试 UP", "pub_ts": ts},
            "module_dynamic": {"desc": {"text": text}},
        },
    }
