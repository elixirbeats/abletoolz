"""Every plugin abletoolz knows, found by what a device stores.

A vendor module declares its plugins with :func:`register` on a function that
builds the plugin's map, and :mod:`abletoolz.plugins.state.all_plugins` imports
every vendor module, so reaching the catalog through that module reaches all of
it. The maps are built the first time the catalog is asked, and two maps that
claim one stored id, or one map id, are refused then: a set would otherwise get
whichever won.

Turning one plugin's patch into another plugin's (Pro-Q 1 into Pro-Q 3) is a
:class:`Converter`, declared with :func:`converter` on the function that
converts, and keyed by the two plugins' map ids. It works on each plugin's own
patch, so the source plugin's map lifts the patch out of whatever format the
device holds and the target plugin's map lowers the result into the format
being written. One pair has one converter; a second is refused.
"""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import Callable, Iterator

from abletoolz.plugins.identity.ids import StoredId
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.measured import MeasuredState
from abletoolz.plugins.state.plugin_map import PluginMap

type MapFactory = Callable[[], PluginMap]


@dataclasses.dataclass(frozen=True, slots=True)
class Converted:
    """Another plugin's own patch, and what loading it will change."""

    patch: bytes
    notices: tuple[str, ...] = ()


# The source plugin's own patch and the bank program the device has selected, in;
# the target plugin's own patch out.
type Convert = Callable[[bytes, int | None], Converted]


@dataclasses.dataclass(frozen=True, slots=True)
class Converter:
    """A worked-out conversion from one plugin into another, and its evidence."""

    source: str
    target: str
    convert: Convert
    measured: MeasuredState


class Catalog:
    """Plugin maps by stored id, built from registered factories on first use.

    Sets are processed on several threads, so the first use builds the whole
    index under a lock and publishes it at once: a thread never sees half of it.
    """

    def __init__(self) -> None:
        self._factories: list[MapFactory] = []
        self._by_id: dict[StoredId, PluginMap] | None = None
        self._maps: dict[str, PluginMap] = {}
        self._converters: dict[tuple[str, str], Converter] = {}
        self._building = threading.Lock()

    def register[F: MapFactory](self, factory: F) -> F:
        """Add the plugin ``factory`` builds; usable as a decorator."""
        if self._by_id is not None:
            raise StateTransformError("The plugin catalog is already built; register plugins before using it")
        self._factories.append(factory)
        return factory

    def add_converter(self, converter: Converter) -> None:
        """Add a conversion between two plugins; a second one for the same pair is refused."""
        pair = (converter.source, converter.target)
        if pair in self._converters:
            raise StateTransformError(f"Two converters are registered from {pair[0]!r} into {pair[1]!r}")
        if converter.source == converter.target:
            raise StateTransformError(f"A converter from {pair[0]!r} into itself is that plugin's own map")
        self._converters[pair] = converter
        if self._by_id is not None:
            self._check_converter(converter)

    def converter(self, source: str, target: str) -> Converter | None:
        """The conversion from plugin map ``source`` into ``target``, or None when nobody has worked one out."""
        self._index()
        return self._converters.get((source, target))

    def find(self, stored: StoredId) -> PluginMap | None:
        """The plugin a device storing ``stored`` is, or None when nobody has mapped it."""
        return self._index().get(stored)

    def by_map_id(self, map_id: str) -> PluginMap | None:
        """The plugin map registered as ``map_id``, or None when nothing is."""
        self._index()
        return self._maps.get(map_id)

    def named(self, name: str) -> tuple[PluginMap, ...]:
        """Every plugin called ``name``, by its own name or the name one of its formats stores."""
        self._index()
        return tuple(
            plugin_map
            for plugin_map in self._maps.values()
            if name == plugin_map.name or any(name == item.name for item in plugin_map.formats.values())
        )

    def file_suffixes(self) -> frozenset[str]:
        """Every file suffix some plugin's patch can point at, for the sample database to index."""
        self._index()
        return frozenset().union(
            *(plugin_map.file_paths.suffixes for plugin_map in self._maps.values() if plugin_map.file_paths is not None)
        )

    def converters(self) -> Iterator[Converter]:
        """Every conversion between two plugins abletoolz ships."""
        self._index()
        return iter(self._converters.values())

    def __iter__(self) -> Iterator[PluginMap]:
        self._index()
        return iter(self._maps.values())

    def _index(self) -> dict[StoredId, PluginMap]:
        built = self._by_id
        if built is not None:
            return built
        with self._building:
            if self._by_id is None:
                maps: dict[str, PluginMap] = {}
                by_id: dict[StoredId, PluginMap] = {}
                for factory in self._factories:
                    plugin_map = factory()
                    if plugin_map.id in maps:
                        raise StateTransformError(f"Two plugin maps are registered as {plugin_map.id!r}")
                    maps[plugin_map.id] = plugin_map
                    for plugin_format in plugin_map.formats.values():
                        claimed = by_id.get(plugin_format.id)
                        if claimed is not None:
                            raise StateTransformError(
                                f"{plugin_map.name} and {claimed.name} both claim {plugin_format.id}"
                            )
                        by_id[plugin_format.id] = plugin_map
                for converter in self._converters.values():
                    self._check_converter(converter, maps)
                self._maps = maps
                self._by_id = by_id
            return self._by_id

    def _check_converter(self, converter: Converter, maps: dict[str, PluginMap] | None = None) -> None:
        known = self._maps if maps is None else maps
        for map_id in (converter.source, converter.target):
            if map_id not in known:
                raise StateTransformError(f"A converter names plugin map {map_id!r}, which nothing registers")


CATALOG = Catalog()


def register[F: MapFactory](factory: F) -> F:
    """Declare a plugin: decorate the function that builds its map."""
    return CATALOG.register(factory)


def converter[F: Convert](source: str, target: str, measured: MeasuredState) -> Callable[[F], F]:
    """Declare a conversion from plugin ``source`` into ``target``: decorate the function that converts."""

    def declare(convert: F) -> F:
        CATALOG.add_converter(Converter(source, target, convert, measured))
        return convert

    return declare
