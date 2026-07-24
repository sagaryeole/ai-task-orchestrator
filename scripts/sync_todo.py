#!/usr/bin/env python3
"""Sync completed checklist items from Todo.md into CompletedTodo.md.

Rules implemented:
- Case-insensitive TODO/Completed file discovery.
- Heading-aware move semantics for full/partial/none-complete sections.
- Preserve top-level title in both files; never duplicate/move title as section.
- Merge into existing completed headings (same level + title, case-insensitive).
- Idempotent on reruns (deduplicates by exact line per heading block).
"""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
CHECKBOX_RE = re.compile(r"^\s*[-*]\s\[([ xX])\]\s+.+$")


@dataclass
class LineEntry:
    text: str
    is_checkbox: bool
    checked: bool


@dataclass
class Node:
    level: int
    title: str
    body: list[tuple[str, object]] = field(default_factory=list)


@dataclass
class ParseDoc:
    title_block: list[str]
    top_misc: list[str]
    sections: list[Node]


@dataclass
class Status:
    checked: int
    unchecked: int

    @property
    def has_any_completed(self) -> bool:
        return self.checked > 0

    @property
    def is_full_complete(self) -> bool:
        return self.checked > 0 and self.unchecked == 0


def _normalize_name(name: str) -> str:
    return name.lower()


def find_todo_file(root: Path) -> Path:
    candidates = []
    for child in root.iterdir():
        if child.is_file() and _normalize_name(child.name) == "todo.md":
            candidates.append(child)
    if not candidates:
        raise FileNotFoundError("No TODO file found (expected TODO.md/Todo.md case-insensitive).")
    return sorted(candidates, key=lambda p: p.name.lower())[0]


def find_or_create_completed_file(root: Path) -> Path:
    accepted = {
        "completedtodo.md",
        "completed_todo.md",
        "completed-todo.md",
    }
    for child in root.iterdir():
        if child.is_file() and _normalize_name(child.name) in accepted:
            return child
    target = root / "CompletedTodo.md"
    target.write_text("# Completed Todo\n\n", encoding="utf-8")
    return target


def parse_doc(lines: list[str]) -> ParseDoc:
    title_block: list[str] = []
    top_misc: list[str] = []

    i = 0
    found_h2 = False
    while i < len(lines):
        m = HEADING_RE.match(lines[i])
        if m and len(m.group(1)) >= 2:
            found_h2 = True
            break
        title_block.append(lines[i])
        i += 1

    if not found_h2:
        return ParseDoc(title_block=title_block, top_misc=[], sections=[])

    root = Node(level=1, title="__ROOT__")
    stack: list[Node] = [root]

    while i < len(lines):
        line = lines[i]
        hm = HEADING_RE.match(line)
        if hm and len(hm.group(1)) >= 2:
            level = len(hm.group(1))
            title = hm.group(2).strip()
            node = Node(level=level, title=title)
            while stack and stack[-1].level >= level:
                stack.pop()
            if not stack:
                stack = [root]
            stack[-1].body.append(("child", node))
            stack.append(node)
            i += 1
            continue

        cm = CHECKBOX_RE.match(line)
        if cm:
            checked = cm.group(1).lower() == "x"
            entry = LineEntry(text=line, is_checkbox=True, checked=checked)
        else:
            entry = LineEntry(text=line, is_checkbox=False, checked=False)

        if len(stack) == 1:
            top_misc.append(line)
        else:
            stack[-1].body.append(("line", entry))
        i += 1

    sections = [obj for kind, obj in root.body if kind == "child"]
    return ParseDoc(title_block=title_block, top_misc=top_misc, sections=sections)


def compute_status(node: Node) -> Status:
    checked = 0
    unchecked = 0
    for kind, obj in node.body:
        if kind == "line":
            line = obj
            assert isinstance(line, LineEntry)
            if line.is_checkbox:
                if line.checked:
                    checked += 1
                else:
                    unchecked += 1
        else:
            child = obj
            assert isinstance(child, Node)
            s = compute_status(child)
            checked += s.checked
            unchecked += s.unchecked
    return Status(checked=checked, unchecked=unchecked)


