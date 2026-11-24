# ProcureAI agentic intake feature

This patch adds the first agentic vertical slice to the existing Python procurement project.

Run from this directory with `python -m agentic.api`. It uses only the Python standard library and persists requests in `procureai_agent.db`.

The first version extracts and validates plain-language procurement requests. It is intentionally not yet an external supplier search agent; that is the next stage after request intake is validated.
