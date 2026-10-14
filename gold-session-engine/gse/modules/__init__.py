from .base import Module
from .module_a import ModuleA
from .module_b import ModuleB
from .module_c import ModuleC
from .module_d import ModuleD


def build_modules(engine) -> list[Module]:
    return [ModuleA(engine), ModuleB(engine), ModuleC(engine), ModuleD(engine)]


__all__ = ["Module", "ModuleA", "ModuleB", "ModuleC", "ModuleD", "build_modules"]
