"""Tool module aggregator: importing this package registers every
assistant tool in the registry. The orchestrator never imports tool
modules directly — it only ever sees the registry.
"""

from services.assistant import tools_read  # noqa: F401
from services.assistant import tools_search  # noqa: F401
from services.assistant import tools_write  # noqa: F401
