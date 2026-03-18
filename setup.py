"""
setup.py — One-time user setup script.

Run this locally ONCE to create user accounts and get your webhook URLs.

Usage:
  python setup.py
"""

import uuid
from models import create_tables, SessionLocal, User


def create_user(db, name: str) -> User:
    token = str(uuid.uuid4())
    user = User(name=name, token=token)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def main():
    print("HealthAgent — User Setup\n" + "=" * 40)
    create_tables()

    db = SessionLocal()
    try:
        existing = db.query(User).all()
        if existing:
            print("\nExisting users:")
            for u in existing:
                print(f"  {u.name}")
                print(f"    Webhook URL : POST  /webhook/{u.token}")
                print(f"    Dashboard   : GET   /dashboard/{u.token}")
            print()
            add_more = input("Add more users? (y/n): ").strip().lower()
            if add_more != "y":
                return

        print("\nEnter user names (press Enter with no name when done):")
        while True:
            name = input("  Name: ").strip()
            if not name:
                break
            user = create_user(db, name)
            print(f"\n  ✓ Created: {user.name}")
            print(f"    Webhook URL : POST  /webhook/{user.token}")
            print(f"    Dashboard   : GET   /dashboard/{user.token}")
            print(f"    (Save this token — it's your personal key!)\n")

        print("\nSetup complete! Next steps:")
        print("1. Copy .env.example → .env and fill in ANTHROPIC_API_KEY")
        print("2. Run the server:  uvicorn main:app --reload")
        print("3. On your iPhone: open Health Auto Export → add REST API automation")
        print("   → paste your Webhook URL above")
        print("4. Schedule it: daily at 7:00 AM")
        print("5. Open your Dashboard URL in any browser")

    finally:
        db.close()


if __name__ == "__main__":
    main()
