# Copyright (c) 2026, Shrihari Mahabal and contributors
# For license information, please see license.txt

# Re-exported so the after_migrate hook stays reachable at flow.assistant.<name>.
from flow.assistant.assistant import (
	ASSISTANT_AGENT_TITLE,
	ASSISTANT_INSTRUCTIONS,
	sync_builtin_assistant,
)

__all__ = ["ASSISTANT_AGENT_TITLE", "ASSISTANT_INSTRUCTIONS", "sync_builtin_assistant"]
