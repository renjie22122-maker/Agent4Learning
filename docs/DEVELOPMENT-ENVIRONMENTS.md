# Managed development environments

Environment preparation is a host operation, separate from the sandbox used by
ordinary commands. A missing sandbox dependency does not mean a host installation
is absent or damaged. Installing a skill does not install its dependencies.

## Workflow

1. `inspect_development_environment` returns host PATH hints without executing programs.
2. `list_development_environments` lists this project's registered plans and receipts.
3. `plan_development_environment` creates an immutable, versioned plan. It does not install anything.
4. `prepare_development_environment` requests approval of one exact host command,
   including its plan, target, probes and timeout. The user can reject it in chat.
5. On approval, the host prepares the environment, checks the version, runs minimal
   functional probes and records the observed results and file fingerprints.
6. Before reuse, `verify` checks the receipt and reruns approved probes. A failed or
   interrupted preparation is `needs_inspection`; it is not automatically reinstalled.
7. `retire` needs a separate approval. Managed files move to a recovery directory;
   pre-existing host installations are never deleted by this action.

## Supported plans

| Kind | Required information | Behavior |
|---|---|---|
| `existing` | Absolute executable, expected version, version arguments, smoke-test argument arrays | Register and test an existing runtime |
| `venv` | Absolute base Python, expected version, runtime path, exact `package==version` requirements | Create a private environment; install binary distributions; save resolved packages |
| `portable` | HTTPS ZIP/TAR URL, SHA-256, expected version, relative executable, probes | Verify and extract a portable distribution, suitable for JDK/Node and other runtimes |

Probes support `{runtime}`, `{env}` and `{scratch}` placeholders. They execute as
argument arrays, not shell fragments. Portable archives reject traversal, links,
special files and unsafe Windows names. Global PATH is not modified.

The host-only helper is `tools/manage_environment.py`. Agents should use the tools,
which enforce authority and produce the approval card, rather than invoking the
helper through a sandbox command.

## Limits and evidence

- A ready receipt means its probes passed at that time. It does not certify arbitrary
  future behavior or automatically grant the sandbox access to the runtime.
- Host commands run with the host user's permissions. A project working directory
  is not an OS isolation boundary.
- Existing-runtime fingerprints do not cover every external DLL or system dependency.
- Version pinning of direct Python requirements is not a complete reproducible lock;
  the receipt records resolved transitive packages after installation.
- Retirement is recoverable quarantine, not automatic garbage collection. External
  consumers of an environment still require operator coordination.
- Tests cover an existing Python, an actual empty venv, receipt tampering, interrupted
  state, unsafe archives and literal approval arguments. No JDK/Node download or
  global package installation has been claimed as verified in this change.

```powershell
python -m unittest tools.test_dev_environments tools.test_approvals tools.test_host_environment
```