def clone_full(node: Node) -> Node:
    out = Node(level=node.level, title=node.title)
    for kind, obj in node.body:
        if kind == "line":
            line = obj
            assert isinstance(line, LineEntry)
            out.body.append(("line", LineEntry(line.text, line.is_checkbox, line.checked)))
        else:
            child = obj
            assert isinstance(child, Node)
            out.body.append(("child", clone_full(child)))
    return out


def clone_for_todo(node: Node) -> Optional[Node]:
    status = compute_status(node)
    if status.checked == 0:
        return clone_full(node)
    if status.is_full_complete:
        return None

    out = Node(level=node.level, title=node.title)
    for kind, obj in node.body:
        if kind == "line":
            line = obj
            assert isinstance(line, LineEntry)
            if line.is_checkbox:
                if not line.checked:
                    out.body.append(("line", LineEntry(line.text, True, False)))
            else:
                # In mixed headings, descriptive text stays in TODO.
                out.body.append(("line", LineEntry(line.text, False, False)))
        else:
            child = obj
            assert isinstance(child, Node)
            child_status = compute_status(child)
            if child_status.is_full_complete:
                continue
            child_todo = clone_for_todo(child)
            if child_todo is not None:
                out.body.append(("child", child_todo))

    if not out.body:
        return None
    return out


def clone_for_completed(node: Node) -> Optional[Node]:
    status = compute_status(node)
    if status.checked == 0:
        return None
    if status.is_full_complete:
        return clone_full(node)

    out = Node(level=node.level, title=node.title)
    for kind, obj in node.body:
        if kind == "line":
            line = obj
            assert isinstance(line, LineEntry)
            if line.is_checkbox and line.checked:
                out.body.append(("line", LineEntry(line.text, True, True)))
        else:
            child = obj
            assert isinstance(child, Node)
            child_completed = clone_for_completed(child)
            if child_completed is not None:
                out.body.append(("child", child_completed))

    if not out.body:
        return None
    return out


def render_nodes(nodes: Iterable[Node]) -> list[str]:
    out: list[str] = []
    for node in nodes:
        out.append("#" * node.level + " " + node.title)
        out.extend(render_body(node.body))
    return out


def render_body(body: list[tuple[str, object]]) -> list[str]:
    out: list[str] = []
    for kind, obj in body:
        if kind == "line":
            line = obj
            assert isinstance(line, LineEntry)
            out.append(line.text)
        else:
            child = obj
            assert isinstance(child, Node)
            out.append("#" * child.level + " " + child.title)
            out.extend(render_body(child.body))
    return out


def index_children(parent: Node) -> list[Node]:
    return [obj for kind, obj in parent.body if kind == "child"]


def same_heading(a: Node, b: Node) -> bool:
    return a.level == b.level and a.title.strip().lower() == b.title.strip().lower()


def dedupe_append_line(node: Node, entry: LineEntry) -> bool:
    for kind, obj in node.body:
        if kind == "line":
            line = obj
            assert isinstance(line, LineEntry)
            if line.text == entry.text:
                return False
    node.body.append(("line", entry))
    return True


def merge_into_node(target: Node, incoming: Node) -> int:
    moved = 0
    for kind, obj in incoming.body:
        if kind == "line":
            line = obj
            assert isinstance(line, LineEntry)
            if dedupe_append_line(target, LineEntry(line.text, line.is_checkbox, line.checked)):
                if line.is_checkbox and line.checked:
                    moved += 1
        else:
            child = obj
            assert isinstance(child, Node)
            existing_child = None
            for tkind, tobj in target.body:
                if tkind != "child":
                    continue
                tchild = tobj
                assert isinstance(tchild, Node)
                if same_heading(tchild, child):
                    existing_child = tchild
                    break
            if existing_child is None:
                target.body.append(("child", clone_full(child)))
                moved += count_checked_items(child)
            else:
                moved += merge_into_node(existing_child, child)
    return moved


