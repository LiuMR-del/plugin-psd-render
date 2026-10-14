"""Parse custom-info fields and assign split values to similar PSD text targets."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

DEFAULT_SIMILARITY_THRESHOLD = 0.80
_VALUE_SEPARATOR_RE = re.compile(r"[,，;；]")
_NAME_CHARACTER_KEY_RE = re.compile(
    r"^enter\s+name\s*[+＋]\s*character\s+code(?:\s|[（(]|$)",
)
_NAME_CHARACTER_SEPARATOR_RE = re.compile(r"[\s,，;；.。]*")
_NAME_CHARACTER_PAIR_RE = re.compile(
    r"(?P<name>[^0-9#,，;；.。]+?)\s*#?\s*(?P<code>[0-9]+)"
    r"(?=$|[\s,，;；.。])"
)
_SPACE_SPLIT_KEYS = {"enter kids' name"}
_WRAPPING_QUOTES = '"“”'


def normalize_key(value: str) -> str:
    """Normalize a field key for exact and edit-distance matching."""
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def levenshtein_distance(left: str, right: str) -> int:
    """Return the Levenshtein edit distance using O(min(m, n)) memory."""
    if left == right:
        return 0
    if len(left) < len(right):
        left, right = right, left
    if not right:
        return len(left)

    previous = list(range(len(right) + 1))
    for i, left_char in enumerate(left, start=1):
        current = [i]
        for j, right_char in enumerate(right, start=1):
            current.append(min(
                current[-1] + 1,
                previous[j] + 1,
                previous[j - 1] + (left_char != right_char),
            ))
        previous = current
    return previous[-1]


def key_similarity(left: str, right: str) -> float:
    """Return normalized Levenshtein similarity in the range 0..1."""
    left_key = normalize_key(left)
    right_key = normalize_key(right)
    if left_key == right_key:
        return 1.0 if left_key else 0.0
    longest = max(len(left_key), len(right_key))
    if not longest:
        return 0.0
    return 1.0 - levenshtein_distance(left_key, right_key) / longest


@dataclass(frozen=True)
class TextTarget:
    path: tuple[str, ...]
    key: str

    @classmethod
    def from_parts(cls, path: list[str], key: str) -> "TextTarget":
        return cls(tuple(str(part) for part in path), str(key or "").strip())


@dataclass
class ParsedCustomInfo:
    pairs: list[tuple[str, str]] = field(default_factory=list)
    ignored_lines: list[str] = field(default_factory=list)


@dataclass
class SplitField:
    keys: list[str]
    values: list[str]

    @property
    def label(self) -> str:
        return self.keys[0]


@dataclass
class FieldMatch:
    target_key: str
    target_keys: list[str]
    targets: list[TextTarget]
    source_matches: list[tuple[str, float]]
    values: list[str]

    @property
    def expected_count(self) -> int:
        return len(self.targets)

    @property
    def actual_count(self) -> int:
        return len(self.values)


@dataclass
class AmbiguousMatch:
    source_key: str
    candidates: list[str]
    similarity: float


@dataclass
class Resolution:
    replacements: list[dict] = field(default_factory=list)
    matches: list[FieldMatch] = field(default_factory=list)
    count_mismatches: list[FieldMatch] = field(default_factory=list)
    ambiguous_matches: list[AmbiguousMatch] = field(default_factory=list)
    unmatched_keys: list[str] = field(default_factory=list)
    ignored_lines: list[str] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return bool(self.count_mismatches or self.ambiguous_matches)


@dataclass
class _TargetGroup:
    targets: list[TextTarget]
    keys: list[str]

    @property
    def label(self) -> str:
        return self.keys[0]


@dataclass
class _SourceGroup:
    entries: list[tuple[int, str, str]]
    keys: list[str]

    @property
    def label(self) -> str:
        return self.keys[0]


def parse_custom_info(text: str) -> ParsedCustomInfo:
    """Parse newline-separated ``key: value`` entries.

    The first half-width or full-width colon separates a key and value. A
    non-empty line without a key continues the previous value; leading text
    without a key is retained as an ignored line for preflight reporting.
    """
    result = ParsedCustomInfo()
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        positions = [pos for pos in (line.find(":"), line.find("：")) if pos >= 0]
        split_at = min(positions) if positions else -1
        key = line[:split_at].strip() if split_at >= 0 else ""
        if not key:
            if result.pairs:
                previous_key, previous_value = result.pairs[-1]
                continued = f"{previous_value}\n{line}" if previous_value else line
                result.pairs[-1] = (previous_key, continued)
            else:
                result.ignored_lines.append(line)
            continue
        result.pairs.append((key, line[split_at + 1:].strip()))
    return result


def split_field_value(value: str, key: str = "") -> list[str]:
    """Split a field value, with a key-specific whitespace rule for kids' names."""
    text = str(value or "")
    if normalize_key(key) in _SPACE_SPLIT_KEYS:
        parts = re.split(r"[,，;；\s]+", text.strip())
    else:
        parts = _VALUE_SEPARATOR_RE.split(text)
    cleaned = [part.strip().strip(_WRAPPING_QUOTES).strip() for part in parts]
    return [part for part in cleaned if part]


