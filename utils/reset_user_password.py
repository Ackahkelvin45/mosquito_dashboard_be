"""Set one password on one or more existing accounts by email — for an admin
issuing (or resetting) credentials directly, bypassing the OTP email flow.

    uv run python -m utils.reset_user_password \\
        --email a@example.com --email b@example.com \\
        --password '@User123'
    # prompts for the password (hidden input) if --password is omitted

Uses the exact same UserRepository.update_password() call reset_password()
uses after OTP verification, so the result is bit-for-bit what a normal
self-service reset would produce — just skipping the OTP step.

Reports each email as updated or not-found; never fails the whole batch for
one typo.
"""
import argparse
import getpass
import sys

from app.core.database import SessionLocal
from app.core.security.hashHelper import HashHelper
from app.authentication.repository.userrepository import UserRepository


def reset_passwords(session, emails: list[str], password: str) -> tuple[list[str], list[str]]:
    from app.audit.models import AuditAction
    from app.audit.recorder import audit

    repo = UserRepository(session)
    hashed = HashHelper.hash_password(password)
    updated, not_found = [], []
    for email in emails:
        user = repo.get_user_by_email(email)
        if not user:
            not_found.append(email)
            continue
        repo.update_password(user, hashed)
        # Out-of-band credential change: kill the account's existing sessions
        # and leave a trail — this CLI bypasses the OTP flow entirely, making
        # it the highest-value action to audit.
        user.token_version = int(user.token_version or 0) + 1
        session.commit()
        audit(session, AuditAction.PASSWORD_RESET_CLI,
              actor_email=f"cli:{getpass.getuser()}",
              target_type="user", target_id=user.id, detail={"email": email})
        updated.append(email)
    return updated, not_found


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", action="append", required=True, dest="emails",
                        help="Repeatable — one per account")
    parser.add_argument("--password", help="Prompted securely if omitted")
    args = parser.parse_args()

    password = args.password
    if not password:
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Confirm password: "):
            print("Passwords do not match.", file=sys.stderr)
            sys.exit(1)

    emails = [e.strip().lower() for e in args.emails]

    session = SessionLocal()
    try:
        updated, not_found = reset_passwords(session, emails, password)
    finally:
        session.close()

    for email in updated:
        print(f"updated: {email}")
    for email in not_found:
        print(f"NOT FOUND (skipped): {email}", file=sys.stderr)
    print(f"\n{len(updated)} updated, {len(not_found)} not found.")


if __name__ == "__main__":
    main()
