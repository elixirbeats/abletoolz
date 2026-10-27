"""What the plugin catalog tells a plan: maps by id, converters, and what is known about a patch crossing."""

from __future__ import annotations

from abletoolz.plugins.identity.ids import PluginKind
from abletoolz.plugins.identity.mapping import name_variants, normalized_keys
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.plugins.state.catalog import Converter
from abletoolz.plugins.state.measured import MeasuredState, measured_state
from abletoolz.plugins.state.plugin_map import PluginMap


def _maps_named(name: str) -> list[PluginMap]:
    return [plugin_map for variant in name_variants(name) for plugin_map in CATALOG.named(variant)]


def known_state(source_name: str, target_name: str, target_kind: PluginKind) -> MeasuredState:
    """What is known about a patch crossing from ``source_name`` to ``target_name``.

    A plugin map holding both names, in any spelling a set uses, answers with
    its evidence for the target format; two plugin maps with a converter
    between them answer with that converter's; otherwise a measured migration
    between the two, or nothing.
    """
    targets = _maps_named(target_name)
    for plugin_map in _maps_named(source_name):
        target = plugin_map.formats.get(target_kind)
        if target is not None and normalized_keys(target.name) & normalized_keys(target_name):
            return target.measured
        for other in targets:
            converter = CATALOG.converter(plugin_map.id, other.id)
            if converter is not None:
                return converter.measured
    return measured_state(source_name, target_name)


def _map_by_id(map_id: str) -> PluginMap:
    plugin_map = CATALOG.by_map_id(map_id)
    if plugin_map is None:
        raise ValueError(f"No plugin map {map_id!r}")
    return plugin_map


def _converter(source_id: str, target_id: str) -> Converter:
    converter = CATALOG.converter(source_id, target_id)
    if converter is None:
        raise ValueError(f"No converter from {source_id!r} into {target_id!r}")
    return converter
