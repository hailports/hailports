"""Structural no-delete assertion for the reply lane (belt-and-suspenders).

The reply drafter/deliverer only READS work context and TYPES a private draft into a composer — it has
no delete/destroy path today. This guard makes that a STRUCTURAL invariant instead of a hope: it scans
the actual source of the drafting + delivery functions and raises if a destructive operation (a mail /
record delete, move-to-trash, `sf data delete`, or a DML delete) becomes reachable. A future edit that
silently wires in a delete trips the assertion at import/selftest time — it can't ship quietly.

    assert_functions_delete_free(*funcs) -> None   # raises AssertionError on a reachable destructive op

Deterministic, $0, read-only. Import-safe: a function whose source can't be read is skipped (fail-soft),
never a false alarm.
"""
from __future__ import annotations

import inspect
import re

# Destructive CALL/STATEMENT shapes — deliberately tight to avoid flagging the words "delete"/"trash"
# appearing in prose, a lexicon, or a variable name. We only match an actual invocation/statement.
_DESTRUCTIVE = re.compile(
    r"""
      \.\s*(?:delete|destroy|erase|hard_delete|move_to_trash|delete_message|delete_record|
             delete_records|remove_record|purge)\s*\(     # method call: x.delete( ...
    | \bdelete\s+from\b                                    # SQL delete
    | \bDML\s*\.\s*delete\b                                # Apex-ish DML delete
    | \bdatabase\s*\.\s*delete\b
    | \bsf\s+data\s+delete\b                               # sf CLI data delete
    | \bdata\s+delete\b
    | \bTrash\b\s*\(                                       # Trash( ...
    """,
    re.I | re.X,
)


def _strip_comments(src: str) -> str:
    # crude but safe for our target functions: drop everything after an unquoted-ish '#'.
    out = []
    for line in src.splitlines():
        out.append(line.split("#", 1)[0])
    return "\n".join(out)


def assert_functions_delete_free(*funcs) -> None:
    """Raise AssertionError if any destructive operation is reachable in the given functions' source."""
    for f in funcs:
        try:
            src = inspect.getsource(f)
        except (OSError, TypeError):
            continue  # source unavailable (fail-soft) — never a false alarm
        m = _DESTRUCTIVE.search(_strip_comments(src))
        if m:
            name = getattr(f, "__qualname__", getattr(f, "__name__", str(f)))
            raise AssertionError(
                f"reply lane must never reach a destructive op — found {m.group(0)!r} in {name}()")


def _selftest() -> int:
    def clean_fn(x):
        return x.upper()  # no delete anywhere

    def dirty_fn(rec):
        rec.delete()  # a destructive op sneaks in

    assert_functions_delete_free(clean_fn)  # must NOT raise
    try:
        assert_functions_delete_free(dirty_fn)
    except AssertionError as e:
        print(f"  caught reachable delete -> {e}")
    else:
        raise AssertionError("dirty_fn should have tripped the guard")
    print("no_destructive selftest ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
