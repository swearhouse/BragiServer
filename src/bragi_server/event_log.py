import json
import logging
import os
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Literal


EventCategory = Literal["critical_warning", "resolve_error", "exception"]

MAX_EVENTS = 1000
_EVENT_LOG_LOCK = Lock()


class EventLogError(Exception):
    """Raised when the persistent Bragi event log cannot be read or written."""


class BragiEventLog:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        with _EVENT_LOG_LOCK:
            data = self._read()
            self._write(data)

    def record(
        self,
        category: EventCategory,
        message: str,
        *,
        context: dict[str, object] | None = None,
        exception: BaseException | None = None,
    ) -> None:
        if category not in {"critical_warning", "resolve_error", "exception"}:
            raise ValueError(f"Unsupported event category: {category}.")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("Event message must be a non-empty string.")
        if category == "exception" and exception is None:
            raise ValueError("Exception events require an exception instance.")

        event: dict[str, object] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "category": category,
            "severity": {
                "critical_warning": "CRITICAL",
                "resolve_error": "ERROR",
                "exception": "ERROR",
            }[category],
            "message": message.strip(),
        }
        if context is not None:
            event["context"] = context
        if exception is not None:
            event["exception"] = {
                "type": type(exception).__name__,
                "traceback": "".join(
                    traceback.format_exception(
                        type(exception),
                        exception,
                        exception.__traceback__,
                    )
                ),
            }

        try:
            json.dumps(event, ensure_ascii=False)
        except (TypeError, ValueError) as error:
            raise ValueError("Event context must contain JSON-compatible data.") from error

        with _EVENT_LOG_LOCK:
            data = self._read()
            data["events"].append(event)
            data["events"] = data["events"][-MAX_EVENTS:]
            self._write(data)

    def _read(self) -> dict[str, list[dict[str, object]]]:
        if not self.path.exists():
            return {"events": []}

        try:
            with self.path.open(encoding="utf-8") as log_file:
                data = json.load(log_file)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise EventLogError("Could not read the Bragi event log.") from error

        if data == {}:
            return {"events": []}
        if (
            not isinstance(data, dict)
            or set(data) != {"events"}
            or not isinstance(data.get("events"), list)
            or any(not isinstance(event, dict) for event in data["events"])
        ):
            raise EventLogError("The Bragi event log has an invalid format.")
        return data

    def _write(self, data: dict[str, list[dict[str, object]]]) -> None:
        temporary_path = None
        try:
            data["events"] = data["events"][-MAX_EVENTS:]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                delete=False,
            ) as log_file:
                temporary_path = Path(log_file.name)
                json.dump(data, log_file, ensure_ascii=False, indent=2)
                log_file.write("\n")
            os.replace(temporary_path, self.path)
        except (OSError, TypeError, ValueError) as error:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    logging.getLogger(__name__).exception(
                        "Could not clean up temporary Bragi event log file"
                    )
            raise EventLogError("Could not save the Bragi event log.") from error
