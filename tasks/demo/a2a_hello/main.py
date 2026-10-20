"""Demo task: ``demo/a2a_hello`` — the canonical agent-to-agent smoke test.

Two roles, two isolated sandboxes, with per-role task data staged from the
host (``task_data_source: local:task-data``). All declarative setup (identity,
vm, per-role briefs, assignment, agent cards, path templates) lives in the
sibling ``task.yaml``; this module holds only the lifecycle hooks. The DATA
lives under ``task-data/``:

    task-data/demo/a2a_hello/base/
    ├── oracle/input/codeword.txt      → staged into the ORACLE sandbox only
    └── asker/reference/expected.txt   → staged into the ASKER sandbox at eval

  * ``oracle`` — the framework stages ``input/codeword.txt`` into its sandbox
    only. Its brief: reveal the code word when asked.
  * ``asker`` — must obtain the code word THROUGH THE CONVERSATION (it has no
    filesystem access to the oracle's sandbox), write it to
    ``output/answer.txt`` in its own sandbox, then end.

``evaluate()`` exact-matches the asker's answer against the expected answer
staged into the asker's ``reference/`` (after the run, so it is never visible
during the conversation) — score 1.0 iff the word crossed the channel and
landed on disk.

A2a lifecycle: ``start(task_cfg, sandboxes)`` / ``evaluate(task_cfg, sandboxes)``
receive a dict of role name → SandboxHandle (NOT a single handle). Per-role
data staging (input before the run, reference before eval) is driven by the
framework from ``task-data/<domain>/<task>/<variant>/<role>/``.
"""
from __future__ import annotations

import logging
from typing import Any

from tasks.task_spec import load_tasks, part, scored, task_config

logger = logging.getLogger(__name__)


def load():
    return load_tasks(__file__)


async def start(task_cfg, sandboxes):
    """Sanity-check the staged code word in the oracle; prep the asker output.

    Data staging is the framework's job (per-role, from ``task-data/``); this
    hook only asserts the code word landed in the oracle's sandbox and readies
    the asker's output dir.
    """
    cfg = task_config(__file__)

    oracle = sandboxes["oracle"]
    if not await oracle.exists(cfg.codeword_path):
        raise RuntimeError(
            f"staged input missing: {cfg.codeword_path} not found in the "
            "oracle sandbox. Check task-data/demo/a2a_hello/base/oracle/input/ "
            "on the host and the local: staging config."
        )

    asker = sandboxes["asker"]
    await asker.mkdir(cfg.remote_output_dir)
    await asker.rm([cfg.answer_path])
    logger.info("[a2a_hello] oracle code word staged; asker output dir ready")


async def evaluate(task_cfg, sandboxes) -> dict[str, Any]:
    """1.0 iff the asker's answer.txt holds exactly the expected code word.

    The expected answer is staged into the asker's reference/ after the run,
    so it was never readable during the conversation.
    """
    cfg = task_config(__file__)
    asker = sandboxes["asker"]

    try:
        expected = (await asker.read_text(cfg.expected_path)).strip()
    except Exception as exc:                                    # noqa: BLE001
        raise RuntimeError(
            f"reference unreadable at {cfg.expected_path}: {exc}"
        )

    try:
        actual = (await asker.read_text(cfg.answer_path)).strip()
    except Exception as exc:                                    # noqa: BLE001
        logger.info("[a2a_hello] answer unreadable at %s: %s", cfg.answer_path, exc)
        return scored(0.0, [part(
            "asker", 0.0,
            criteria={"answer_written": False, "code_word_correct": False},
            reason=f"no readable answer at {cfg.answer_path}",
        )])

    if actual == expected:
        logger.info("[a2a_hello] correct code word relayed")
        return scored(1.0, [part(
            "asker", 1.0,
            criteria={"answer_written": True, "code_word_correct": True},
            answer=actual[:120],
        )])
    logger.info("[a2a_hello] mismatch: got %r, expected %r", actual[:120], expected[:120])
    return scored(0.0, [part(
        "asker", 0.0,
        criteria={"answer_written": True, "code_word_correct": False},
        reason="answer written but the code word does not match",
        answer=actual[:120], expected=expected[:120],
    )])
