"""Hermes tool schemas for Prime Agent minions."""

_SESSION_ID = {
    "type": "string",
    "pattern": "^minion_[0-9a-f]{32}$",
    "description": "Opaque resumable minion session identifier returned by delegate_minion.",
}

DELEGATE_MINION = {
    "name": "delegate_minion",
    "description": (
        "Run a Prime Agent coding/research minion in a local workspace. Prime owns the "
        "agent/tool loop; all LLM requests are streamed through Hermes-managed OpenAI Codex "
        "OAuth. Choose provider, model, and reasoning effort per task. Set session_mode to "
        "resumable to persist the transcript, then pass the returned session_id to continue it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "Task or follow-up for the minion."},
            "workdir": {
                "type": "string",
                "description": "Existing local directory the minion may read and modify.",
            },
            "provider": {
                "type": "string",
                "enum": ["openai-codex"],
                "default": "openai-codex",
            },
            "model": {
                "type": "string",
                "enum": ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"],
                "default": "gpt-5.6-terra",
            },
            "reasoning_effort": {
                "type": "string",
                "enum": ["none", "low", "medium", "high", "xhigh", "max"],
                "default": "high",
            },
            "timeout_seconds": {
                "type": "integer",
                "minimum": 30,
                "maximum": 7200,
                "description": "Hard wall-clock timeout for the whole minion task.",
            },
            "session_mode": {
                "type": "string",
                "enum": ["ephemeral", "resumable"],
                "default": "ephemeral",
                "description": "Persist the Prime transcript only when set to resumable.",
            },
            "session_id": _SESSION_ID,
        },
        "required": ["task", "workdir"],
        "additionalProperties": False,
    },
}

MINION_SESSION_STATUS = {
    "name": "minion_session_status",
    "description": "Read sanitized durable state for one resumable Prime minion session.",
    "parameters": {
        "type": "object",
        "properties": {"session_id": _SESSION_ID},
        "required": ["session_id"],
        "additionalProperties": False,
    },
}

CLOSE_MINION_SESSION = {
    "name": "close_minion_session",
    "description": (
        "Close a resumable Prime minion session without deleting its transcript. "
        "Closed sessions cannot be resumed."
    ),
    "parameters": {
        "type": "object",
        "properties": {"session_id": _SESSION_ID},
        "required": ["session_id"],
        "additionalProperties": False,
    },
}

__all__ = ["CLOSE_MINION_SESSION", "DELEGATE_MINION", "MINION_SESSION_STATUS"]
