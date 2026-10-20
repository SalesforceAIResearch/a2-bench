"""Environments: sandbox-backed env + providers.

``A2Env`` (env.py) wraps a Provider + SandboxHandle. There is no step()
loop — the tested agent drives itself inside the sandbox.

Contents:
  - ``providers/docker.py``  DockerProvider (the only Provider).
  - ``images/``              Sandbox image registry.
  - ``task_data/``           Task-data staging backends.

Import as ``from a2_run.environments.env import A2Env``.
"""

__all__: list[str] = []
