import re
from response_history.model import UserError  # noqa: F401  (re-exported for callers of select)


EXAMPLES = "a number (5), a range (2-4), a list (1,3) or the latest few (-3)"


def select(raw: str | None, count: int, what: str = "this session") -> list[int]:
    if count < 1:
        raise UserError("No responses yet.")
    selector = (raw or "-1").strip()
    if re.fullmatch(r"-\d+", selector):
        n = int(selector[1:])
        if not 1 <= n <= 10:
            raise UserError(f"Nothing copied: {selector} is outside -1 to -10.")
        return list(range(max(1, count - n + 1), count + 1))
    if re.search(r"(?<![0-9])#?0+(?![0-9])", selector):
        raise UserError("Nothing copied: responses are numbered from #1.")
    atom = r"#?[1-9][0-9]*"
    if not re.fullmatch(rf"{atom}(?:\s*-\s*{atom})?(?:\s*,\s*{atom}(?:\s*-\s*{atom})?)*", selector):
        raise UserError(f"Nothing copied: can't read {selector[:40]!r}. Use {EXAMPLES}.")
    chosen = []
    for item in selector.split(","):
        bounds = [int(x.strip().lstrip("#")) for x in item.split("-")]
        first, last = bounds[0], bounds[-1]
        if first > last:
            raise UserError(f"Nothing copied: {first}-{last} goes backwards; write {last}-{first}.")
        if last > count:
            have = "only #1" if count == 1 else f"#1 to #{count}"
            raise UserError(f"Nothing copied: there is no response #{last}; {what} has {have}.")
        chosen.extend(range(first, last + 1))
    return chosen
