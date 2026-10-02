"""Run directly: python3 tests/test_plugin.py."""
import importlib.machinery
import importlib.util
import json
import os
import select
import subprocess
import sys
from pathlib import Path
import tempfile
from unittest.mock import patch
from contextlib import ExitStack

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('doomface', str(ROOT / 'bin/herdr-doomface'))
spec = importlib.util.spec_from_loader(loader.name, loader)
doomface = importlib.util.module_from_spec(spec)
loader.exec_module(doomface)


def check_workers():
    # Real subprocesses exercise kernel lock contention, crash recovery, and
    # stop requests. No test is allowed to signal a PID taken from a file.
    runner = '''import sys, time
from pathlib import Path
namespace = {'__name__': 'test_worker', '__file__': sys.argv[1]}
exec(Path(sys.argv.pop(1)).read_text(), namespace)
def run(pane, stop):
    print('started', flush=True)
    while not stop.exists():
        time.sleep(0.01)
    return 0
namespace['run'] = run
sys.exit(namespace['main']())
'''
    with tempfile.TemporaryDirectory() as cache:
        env = {**os.environ, 'XDG_CACHE_HOME': cache}
        processes = []
        def start(pane='w1:p1'):
            process = subprocess.Popen([sys.executable, '-c', runner, str(ROOT / 'bin/herdr-doomface'), pane],
                                       env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            processes.append(process)
            return process
        def ready(process):
            assert select.select([process.stdout], [], [], 10)[0], 'Worker did not start'
            assert process.stdout.readline().strip() == 'started'
        try:
            first = start()
            ready(first)
            competitor = start()
            output, error = competitor.communicate(timeout=10)
            assert competitor.returncode == 0 and not output, (output, error)
            first.kill()  # Our own test child, never a PID read from disk.
            first.communicate(timeout=10)
            replacement = start()
            ready(replacement)
            second_pane = start('w1:p2')
            ready(second_pane)
            unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
            processes.append(unrelated)
            stale = Path(cache) / 'herdr-kit/doomface/pids/w1_p3.pid'
            stale.parent.mkdir(parents=True)
            stale.write_text(str(unrelated.pid))
            result = subprocess.run(['zsh', str(ROOT / 'doomface-hook.sh'), '--stop-all'],
                                    env={**env, 'HERDR_PLUGIN_ROOT': str(ROOT)},
                                    text=True, capture_output=True, timeout=20)
            assert result.returncode == 0 and '2 workers' in result.stdout, result
            for process in [replacement, second_pane]:
                process.communicate(timeout=10)
                assert process.returncode == 0
            assert unrelated.poll() is None, 'Stop-all signaled an unrelated process'
            assert not list((Path(cache) / 'herdr-kit/doomface/workers').glob('*.stop'))
            restarted = start()
            ready(restarted)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=10)


def check_metadata():
    with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
        stack.enter_context(patch.object(doomface, 'load_manifest', return_value={'STFST00': {}}))
        stack.enter_context(patch.object(doomface, 'plugin_enabled', side_effect=[True, True, True, False]))
        stack.enter_context(patch.object(doomface, 'herdr_pane_get', return_value={'agent': 'claude'}))
        stack.enter_context(patch.object(doomface, 'remaining_pct_for_pane', side_effect=[0.91, 0.90, 0.90]))
        stack.enter_context(patch.object(doomface, 'corner_offset', return_value=(0, 0)))
        stack.enter_context(patch.object(doomface.time, 'sleep'))
        stack.enter_context(patch.object(doomface.signal, 'signal'))
        draw = stack.enter_context(patch.object(doomface, 'draw'))
        report = stack.enter_context(patch.object(doomface, 'report_tokens'))
        clear = stack.enter_context(patch.object(doomface, 'clear'))
        stack.enter_context(patch.object(doomface, 'clear_tokens'))
        assert doomface.run('w1:p1', Path(temporary) / 'stop') == 0
        assert draw.call_count == 1
        assert [call.args[2] for call in report.call_args_list] == [0.91, 0.90]
        clear.assert_called_once_with('w1:p1')
        from _doomface_core import transcript_path
        stack.enter_context(patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': temporary}))
        assert transcript_path('/project/name', 'session') == Path(temporary) / 'projects/-project-name/session.jsonl'


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
    check_workers()
    check_metadata()
