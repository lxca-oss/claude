"""cTrader Open API protobuf messages.

These generated modules are vendored from Spotware's official ``ctrader-open-api``
package (MIT licence, see LICENSE-spotware.txt). Only the import paths were changed so
the bot does not need Twisted or the package's pinned protobuf 3.20.
"""

from . import OpenApiCommonModelMessages_pb2 as common_model  # noqa: F401
from . import OpenApiCommonMessages_pb2 as common  # noqa: F401
from . import OpenApiModelMessages_pb2 as model  # noqa: F401
from . import OpenApiMessages_pb2 as messages  # noqa: F401
