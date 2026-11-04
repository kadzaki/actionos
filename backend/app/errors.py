from dataclasses import dataclass, field


@dataclass
class AppError(Exception):
    code: str
    message: str
    status: int = 400
    retryable: bool = False
    details: dict = field(default_factory=dict)

    def envelope(self):
        return {"code": self.code, "message": self.message,
                "details": self.details, "retryable": self.retryable}
