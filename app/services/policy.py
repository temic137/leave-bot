import json
from dataclasses import dataclass
from pathlib import Path
import re

from app.core.config import settings


@dataclass(frozen=True)
class LeaveTypePolicy:
    key: str
    display_name: str
    annual_days: float
    requires_document: bool
    requires_hr: bool
    allow_negative_balance: bool


class LeavePolicy:
    def __init__(self, path: str | Path = settings.leave_policy_path):
        self.path = Path(path)
        self._rules = self._load()

    def _load(self) -> dict[str, LeaveTypePolicy]:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return self._parse(raw)

    def _parse(self, raw: dict) -> dict[str, LeaveTypePolicy]:
        return {
            key: LeaveTypePolicy(
                key=key,
                display_name=value["display_name"],
                annual_days=float(value["annual_days"]),
                requires_document=bool(value["requires_document"]),
                requires_hr=bool(value["requires_hr"]),
                allow_negative_balance=bool(value["allow_negative_balance"]),
            )
            for key, value in raw.items()
        }

    def reload(self) -> None:
        self._rules = self._load()

    def get(self, leave_type: str) -> LeaveTypePolicy:
        try:
            return self._rules[leave_type]
        except KeyError as exc:
            raise ValueError(f"Unknown leave type: {leave_type}") from exc

    def all(self) -> dict[str, LeaveTypePolicy]:
        return self._rules

    def upsert(
        self,
        key: str,
        display_name: str,
        annual_days: float,
        requires_document: bool,
        requires_hr: bool,
        allow_negative_balance: bool,
    ) -> LeaveTypePolicy:
        normalized_key = self._normalize_key(key)
        if annual_days < 0:
            raise ValueError("annual_days cannot be negative")

        raw = self.to_raw()
        raw[normalized_key] = {
            "display_name": display_name.strip(),
            "annual_days": annual_days,
            "requires_document": requires_document,
            "requires_hr": requires_hr,
            "allow_negative_balance": allow_negative_balance,
        }
        self.path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        self._rules = self._parse(raw)
        return self._rules[normalized_key]

    def replace_raw_text(self, text: str) -> dict[str, LeaveTypePolicy]:
        raw = self._parse_plain_text(text)
        parsed = self._parse(raw)
        self.path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        self._rules = parsed
        return self._rules

    def to_raw(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def to_raw_text(self) -> str:
        lines = []
        for rule in self.all().values():
            document = "Document required" if rule.requires_document else "No document required"
            approval = "HR approval required" if rule.requires_hr else "Manager approval only"
            negative = "Negative balance allowed" if rule.allow_negative_balance else "Negative balance not allowed"
            lines.append(f"{rule.display_name}: {rule.annual_days:g} days maximum. {document}. {approval}. {negative}.")
        return "\n".join(lines) + "\n"

    def _normalize_key(self, key: str) -> str:
        normalized = re.sub(r"[^a-z0-9_]+", "_", key.strip().lower()).strip("_")
        if not normalized:
            raise ValueError("Leave type key is required")
        return normalized

    def _existing_key_for_display_name(self, display_name: str) -> str | None:
        target = display_name.strip().lower()
        for key, rule in self._rules.items():
            if rule.display_name.strip().lower() == target:
                return key
        return None

    def _parse_plain_text(self, text: str) -> dict[str, dict]:
        raw: dict[str, dict] = {}
        for line_number, original_line in enumerate(text.splitlines(), start=1):
            line = original_line.strip()
            if not line or line.startswith("#"):
                continue
            display_name, rule_text = self._split_policy_line(line, line_number)
            annual_days = self._extract_days(rule_text, line_number)
            normalized = rule_text.lower()
            key = self._existing_key_for_display_name(display_name) or self._normalize_key(display_name)
            raw[key] = {
                "display_name": display_name,
                "annual_days": annual_days,
                "requires_document": self._extract_requires_document(normalized),
                "requires_hr": self._extract_requires_hr(normalized),
                "allow_negative_balance": self._extract_allow_negative(normalized),
            }
        if not raw:
            raise ValueError("Policy text must contain at least one leave type")
        return raw

    def _split_policy_line(self, line: str, line_number: int) -> tuple[str, str]:
        stripped = re.sub(r"^[-*]\s+", "", line).strip()
        if ":" in stripped:
            name, body = stripped.split(":", 1)
        elif " - " in stripped:
            name, body = stripped.split(" - ", 1)
        else:
            match = re.match(r"(.+?)\s+(\d+(?:\.\d+)?\s+days?.*)$", stripped, flags=re.IGNORECASE)
            if not match:
                raise ValueError(f"Line {line_number}: use a format like 'Sick Leave: 10 days maximum. Document required.'")
            name, body = match.group(1), match.group(2)

        name = name.strip()
        body = body.strip()
        if not name or not body:
            raise ValueError(f"Line {line_number}: leave type name and rules are required")
        return name, body

    def _extract_days(self, text: str, line_number: int) -> float:
        match = re.search(r"(\d+(?:\.\d+)?)\s+days?", text, flags=re.IGNORECASE)
        if not match:
            raise ValueError(f"Line {line_number}: include the number of days, for example '10 days maximum'")
        return float(match.group(1))

    def _extract_requires_document(self, text: str) -> bool:
        if any(phrase in text for phrase in ("no document", "document not required", "without document")):
            return False
        return any(phrase in text for phrase in ("document required", "requires document", "proof required", "certificate required"))

    def _extract_requires_hr(self, text: str) -> bool:
        if any(phrase in text for phrase in ("no hr", "without hr", "manager approval only", "manager only")):
            return False
        return "hr" in text or "human resources" in text

    def _extract_allow_negative(self, text: str) -> bool:
        if any(phrase in text for phrase in ("negative balance not allowed", "no negative", "cannot go negative")):
            return False
        return "negative" in text and any(phrase in text for phrase in ("allowed", "allow", "can go"))


leave_policy = LeavePolicy()
