import logging
import re
from collections.abc import Iterable


SENSITIVE_KEYS = {"api_key", "authorization", "password", "token", "secret"}


def _mask_value(value: str) -> str:
    if len(value) <= 4:
        return "****"
    return f"{value[:2]}***{value[-2:]}"


def mask_sensitive(data: object) -> object:
    if isinstance(data, dict):
        return {
            key: _mask_value(str(value)) if key.lower() in SENSITIVE_KEYS else mask_sensitive(value)
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [mask_sensitive(item) for item in data]
    if isinstance(data, tuple):
        return tuple(mask_sensitive(item) for item in data)
    if isinstance(data, str):
        return _mask_in_text(data)
    return data


def _mask_in_text(message: str) -> str:
    pattern = re.compile(r"(?i)(api_key|authorization|password|token|secret)\s*[:=]\s*([^\s,]+)")

    def _replace(match: re.Match) -> str:
        return f"{match.group(1)}={_mask_value(match.group(2))}"

    return pattern.sub(_replace, message)


class MaskingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _mask_in_text(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = mask_sensitive(record.args)
            elif isinstance(record.args, Iterable):
                record.args = tuple(mask_sensitive(arg) for arg in record.args)
        return True


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(level=level)
    logger = logging.getLogger()
    logger.addFilter(MaskingFilter())