import os
import tempfile

import pytest

_TMP_DIR = tempfile.mkdtemp(prefix="rule-engine-tests-")
_DB_PATH = os.path.join(_TMP_DIR, "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"
os.environ["SEED_DEMO"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from app.database import connect, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.seed import DEMO_ID  # noqa: E402

# 会话开始即建表（startup 之外也可独立使用 DB）
with connect() as _conn:
    init_db(_conn)


@pytest.fixture()
def client():
    # 每个测试前清空，保证相互独立（演示规则集由 startup 重新播种）
    conn = connect()
    init_db(conn)
    conn.executescript(
        "DELETE FROM idempotency_keys; DELETE FROM evaluations; "
        "DELETE FROM ruleset_versions; DELETE FROM rulesets;"
    )
    conn.commit()
    conn.close()

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def simple_ruleset_payload():
    return {
        "name": "simple",
        "default_conflict_policy": "collect_all",
        "rules": [
            {
                "code": "r1",
                "condition": 'doc["x"] >= 10',
                "action": '{"level": "high"}',
            },
            {
                "code": "r2",
                "condition": "r1.hit",
                "depends_on": ["r1"],
                "action": '{"bonus": 1}',
            },
        ],
    }


@pytest.fixture()
def demo_id():
    return DEMO_ID
