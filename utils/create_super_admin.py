"""Create (or promote) a super admin — for bootstrapping the very first
account, since /auth/register requires an existing admin to call it.

    uv run python -m utils.create_super_admin --email me@example.com --first-name Ama --last-name Mensah
    # prompts for a password (hidden input) if --password is omitted

If the email already exists, it is promoted to super admin (role, approval
and active flags updated) instead of failing — the common case is that
whoever needs this already signed up as a plain user.
"""
import argparse
import getpass
import sys

from pydantic import ValidationError

from app.core.database import SessionLocal
from app.authentication.enums import UserRole, ApprovalStatus
from app.authentication.schema import UserCreate
from app.authentication.repository.userrepository import UserRepository
from app.service.user_service import UserService


def create_or_promote_super_admin(session, email: str, password: str, first_name: str, last_name: str):
    repo = UserRepository(session)
    existing = repo.get_user_by_email(email)
    if existing:
        existing.role = UserRole.SUPER_ADMIN
        existing.approval_status = ApprovalStatus.APPROVED
        existing.is_active = True
        existing.cluster_id = None  # super admins aren't tied to one cluster
        session.commit()
        session.refresh(existing)
        return existing, True

    # Runs the same password-strength validator as normal signup, then
    # UserService hashes it and inserts the row — the exact path /auth/register uses.
    user_data = UserCreate(
        first_name=first_name, last_name=last_name, email=email, password=password,
        role=UserRole.SUPER_ADMIN, approval_status=ApprovalStatus.APPROVED,
    )
    user = UserService(session).create_user(user_data)
    return user, False


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", required=True)
    parser.add_argument("--first-name", required=True)
    parser.add_argument("--last-name", required=True)
    parser.add_argument("--password", help="Prompted securely if omitted")
    args = parser.parse_args()

    password = args.password
    if not password:
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Confirm password: "):
            print("Passwords do not match.", file=sys.stderr)
            sys.exit(1)

    session = SessionLocal()
    try:
        user, promoted = create_or_promote_super_admin(
            session, args.email.strip().lower(), password, args.first_name.strip(), args.last_name.strip()
        )
    except ValidationError as e:
        print(f"Invalid input:\n{e}", file=sys.stderr)
        sys.exit(1)
    finally:
        session.close()

    action = "Promoted existing user to" if promoted else "Created"
    print(f"{action} super admin: {user.email} (id={user.id})")


if __name__ == "__main__":
    main()
