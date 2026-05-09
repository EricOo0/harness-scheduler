class SymphonyError(Exception):
    code = "symphony_error"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code


class WorkflowError(SymphonyError):
    pass


class ConfigError(SymphonyError):
    pass


class WorkspaceError(SymphonyError):
    pass


class TrackerError(SymphonyError):
    pass


class AgentError(SymphonyError):
    pass
