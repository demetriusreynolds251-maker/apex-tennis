"""
DEMETRIUS — Discord message classification (v7.9.6, Step 9 + Image Intake)
"""
import re

_RESULT_MARKERS = ["✅", "❌", "PUSH", "VOID", "WIN", "LOSS"]
_APPROVAL_PATTERN = re.compile(r"\b(APPROVE|REJECT)\s+(\d+)\b", re.IGNORECASE)


def classify_slip_message(text: str) -> str:
    if not text or not text.strip():
        return None
    upper = text.upper()
    if any(marker in text for marker in ("✅", "❌")) or any(
        re.search(rf"\b{marker}\b", upper) for marker in ("WIN", "LOSS", "PUSH", "VOID")
    ):
        return "result"
    if re.search(r"\b(MORE|LESS)\b", upper):
        return "entry"
    return None


def parse_approval_command(text: str):
    if not text:
        return None
    m = _APPROVAL_PATTERN.search(text)
    if not m:
        return None
    return m.group(1).upper(), int(m.group(2))


def classify_image_content(parsed_data) -> str:
    if not parsed_data:
        return None
    if isinstance(parsed_data, dict):
        if parsed_data.get("type") == "moneyline":
            return "moneyline"
        if "player_odds" in parsed_data or "opponent_odds" in parsed_data:
            return "moneyline"
    if isinstance(parsed_data, list) and len(parsed_data) > 0:
        if any("stat_type" in item for item in parsed_data
               if isinstance(item, dict)):
            return "props"
    return None