def merge_completed(existing_sections: list[Node], incoming_sections: list[Node]) -> int:
    moved = 0
    for inc in incoming_sections:
        match = None
        for ex in existing_sections:
            if same_heading(ex, inc):
                match = ex
                break
        if match is None:
            existing_sections.append(clone_full(inc))
            moved += count_checked_items(inc)
        else:
            moved += merge_into_node(match, inc)
    return moved


def count_checked_items(node: Node) -> int:
    total = 0
    for kind, obj in node.body:
        if kind == "line":
            line = obj
            assert isinstance(line, LineEntry)
            if line.is_checkbox and line.checked:
                total += 1
        else:
            child = obj
            assert isinstance(child, Node)
            total += count_checked_items(child)
    return total


def gather_fully_removed_headings(nodes: list[Node]) -> list[str]:
    removed: list[str] = []
    for n in nodes:
        st = compute_status(n)
        if st.is_full_complete:
            removed.append(("#" * n.level) + " " + n.title)
        else:
            for kind, obj in n.body:
                if kind == "child":
                    child = obj
                    assert isinstance(child, Node)
                    removed.extend(gather_fully_removed_headings([child]))
    return removed


def write_doc(path: Path, title_block: list[str], top_misc: list[str], sections: list[Node]) -> None:
    lines: list[str] = []
    lines.extend(title_block)
    if top_misc:
        if lines and lines[-1] != "":
            lines.append("")
        lines.extend(top_misc)
    rendered = render_nodes(sections)
    if rendered:
        if lines and lines[-1] != "":
            lines.append("")
        lines.extend(rendered)
    text = "\n".join(lines).rstrip() + "\n"
    path.write_text(text, encoding="utf-8")


def ensure_title_block(doc: ParseDoc, fallback_title: str) -> list[str]:
    if doc.title_block:
        return doc.title_block
    return [fallback_title, ""]


def run(directory: Path) -> int:
    todo_path = find_todo_file(directory)
    completed_path = find_or_create_completed_file(directory)

    todo_lines = todo_path.read_text(encoding="utf-8").splitlines()
    completed_lines = completed_path.read_text(encoding="utf-8").splitlines()

    todo_doc = parse_doc(todo_lines)
    completed_doc = parse_doc(completed_lines)

    completed_from_todo: list[Node] = []
    todo_remaining: list[Node] = []

    for section in todo_doc.sections:
        c = clone_for_completed(section)
        if c is not None:
            completed_from_todo.append(c)
        t = clone_for_todo(section)
        if t is not None:
            todo_remaining.append(t)

    moved = merge_completed(completed_doc.sections, completed_from_todo)
    removed_headings = gather_fully_removed_headings(todo_doc.sections)

    todo_title = ensure_title_block(todo_doc, "# Todo")
    completed_title = ensure_title_block(completed_doc, "# Completed Todo")

    write_doc(todo_path, todo_title, todo_doc.top_misc, todo_remaining)
    write_doc(completed_path, completed_title, completed_doc.top_misc, completed_doc.sections)

    if moved == 0:
        print("Nothing newly completed")
    else:
        print(f"Moved {moved} completed item(s)")

    if removed_headings:
        print("Fully removed headings from TODO:")
        for h in removed_headings:
            print(f"- {h}")
    else:
        print("Fully removed headings from TODO: none")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Move completed TODO items into CompletedTodo.md")
    parser.add_argument("directory", nargs="?", default=".", help="Project directory (default: current dir)")
    args = parser.parse_args()

    directory = Path(args.directory).resolve()
    if not directory.exists() or not directory.is_dir():
        raise SystemExit(f"Directory not found: {directory}")

    return run(directory)


if __name__ == "__main__":
    raise SystemExit(main())
