from collections.abc import Mapping


def topological_layers(dependencies: Mapping[str, tuple[str, ...]]) -> tuple[tuple[str, ...], ...]:
    """Validate references and cycles before executing any provider or agent."""
    nodes = set(dependencies)
    unknown = {dependency for deps in dependencies.values() for dependency in deps} - nodes
    if unknown:
        raise ValueError(f"Unknown dependency nodes: {sorted(unknown)}")
    completed: set[str] = set()
    layers = []
    while completed != nodes:
        ready = tuple(
            sorted(
                node
                for node, deps in dependencies.items()
                if node not in completed and set(deps) <= completed
            )
        )
        if not ready:
            raise ValueError("Workflow dependency cycle")
        layers.append(ready)
        completed.update(ready)
    return tuple(layers)
