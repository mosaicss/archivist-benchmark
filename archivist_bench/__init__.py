"""Archivist benchmark harness (harness 1.0.0 for the smoke runs; guards, resume, audit and
report for the codex campaign; grounding, provenance and the forum cross check for the grounding
campaign; the light Claude Code pass for the claude-code campaign; key corrections and the light
audit route of amendment 5 in harness 1.6.0).

Drives Codex CLI and Claude Code headless in three isolated arms (web only, Archivist only, both),
extracts exact usage and events, runs a blinded cross family judge (Claude Code and Codex on the
operator's plans) and computes paired bootstrap intervals and cost. Standard library only. Run
from the repository root as ``python3 -m archivist_bench <subcommand>``; see ``docs/HARNESS.md``.
"""

__version__ = "1.6.0"
