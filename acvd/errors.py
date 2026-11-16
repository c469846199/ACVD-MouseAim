"""errors.py — 模块级异常。"""


class ACVDError(Exception):
    pass


class ProcessNotFound(ACVDError):
    pass


class AccessDenied(ACVDError):
    pass


class ReadFailed(ACVDError):
    pass


class WriteFailed(ACVDError):
    pass


class ScanFailed(ACVDError):
    pass