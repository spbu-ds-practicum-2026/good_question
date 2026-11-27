import argparse
import os
import re
from datetime import UTC, datetime, timedelta

import jwt

ROLES = ("user", "admin")
SUBJECT = re.compile(r"[A-Za-z0-9._:@-]{1,128}")


def signing_key() -> str:
    key = os.environ.get("JWT_SECRET", "")
    if len(key.encode()) < 32:
        raise RuntimeError("JWT_SECRET must contain at least 32 bytes")
    return key


def issue_token(key: str, subject: str, role: str, lifetime: timedelta) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {"sub": subject, "role": role, "iat": now, "exp": now + lifetime},
        key,
        algorithm="HS256",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Issue a signed token for the local demo")
    parser.add_argument("role", choices=ROLES)
    parser.add_argument("--subject")
    parser.add_argument("--hours", type=int, default=24)
    args = parser.parse_args()
    subject = args.subject or f"demo-{args.role}"
    if not SUBJECT.fullmatch(subject) or not 1 <= args.hours <= 168:
        parser.error("Use a valid subject and a lifetime from 1 to 168 hours")
    print(issue_token(signing_key(), subject, args.role, timedelta(hours=args.hours)))


if __name__ == "__main__":
    main()
