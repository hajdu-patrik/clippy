# Working in this repository

Clippy: a local AI clipper, long video in, vertical 9:16 clips out, with a chat web app and a `clippy` command (see
README.md). Code, comments-free modules, docs, commit messages and the product itself are English; a video keeps its
own language for its transcript, captions and post texts. No personal data, machine names, account details or absolute user paths in the repository: per-user state
belongs in `data/` (ignored) and `.env` (ignored).

## Answering

- Respond in the language of the user's prompt.
- A request that publishes, deletes or rewrites history (push, release, force, mass delete) needs an explicit "yes".

## Conventions

- Python 3.12+, Windows 10/11 first. One command: `clippy <command>` = `bin\clippy.cmd` = `python -m clippy`.
  Commands, flags and their help live in `clippy/shell.py` (`COMMANDS`) and `clippy/cli.py` (`build_parser`); the
  PowerShell and Clink completion scripts are generated from them by `clippy install`.
- No comments in code; one-line docstrings where the intent is not obvious. No dead code (`pyflakes`, `vulture`).
- User-facing texts live in catalogues: server and CLI texts in `clippy/i18n.py` (`CATALOG`), UI texts in
  `clippy/web/static/js/i18n.js`. The server sends `{key, params}`; errors raise `ClippyError(key=..., **params)`.
- Long-running steps report progress with `tools.report(fraction)` inside `tools.report_span(lo, hi)`; the web job runner
  turns it into the live progress card (`clippy/web/progress.py`, `Progress`).
- Paths come from `clippy/storage.py` only. Deleting a chat must remove every file of it.
- Parallel work goes through `clippy/concurrency.py`: lock order source video → render slot → GPU (checked at
  runtime), one job per chat at a time, LLM calls of jobs through `GuardedLLM`. Files that others read are written
  all-or-nothing (`tools.atomic_path`, `write_json`, `move_atomic`); a clip version is committed by its DB row.
- Web security is part of the design: keep the CSP strict (no inline script or style, set CSS variables from JS), every
  state-changing request needs the `X-Clippy-Request` header, media is served only through `MediaStore`, video links
  pass `require_public_url`, and LLM output is validated by `ActionValidator` before anything runs.
- The UI is plain ES modules, responsive down to 320 px, honours `prefers-reduced-motion`; colours are tokens in
  `style.css` (`docs/BRAND.md`).

## Checks

```bat
.venv\Scripts\python -m pyflakes clippy tests
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m pip_audit
```

The git hook in `.githooks/` runs the first two (`git config core.hooksPath .githooks`). Server code is imported at
start: restart with `clippy restart` after a change, never while a job is running.
