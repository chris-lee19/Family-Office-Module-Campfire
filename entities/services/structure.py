"""The ownership structure as a drawing: who owns what, top down.

People sit on the top row, the entities they own below them, and the entities those own below that: a node's row is
one below its lowest owner. Each line carries the percentage. The layout is computed here (positions in pixels) so
the page only draws it; the table under the chart remains the full, sortable view.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from entities.models import Entity, Family
from entities.services.ownership import active_interests

NODE_W, NODE_H, GAP_X, GAP_Y, PAD = 184, 60, 26, 84, 8


@dataclass(eq=False)  # nodes and links point at each other: compare them by identity, never field by field
class Node:
    key: str  # "p<id>" or "e<id>"
    person: bool
    name: str
    sub: str
    pk: int
    booked: bool = False
    layer: int = 0
    x: float = 0
    y: float = 0
    owners: list["Link"] = field(default_factory=list)
    owned: list["Link"] = field(default_factory=list)

    @property
    def initials(self) -> str:
        parts = [p for p in self.name.replace(".", " ").split() if p[:1].isalpha()]
        return (parts[0][:1] + (parts[-1][:1] if len(parts) > 1 else "")).upper() if parts else "?"


@dataclass(eq=False)
class Link:
    source: Node
    target: Node
    percentage: Decimal
    path: str = ""
    label_x: float = 0
    label_y: float = 0

    @property
    def label(self) -> str:
        return f"{self.percentage.normalize():f}%"


@dataclass
class Structure:
    nodes: list[Node]
    links: list[Link]
    width: int = 0
    height: int = 0
    node_w: int = NODE_W
    node_h: int = NODE_H

    @property
    def summary(self) -> str:
        people = sum(1 for n in self.nodes if n.person)
        return f"{people} {'person' if people == 1 else 'people'} and {len(self.nodes) - people} entities, {len(self.links)} ownership interests"


def _layers(nodes: dict[str, Node]) -> None:
    """A node's row: people on top, an entity one row below its lowest owner (the longest chain above it)."""
    def depth(node: Node, seen: frozenset[str]) -> int:
        if node.person:
            return 0
        above = [depth(link.source, seen | {node.key}) for link in node.owners if link.source.key not in seen]  # a cycle stops here
        return 1 + max(above, default=0)

    for node in nodes.values():
        node.layer = depth(node, frozenset())


def build(family: Family, as_of: date) -> Structure:
    nodes: dict[str, Node] = {}
    for e in Entity.objects.filter(family=family):
        nodes[f"e{e.pk}"] = Node(f"e{e.pk}", False, e.name, e.get_entity_type_display(), e.pk, e.is_booked)
    links: list[Link] = []
    for i in active_interests(family, as_of).select_related("owner_entity", "owner_person"):
        if i.owner_person_id:
            key = f"p{i.owner_person_id}"
            source = nodes.setdefault(key, Node(key, True, i.owner_person.display_name, "Family member", i.owner_person_id))
        else:
            source = nodes.get(f"e{i.owner_entity_id}")
        target = nodes.get(f"e{i.owned_id}")
        if source is None or target is None:
            continue
        link = Link(source, target, i.percentage)
        source.owned.append(link)
        target.owners.append(link)
        links.append(link)
    if not nodes:
        return Structure([], [])
    _layers(nodes)

    rows: dict[int, list[Node]] = {}
    for node in sorted(nodes.values(), key=lambda n: n.name):
        rows.setdefault(node.layer, []).append(node)
    middle = lambda others, fallback: sum(others) / len(others) if others else fallback  # noqa: E731
    for _ in range(4):  # fewer crossings: each row follows the row above it, then each row follows the row below
        for layers, ends, other in ((sorted(rows)[1:], "owners", "source"), (sorted(rows, reverse=True)[1:], "owned", "target")):
            for layer in layers:
                # Where every node sits now, as a share of its row's width (taken before sorting: a row can't be read while it is sorted).
                place = {n.key: i / max(len(row), 1) for row in rows.values() for i, n in enumerate(row)}
                rows[layer].sort(key=lambda n: (middle([place[getattr(k, other).key] for k in getattr(n, ends)], 0.5), n.name))  # noqa: B023

    widest = max(len(row) for row in rows.values())
    width = PAD * 2 + widest * NODE_W + (widest - 1) * GAP_X
    for layer, row in rows.items():
        span = len(row) * NODE_W + (len(row) - 1) * GAP_X
        left = (width - span) / 2
        for n, node in enumerate(row):
            node.x, node.y = left + n * (NODE_W + GAP_X), PAD + layer * (NODE_H + GAP_Y)
    height = PAD * 2 + (max(rows) + 1) * NODE_H + max(rows) * GAP_Y

    # Lines leave the bottom of the owner and enter the top of the owned entity, each at its own point so they don't pile up.
    for node in nodes.values():
        node.owned.sort(key=lambda k: k.target.x)
        node.owners.sort(key=lambda k: k.source.x)
    for link in links:
        s, t = link.source, link.target
        sx = s.x + NODE_W * (s.owned.index(link) + 1) / (len(s.owned) + 1)
        tx = t.x + NODE_W * (t.owners.index(link) + 1) / (len(t.owners) + 1)
        sy, ty = s.y + NODE_H, t.y
        bend = (ty - sy) * 0.55
        link.path = f"M{sx:.1f},{sy:.1f} C{sx:.1f},{sy + bend:.1f} {tx:.1f},{ty - bend:.1f} {tx:.1f},{ty:.1f}"
        # Many owners on one entity: alternate the labels on two levels so they don't run into each other.
        link.label_x, link.label_y = tx, ty - (15 if len(t.owners) < 4 or t.owners.index(link) % 2 == 0 else 31)
    return Structure(sorted(nodes.values(), key=lambda n: (n.layer, n.x)), links, int(width), int(height))
