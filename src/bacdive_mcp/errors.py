"""Errors safe to show to an MCP client. They never include credentials."""


class BacdiveError(Exception):
    def __init__(self, message: str, *, code: str = "bacdive_error", status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
