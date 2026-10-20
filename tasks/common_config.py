"""Common configuration for AgentHLE tasks."""

import os
from dataclasses import dataclass


@dataclass
class GeneralTaskConfig:
    """Base configuration for Windows tasks (default OS).

    Primary fields follow the canonical domain/task/variant hierarchy:
      - DOMAIN_NAME: top-level task family. One of:
            engineering, physical_sciences, life_sciences, health_medicine,
            psychology_neuro, business_finance, legal, visual_media,
            computing_math, transport_safety, education_info, agriculture_env,
            social_sciences, other
      - TASK_NAME:   task implementation id within the domain (e.g. "taxform_6_1")
      - VARIANT_NAME: one concrete runnable case (e.g. "variant_1" or "base")

    Paths use Windows backslash convention rooted at ``REMOTE_ROOT_DIR``
    (default ``E:\\agenthle``, overridable via the ``REMOTE_ROOT_DIR`` env var).
    """

    # Global settings
    REMOTE_OUTPUT_DIR: str = os.environ.get("REMOTE_OUTPUT_DIR", "output")
    REMOTE_ROOT_DIR: str = os.environ.get("REMOTE_ROOT_DIR", r"E:\agenthle")
    DOMAIN_NAME: str = ""
    TASK_NAME: str = ""
    VARIANT_NAME: str = ""
    OS_TYPE: str = os.environ.get("OS_TYPE", "windows")
    REQUIRES_TASK_DATA: bool = True

    @property
    def task_description(self) -> str:
        """Task description for the agent."""
        return ""

    @property
    def task_dir(self) -> str:
        """Generate task directory based on domain/task/variant."""
        return rf"{self.REMOTE_ROOT_DIR}\{self.DOMAIN_NAME}\{self.TASK_NAME}\{self.VARIANT_NAME}"

    @property
    def input_dir(self) -> str:
        """Agent-visible input directory."""
        return rf"{self.task_dir}\input"

    @property
    def software_dir(self) -> str:
        """Generate software directory."""
        return rf"{self.task_dir}\software"

    @property
    def remote_output_dir(self) -> str:
        """Output directory."""
        return rf"{self.task_dir}\{self.REMOTE_OUTPUT_DIR}"

    @property
    def reference_dir(self) -> str:
        """Reference directory."""
        return rf"{self.task_dir}\reference"

    @property
    def required_credentials(self) -> list[dict]:
        """Agent-side credentials this task needs at run time.

        Declarative tasks source these from ``task.yaml`` (see
        :class:`tasks.task_spec.TaskYamlConfig`, which overrides this). The base
        config declares none.
        """
        return []

    def to_metadata(self) -> dict:
        """Convert config to metadata dict for the framework Task."""
        meta = {
            "domain_name": self.DOMAIN_NAME,
            "task_name": self.TASK_NAME,
            "variant_name": self.VARIANT_NAME,
            "requires_task_data": self.REQUIRES_TASK_DATA,
            "task_dir": self.task_dir,
            "input_dir": self.input_dir,
            "software_dir": self.software_dir,
            "remote_output_dir": self.remote_output_dir,
            "reference_dir": self.reference_dir,
        }
        creds = self.required_credentials
        if creds:
            meta["required_credentials"] = creds
        return meta
