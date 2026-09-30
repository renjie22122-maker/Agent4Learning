# Current implementation

Checked against the working tree on **2026-09-30**. This is a capability and limitation
index, not a production certification. [Previous Chinese guide](CURRENT.zh-CN.md).
Dated reports describe the version they inspected and remain historical records.

## Using the agent

Run `python tools/start_desktop.py`, configure the LLM endpoint in settings and use
the authenticated local interface. English is the default UI language; the header
selector switches to Chinese. Replies follow the user's language. Final answers
present the deliverable directly, with host acceptance shown separately.

General chats have private artifact storage and no project shell privileges. Project
chats can bind several non-overlapping folders. Follow-up messages and approval
cards appear in the execution timeline; recovery does not blindly replay operations
whose side effects are unknown.

## Capabilities and boundaries

| Area | Current implementation and limits |
|---|---|
| Model protocols | Chat Completions client split from protocol/error handling; native Responses, Anthropic and Gemini text/function tools. Opaque reasoning/signature data retained. Native streaming, vision and server-side tools are not implemented by these adapters. |
| Task lifecycle | Explicit event projection and completion guards for pending questions and unknown side effects. This is not yet a single authoritative state engine replacing every subsystem. |
| Sandboxing | Backend-specific boundary information; file inventories reject links/reparse points and hardlinks. Preflight checks do not prove resistance to a concurrent privileged attacker. Host-approved commands run outside the ordinary sandbox. |
| Environment preparation | Versioned existing/venv/portable plans, exact-command approvals, probes, fingerprints and recoverable retirement. See [environment management](DEVELOPMENT-ENVIRONMENTS.md). |
| Review | Requirement-bound finite plans, independent assumptions and source checks, revision-bound acceptance. No guarantee that model reviewers catch all defects. |
| Team execution | Recursive workers, messages and DAG merge; common host verdict validation. A revised task must actually change its task/acceptance contract. Optimal delegation has not been demonstrated. |
| Knowledge | Text/CSV/Office/PDF/OCR import; selected session/project/public scopes; hybrid retrieval and optional ANN. Real large-corpus answer quality remains an evaluation task. |
| Memory | User-confirmed entries, semantic retrieval, validity dates, revisions and explicit conflict/duplicate/supersession relations. No autonomous learning or complete temporal reasoning. |
| Spill retention | SHA-256 reuse checks and a read-only retention audit with active/reference protection. No comprehensive automatic deletion or lifecycle GC. |
| UI and prompts | General English core instructions, English/Chinese UI controls, direct final answers. User content and historic diagnostics are not translated. See [language contract](UI-LANGUAGE.md). |

Knowledge and skills: [knowledge scopes](KNOWLEDGE-SCOPES.md), [hybrid RAG](RAG_HYBRID.md),
[skills](SKILLS.md). Safety: [native sandbox](NATIVE-SANDBOX.md),
[permission recovery](PERMISSION-RECOVERY.md). Existing guides may retain Chinese text.

## Verification performed

- Affected model/runtime/review/recovery regression group: **83 tests, one skipped**.
- New UI/delivery, environment, lifecycle and native-protocol group: **29 tests passed**.
- Following the prompt/delivery changes: **47 related tests passed**.
- Real browser language check: English default, Chinese persistence, dynamic labels,
  protected conversation/code/drafts/data and no JavaScript errors.
- Three isolated real-model plain-answer checks: all completed in one model call,
  no tool calls or files, no forced summary opener in the first smoke run.
  After tightening brevity guidance, all three passed again; one used only `finish`,
  and no files were created. The short Chinese explanation followed the requested
  two-sentence format, while the code answer still included some unsolicited detail. Chinese and English reply prompts
  are included. These simple samples do not establish general agent reliability.
- A real-model environment check registered the exact existing-runtime plan in an
  isolated registry (five model calls); no host command was executed. Its first run
  exposed tool registration being incorrectly coupled to sub-agent enablement; this
  was fixed, with 32 related regression tests passing.
- Environment tests created and verified an actual empty venv and an existing Python;
  malicious archive paths, receipt tampering and literal argv approvals were checked.
  No real JDK/Node installation was performed.
- Native sandbox integration tests requiring an elevated host were skipped in the
  non-elevated test run. A skip is not evidence of security or a passed integration.
- Native non-Chat-Completions providers were tested with protocol fixtures; no real
  Anthropic/Gemini/Responses credentials were exercised.

These groups overlap and must not be added together as a unique full-suite count.

The isolated offline runner also exercised **68 test modules**. Three outdated
fixtures were corrected (source-bound RAG positives, the removed summary heading,
and a mock session missing lifecycle state); those modules passed focused reruns.
Two modules include explicit skips for unavailable link/history evidence. This is
not a claim that every optional integration ran.

## Real task sweep: negative results retained

The local `runtime-boundaries-stratified-v1` sweep ran **15 tasks × 3 trials**:

| Measure | Result |
|---|---:|
| Artifact grading passed | 35 / 45 |
| Artifact and workflow both completed | 34 / 45 |
| Declared success but hidden grading failed | 4 |
| Timed out | 6 |

Three false successes concerned CSV numeric precision; one concerned nested JSON
merge behavior. Two interval-task timeouts waited for approval of an introduced pytest
dependency. Other timeouts require their own evidence-based classification. This is
not an all-green result. Completed-trial cost estimates total about **$0.71994**;
six timed-out trials lack complete cost rows, so that figure is not the full bill.

The sweep used public repository tasks, not unseen held-out tasks. Development
continued while workers were being launched; per-worker source fingerprints were
not captured. Treat it as exploratory failure discovery, **not a controlled comparison
of one frozen revision**. Original outputs stay unchanged in the local diagnostics
folder. Earlier RAG grader corrections are separately recorded regrades, not new trials.

## Next priorities

- Resolve remaining review false successes and distinguish approval waits from
  model/execution timeouts without approving host commands automatically.
- Run a frozen-revision, held-out evaluation with complete parent/child accounting.
- Expand native-provider live coverage only with the corresponding configured accounts.
- Continue dependency-boundary refactoring, safe artifact lifecycle management and
  measured delegation improvements rather than claiming them from module counts.

Private diagnostics, chats, keys, attachment stores and downloaded skill bundles are
not part of the public repository. Publishing documentation does not publish their contents.
