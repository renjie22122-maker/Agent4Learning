"""Shared review scope rules for single-agent and team acceptance."""
INSTRUCTIONS = (
    'Separate explicit user requirements, implementation assumptions and unspecified behavior. '
    'Every blocking check must identify its requirement. Neither narrow permitted inputs '
    'to suit the implementation nor expand the contract to infinite inputs or unrequested performance guarantees. '
    'For follow-ups, check original requirements, new requirements and their interaction. '
    'Independently challenge a relevant implementation assumption; author tests are leads only. '
    'Optional probes must not delay delivery. Run potentially hanging checks in a separate '
    'process with a short timeout; confirm contractual relevance before retrying. '
    'Write tests in a new create_verification_scratch directory, never over existing artifacts. '
    'Check original knowledge sources or authorized snapshots, not author transcriptions. '
    'Run a minimal check early, finish a finite plan, and report each requirement, check and observed result. '
)
