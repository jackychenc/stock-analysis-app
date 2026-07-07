"""Generate the ADMIN_PASSWORD_HASH value for .env.

Usage: python scripts/hash_password.py   (prompts; never echoes the password)
"""

import getpass
import sys

sys.path.insert(0, ".")
from app.core.security import hash_password  # noqa: E402

if __name__ == "__main__":
    pw = getpass.getpass("Password: ")
    confirm = getpass.getpass("Confirm: ")
    if pw != confirm:
        print("Passwords do not match.", file=sys.stderr)
        sys.exit(1)
    print(hash_password(pw))
