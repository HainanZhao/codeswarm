"""Runtime defaults for CodeSwarm's small internal settings store.

The `/config` screen renders the user preferences below; the launcher roster
is retained here as internal launch state and is managed by the agent store.
"""

from codeswarm.settings import SchemaDict

SCHEMA: list[SchemaDict] = [
    {
        "key": "ui",
        "type": "object",
        "fields": [
            {
                "key": "theme",
                "type": "string",
                "default": "codeswarm-black",
                "editable": False,
            },
            {
                "key": "prompt_message",
                "type": "string",
                "default": "How can I help you today?",
            },
            {"key": "density", "type": "string", "default": "comfortable"},
            {"key": "scrollbar", "type": "string", "default": "normal"},
            {"key": "flash_duration", "type": "number", "default": 3.0},
            {
                "key": "prune_low_mark",
                "type": "integer",
                "default": 1500,
                "editable": False,
            },
            {
                "key": "prune_excess",
                "type": "integer",
                "default": 1000,
                "editable": False,
            },
        ],
    },
    {
        "key": "notifications",
        "type": "object",
        "fields": [
            {"key": "system", "type": "string", "default": "blur"},
            {"key": "blink_title", "type": "boolean", "default": True},
            {"key": "enable_sounds", "type": "boolean", "default": True},
            {"key": "turn_over", "type": "boolean", "default": True},
            {
                "key": "hide_low_severity",
                "type": "boolean",
                "default": True,
                "editable": False,
            },
        ],
    },
    {
        "key": "agent",
        "type": "object",
        "fields": [
            {"key": "thoughts", "type": "boolean", "default": False},
        ],
    },
    {
        "key": "tools",
        "type": "object",
        "fields": [
            {"key": "expand", "type": "string", "default": "fail"},
        ],
    },
    {
        "key": "diff",
        "type": "object",
        "fields": [
            {"key": "view", "type": "string", "default": "auto"},
            {
                "key": "annotations",
                "type": "boolean",
                "default": False,
                "editable": False,
            },
            {"key": "wrap", "type": "string", "default": "no-wrap"},
        ],
    },
    {
        "key": "launcher",
        "type": "object",
        "fields": [
            {"key": "roster", "type": "string", "default": ""},
        ],
    },
]
