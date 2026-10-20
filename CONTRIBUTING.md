# Contributing Guide For a2-bench

This page lists the operational governance model of this project, as well as the recommendations and requirements for how to best contribute to a2-bench. We strive to obey these as best as possible. As always, thanks for contributing – we hope these guidelines make it easier and shed some light on our approach and processes.

# Governance Model

## Salesforce Sponsored

The intent and goal of open sourcing this project is to increase the contributor and user base. However, only Salesforce employees will be given `admin` rights and will be the final arbitrars of what contributions are accepted or not.

# Getting started

Start with the [README](README.md): it covers requirements, fetching task data, running an experiment, and viewing runs.

```bash
uv sync --extra dev             # add --extra metrics for the judge-backed metrics
scripts/fetch_task_data.sh      # once; needs HF_TOKEN with access to the dataset
```

Questions and discussion happen on [GitHub Issues](https://github.com/SalesforceAIResearch/a2-bench/issues).

# Issues, requests & ideas

Use GitHub Issues page to submit issues, enhancement requests and discuss ideas.

### Bug Reports and Fixes
-  If you find a bug, please search for it in the [Issues](https://github.com/SalesforceAIResearch/a2-bench/issues), and if it isn't already tracked,
   [create a new issue](https://github.com/SalesforceAIResearch/a2-bench/issues/new). Fill out the "Bug Report" section of the issue template. Even if an Issue is closed, feel free to comment and add details, it will still
   be reviewed.
-  Issues that have already been identified as a bug (note: able to reproduce) will be labelled `bug`.
-  If you'd like to submit a fix for a bug, [send a Pull Request](#creating-a-pull-request) and mention the Issue number.
  -  Include tests that isolate the bug and verifies that it was fixed.
  -  For run-time bugs, include the experiment yaml, the agent presets/models involved, and the relevant run directory or transcript excerpt.

### New Features
-  If you'd like to add new functionality to this project, describe the problem you want to solve in a [new Issue](https://github.com/SalesforceAIResearch/a2-bench/issues/new).
-  Issues that have been identified as a feature request will be labelled `enhancement`.
-  If you'd like to implement the new feature, please wait for feedback from the project
   maintainers before spending too much time writing the code. In some cases, `enhancement`s may
   not align well with the project objectives at the time.

### New Tasks
-  A task lives in `tasks/<domain>/<task>/` as exactly two files: `task.yaml` (declarative setup —
   roles, agent cards, assignment, `vm`, `paths`) and `main.py` (`load()` / `start()` / `evaluate()`).
   See the "Tasks" section of the [README](README.md) and existing tasks for the pattern.
-  Task data (`input/`, `software/`, `reference/`) is not committed to git; it lives in the private
   HuggingFace dataset (fetched with `scripts/fetch_task_data.sh`). Never commit
   `reference/` answers or anything else that would leak the evaluation into the repo.
-  Write briefs and agent cards so that nothing in an agent's context frames the setup as a
   benchmark or an evaluation.
-  Add the task's `domain/task` line to a file in `selected_tasks/`, and run it end to end at least
   once before opening the Pull Request.

### Tests, Documentation, Miscellaneous
-  If you'd like to improve the tests, you want to make the documentation clearer, you have an
   alternative implementation of something that may have advantages over the way it's currently
   done, or you have any other change, we would be happy to hear about it!
  -  If it's a trivial change, go ahead and [send a Pull Request](#creating-a-pull-request) with the changes you have in mind.
  -  If not, [open an Issue](https://github.com/SalesforceAIResearch/a2-bench/issues/new) to discuss the idea first.

If you're new to our project and looking for some way to make your first contribution, look for
Issues labelled `good first contribution`.

# Contribution Checklist

- [x] Clean, simple, well styled code
  - Follow the existing style; `ruff` is configured in `pyproject.toml` (line length 100, Python 3.12).
- [x] Commits should be atomic and messages must be descriptive. Related issues should be mentioned by Issue number.
- [x] Comments
  - Module-level & function-level comments.
  - Comments on complex blocks of code or algorithms (include references to sources).
- [x] Tests
  - CI must pass: `uv run ruff check --select E9,F63,F7,F82 .` and `uv run python -c "import a2_run, tasks.task_spec, metrics"`
- [x] Dependencies
  - Minimize number of dependencies. Optional features (e.g. judge-backed metrics) go behind an extra in `pyproject.toml`.
  - Prefer Apache 2.0, BSD3, MIT, ISC and MPL licenses.
- [x] Secrets
  - Never commit credentials; `secret/.env` is gitignored — copy `secret/.env.example` instead.
- [x] Reviews
  - Changes must be approved via peer code review

# Creating a Pull Request

1. **Ensure the bug/feature was not already reported** by searching on GitHub under Issues.  If none exists, create a new issue so that other contributors can keep track of what you are trying to add/fix and offer suggestions (or let you know if there is already an effort in progress).
2. **Clone** the forked repo to your machine.
3. **Create** a new branch to contain your work (e.g. `git checkout -b fix-issue-11`)
4. **Commit** changes to your own branch.
5. **Push** your work back up to your fork. (e.g. `git push origin fix-issue-11`)
6. **Submit** a Pull Request against the `main` branch and refer to the issue(s) you are fixing. Try not to pollute your pull request with unintended changes. Keep it simple and small.
7. **Sign** the Salesforce CLA (you will be prompted to do so when submitting the Pull Request)

> **NOTE**: Be sure to [sync your fork](https://help.github.com/articles/syncing-a-fork/) before making a pull request.

# Contributor License Agreement ("CLA")
In order to accept your pull request, we need you to submit a CLA. You only need
to do this once to work on any of Salesforce's open source projects.

Complete your CLA here: <https://cla.salesforce.com/sign-cla>

# Issues
We use GitHub issues to track public bugs. Please ensure your description is
clear and has sufficient instructions to be able to reproduce the issue.

# Code of Conduct
Please follow our [Code of Conduct](CODE_OF_CONDUCT.md).

# License
By contributing your code, you agree to license your contribution under the terms of our project [LICENSE](LICENSE.txt) and to sign the [Salesforce CLA](https://cla.salesforce.com/sign-cla)
