"""Provider implementations for sandbox lifecycle.

``Provider`` ABC + ``SandboxSpec`` + ``SandboxHandle`` + ``ReleaseMode`` live in
:mod:`a2_run.base_interface`; this package only holds the backends:

  - :class:`DockerProvider` (``docker.py``): ephemeral local Docker containers.
"""

from ...base_interface import SandboxSpec, Provider, ReleaseMode, SandboxHandle
from .docker import DockerProvider, DockerProviderConfig

__all__ = [
    "SandboxSpec",
    "DockerProvider",
    "DockerProviderConfig",
    "Provider",
    "ReleaseMode",
    "SandboxHandle",
]
