# Herdr Doomface

Show Claude's context usage as sidebar metadata or a separate bitmap widget.

The automatic reporter publishes a face and remaining-context percentage for sidebar layouts. The opt-in widget writes standard Kitty graphics from its own split pane, which is the graphics path supported by Herdr 0.9.3. The build downloads widget assets into the existing XDG cache location. Transcript lookup respects `CLAUDE_CONFIG_DIR`.

## Install

[Herdr Setup](https://github.com/ariel-ps/herdr-setup) installs prerequisites and lets you select this plugin in `dependencies.json`.

With Herdr 0.9.3+ already installed:

```sh
herdr plugin install ariel-ps/herdr-doomface --ref main --yes
```

Use a commit or release tag instead of `main` to pin a version. Supports macOS and Ubuntu/Debian Linux.

Add the custom tokens to the Claude sidebar rows where you want them displayed:

```toml
[ui.sidebar.agents.rows_by_agent]
claude = [
  ["state_icon", "machine", "workspace", "tab"],
  ["agent", "$doomface", "$doomface_pct"],
]
```

Edit `config.sh` in the directory printed by `herdr plugin config-dir dev.ariel.herdr-doomface`. Existing media caches are reused. Open the bitmap widget with:

```sh
herdr plugin action invoke doomface-widget --plugin dev.ariel.herdr-doomface
```

To request that current metadata reporters stop:

```sh
herdr plugin action invoke doomface-stop-all --plugin dev.ariel.herdr-doomface
```

Reporters exit at the next poll (normally 4 seconds apart). New agent events can start them again; disable the plugin to keep reporting off. Reporters use kernel locks and stop requests, so stale PID files cannot cause an unrelated process to be terminated. Existing widget panes can be closed normally.

## Development

Herdr-facing event and action adapters live in `hooks/` and `actions/`. Private launchers live in `libexec/`, and the importable Python implementation lives in `src/herdr_doomface/`. Build-only asset tooling lives in `scripts/build/`.

Run the contract and behavior tests directly:

```sh
python3 tests/test_plugin.py
```

See [CHANGELOG.md](CHANGELOG.md) for release notes and [SECURITY.md](SECURITY.md) for vulnerability reporting.

## License

Original project code is licensed under the [MIT License](LICENSE). Third-party code and media retain their own terms; this license does not grant rights to game assets, downloaded themes, or other third-party content.
