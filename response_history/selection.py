import re


def select(raw: str | None, count: int) -> list[int]:
    if count < 1:
        raise ValueError("No completed responses")
    selector = (raw or "-1").strip()
    if re.fullmatch(r"-(?:[1-9]|10)", selector):
        n = int(selector[1:])
        return list(range(max(1, count - n + 1), count + 1))
    atom = r"#?[1-9][0-9]*"
    if not re.fullmatch(rf"{atom}(?:\s*-\s*{atom})?(?:\s*,\s*{atom}(?:\s*-\s*{atom})?)*", selector):
        raise ValueError("Invalid response selection")
    chosen = []
    for item in selector.split(","):
        bounds = [int(x.strip().lstrip("#")) for x in item.split("-")]
        first, last = bounds[0], bounds[-1]
        if first > last or last > count:
            raise ValueError("Reversed or out-of-range response selection")
        chosen.extend(range(first, last + 1))
    return chosen
