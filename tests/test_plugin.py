"""Run directly: python3 tests/test_plugin.py."""
import base64
from contextlib import ExitStack
import importlib.util
import io
import json
import os
import select
import shutil
import signal
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
from herdr_doomface import widget  # noqa: E402
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

    with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {'HERDR_DOOMFACE_INTERVAL': '4'}))
        stack.enter_context(patch.object(doomface, 'plugin_enabled', side_effect=[True, True, True, False]))
        pane_get = stack.enter_context(
            patch.object(doomface, 'herdr_pane_get', return_value={'agent': 'claude'}),
        )
        stack.enter_context(patch.object(doomface, 'remaining_pct_for_pane', side_effect=[0.91, None, 0.90]))
        stack.enter_context(patch.object(doomface.time, 'sleep'))
        signal_signal = stack.enter_context(patch.object(doomface.signal, 'signal'))
        report = stack.enter_context(patch.object(doomface, 'report_tokens'))
        report.side_effect = [False, True]
        clear_tokens = stack.enter_context(patch.object(doomface, 'clear_tokens'))
        assert doomface.run('w1:p1', Path(temporary) / 'stop') == 0
        assert [call.args[2] for call in report.call_args_list] == [0.91, 0.90]
        assert [call.args[3] for call in report.call_args_list] == [12000, 12000]
        assert [call.args for call in clear_tokens.call_args_list] == [('w1:p1',), ('w1:p1',)]
        assert 'pane.graphics.' not in Path(doomface.__file__).read_text()
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
            assert real_report_tokens('w1:p1', 'face', 0.5, 15000)
            assert real_clear_tokens('w1:p1')
            assert all(call.args[0][0] == '/custom/herdr' for call in herdr_run.call_args_list)
            assert '--ttl-ms' in herdr_run.call_args_list[-2].args[0]

        pane_get.side_effect = RuntimeError('pane lookup failed')
        doomface.plugin_enabled.side_effect = [True]
        clear_tokens.reset_mock()
        try:
            doomface.run('w1:p1', Path(temporary) / 'stop')
        except RuntimeError:
            pass
        else:
            raise AssertionError('Worker swallowed a pane lookup failure')
        clear_tokens.assert_called_once_with('w1:p1')

        pane_get.side_effect = lambda _pane_id: signal_signal.call_args.args[1](
            signal.SIGTERM,
            None,
        )
        doomface.plugin_enabled.side_effect = [True]
        clear_tokens.reset_mock()
        try:
            doomface.run('w1:p1', Path(temporary) / 'stop')
        except SystemExit:
            pass
        else:
            raise AssertionError('Worker ignored SIGTERM')
        clear_tokens.assert_called_once_with('w1:p1')


def check_widget_protocol() -> None:
    data = bytes(range(256)) * 16
    output = io.StringIO()
    with patch.object(widget.sys, 'stdout', output):
        widget.send_image(data, 1024, 1)
        widget.clear_image()
    rendered = output.getvalue()
    packets = [
        packet.split('\033\\', 1)[0]
        for packet in rendered.split('\033_G')[1:]
    ]
    transfers = [packet for packet in packets if ';' in packet]
    assert len(transfers) == 2
    assert 's=1024,v=1' in transfers[0]
    assert ',m=1;' in transfers[0]
    assert transfers[-1].split(';', 1)[0] == 'q=2,m=0'
    assert base64.b64decode(''.join(packet.split(';', 1)[1] for packet in transfers)) == data
    assert rendered.count(f'a=d,d=I,i={widget.IMAGE_ID},q=2') == 2

    handlers = {}
    with (
        patch.dict(os.environ, {'HERDR_DOOMFACE_TARGET': 'w1:p1'}),
        patch.object(widget, 'load_manifest', return_value={'STFST00': {}}),
        patch.object(widget, 'herdr_pane_get', return_value={'agent': 'cursor'}),
        patch.object(widget, 'show_message') as show_message,
        patch.object(widget, 'clear_image') as clear_image,
        patch.object(widget.signal, 'signal', side_effect=lambda signum, handler: handlers.setdefault(signum, handler)),
        patch.object(
            widget.time,
            'sleep',
            side_effect=lambda _interval: handlers[signal.SIGTERM](signal.SIGTERM, None),
        ) as wait,
    ):
        assert widget.main() == 0
    show_message.assert_called_once_with('doomface: select a Claude pane before opening this widget')
    wait.assert_called_once_with(0.1)
    clear_image.assert_called_once()


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
        assert 'no frames cached, bitmap widget stays off' in build.stdout
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

    def test_widget_uses_kitty_graphics(self) -> None:
        check_widget_protocol()

    def test_layout_contract(self) -> None:
        check_layout_contract()

    def test_frame_cache(self) -> None:
        check_frame_cache()

    def test_relocated_entrypoints(self) -> None:
        check_relocated_entrypoints()


if __name__ == '__main__':
    unittest.main()
