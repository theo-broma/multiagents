"""multiagents — any agent CLI as a subagent of any other.

Exposes opencode, Antigravity (``agy``) and Claude Code to each other through
MCP. Whichever one orchestrates is a line of config; the rest run as supervised
subagents with git worktree isolation, a shared agent tree, stream-level
supervision, quota-aware routing, and a blocking escalation path for decisions
only a human can make.
"""

__version__ = "0.1.0"
