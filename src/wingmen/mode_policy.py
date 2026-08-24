"""Agent-independent permission policies for a Wingmen roster."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

from wingmen.acp.agent import Mode


def _key(value: str) -> str:
    """Normalize adapter mode identifiers and labels for exact alias matching."""
    return re.sub(r"[^a-z0-9]", "", value.casefold())


@dataclass(frozen=True)
class ModePolicy:
    """A Wingmen-level intent and the adapter names known to implement it."""

    id: str
    name: str
    description: str
    aliases: frozenset[str]

    def resolve(self, modes: dict[str, Mode]) -> Mode | None:
        """Return the native mode implementing this policy, if advertised."""
        for mode in modes.values():
            if _key(mode.id) in self.aliases or _key(mode.name) in self.aliases:
                return mode
        return None

    @property
    def display_mode(self) -> Mode:
        return Mode(self.id, self.name, self.description)


MODE_POLICIES: tuple[ModePolicy, ...] = (
    ModePolicy(
        "wingmen:mode:plan",
        "Plan",
        "Read-only planning with no tool execution",
        frozenset({"plan", "planmode", "readonly"}),
    ),
    ModePolicy(
        "wingmen:mode:manual",
        "Manual",
        "Ask before operations that require permission",
        frozenset({"default", "manual", "ask"}),
    ),
    ModePolicy(
        "wingmen:mode:accept-edits",
        "Accept Edits",
        "Automatically approve file edits, but keep other safeguards",
        frozenset({"acceptedits", "autoedit", "autoapproveedits"}),
    ),
    ModePolicy(
        "wingmen:mode:full-access",
        "Fully Auto",
        "Automatically approve all tools and bypass permission prompts",
        frozenset(
            {"fullaccess", "yolo", "bypasspermissions", "skippermissions"}
        ),
    ),
)

POLICIES_BY_ID = {policy.id: policy for policy in MODE_POLICIES}
DEFAULT_MODE_POLICY_ID = "wingmen:mode:full-access"
MODE_ORDER = {
    "wingmen:discuss": 0,
    **{policy.id: index for index, policy in enumerate(MODE_POLICIES, start=1)},
}


def shared_modes(mode_sets: Iterable[dict[str, Mode]]) -> dict[str, Mode]:
    """Return semantic modes implemented by every supplied agent."""
    sets = list(mode_sets)
    if not sets:
        return {}
    return {
        policy.id: policy.display_mode
        for policy in MODE_POLICIES
        if all(policy.resolve(modes) is not None for modes in sets)
    }


def shared_current_mode(
    states: Iterable[tuple[dict[str, Mode], str | None]],
) -> Mode | None:
    """Return the common semantic mode, or ``None`` for mixed/native states."""
    state_list = list(states)
    if not state_list:
        return None
    for policy in MODE_POLICIES:
        if all(
            current_mode_id is not None
            and (native := policy.resolve(modes)) is not None
            and native.id == current_mode_id
            for modes, current_mode_id in state_list
        ):
            return policy.display_mode
    return None
