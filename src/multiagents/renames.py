"""Declared provider renames, and the mechanism every surface reads them with.

A provider can change its name. The old name then appears in places this
package does not control — a config file, a CLI argument, an MCP tool argument,
durable state written by the code that ran before the change — and each of them
has to keep working, loudly, until its author has migrated it.

The declaration lives with the provider, in the shipped `providers.yaml`:

    some-route:
      renamed_from: [the-old-name]     # this route is what that name became
    some-cli:
      routable: false                  # plumbing, never somewhere to spend

`renamed_from` is enough on its own: a name that has been renamed away is not
a route, because every mention of it is read as the route that replaced it and
so the name itself reaches nothing. Whatever block still carries it is the CLI
integration, which is what `extends:` is for. `routable: false` says the same
about a block that was never a route.

Everything below is driven by those keys and knows no provider's name, so the
next rename is a data change. Three behaviours hang off them:

* :meth:`Renames.canonical` reads the old name as the route it became, and
  :meth:`Renames.warning` says so where the reader can find the line it came
  from;
* :meth:`Renames.is_route` answers whether a provider is somewhere an agent can
  be pinned, which is what stops the surfaces that report per-route figures from
  inventing a row for a CLI;
* :attr:`Renames.aliases` is the migration table `multiagents.tree` and
  `multiagents.spendcap` need — they move durable state and read a ledger, and
  both do it before any config has been loaded.

`shipped()` is where every caller gets that table: the declaration is a shipped
fact about a shipped integration, and one place means the tree, the ledger and
the config migrate with the same table in the same process. Only the shipped
defaults are read for it. A project can still say `routable: false` about one
of its OWN blocks — that is a property of the loaded provider, and
`providers.load_providers` reads it from the raw block — and its
`renamed_from` reaches the one rule that needs only the block itself (a
provider that declares the rename of the base it extends owns that base's
built-in reading). What a project cannot do is have its rename alias configs,
for the reason above: the tree and the ledger would then migrate a name the
config does not, and a rename is the one thing all three must agree on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class Renames:
    """The renames and non-routable providers a set of provider blocks declares."""

    # old route name -> the route that replaced it. Exact names, never a prefix
    # rule: a provider whose name merely starts with another's is its own.
    aliases: Mapping[str, str]
    # Providers no agent may be pinned to and no routing may select.
    unroutable: frozenset[str]

    @classmethod
    def from_blocks(cls, blocks: Mapping[str, Any]) -> "Renames":
        """Read the declarations out of raw `providers:` blocks.

        Nothing here raises: a wrongly typed `renamed_from` is refused at config
        load by `providers._renamed_from`, where a user can see which block said
        it, and this runs on the tree's read path where a raise would be worse
        than an alias that is not applied. A declaration that cannot be read is
        no alias, which is the same state a rename-less install is in.
        """
        aliases: dict[str, str] = {}
        unroutable: set[str] = set()
        for name, block in (blocks or {}).items():
            if not isinstance(block, Mapping):
                continue
            if block.get("routable") is False:
                unroutable.add(str(name))
            declared = block.get("renamed_from")
            # A scalar is accepted as well as a list.
            names = [declared] if isinstance(declared, str) else declared
            if not isinstance(names, list):
                continue
            for old in names:
                if isinstance(old, str) and old:
                    aliases[old] = str(name)
        return cls(aliases=aliases, unroutable=frozenset(unroutable))

    def canonical(self, name: Any) -> str:
        """The provider a reader meant, with every declared rename applied."""
        return self.aliases.get(str(name), str(name))

    def is_alias(self, name: Any) -> bool:
        """Is this name the pre-rename spelling of a route?"""
        return str(name) in self.aliases

    def is_route(self, name: Any) -> bool:
        """Is this provider somewhere an agent can be pinned and spend against?

        False for a block that declares `routable: false`, and for a name that
        has been renamed away: naming it as a route reads as the route that
        replaced it, so the name itself is not a route anyone can reach.
        """
        return str(name) not in self.unroutable and not self.is_alias(name)

    def warning(self, name: str, where: str = "") -> str:
        """One line naming what was read and what to write instead.

        `where` is a `file:line` (or a file) the name came from, so a user can
        go straight to it; a name with no file behind it — a CLI argument, an
        MCP tool argument — gets the same sentence without one.
        """
        route = self.canonical(name)
        at = f"{where}: " if where else ""
        return (f"{at}provider {str(name)!r} is a deprecated alias for "
                f"{route!r}; write {route!r} there.")


_shipped: Renames | None = None


def shipped() -> Renames:
    """The renames the shipped `providers.yaml` declares.

    Read once per process and remembered: the shipped defaults do not change
    while the process runs. A missing or unreadable file declares nothing,
    which is the same answer as a file with no `renamed_from` in it — the
    surfaces that would have warned stay silent and the migration is a no-op,
    rather than every tree read raising.
    """
    global _shipped
    if _shipped is None:
        _shipped = _read_shipped()
    return _shipped


def _read_shipped() -> Renames:
    import yaml

    from .paths import shipped_defaults_dir

    try:
        parsed = yaml.safe_load(
            (shipped_defaults_dir() / "providers.yaml").read_text())
    except (OSError, yaml.YAMLError):
        return Renames(aliases={}, unroutable=frozenset())
    blocks = parsed.get("providers") if isinstance(parsed, Mapping) else None
    return Renames.from_blocks(blocks or {})