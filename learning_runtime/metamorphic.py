"""Deterministic rename-only Domain Map transformations."""

from __future__ import annotations

from pathlib import PurePosixPath

from .domain_map import ComponentObservation, DomainMap, DomainRelation


def renamed_domain_map(source: DomainMap) -> DomainMap:
    """Rename local identities while preserving the structural projection."""

    identifiers = {
        component.component_id: f"component-{index:03d}"
        for index, component in enumerate(source.components, start=1)
    }
    components = tuple(
        ComponentObservation(
            component_id=identifiers[component.component_id],
            path=(
                "metamorphic/"
                + identifiers[component.component_id]
                + PurePosixPath(component.path).suffix
            ),
            symbols=tuple(
                f"symbol_{index:03d}_{symbol_index:03d}"
                for symbol_index, _symbol in enumerate(component.symbols, start=1)
            ),
            roles=component.roles,
            technologies=component.technologies,
        )
        for index, component in enumerate(source.components, start=1)
    )
    relations = tuple(
        DomainRelation(
            source_component_id=identifiers[relation.source_component_id],
            relation=relation.relation,
            target_component_id=identifiers[relation.target_component_id],
        )
        for relation in source.relations
    )
    transformed = DomainMap(
        project_fingerprint=source.project_fingerprint,
        components=components,
        relations=relations,
    )
    if transformed.structural_projection() != source.structural_projection():
        raise ValueError("metamorphic rename changed the structural projection")
    return transformed
