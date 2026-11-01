"""Walk a decoded legacy document by source path."""

from __future__ import annotations

from collections.abc import Iterator

from .legacy_document import Node


def walk(node: Node, path: str = "LiveDocument") -> Iterator[tuple[str, Node]]:
    # An explicit stack avoids forwarding every descendant through its ancestors.
    pending = [(path, node)]
    while pending:
        current_path, current = pending.pop()
        yield current_path, current
        for index in range(len(current.entries) - 1, -1, -1):
            item = current.entries[index]
            identifier = item.identifier if item.identifier is not None else index
            pending.append((f"{current_path}/{item.node.class_name}[{identifier}]", item.node))
        for name, value in reversed(current.fields):
            pending.append((f"{current_path}/{name}", value))
