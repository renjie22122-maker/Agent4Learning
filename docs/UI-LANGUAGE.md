# Interface language and delivery

The web interface defaults to English. Choose **English / 中文** in the top-left
header. The preference is saved in the current browser's local storage. Settings
and utility pages use the same selector in their header.

Language selection affects interface controls only. It does not rewrite conversation
history, drafts, code, filenames, approval commands, knowledge documents or model
requests. Assistant replies follow the user's language. The UI catalog is bundled
locally: changing language does not call a translation service.

Core assistant and independent-review instructions are maintained in English. They
describe generic scope, execution, evidence and authority rules, rather than special
behavior for one task type. Plain questions need no automatic project creation,
tool inventory or test suite. Actual files and code tasks retain appropriate checks.

Final replies have no mandatory “round summary” heading. The requested result comes
first. Host acceptance remains structured metadata and appears separately after the
answer; the model cannot turn a failed or missing review into a pass. Historical
responses retain their original text, including old headings.

## Extending the UI

- UI labels live in `agentplat/ui_catalog.json`; `ui_i18n.py` applies them to eligible
  controls and observes dynamic updates.
- Mark user-origin content with `data-user-content`; do not place it in translatable
  UI containers without that boundary. Arbitrary table/document content is excluded.
- Assign translations as text, never executable HTML. Technical error payloads and
  tool output retain their original language; not every historical diagnostic is translated.
- New UI templates must add catalog entries and browser coverage. The optional
  `tools/build_ui_catalog.py --real` sends repository UI literals to the configured
  model to draft translations; review the resulting data before publishing.

```powershell
python -m unittest tools.test_ui_language tools.test_runtime_contracts
python tools/check_live_delivery.py --real --output .diagnostics/delivery-check-new
```

The live check uses private stores and three simple prompts. Passing is a smoke
check, not proof that a model will always follow style or use tools appropriately.
