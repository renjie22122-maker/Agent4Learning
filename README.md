# Agent4Learning

**An agent engineering lab with a runnable, real-model execution runtime.**

[中文说明](README.zh-CN.md) · [Current capabilities](docs/CURRENT.md) ·
[Interface language](docs/UI-LANGUAGE.md) · [Development environments](docs/DEVELOPMENT-ENVIRONMENTS.md)

The repository combines controlled teaching experiments with an interactive agent.
The experiments demonstrate runtime mechanisms; their simulated workloads are not
proof of real-world model quality, production security or superiority to other agents.

## Start

Python 3.11 or later is recommended. From the repository directory on Windows:

```powershell
python tools/start_desktop.py
```

The desktop launcher opens the authenticated local interface. Configure your model
endpoint and credentials in **Model & API settings**. If you need to reopen the
local login entry, run `python -m agentplat.knowledge_cli open`.

The interface defaults to English and offers a **中文** switch in the header.
Replies follow the user's language. General chats have private attachment/artifact
storage; select a project to work on local directories. Projects can contain multiple
non-overlapping folders. A skill import supplies instructions, not its external tools,
accounts or runtime dependencies.

## What the runtime provides

- Tool execution, streaming chat, follow-up messages during work, approval cards,
  scoped files, conversation branches and recovery records.
- Configurable AppContainer, Docker or explicit local execution. These are distinct
  security boundaries; a workspace path alone is not a process sandbox.
- Recursive sub-agents and team DAGs with scoped context, messages, review and
  conflict-aware merge. Delegation quality still depends on the model.
- Independent acceptance tied to requirements and artifact versions. Host verdicts
  are separate from the author's final answer.
- Document import, scoped knowledge sources, lexical/vector retrieval and optional
  ANN indexing; confirmed memory with validity, revision and explicit conflict links.
- OpenAI-compatible Chat Completions plus native Responses, Anthropic and Gemini
  text/function-tool adapters. Native adapters currently have protocol tests, not
  live-provider certification; unsupported features fail explicitly.
- Approved, versioned development-environment plans, probes, reuse checks and
  recoverable retirement. Host preparation does not silently expand sandbox rights.

See [CURRENT.md](docs/CURRENT.md) for limitations and verification evidence.

## Learn and test

```powershell
python verify.py --list
python verify.py
python tools/test_offline.py
python -m unittest tools.test_runtime_contracts tools.test_native_protocols tools.test_lifecycle_contract
```

Optional checks require their corresponding environment. Skipped integration tests
are not passed tests. Browser and native isolation tests need supported OS facilities.

For a paid, isolated task benchmark using the configured model:

```powershell
python tools/benchmark_agent.py --real --review --review-profile balanced --runs 3 --jobs 3 --timeout 300 --max-steps 40 --output .diagnostics/benchmark-new
```

The timeout and step limit above belong to this benchmark, not a hidden universal
limit on interactive conversations. Do not reuse an output directory. Report
artifact correctness, workflow completion, false success, latency and parent/child
cost separately. Public repository tasks are not an unseen external benchmark.

## Project map

| Directory | Contents |
|---|---|
| `agentplat/` | Interactive runtime, model adapters, tools, UI and host controls |
| `agentlab/` | Teaching models and experimental infrastructure |
| `labs/` | Controlled failure/fix experiments |
| `tools/` | Launchers, regression tests, live checks and evaluation runners |
| `docs/` | Usage guides, architecture notes and dated evidence |

English is the primary language for current entry-point documentation and new core
contracts. The previous Chinese overview and dated reports remain available; historic
results are not rewritten to describe the current implementation.

## Privacy and publication

Keep credentials, private sessions, attachments, knowledge stores, model weights,
diagnostic logs and downloaded skill archives out of Git. Local `.diagnostics/`
references identify evidence on the development machine, not files available in a
public clone. Never treat a model's self-assessment as independent evidence.
