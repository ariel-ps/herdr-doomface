# Herdr Doomface

Show Claude's context usage as a Doom face overlay or a separate widget.

The build downloads face assets. The overlay stops when the plugin is disabled (within its polling interval). Existing widget panes can be closed normally. Configure overlay position and size in `config.sh`.

## Install

[Herdr Setup](https://github.com/ariel-ps/herdr-setup) installs prerequisites and lets you select this plugin in `dependencies.json`.

With Herdr 0.9.3+ already installed:

```sh
herdr plugin install ariel-ps/herdr-doomface --ref main --yes
```

Use a commit or release tag instead of `main` to pin a version. Supports macOS and Ubuntu/Debian Linux.


Edit `config.sh` in the directory printed by `herdr plugin config-dir dev.ariel.herdr-doomface`. Existing media caches are reused.

## License

Original project code is licensed under the [MIT License](LICENSE). Third-party code and media retain their own terms; this license does not grant rights to game assets, downloaded themes, or other third-party content.