def is_name_character_key(key: str) -> bool:
    """Return whether a custom-info key uses the name/character pair format."""
    return bool(_NAME_CHARACTER_KEY_RE.match(normalize_key(key)))


def parse_name_character_pairs(value: str) -> list[tuple[str, str]] | None:
    """Parse a complete sequence of ``name + optional # + integer`` pairs."""
    text = str(value or "")
    position = 0
    pairs: list[tuple[str, str]] = []
    while position < len(text):
        separator = _NAME_CHARACTER_SEPARATOR_RE.match(text, position)
        position = separator.end()
        if position >= len(text):
            break

        match = _NAME_CHARACTER_PAIR_RE.match(text, position)
        if not match:
            return None
        name = match.group("name").strip().strip(_WRAPPING_QUOTES).strip()
        if not name:
            return None
        pairs.append((name, str(int(match.group("code")))))
        position = match.end()
    return pairs or None


def expand_name_character_pairs(
    pairs: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Expand valid special fields into Name/Character pairs in source order."""
    special_entries = [
        (index, value)
        for index, (key, value) in enumerate(pairs)
        if is_name_character_key(key)
    ]
    if not special_entries:
        return list(pairs)

    parsed_pairs: list[tuple[str, str]] = []
    for _, value in special_entries:
        parsed = parse_name_character_pairs(value)
        if parsed is None:
            return list(pairs)
        for name, code in parsed:
            parsed_pairs.append((name, code))

    expanded: list[tuple[str, str]] = []
    inserted = False
    for key, value in pairs:
        if not is_name_character_key(key):
            expanded.append((key, value))
            continue
        if inserted:
            continue
        for index, (name, code) in enumerate(parsed_pairs, start=1):
            expanded.append((f"Name{index}", name))
            expanded.append((f"Character{index}", code))
        inserted = True
    return expanded


def merge_path_replacements(*replacement_groups: list[dict]) -> list[dict]:
    """Merge replacements by full path; later groups override earlier values."""
    order: list[tuple[str, ...]] = []
    merged: dict[tuple[str, ...], dict] = {}
    for group in replacement_groups:
        for replacement in group:
            path = tuple(str(part) for part in replacement.get("path") or [])
            if not path:
                continue
            if path not in merged:
                order.append(path)
            merged[path] = {"path": list(path), "value": replacement.get("value", "")}
    return [merged[path] for path in order]


def _append_unique(items: list[str], value: str) -> None:
    key = normalize_key(value)
    if key and key not in {normalize_key(item) for item in items}:
        items.append(value)


def _component_indices(keys: list[str], threshold: float) -> list[list[int]]:
    """Return edit-distance connected components in first-seen order."""
    if not keys:
        return []

    parents = list(range(len(keys)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    for left in range(len(keys)):
        for right in range(left + 1, len(keys)):
            if key_similarity(keys[left], keys[right]) >= threshold:
                union(left, right)

    groups_by_root: dict[int, list[int]] = {}
    group_order: list[int] = []
    for index in range(len(keys)):
        root = find(index)
        if root not in groups_by_root:
            groups_by_root[root] = []
            group_order.append(root)
        groups_by_root[root].append(index)
    return [groups_by_root[root] for root in group_order]


def _build_target_groups(
    targets: list[TextTarget], threshold: float,
) -> list[_TargetGroup]:
    """Group similar PSD field keys while preserving target traversal order."""
    usable = [target for target in targets if normalize_key(target.key)]
    groups: list[_TargetGroup] = []
    for indices in _component_indices([target.key for target in usable], threshold):
        group = _TargetGroup(targets=[], keys=[])
        for index in indices:
            target = usable[index]
            group.targets.append(target)
            _append_unique(group.keys, target.key)
        groups.append(group)
    return groups


def _build_source_groups(
    pairs: list[tuple[str, str]], threshold: float,
) -> list[_SourceGroup]:
    """Group repeated/similar custom-info keys and retain original entry order."""
    usable = [
        (index, key, value)
        for index, (key, value) in enumerate(pairs)
        if normalize_key(key)
    ]
    groups: list[_SourceGroup] = []
    for indices in _component_indices([entry[1] for entry in usable], threshold):
        group = _SourceGroup(entries=[], keys=[])
        for index in indices:
            entry = usable[index]
            group.entries.append(entry)
            _append_unique(group.keys, entry[1])
        groups.append(group)
    return groups


def split_custom_info_fields(
    text: str,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> list[SplitField]:
    """Group similar custom-info keys and split their values in source order."""
    parsed = parse_custom_info(text)
    fields: list[SplitField] = []
    pairs = expand_name_character_pairs(parsed.pairs)
    for group in _build_source_groups(pairs, threshold):
        entries = sorted(group.entries, key=lambda entry: entry[0])
        values = [
            value
            for _, key, raw_value in entries
            for value in split_field_value(raw_value, key)
        ]
        fields.append(SplitField(keys=list(group.keys), values=values))
    return fields


def resolve_custom_info(
    text: str,
    targets: list[TextTarget],
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> Resolution:
    """Match custom-info keys to PSD target groups and build path replacements."""
    parsed = parse_custom_info(text)
    pairs = expand_name_character_pairs(parsed.pairs)
    resolution = Resolution(ignored_lines=list(parsed.ignored_lines))
    groups = _build_target_groups(targets, threshold)
    grouped_values: dict[int, list[tuple[int, str, str, float]]] = {}

    for source_group in _build_source_groups(pairs, threshold):
        scored: list[tuple[float, int]] = []
        for group_index, group in enumerate(groups):
            score = max(
                key_similarity(source_key, target_key)
                for source_key in source_group.keys
                for target_key in group.keys
            )
            if score >= threshold:
                scored.append((score, group_index))
        if not scored:
            for source_key in source_group.keys:
                _append_unique(resolution.unmatched_keys, source_key)
            continue

        best_score = max(score for score, _ in scored)
        best_groups = [index for score, index in scored if abs(score - best_score) < 1e-12]
        if len(best_groups) > 1:
            resolution.ambiguous_matches.append(AmbiguousMatch(
                source_key="、".join(source_group.keys),
                candidates=[groups[index].label for index in best_groups],
                similarity=best_score,
            ))
            continue

        group_index = best_groups[0]
        target_group = groups[group_index]
        bucket = grouped_values.setdefault(group_index, [])
        for source_index, source_key, raw_value in source_group.entries:
            source_score = max(key_similarity(source_key, key) for key in target_group.keys)
            bucket.append((source_index, source_key, raw_value, source_score))

    for group_index, entries in grouped_values.items():
        group = groups[group_index]
        entries.sort(key=lambda entry: entry[0])
        source_matches = [(source_key, score) for _, source_key, _, score in entries]
        values = [
            value
            for _, source_key, raw_value, _ in entries
            for value in split_field_value(raw_value, source_key)
        ]
        match = FieldMatch(
            target_key=group.label,
            target_keys=list(group.keys),
            targets=list(group.targets),
            source_matches=source_matches,
            values=values,
        )
        resolution.matches.append(match)
        if match.actual_count != match.expected_count:
            resolution.count_mismatches.append(match)
            continue
        resolution.replacements.extend(
            {"path": list(target.path), "value": value}
            for target, value in zip(match.targets, match.values)
        )

    return resolution
