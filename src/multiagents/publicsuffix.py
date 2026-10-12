"""Public suffix checks against the packaged, pinned ICANN and private list."""

from functools import lru_cache
from pathlib import Path


def normalize(host: str) -> str:
    return host.encode("idna").decode("ascii").lower().removesuffix(".")


@lru_cache(maxsize=1)
def _rules() -> tuple[set[str], set[str], set[str]]:
    exact, wildcard, exceptions = set(), set(), set()
    for line in Path(__file__).with_name("public_suffix_list.dat").read_text().splitlines():
        if not line.strip() or line.startswith("//"):
            continue
        rule = line.split()[0]
        if rule.startswith("!"):
            exceptions.add(normalize(rule[1:]))
        elif rule.startswith("*."):
            wildcard.add(normalize(rule[2:]))
        else:
            exact.add(normalize(rule))
    return exact, wildcard, exceptions


def is_public_suffix(host: str) -> bool:
    host = normalize(host)
    exact, wildcard, exceptions = _rules()
    # ST9-R1/D14 also refuses wildcard roots: suffix-matching the root
    # would admit every tenant, even when the root is formally registrable.
    if host in wildcard:
        return host not in exceptions
    labels = host.split(".")
    # An exception overrides every matching ordinary or wildcard rule.
    for index in range(len(labels)):
        if ".".join(labels[index:]) in exceptions:
            return False
    return (len(labels) == 1 or host in exact
            or (len(labels) > 1 and ".".join(labels[1:]) in wildcard))
