"""The frozen bkg protocol: node/edge/IR vocabulary + canonical serializer.

Everything else (engine, adapters, store) imports this and never extends it.
"""

from . import canonical, enums, models

__all__ = ["canonical", "enums", "models"]
