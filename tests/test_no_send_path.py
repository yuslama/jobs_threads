"""R8 acceptance: no outbound email is sent by the system under any condition.

PRD section 9 makes this a design rule rather than a setting, so it is worth a
test that fails loudly if someone later wires a mail client in.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

FORBIDDEN = [
    r"\bimport\s+smtplib\b",
    r"\bfrom\s+smtplib\b",
    r"\bimport\s+aiosmtplib\b",
    r"\bsendmail\s*\(",
    r"\bsend_message\s*\(\s*msg",
    r"sendgrid",
    r"mailgun",
    r"\bSMTP\b",
]


def test_no_email_sending_code_exists_anywhere_in_src():
    offenders = []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pattern in FORBIDDEN:
            if re.search(pattern, text):
                offenders.append(f"{path.name}: {pattern}")
    assert offenders == [], (
        "an email send path appeared in src/; PRD section 9 rules this out: " + ", ".join(offenders)
    )
