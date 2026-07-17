# appdriver

Dev-only macOS window automation CLI so agent sessions can see and drive the
real native window of the app under test (not just its HTTP engine). Local automation of the
user's own apps; the sensitive parts are gated by macOS permissions only the
user can grant. Never bundled into the shipped app.

```sh
make build-appdriver     # single-arch x86_64 (this Mac); no lipo — dev tool only
tools/appdriver/.build/x86_64-apple-macosx/release/appdriver --help
```

## Subcommands

| command | needs | notes |
|---|---|---|
| `windows [--app <name>]` | nothing | JSON list; `title` is "" until Screen Recording is granted (macOS rule) |
| `shot (--window <id> \| --app <name>) <out.png>` | Screen Recording | `CGWindowListCreateImage`, falls back to `screencapture -l` |
| `click <x> <y> [--window <id> [--relative]] [--double]` | Accessibility | with `--window`, x/y are window-relative |
| `key --text "abc"` / `key --code <n> [--cmd --shift --alt --ctrl]` | Accessibility | |
| `front --app <name>` | nothing | `NSRunningApplication.activate` |
| `status` | nothing | JSON: `ax_trusted`, `screen_recording` (preflight), capture probe |

## Permissions

Grant in **System Settings → Privacy & Security** to the app that *launches*
appdriver (the terminal / agent host), not to appdriver itself:

- **Screen Recording** → `shot` + window titles. Quit and reopen the launching
  app after granting.
- **Accessibility** → `click` / `key`. No restart needed.

Missing permissions never fail silently: the exact grant path prints to stderr
with a distinct exit code.

## Exit codes

`0` ok · `1` usage · `2` app/window not found · `3` Screen Recording missing ·
`4` Accessibility missing · `5` capture failed (e.g. stale grant, window gone)
