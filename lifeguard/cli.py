"""Admin command line:  python -m lifeguard.cli <command>

  unlock <username>                    clear a login lockout
  create-admin <username> <password>   create an administrator, or reset one's password
"""
import sys

from werkzeug.security import generate_password_hash

from . import db

USAGE = __doc__


def main(argv):
    db.init_db()
    if len(argv) == 2 and argv[0] == "unlock":
        db.reset_throttle(argv[1])
        print(f"Lock cleared for '{argv[1]}'.")
    elif len(argv) == 3 and argv[0] == "create-admin":
        username, password = argv[1], argv[2]
        if db.get_user(username):
            with db.connect() as c:
                c.execute("UPDATE users SET password_hash=?, role='Administrator', approved=1 "
                          "WHERE username=? COLLATE NOCASE",
                          (generate_password_hash(password), username))
            db.reset_throttle(username)
            print(f"Administrator '{username}' password reset.")
        else:
            ok, err = db.register_user(username, password, "Administrator")
            if not ok:
                print("Error:", err)
                return 1
            with db.connect() as c:
                c.execute("UPDATE users SET approved=1 WHERE username=? COLLATE NOCASE", (username,))
            print(f"Administrator '{username}' created.")
    else:
        print(USAGE)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
