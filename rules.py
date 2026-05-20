"""Time-of-day rules that compute a target temperature.

A rule defines a daily time window `[start, end)` and how to derive the
target temperature from the current tank temperature inside that window.

Examples (from the original spec):
  * "After 12:00 — set target = current temp (don't kick on)."
    Rule(start="12:00", end="19:00", offset=0, cap=50)
  * "After 19:00 — raise target 6° above current, but never above 50
    (so the resistance never engages)."
    Rule(start="19:00", end="23:59", offset=6, cap=50)

Rules are evaluated in priority order; the first one whose window matches
"now" wins. If none match, a default rule (or no-op) is used.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, time
from pathlib import Path
from typing import Optional


@dataclass
class Rule:
    name: str
    start: str  # "HH:MM"
    end: str    # "HH:MM" exclusive; "00:00" means end-of-day
    offset: float = 0.0   # added to current temp
    floor: float = 40.0   # don't request a target below this
    cap: float = 50.0     # don't request a target above this (heat-pump-only)
    enabled: bool = True

    def matches(self, now: time) -> bool:
        s = _parse_hhmm(self.start)
        e = _parse_hhmm(self.end)
        if e == time(0, 0):
            # Treat "00:00" as end-of-day.
            return now >= s
        if s <= e:
            return s <= now < e
        # Wrap-around window (e.g. 22:00 -> 06:00).
        return now >= s or now < e

    def compute_target(self, current_temp: float) -> float:
        target = current_temp + self.offset
        target = max(self.floor, min(self.cap, target))
        # Lydos Hybrid only accepts integer steps.
        return float(round(target))


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


@dataclass
class RuleSet:
    rules: list[Rule] = field(default_factory=list)

    def active_rule(self, now: Optional[datetime] = None) -> Optional[Rule]:
        now = now or datetime.now()
        t = now.time()
        for rule in self.rules:
            if rule.enabled and rule.matches(t):
                return rule
        return None

    def to_json(self) -> str:
        return json.dumps({"rules": [asdict(r) for r in self.rules]}, indent=2)

    @classmethod
    def from_json(cls, raw: str) -> "RuleSet":
        data = json.loads(raw)
        return cls(rules=[Rule(**r) for r in data.get("rules", [])])

    @classmethod
    def default(cls) -> "RuleSet":
        return cls(rules=[
            Rule(name="Daytime hold",
                 start="12:00", end="19:00",
                 offset=0.0, floor=40.0, cap=50.0),
            Rule(name="Pre-night warm-up",
                 start="19:00", end="23:59",
                 offset=6.0, floor=40.0, cap=50.0),
            Rule(name="Night/off-peak hold",
                 start="23:59", end="12:00",
                 offset=0.0, floor=40.0, cap=50.0),
        ])


CONFIG_PATH = Path(__file__).with_name("rules.json")


def load_rules() -> RuleSet:
    if CONFIG_PATH.exists():
        try:
            return RuleSet.from_json(CONFIG_PATH.read_text())
        except Exception:
            pass
    return RuleSet.default()


def save_rules(ruleset: RuleSet) -> None:
    CONFIG_PATH.write_text(ruleset.to_json())
