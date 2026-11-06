import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from astrbot.api import logger  # noqa: F401
except Exception:
    class _NullLogger:
        def debug(self, *args, **kwargs):
            pass

        def info(self, *args, **kwargs):
            pass

        def warning(self, *args, **kwargs):
            pass

        def error(self, *args, **kwargs):
            pass

        def exception(self, *args, **kwargs):
            pass

        def critical(self, *args, **kwargs):
            pass

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = _NullLogger()
    astrbot.api = api
    sys.modules.setdefault("astrbot", astrbot)
    sys.modules.setdefault("astrbot.api", api)
