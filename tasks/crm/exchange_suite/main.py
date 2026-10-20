"""Generated CRM suite task; logic lives in ``tasks.crm._suite_runtime``."""
from tasks.crm._suite_runtime import evaluate_for, start_for
from tasks.task_spec import load_tasks


def load():
    return load_tasks(__file__)


async def start(task_cfg, sandboxes):
    return await start_for(__file__, task_cfg, sandboxes)


async def evaluate(task_cfg, sandboxes):
    return await evaluate_for(__file__, task_cfg, sandboxes)
