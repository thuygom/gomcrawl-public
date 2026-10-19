import os
from unittest.mock import patch

from core import gateway


def test_gateway_requires_an_explicit_endpoint():
    with patch.dict(os.environ, {}, clear=True):
        assert not gateway.should_use_gateway()
    with patch.dict(os.environ, {"WEB_SEARCH_GATEWAY_URL": "http://127.0.0.1:9101"}, clear=True):
        assert gateway.should_use_gateway()
        assert not gateway.should_use_gateway(local=True)
    with patch.dict(os.environ, {"WEB_SEARCH_GATEWAY_URL": "http://127.0.0.1:9101", "WEB_SEARCH_GATEWAY_DISABLED": "1"}, clear=True):
        assert not gateway.should_use_gateway()


def test_default_credentials_stay_inside_project():
    import subprocess
    import sys
    check = "from core.credentials import PROJECT_ROOT, KEY_DIRS; assert set(KEY_DIRS) == {PROJECT_ROOT / 'key'}"
    env = {k: v for k, v in os.environ.items() if k not in {"WORKSPACE_ROOT", "WEB_SEARCH_PROJECT_ROOT"}}
    subprocess.run([sys.executable, "-c", check], env=env, check=True)
