# Conversation rendering and live state

The chat renders inline and display LaTeX (`$…$`, `$$…$$`, `\(…\)`,
`\[…\]`), including matrices, through locally bundled KaTeX. Fenced `math`
and `latex` blocks are supported. Code highlighting is local too. Existing
Markdown tables, lists, links, blockquotes and code copying remain available.
HTML from messages is escaped; KaTeX trust is disabled and macro expansion is
bounded. Unsupported formulas retain their source. Assets do not require a CDN.

Each turn displays wall-clock elapsed time, including model/tool execution and
waiting for input. Completed turns use the persisted end timestamp. Historical
turns without timing evidence must not be assigned fabricated durations.

The live stream sends heartbeats. After a transport error or 45 seconds without
events, the browser makes at most five automatic reconnection attempts with
1/2/4/8/16-second delays. A healthy heartbeat resets the failure streak. The
manual reconnect button starts a new attempt sequence. Recovery refreshes output;
it never resubmits a message or repeats a command. A timed-out POST can have an
unknown result: inspect the conversation before sending again.

Pending questions and approvals appear in a global attention menu, including on
settings pages. Clicking an item opens the owning conversation at the interaction
card. The browser may show Windows notifications after the user explicitly grants
permission. Unsupported browsers and denied permissions retain in-page alerts.
The tab must remain open; this is not a background Windows notification service.
Notification bodies do not include commands, answers or other private content.

File tools show bounded before/after text diffs. Commands also observe authorized
workspace folders, up to 400 entries / 2 MB with depth and per-file size limits.
Hidden, generated, binary, linked and large files are excluded. Observations can
include concurrent external edits; they do not prove authorship or implement undo.
Pre-upgrade turns cannot show changes for which no before-image was recorded.

## Verification

- `python -m unittest tools.test_ui_reliability tools.test_ui_language tools.test_live_chat tools.test_general_chat tools.test_streaming`
- Browser regression: `tools/test_ui_reliability_browser.cjs` takes a rendered
  fixture JSON with `page` and `thread` fields. It checks math, highlighting,
  elapsed time, diffs, five failed reconnects, manual retry and notification links.
  The fixture page must use a standards-mode doctype, like the production page.

Local vendor licenses are preserved in `agentplat/static`.
