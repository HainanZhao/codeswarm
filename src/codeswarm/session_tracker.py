class SessionTracker:
    """Allocate and track the screen modes for live conversations."""

    def __init__(self) -> None:
        self.sessions: set[str] = set()
        self._session_index = 0

    def new_session(self) -> str:
        self._session_index += 1
        mode_name = f"session-{self._session_index}"
        self.sessions.add(mode_name)
        return mode_name

    def close_session(self, mode_name: str) -> None:
        self.sessions.discard(mode_name)
