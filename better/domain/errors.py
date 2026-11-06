from better.domain.common import Contract
from better.domain.enums import ErrorCode


class ErrorDetail(Contract):
    code: ErrorCode
    message: str
    source: str | None = None
    retryable: bool = False


class BetterError(Exception):
    def __init__(self, code: ErrorCode, message: str, *, source: str | None = None):
        super().__init__(message)
        self.detail = ErrorDetail(
            code=code,
            message=message,
            source=source,
            retryable=code in {ErrorCode.TRANSIENT_PROVIDER_ERROR, ErrorCode.RATE_LIMIT, ErrorCode.TIMEOUT},
        )
