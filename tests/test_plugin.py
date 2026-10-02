"""Run directly: python3 tests/test_plugin.py."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('doomface', str(ROOT / 'bin/herdr-doomface'))
spec = importlib.util.spec_from_loader(loader.name, loader)
doomface = importlib.util.module_from_spec(spec)
loader.exec_module(doomface)


def check():
    with tempfile.TemporaryDirectory() as config, patch.dict(os.environ, {'XDG_CONFIG_HOME': config}):
        registry = Path(config) / 'herdr/plugins.json'
        registry.parent.mkdir()
        assert not doomface.plugin_enabled()
        for enabled in [True, False]:
            registry.write_text(json.dumps([{'plugin_id': 'dev.ariel.herdr-doomface', 'enabled': enabled}]))
            assert doomface.plugin_enabled() == enabled
        registry.write_text('incomplete write')
        assert not doomface.plugin_enabled()


if __name__ == '__main__':
    check()
