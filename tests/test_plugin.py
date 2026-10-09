"""Run directly: python3 tests/test_plugin.py."""
from contextlib import ExitStack
import importlib.util
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from herdr_doomface import core  # noqa: E402
from herdr_doomface import worker as doomface  # noqa: E402

FETCHER_SPEC = importlib.util.spec_from_file_location(
    'fetch_doom_wad',
    ROOT / 'scripts/build/fetch-doom-wad.py',
)
assert FETCHER_SPEC and FETCHER_SPEC.loader
fetch_doom_wad = importlib.util.module_from_spec(FETCHER_SPEC)
FETCHER_SPEC.loader.exec_module(fetch_doom_wad)


def check_workers() -> None:
    # Real subprocesses exercise kernel lock contention, crash recovery, and
    # stop requests. No test is allowed to signal a PID taken from a file.
    runner = '''import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv.pop(1))
from herdr_doomface import worker
def run(pane, stop):
    print('started', flush=True)
    while not stop.exists():
        time.sleep(0.01)
    return 0
worker.run = run
sys.exit(worker.main())
'''
    with tempfile.TemporaryDirectory() as cache:
        env = {**os.environ, 'XDG_CACHE_HOME': cache}
        processes = []

        def start(pane: str = 'w1:p1') -> subprocess.Popen[str]:
            process = subprocess.Popen(
                [sys.executable, '-c', runner, str(ROOT / 'src'), pane],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            processes.append(process)
            return process

        def ready(process: subprocess.Popen[str]) -> None:
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
            result = subprocess.run(
                ['zsh', str(ROOT / 'hooks/on-pane-agent-status-changed-doomface.zsh'), '--stop-all'],
                env={**env, 'HERDR_PLUGIN_ROOT': str(ROOT)},
                text=True, capture_output=True, timeout=20,
            )
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


def check_metadata() -> None:
    real_report_tokens = doomface.report_tokens
    real_clear_tokens = doomface.clear_tokens

    class FragmentedSocket:
        def __init__(self) -> None:
            self.chunks = [b'{"res', b'ult":{}}\n']

        def __enter__(self) -> 'FragmentedSocket':
            return self

        def __exit__(
            self,
            _exc_type: object,
            _exc: object,
            _traceback: object,
        ) -> None:
            return None

        def settimeout(self, _timeout: int) -> None:
            return None

        def connect(self, _path: str) -> None:
            return None

        def sendall(self, request: bytes) -> None:
            assert request.endswith(b'\n')

        def recv(self, _size: int) -> bytes:
            return self.chunks.pop(0) if self.chunks else b''

    with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
        stack.enter_context(patch.object(doomface, 'load_manifest', return_value={'STFST00': {}}))
        stack.enter_context(patch.object(doomface, 'plugin_enabled', side_effect=[True, True, True, False]))
        stack.enter_context(patch.object(doomface, 'herdr_pane_get', return_value={'agent': 'claude'}))
        stack.enter_context(patch.object(doomface, 'remaining_pct_for_pane', side_effect=[0.91, 0.90, 0.90]))
        stack.enter_context(patch.object(doomface, 'corner_offset', return_value=(0, 0)))
        stack.enter_context(patch.object(doomface.time, 'sleep'))
        stack.enter_context(patch.object(doomface.signal, 'signal'))
        draw = stack.enter_context(patch.object(doomface, 'draw'))
        draw.side_effect = [False, True]
        report = stack.enter_context(patch.object(doomface, 'report_tokens'))
        report.side_effect = [False, True]
        clear = stack.enter_context(patch.object(doomface, 'clear'))
        stack.enter_context(patch.object(doomface, 'clear_tokens'))
        assert doomface.run('w1:p1', Path(temporary) / 'stop') == 0
        assert draw.call_count == 2
        assert [call.args[2] for call in report.call_args_list] == [0.91, 0.90]
        clear.assert_called_once_with('w1:p1')
        stack.enter_context(patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': temporary}))
        assert core.transcript_path('/project/name', 'session') == (
            Path(temporary) / 'projects/-project-name/session.jsonl'
        )
        try:
            core.transcript_path('/project/name', '../../outside')
        except ValueError:
            pass
        else:
            raise AssertionError('Session path traversal was accepted')
        assert core.remaining_pct_for_pane({
            'cwd': '/project/name',
            'agent_session': {'value': '../../outside'},
        }) is None
        with (
            patch.dict(os.environ, {'HERDR_BIN_PATH': '/custom/herdr'}),
            patch.object(core.subprocess, 'run') as herdr_run,
        ):
            herdr_run.return_value.returncode = 0
            herdr_run.return_value.stdout = '{"result":{"pane":{}}}'
            assert core.herdr_pane_get('w1:p1') == {}
            assert doomface.pane_rect('w1:p1') is None
            assert real_report_tokens('w1:p1', 'face', 0.5)
            assert real_clear_tokens('w1:p1')
            assert all(call.args[0][0] == '/custom/herdr' for call in herdr_run.call_args_list)
        with patch.object(doomface.socket, 'socket', return_value=FragmentedSocket()):
            assert doomface.rpc('test', {})


def check_layout_contract() -> None:
    manifest = tomllib.loads((ROOT / 'herdr-plugin.toml').read_text())
    assert manifest['id'] == 'dev.ariel.herdr-doomface'
    assert manifest['build'] == [{
        'command': ['sh', './scripts/build/cache-doom-frames.sh'],
    }]
    assert manifest['events'] == [{
        'on': 'pane.agent_status_changed',
        'command': ['zsh', './hooks/on-pane-agent-status-changed-doomface.zsh'],
    }]
    assert manifest['actions'] == [
        {
            'id': 'doomface-stop-all',
            'title': 'Stop all doom faces',
            'command': ['./libexec/herdr-doomface', '--stop-all'],
        },
        {
            'id': 'doomface-widget',
            'title': 'Open Doom face widget',
            'command': ['zsh', './actions/open-widget.zsh'],
        },
    ]
    assert manifest['panes'] == [{
        'id': 'doomface-widget',
        'title': 'Doom Face',
        'platforms': ['macos', 'linux'],
        'placement': 'split',
        'command': ['./libexec/herdr-doomface-widget'],
    }]

    required = [
        'CHANGELOG.md',
        'SECURITY.md',
        'pyproject.toml',
        'bin/herdr-doomface',
        'bin/herdr-doomface-widget',
        'hooks/on-pane-agent-status-changed-doomface.zsh',
        'actions/open-widget.zsh',
        'bin/herdr-doomface',
        'bin/herdr-doomface-widget',
        'libexec/herdr-doomface',
        'libexec/herdr-doomface-widget',
        'src/herdr_doomface/__init__.py',
        'src/herdr_doomface/core.py',
        'src/herdr_doomface/worker.py',
        'src/herdr_doomface/widget.py',
        'scripts/build/fetch-doom-wad.py',
    ]
    assert all((ROOT / path).is_file() for path in required)

    executables = [
        'hooks/on-pane-agent-status-changed-doomface.zsh',
        'actions/open-widget.zsh',
        'libexec/herdr-doomface',
        'libexec/herdr-doomface-widget',
    ]
    assert all(os.access(ROOT / path, os.X_OK) for path in executables)

    legacy = [
        'bin/_doomface_core.py',
        'doomface-hook.sh',
        'doomface-widget-action.sh',
        'scripts/fetch-doom-wad.py',
    ]
    assert not any((ROOT / path).exists() for path in legacy)


def check_frame_cache() -> None:
    with tempfile.TemporaryDirectory() as temporary, patch.dict(
        os.environ,
        {'XDG_CACHE_HOME': temporary},
    ):
        root = Path(temporary) / 'herdr-kit/doomface'
        staged = root / 'sets/frames-test'
        staged.mkdir(parents=True)
        manifest = {}
        for name in fetch_doom_wad.LUMP_NAMES:
            (staged / f'{name}.rgba').write_bytes(b'\0\0\0\0')
            manifest[name] = {'width': 1, 'height': 1}
        (staged / 'manifest.json').write_text(json.dumps(manifest))
        fetch_doom_wad.publish_cache(staged, root)
        assert core.frames_dir() == staged
        assert fetch_doom_wad.already_done(staged)
        (staged / f'{fetch_doom_wad.LUMP_NAMES[0]}.rgba').write_bytes(b'')
        assert not fetch_doom_wad.already_done(staged)


def check_relocated_entrypoints() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        relocated = Path(temporary) / 'relocated plugin with spaces'
        shutil.copytree(
            ROOT,
            relocated,
            ignore=shutil.ignore_patterns('.git', '.ruff_cache', '__pycache__', '*.pyc'),
        )
        env = os.environ.copy()
        env.pop('HERDR_PLUGIN_ROOT', None)
        for name in [
            'HERDR_DOOMFACE_OFF',
            'HERDR_DOOMFACE_TARGET',
            'HERDR_DOOMFACE_INTERVAL',
            'HERDR_DOOMFACE_COLS',
            'HERDR_DOOMFACE_ROWS',
            'HERDR_DOOMFACE_CORNER',
            'HERDR_DOOMFACE_Z',
        ]:
            env.pop(name, None)
        env['XDG_CACHE_HOME'] = str(Path(temporary) / 'cache')

        stopped = subprocess.run(
            ['zsh', str(relocated / 'hooks/on-pane-agent-status-changed-doomface.zsh'), '--stop-all'],
            cwd=temporary, env=env, text=True, capture_output=True, timeout=20,
        )
        assert stopped.returncode == 0, stopped
        assert 'requested stop for 0 workers' in stopped.stdout

        widget = subprocess.run(
            [str(relocated / 'libexec/herdr-doomface-widget')],
            cwd=temporary, env=env, text=True, capture_output=True, timeout=20,
        )
        assert widget.returncode == 2, widget
        assert 'HERDR_DOOMFACE_TARGET not set' in widget.stderr

        fake_bin = Path(temporary) / 'fake-bin'
        fake_bin.mkdir()
        fake_herdr = fake_bin / 'custom-herdr'
        herdr_args = Path(temporary) / 'herdr-args'
        fake_herdr.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$FAKE_HERDR_ARGS"\nexit 7\n')
        fake_herdr.chmod(0o755)
        opened = subprocess.run(
            ['zsh', str(relocated / 'actions/open-widget.zsh')],
            cwd=temporary,
            env={
                **env,
                'HERDR_ACTIVE_PANE_ID': 'w1:p1',
                'HERDR_BIN_PATH': str(fake_herdr),
                'FAKE_HERDR_ARGS': str(herdr_args),
            },
            text=True, capture_output=True, timeout=20,
        )
        assert opened.returncode == 7, opened
        assert herdr_args.read_text().splitlines()[:3] == ['plugin', 'pane', 'open']

        fake_uv = fake_bin / 'uv'
        fake_uv.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$FAKE_UV_ARGS"\nexit 7\n')
        fake_uv.chmod(0o755)
        uv_args = Path(temporary) / 'uv-args'
        build_env = {
            **env,
            'PATH': f"{fake_bin}:/usr/bin:/bin",
            'FAKE_UV_ARGS': str(uv_args),
        }
        build = subprocess.run(
            ['sh', str(relocated / 'scripts/build/cache-doom-frames.sh')],
            cwd=temporary, env=build_env, text=True, capture_output=True, timeout=20,
        )
        assert build.returncode == 0, build
        assert 'no frames cached, corner face stays off' in build.stdout
        actual_uv_args = uv_args.read_text().splitlines()
        expected_uv_args = [
            'run',
            '--no-project',
            'python',
            str((relocated / 'scripts/build/fetch-doom-wad.py').resolve()),
        ]
        assert actual_uv_args == expected_uv_args, (actual_uv_args, expected_uv_args)


def check() -> None:
    with tempfile.TemporaryDirectory() as config, patch.dict(os.environ, {'XDG_CONFIG_HOME': config}):
        registry = Path(config) / 'herdr/plugins.json'
        registry.parent.mkdir()
        assert not doomface.plugin_enabled()
        for enabled in [True, False]:
            registry.write_text(json.dumps([{'plugin_id': 'dev.ariel.herdr-doomface', 'enabled': enabled}]))
            assert doomface.plugin_enabled() == enabled
        registry.write_text('incomplete write')
        assert not doomface.plugin_enabled()


class DoomfacePluginTests(unittest.TestCase):
    def test_registry_state(self) -> None:
        check()

    def test_worker_lifecycle(self) -> None:
        check_workers()

    def test_metadata_updates(self) -> None:
        check_metadata()

    def test_layout_contract(self) -> None:
        check_layout_contract()

    def test_frame_cache(self) -> None:
        check_frame_cache()

    def test_relocated_entrypoints(self) -> None:
        check_relocated_entrypoints()


if __name__ == '__main__':
    unittest.main()
