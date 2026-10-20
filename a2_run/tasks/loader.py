"""Task definition loader.

Reads ``main.py`` under each task dir and produces normalised task metadata.
Large corpus tasks may expose ``variant_names()`` and ``load_variant(index)``
instead of constructing every variant through ``load()``.
Declarative setup — identity, briefs, agent cards, and the ``vm`` machine spec
— lives in the sibling ``task.yaml`` and reaches the loader on the ``Task``
object (``Task.vm`` for the machine spec). ``TaskDataSpec`` lives in
:mod:`a2_run.base_interface`.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict

from ..base_interface import TaskDataSpec

__all__ = ["TaskDataSpec", "TaskLoader"]

logger = logging.getLogger(__name__)

# Task-local bare-name imports declared by the Stage 2 hard rule.
_TASK_LOCAL_MODULE_NAMES = (
    "score_outputs",
    "verify_outputs",
)

_TASK_IMPORT_LOCK = threading.Lock()


# ======================================================================
# TaskLoader
# ======================================================================


class TaskLoader:
    def __init__(self, task_path: str):
        self.task_path = Path(task_path).resolve()
        self.main_py = self.task_path / "main.py"
        if not self.main_py.exists():
            raise FileNotFoundError(f"main.py not found at {self.main_py}")
        self._module = None

    def _load_task_variant(self, variant_index: int = 0) -> Any | None:
        module = self._load_module()
        load_variant_fn = getattr(module, "load_variant", None)
        if load_variant_fn is not None:
            if not callable(load_variant_fn):
                raise TypeError(f"{self.main_py}: load_variant must be callable")
            return load_variant_fn(variant_index)
        load_fn = getattr(module, "load", None)
        if load_fn is None or not callable(load_fn):
            return None
        tasks = load_fn() or []
        if tasks and len(tasks) > variant_index:
            return tasks[variant_index]
        return None

    def variant_names(self) -> list[str]:
        """Ordered variant names this task declares (``names[i]`` is variant i).

        Read from each variant's ``metadata['variant_name']`` — the same string
        the run-folder slug uses — so an experiment yaml can select variants by
        NAME instead of by positional index. Empty when the task exposes no
        ``load()`` (single-``config`` tasks); callers fall back to index
        selection in that case.
        """
        module = self._load_module()
        variant_names_fn = getattr(module, "variant_names", None)
        if variant_names_fn is not None:
            if not callable(variant_names_fn):
                raise TypeError(f"{self.main_py}: variant_names must be callable")
            names = variant_names_fn() or []
            if not isinstance(names, list) or not all(
                isinstance(name, str) for name in names
            ):
                raise TypeError(f"{self.main_py}: variant_names() must return list[str]")
            return names
        load_fn = getattr(module, "load", None)
        if load_fn is None or not callable(load_fn):
            return []
        names: list[str] = []
        for task in load_fn() or []:
            md = getattr(task, "metadata", None) or {}
            names.append(str(md.get("variant_name") or ""))
        return names

    def _load_module(self):
        if self._module is not None:
            return self._module

        rel_parts = self.task_path.parts
        try:
            tasks_idx = rel_parts.index("tasks")
            unique_suffix = "_".join(rel_parts[tasks_idx + 1 :])
        except ValueError:
            unique_suffix = self.task_path.name
        module_name = f"_task_{unique_suffix}"

        with _TASK_IMPORT_LOCK:
            scripts_dir = str(self.task_path / "scripts")
            task_dir = str(self.task_path)

            for mod_name in _TASK_LOCAL_MODULE_NAMES:
                sys.modules.pop(mod_name, None)

            added_paths = []
            for p in (scripts_dir, task_dir):
                if os.path.isdir(p):
                    try:
                        sys.path.remove(p)
                    except ValueError:
                        pass
                    sys.path.insert(0, p)
                    added_paths.append(p)

            modules_before = set(sys.modules.keys())

            # Task-local modules (verify_outputs/score_outputs) are NOT
            # eagerly executed here. They live in scripts/ which is on
            # sys.path (above) for the duration of main.py's load, so a
            # task that genuinely does ``import verify_outputs`` resolves
            # it lazily via the normal import machinery. Eager execution
            # would run their top-level imports (e.g. rasterio, netCDF4)
            # on the host even for tasks that only ship the scripts as
            # text into the sandbox and never import them — forcing those
            # sandbox-only deps into the host venv. The pop-before /
            # pop-after bookkeeping below still guarantees cross-task
            # cache isolation for the bare module names.

            try:
                spec = importlib.util.spec_from_file_location(module_name, str(self.main_py))
                if spec is None or spec.loader is None:
                    raise ImportError(f"Cannot create module spec for {self.main_py}")

                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                self._module = module
                return module
            finally:
                for mod_name in _TASK_LOCAL_MODULE_NAMES:
                    sys.modules.pop(mod_name, None)
                _task_prefixes = tuple(os.path.abspath(p) + os.sep for p in added_paths)
                for k in set(sys.modules.keys()) - modules_before:
                    if k == module_name:
                        continue
                    mod = sys.modules.get(k)
                    origin = getattr(getattr(mod, "__spec__", None), "origin", None) or ""
                    f = getattr(mod, "__file__", None) or ""
                    src = os.path.abspath(origin or f) if (origin or f) else ""
                    if src and any(src.startswith(tp) for tp in _task_prefixes):
                        del sys.modules[k]
                for p in added_paths:
                    try:
                        sys.path.remove(p)
                    except ValueError:
                        pass

    def load(self, variant_index: int = 0) -> Dict[str, Any]:
        module = self._load_module()
        config = getattr(module, "config", None)

        try:
            task = self._load_task_variant(variant_index=variant_index)
            if task is not None:
                description = getattr(task, "description", "")
                metadata = getattr(task, "metadata", {}) or {}
                computer = getattr(task, "computer", {}) or {}
                logger.info("Loaded task config via load() function")
                task_data = self._extract_task_data(metadata=metadata, config=config)
                task_info = {
                    "description": description,
                    "metadata": metadata,
                    "computer": computer,
                    "os_type": self._extract_os_type(task=task, config=config),
                    "task_data": task_data,
                }
                # A2a (pair) tasks declare per-role briefs instead of a
                # single description: ``roles`` maps role name → brief and
                # ``first_speaker`` names the role that opens the exchange.
                roles = getattr(task, "roles", None) or {}
                if roles:
                    if not isinstance(roles, dict):
                        raise TypeError(
                            f"task {self.task_path}: roles must be a dict "
                            f"of role→brief, got {type(roles).__name__}"
                        )
                    first_speaker = getattr(task, "first_speaker", None)
                    if not first_speaker or first_speaker not in roles:
                        raise ValueError(
                            f"task {self.task_path}: first_speaker "
                            f"{first_speaker!r} must name one of the roles "
                            f"{sorted(roles)}"
                        )
                    task_info["roles"] = {str(k): str(v) for k, v in roles.items()}
                    task_info["first_speaker"] = str(first_speaker)
                    # A2A framing (optional, validated here): the task is
                    # assigned to one role; peers are described by agent
                    # cards (dicts in the AgentCard.from_dict shape).
                    assigned_role = getattr(task, "assigned_role", None)
                    if assigned_role is not None:
                        if str(assigned_role) not in roles:
                            raise ValueError(
                                f"task {self.task_path}: assigned_role "
                                f"{assigned_role!r} must name one of the "
                                f"roles {sorted(roles)}"
                            )
                        task_info["assigned_role"] = str(assigned_role)
                    assignment = getattr(task, "assignment", None)
                    if assignment:
                        task_info["assignment"] = str(assignment)
                    agent_cards = getattr(task, "agent_cards", None) or {}
                    if agent_cards:
                        if not isinstance(agent_cards, dict) or not (
                            set(agent_cards) <= set(roles)
                        ):
                            raise ValueError(
                                f"task {self.task_path}: agent_cards keys "
                                f"{sorted(agent_cards)} must be a subset of "
                                f"the roles {sorted(roles)}"
                            )
                        task_info["agent_cards"] = {
                            str(k): dict(v) for k, v in agent_cards.items()
                        }
                    # Per-role peer allow-list (visibility graph). Resolved
                    # and symmetry-checked task-side; here we only enforce that
                    # every key/peer is a declared role before it reaches the
                    # roster builder.
                    visibility = getattr(task, "visibility", None) or {}
                    if visibility:
                        if not isinstance(visibility, dict) or not (
                            set(visibility) <= set(roles)
                        ):
                            raise ValueError(
                                f"task {self.task_path}: visibility keys "
                                f"{sorted(visibility)} must be a subset of "
                                f"the roles {sorted(roles)}"
                            )
                        bad = {
                            p for peers in visibility.values() for p in peers
                            if p not in roles
                        }
                        if bad:
                            raise ValueError(
                                f"task {self.task_path}: visibility names "
                                f"unknown peer(s) {sorted(bad)}; roles are "
                                f"{sorted(roles)}"
                            )
                        task_info["visibility"] = {
                            str(k): [str(p) for p in v]
                            for k, v in visibility.items()
                        }
                    # Per-role network egress policy for this variant
                    # ({role: "restricted"}). Host-side only — never staged or
                    # shown to an agent. The concrete allowlist is derived in
                    # pair_lifecycle from the harness bound to each role.
                    egress = getattr(task, "egress", None) or {}
                    if egress:
                        if not isinstance(egress, dict) or not (
                            set(egress) <= set(roles)
                        ):
                            raise ValueError(
                                f"task {self.task_path}: egress keys "
                                f"{sorted(egress)} must be a subset of "
                                f"the roles {sorted(roles)}"
                            )
                        task_info["egress"] = {
                            str(k): str(v) for k, v in egress.items()
                        }
                return self._enrich_with_vm(
                    task_info, getattr(task, "vm", None))
        except Exception as e:
            logger.warning(f"Failed to call load(): {e}")

        if config is not None and hasattr(config, "task_description"):
            description = config.task_description
            metadata = config.to_metadata() if hasattr(config, "to_metadata") else {}
            logger.info("Loaded task config from module-level 'config' object")
            task_data = self._extract_task_data(metadata=metadata, config=config)
            return self._enrich_with_vm(
                {
                    "description": description,
                    "metadata": metadata,
                    "os_type": self._extract_os_type(config=config),
                    "task_data": task_data,
                },
                None,
            )

        for attr_name in dir(module):
            obj = getattr(module, attr_name)
            if (
                isinstance(obj, type)
                and attr_name.endswith("Config")
                and attr_name != "GeneralTaskConfig"
                and hasattr(obj, "task_description")
            ):
                try:
                    instance = obj()
                    description = instance.task_description
                    metadata = instance.to_metadata() if hasattr(instance, "to_metadata") else {}
                    logger.info(f"Loaded task config from class {attr_name}")
                    task_data = self._extract_task_data(metadata=metadata, config=instance)
                    return self._enrich_with_vm(
                        {
                            "description": description,
                            "metadata": metadata,
                            "os_type": self._extract_os_type(config=instance),
                            "task_data": task_data,
                        },
                        None,
                    )
                except Exception as e:
                    logger.warning(f"Failed to instantiate {attr_name}: {e}")

        raise RuntimeError(
            f"Could not extract task config from {self.main_py}. "
            f"Expected a module-level 'config' object, a *Config class, "
            f"or a load() function."
        )

    def _enrich_with_vm(self, task_info: dict, vm: dict | None) -> dict:
        """Map the task's ``vm`` block onto the top-level image/timeout/size keys.

        ``vm`` is supplied by the task itself (``Task.vm``, sourced from
        ``task.yaml``); the loader no longer reads a task_card file.
        """
        vm_cfg = vm or {}
        if not vm_cfg and (task_info.get("snapshot")):
            vm_cfg = {"snapshot": task_info.get("snapshot")}
        if vm_cfg:
            # Snapshot tag is the *logical* identifier the task asks for
            # (e.g. ``cpu-free``, ``gpu-free``). The gcloud Provider's yaml
            # maps it to a concrete image + machine_type fallback list.
            # Validation against the provider's map happens at acquire time
            # — TaskLoader doesn't know which provider will be used.
            task_info["image_category"] = vm_cfg.get("snapshot")
            task_info["snapshot_name"] = vm_cfg.get("snapshot")
            raw_timeout = vm_cfg.get("timeout_s", vm_cfg.get("timeout"))
            if raw_timeout is not None:
                task_info["timeout_s"] = self._parse_task_timeout(raw_timeout)
            for field_name in ("vcpus", "memory_gb"):
                raw_value = vm_cfg.get(field_name)
                if raw_value is not None:
                    task_info[field_name] = self._parse_positive_int(
                        field_name,
                        raw_value,
                    )
            task_info["machine_type"] = vm_cfg.get("machineType")
        return task_info

    def _parse_positive_int(self, field_name: str, raw_value: Any) -> int:
        if isinstance(raw_value, bool):
            raise ValueError(
                f"task.yaml for {self.task_path} has invalid "
                f"vm.{field_name}={raw_value!r}; expected positive integer"
            )
        try:
            if isinstance(raw_value, float) and not raw_value.is_integer():
                raise ValueError
            value = int(raw_value)
        except (TypeError, ValueError):
            raise ValueError(
                f"task.yaml for {self.task_path} has invalid "
                f"vm.{field_name}={raw_value!r}; expected positive integer"
            ) from None
        if value <= 0:
            raise ValueError(
                f"task.yaml for {self.task_path} has invalid "
                f"vm.{field_name}={raw_value!r}; expected positive integer"
            )
        return value

    def _parse_task_timeout(self, raw_timeout: Any) -> int:
        try:
            return self._parse_positive_int("timeout_s", raw_timeout)
        except ValueError as exc:
            raise ValueError(f"{exc}; value is in seconds") from None

    def build_task_cfg(self, variant_index: int = 0) -> Any:
        module = self._load_module()

        try:
            task = self._load_task_variant(variant_index=variant_index)
            if task is not None:
                if not hasattr(task, "metadata") or getattr(task, "metadata") is None:
                    setattr(task, "metadata", {})
                return task
        except Exception as e:
            logger.warning(f"Failed to resolve task object via load(): {e}")

        config = getattr(module, "config", None)
        if config is not None:
            if not hasattr(config, "metadata"):
                if hasattr(config, "to_metadata"):
                    config.metadata = config.to_metadata()
                else:
                    config.metadata = {}
            return config

        for attr_name in dir(module):
            obj = getattr(module, attr_name)
            if (
                isinstance(obj, type)
                and attr_name.endswith("Config")
                and attr_name != "GeneralTaskConfig"
                and hasattr(obj, "task_description")
            ):
                instance = obj()
                if not hasattr(instance, "metadata"):
                    if hasattr(instance, "to_metadata"):
                        instance.metadata = instance.to_metadata()
                    else:
                        instance.metadata = {}
                return instance

        raise RuntimeError(
            f"Could not build task cfg from {self.main_py}. "
            f"Expected a module-level 'config' object, a *Config class, "
            f"or a load() function."
        )

    def _extract_os_type(self, *, task: Any | None = None, config: Any | None = None) -> str:
        computer = getattr(task, "computer", None)
        if isinstance(computer, dict):
            setup_config = computer.get("setup_config") or {}
            os_type = setup_config.get("os_type")
            if isinstance(os_type, str) and os_type:
                return os_type

        for candidate in (config,):
            if candidate is None:
                continue
            for attr_name in ("OS_TYPE", "os_type"):
                value = getattr(candidate, attr_name, None)
                if isinstance(value, str) and value:
                    return value

        logger.warning("Task %s did not expose os_type; defaulting to windows", self.task_path)
        return "windows"

    def _extract_task_data(
        self, *, metadata: dict[str, Any], config: Any | None = None
    ) -> TaskDataSpec:
        explicit_requires = metadata.get("requires_task_data")
        if (
            explicit_requires is None
            and config is not None
            and hasattr(config, "REQUIRES_TASK_DATA")
        ):
            explicit_requires = getattr(config, "REQUIRES_TASK_DATA")
        if explicit_requires is None:
            explicit_requires = any(
                metadata.get(key) for key in ("input_dir", "software_dir", "reference_dir")
            )

        requires_task_data = bool(explicit_requires)

        domain_name = str(metadata.get("domain_name") or "").strip()
        task_name = str(metadata.get("task_name") or "").strip()
        variant_name = str(metadata.get("variant_name") or "").strip()
        have_identity = bool(domain_name and task_name and variant_name)

        if requires_task_data and not have_identity:
            raise RuntimeError(
                f"Task {self.task_path} requires task data but metadata is missing "
                f"domain_name/task_name/variant_name "
                f"(got domain_name={domain_name!r}, task_name={task_name!r}, variant_name={variant_name!r})"
            )

        # Always populate the task identity (domain/task/variant) even when the
        # task stages NO input data (requires_task_data=False). Output-pull only
        # needs the identity to locate <task_data_root>/<domain>/<task>/<variant>/
        # output; it must not be coupled to input staging — a task can produce
        # output to gather without consuming any staged input (e.g. tool_smoke).
        return TaskDataSpec(
            requires_task_data=requires_task_data,
            domain_name=domain_name,
            task_name=task_name,
            variant_name=variant_name,
            source_relpath=f"{domain_name}/{task_name}/{variant_name}" if have_identity else "",
            input_dir=metadata.get("input_dir"),
            software_dir=metadata.get("software_dir"),
            reference_dir=metadata.get("reference_dir"),
            reference_gcs_prefix=metadata.get("reference_gcs_prefix"),
            remote_output_dir=metadata.get("remote_output_dir"),
        )

    def get_setup_fn(self):
        module = self._load_module()
        return getattr(module, "start", None)

    def get_evaluate_fn(self):
        module = self._load_module()
        return getattr(module, "evaluate", None)
