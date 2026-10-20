"""Shared Linux runtime helpers for Ubuntu-native tasks."""

import os
from dataclasses import dataclass

from tasks.common_config import GeneralTaskConfig


@dataclass
class LinuxTaskConfig(GeneralTaskConfig):
    """Base config for Ubuntu-native tasks.
    """
    REMOTE_ROOT_DIR: str = os.environ.get("REMOTE_ROOT_DIR", "/media/user/data/work")
    DOMAIN_NAME: str = ""
    OS_TYPE: str = "linux"
    VARIANT_NAME: str = "base"

    @property
    def task_dir(self) -> str:
        # Flat, generic container mount (matches the staging destination in
        # ``a2_run.environments.task_data.task_subdir``). The container holds
        # exactly one role of one variant, so the host's
        # ``<domain>/<task>/<variant>`` hierarchy carries no in-box information
        # and would only surface the task identity + the hidden variant (e.g.
        # ``access_b``) to an agent that runs ``ls`` on its data mount. The host
        # corpus stays structured; only the sandbox path is flattened.
        return self.REMOTE_ROOT_DIR

    @property
    def data_task_dir(self) -> str:
        return self.task_dir

    @property
    def input_dir(self) -> str:
        return f"{self.task_dir}/input"

    @property
    def reference_dir(self) -> str:
        return f"{self.task_dir}/reference"

    @property
    def software_dir(self) -> str:
        return f"{self.task_dir}/software"

    @property
    def remote_output_dir(self) -> str:
        return f"{self.task_dir}/{self.REMOTE_OUTPUT_DIR}"

    def to_metadata(self) -> dict:
        # Parent to_metadata() already pushes task_dir / input_dir /
        # software_dir / reference_dir / remote_output_dir via self.<prop>,
        # which resolves to the POSIX overrides above. Only add the keys
        # that are LinuxTaskConfig-specific.
        metadata = super().to_metadata()
        metadata.update(
            {
                "data_task_dir": self.data_task_dir,
            }
        )
        return metadata
