"""A stand-in for a notary's headless session: applies the prompt's patch, reports Edit calls."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any

from kintsugi.crew import claude_session as cs
from kintsugi.sandbox import proc

_DIFF = re.compile(r"```diff\n(.*?)\n```", re.DOTALL)


class FakeNotarySession:
    def __init__(self, *, tamper: bool = False) -> None:
        self.prompts: list[str] = []
        self.tamper = tamper  # apply the patch, then change one more line (must be refused)

    def __call__(self, request: cs.SessionRequest, on_start: Any = None) -> cs.SessionResult:
        self.prompts.append(request.prompt)
        found = _DIFF.search(request.prompt)
        assert found is not None, "the notary prompt carries the patch"
        cwd = Path(request.cwd)
        with tempfile.NamedTemporaryFile("w", suffix=".diff", delete=False, encoding="utf-8",
                                         newline="\n") as handle:  # fmt: skip
            handle.write(found.group(1) + "\n")
        proc.run(["git", "-C", str(cwd), "apply", "--whitespace=nowarn", handle.name],
                 timeout_s=60, check=True)  # fmt: skip
        Path(handle.name).unlink(missing_ok=True)
        changed = proc.run(["git", "-C", str(cwd), "status", "--porcelain", "-uall"],
                           timeout_s=60, check=True).stdout.split("\n")  # fmt: skip
        paths = [line[3:].strip() for line in changed if line.strip()]
        if self.tamper and paths:
            target = cwd / paths[0]
            target.write_text(target.read_text("utf-8") + "# tampered\n", encoding="utf-8",
                              newline="\n")  # fmt: skip
        stream = cs.StreamSummary(session_id=request.session_id)
        stream.tool_uses = [{"name": "Edit", "input": {"file_path": str(cwd / p)}} for p in paths]
        stream.result = {"result": "applied", "total_cost_usd": 0.02}
        return cs.SessionResult(request, stream, 0, False, False, None, None)
