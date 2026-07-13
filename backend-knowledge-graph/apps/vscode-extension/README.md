# bkg — VS Code extension

A **thin editor client** over the headless `bkg` engine (no analysis logic lives
here — it shells out to the `bkg` CLI). It adds a **Backend Graph** sidebar that
lists every endpoint in the workspace with a confidence marker, and lets you jump
to the handler.

- Sidebar view **Endpoints**: `METHOD  /resolved/path  →  handler  [= ~ !]`
  (`=` static-certain · `~` inferred · `!` partial — an unresolved DTO).
- Click an endpoint → opens its handler at the exact line.
- Auto-refreshes when you save a `.py` file; manual refresh in the view title.

## Prerequisites
The `bkg` CLI must be installed and on `PATH` (or set `bkg.command` in settings):

```bash
# from the repo root
uv sync            # installs the `bkg` console script into the project venv
```

## Develop / run
```bash
npm install
npm run compile        # or: npm run watch
# then press F5 in VS Code to launch an Extension Development Host
```

`npm run typecheck` runs `tsc --noEmit`.

Works for any framework the `bkg` engine supports (FastAPI and Flask today) —
the extension is framework-agnostic because the CLI is.

## Package / publish
```bash
npm run package        # -> bkg-vscode-<version>.vsix  (installable via "Install from VSIX…")
# publish to the Marketplace (needs a publisher + PAT):
#   npx @vscode/vsce publish
```
The Marketplace listing icon should be a 128×128 PNG (`icon` in package.json); the
sidebar view uses `media/icon.svg`.

## Design
This mirrors the design doc's "thin extension": all understanding lives in the
Python engine; the extension only renders and supervises. A future version can
launch `bkg-mcp` (the MCP server) or `bkg watch` (the live daemon) instead of
one-shot CLI calls, for always-fresh incremental updates.
