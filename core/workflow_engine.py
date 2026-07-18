"""Workflow engine — register + run deterministic, rules-based business workflows.

A workflow is a REAL function with genuine domain logic (accounting / finance /
supply-chain / ops math) that turns structured inputs into a verifiable artifact.
No stubs, no fake outputs: the numbers are computed, not asserted. Sample inputs
ship with each workflow so `--demo` proves the capability on representative data;
a real customer feeds their own data through the same code path.

Design
------
* Workflow: name, domain, description, input_schema, a run() callable, built-in
  sample inputs, and a preferred output format (csv / json / md).
* Artifact: the result of a run. Holds a structured `summary` + `data` (the
  authoritative, JSON-serializable numbers — for verification) and `tables`
  (human-facing rows). Renders to csv / json / md.
* Registry: a process-global dict. New workflows are a ONE-FILE add — drop a
  module in workflows/ that calls @workflow(...); load_library() auto-imports it.

CLI
---
    python -m core.workflow_engine --list
    python -m core.workflow_engine --demo <name>
    python -m core.workflow_engine --run <name> --input data.json [--output out.csv]
    python -m core.workflow_engine --demo-all          # smoke-run every workflow

$0 / local / stdlib-only.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

__all__ = ["Artifact", "Workflow", "workflow", "register", "run", "get",
           "list_workflows", "load_library", "WorkflowError"]


class WorkflowError(Exception):
    """Bad inputs, unknown workflow, or a failed internal correctness check."""


# --------------------------------------------------------------------------- #
# Artifact
# --------------------------------------------------------------------------- #
@dataclass
class Artifact:
    """A workflow result. `summary`/`data` carry the authoritative numbers
    (verify against these); `tables` carry human-facing rows."""
    name: str
    domain: str = ""
    summary: Dict[str, Any] = field(default_factory=dict)
    tables: List[Dict[str, Any]] = field(default_factory=list)
    data: Dict[str, Any] = field(default_factory=dict)
    format: str = "md"

    def add_table(self, title: str, headers: List[str], rows: List[List[Any]]) -> "Artifact":
        self.tables.append({"title": title, "headers": list(headers),
                            "rows": [list(r) for r in rows]})
        return self

    def to_dict(self) -> Dict[str, Any]:
        return {"workflow": self.name, "domain": self.domain,
                "summary": self.summary, "tables": self.tables, "data": self.data}

    # -- renderers ---------------------------------------------------------- #
    def render(self, fmt: Optional[str] = None) -> str:
        fmt = (fmt or self.format or "md").lower()
        if fmt == "json":
            return self._render_json()
        if fmt == "csv":
            return self._render_csv()
        return self._render_md()

    def _render_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)

    def _render_csv(self) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        if self.summary:
            w.writerow(["# SUMMARY"])
            for k, v in self.summary.items():
                w.writerow([k, v])
            w.writerow([])
        for t in self.tables:
            w.writerow(["# " + str(t["title"])])
            w.writerow(t["headers"])
            for row in t["rows"]:
                w.writerow(row)
            w.writerow([])
        return buf.getvalue()

    def _render_md(self) -> str:
        out: List[str] = [f"# {self.name}"]
        if self.domain:
            out.append(f"_domain: {self.domain}_\n")
        if self.summary:
            out.append("## Summary")
            for k, v in self.summary.items():
                out.append(f"- **{k}**: {v}")
            out.append("")
        for t in self.tables:
            out.append(f"## {t['title']}")
            hdr = t["headers"]
            out.append("| " + " | ".join(str(h) for h in hdr) + " |")
            out.append("| " + " | ".join("---" for _ in hdr) + " |")
            for row in t["rows"]:
                out.append("| " + " | ".join(str(c) for c in row) + " |")
            out.append("")
        return "\n".join(out)

    def write(self, path: str, fmt: Optional[str] = None) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.render(fmt))


# --------------------------------------------------------------------------- #
# Workflow + registry
# --------------------------------------------------------------------------- #
@dataclass
class Workflow:
    name: str
    domain: str
    description: str
    input_schema: Dict[str, str]
    fn: Callable[[Dict[str, Any]], Artifact]
    sample: Dict[str, Any] = field(default_factory=dict)
    output: str = "md"

    def validate(self, inputs: Dict[str, Any]) -> None:
        # A schema field is optional iff its description mentions "optional".
        missing = [k for k, desc in self.input_schema.items()
                   if k not in inputs and "optional" not in str(desc).lower()]
        if missing:
            raise WorkflowError(
                f"workflow '{self.name}' missing required input(s): {', '.join(missing)}")

    def run(self, inputs: Dict[str, Any]) -> Artifact:
        self.validate(inputs)
        art = self.fn(inputs)
        if not isinstance(art, Artifact):
            raise WorkflowError(f"workflow '{self.name}' did not return an Artifact")
        art.name = art.name or self.name
        art.domain = art.domain or self.domain
        art.format = art.format or self.output
        return art


_REGISTRY: Dict[str, Workflow] = {}


def register(wf: Workflow) -> Workflow:
    if wf.name in _REGISTRY:
        raise WorkflowError(f"duplicate workflow name: {wf.name}")
    _REGISTRY[wf.name] = wf
    return wf


def workflow(*, name: str, domain: str, description: str,
             input_schema: Dict[str, str], sample: Dict[str, Any],
             output: str = "md") -> Callable[[Callable], Callable]:
    """Decorator: register a workflow. The decorated fn keeps its identity so it
    stays unit-testable directly."""
    def deco(fn: Callable[[Dict[str, Any]], Artifact]) -> Callable:
        register(Workflow(name=name, domain=domain, description=description,
                          input_schema=input_schema, fn=fn, sample=sample,
                          output=output))
        return fn
    return deco


def get(name: str) -> Workflow:
    if name not in _REGISTRY:
        raise WorkflowError(f"unknown workflow: {name} (try --list)")
    return _REGISTRY[name]


def run(name: str, inputs: Dict[str, Any]) -> Artifact:
    return get(name).run(inputs)


def list_workflows() -> List[Workflow]:
    return sorted(_REGISTRY.values(), key=lambda w: (w.domain, w.name))


_LIBRARY_LOADED = False


def load_library() -> None:
    """Import the workflows package so every module self-registers. Idempotent."""
    global _LIBRARY_LOADED
    if _LIBRARY_LOADED:
        return
    import importlib
    import pkgutil
    try:
        import workflows  # noqa: F401  (package living at repo root)
    except Exception as exc:  # pragma: no cover
        raise WorkflowError(f"could not import workflows package: {exc}") from exc
    for mod in pkgutil.iter_modules(workflows.__path__):
        if mod.name.startswith("_"):
            continue
        importlib.import_module(f"workflows.{mod.name}")
    _LIBRARY_LOADED = True


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _cmd_list() -> int:
    rows: List[Tuple[str, str, str]] = []
    cur = None
    for wf in list_workflows():
        if wf.domain != cur:
            cur = wf.domain
            print(f"\n=== {cur.upper()} ===")
        print(f"  {wf.name:<28} {wf.description}")
    print(f"\n{len(_REGISTRY)} workflows registered.")
    return 0


def _emit(art: Artifact, fmt: Optional[str], output: Optional[str]) -> None:
    text = art.render(fmt)
    if output:
        art.write(output, fmt)
        print(f"[written] {output} ({fmt or art.format})")
    else:
        print(text)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="workflow_engine",
                                 description="Run deterministic business workflows.")
    ap.add_argument("--list", action="store_true", help="list registered workflows")
    ap.add_argument("--run", metavar="NAME", help="run a workflow")
    ap.add_argument("--demo", metavar="NAME", help="run a workflow on its built-in sample")
    ap.add_argument("--demo-all", action="store_true", help="smoke-run every workflow's demo")
    ap.add_argument("--input", metavar="FILE", help="JSON input file for --run")
    ap.add_argument("--output", metavar="FILE", help="write artifact to file instead of stdout")
    ap.add_argument("--format", metavar="FMT", choices=["csv", "json", "md"],
                    help="override output format")
    args = ap.parse_args(argv)

    load_library()

    if args.list:
        return _cmd_list()

    if args.demo_all:
        ok = 0
        for wf in list_workflows():
            try:
                art = wf.run(wf.sample)
                print(f"[ok]   {wf.domain}/{wf.name}: {len(art.tables)} table(s); "
                      f"summary keys: {list(art.summary)[:4]}")
                ok += 1
            except Exception as exc:  # pragma: no cover
                print(f"[FAIL] {wf.domain}/{wf.name}: {exc}")
        print(f"\n{ok}/{len(_REGISTRY)} demos ran clean.")
        return 0 if ok == len(_REGISTRY) else 1

    if args.demo:
        wf = get(args.demo)
        _emit(wf.run(wf.sample), args.format, args.output)
        return 0

    if args.run:
        if not args.input:
            ap.error("--run requires --input FILE (JSON)")
        with open(args.input, encoding="utf-8") as fh:
            inputs = json.load(fh)
        _emit(run(args.run, inputs), args.format, args.output)
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    # Run as `python -m core.workflow_engine`: delegate to the canonical module so
    # workflows (which `from core.workflow_engine import ...`) share ONE registry,
    # not the separate one this __main__ copy would otherwise own.
    from core.workflow_engine import main as _main
    sys.exit(_main())
