# legal/practice_suite

This task is built on top of the files and setups from
[Harvey LAB](https://github.com/harveyai/harvey-labs) (Harvey AI, MIT License).
The 224 scenarios here are a shortlist of Harvey LAB tasks converted to the
a2-bench multi-agent format: each source task's instructions and documents are
distributed across the roles in `task-data/legal/practice_suite/<scenario>/`, and its
rubric is carried over in `rubrics.jsonl`. Each scenario's `source` field in
`scenario_index.json` names the Harvey LAB task it came from.

The content-level judge (`tasks/rubric_judge.py`) ports Harvey LAB's evaluator,
including its criterion prompt.

The directory name refers to the source corpus only. This task is not
affiliated with or endorsed by Harvey AI.

## Citation

```bibtex
@misc{harveylab2026,
  title   = {Harvey LAB: The Legal Agent Benchmark},
  author  = {{Harvey AI}},
  year    = {2026},
  version = {v1.0},
  url     = {https://github.com/harveyai/harvey-labs/tree/v1.0},
  note    = {Announcement: \url{https://www.harvey.ai/blog/introducing-harveys-legal-agent-benchmark}}
}
```

## Upstream license

```
MIT License

Copyright (c) 2026 Harvey AI

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
