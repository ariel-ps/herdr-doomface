# Changelog

All notable changes to Herdr Doomface are documented here.

## Unreleased

### Changed

- Adopt the standard Herdr hybrid-plugin layout.
- Keep event and action adapters thin while moving Python runtime logic into the
  `herdr_doomface` package.
- Preserve plugin IDs, cache and configuration paths, worker locking, metadata,
  overlay behavior, and widget behavior.

## 0.1.0

- Initial Doomface overlay, sidebar metadata, and standalone widget.
