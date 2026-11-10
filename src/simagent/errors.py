"""Errors that distinguish invalid plans, known tool failures and uncertain jobs."""


class SimAgentError(Exception):
    pass


class PlanValidationError(SimAgentError):
    def __init__(self, issues: list[str]) -> None:
        self.issues = issues
        super().__init__("\n".join(issues))


class ToolExecutionError(SimAgentError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        self.retryable = retryable
        super().__init__(message)


class JobNotFoundError(SimAgentError):
    pass


class RecoveryRequiredError(SimAgentError):
    pass


class RunBusyError(SimAgentError):
    pass
