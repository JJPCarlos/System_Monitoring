"""
Manage dashboard login accounts (run on the server PC, from the server folder).

    python manage_users.py add <username>
    python manage_users.py passwd <username>
    python manage_users.py remove <username>
    python manage_users.py list
    python manage_users.py resetadmin     (sets admin back to admin / admin123)

There is exactly one admin: the built-in "admin" account. It is hidden from
the dashboard's Users tab, which manages everyone else. This script is the
fallback — for example `python manage_users.py passwd admin` if the admin
password is forgotten.
"""
import getpass
import sqlite3
import sys

import server


def ask_password() -> str:
    while True:
        pw = getpass.getpass("Password: ")
        if len(pw) < server.MIN_PASSWORD_LENGTH:
            print(f"Must be at least {server.MIN_PASSWORD_LENGTH} characters.")
            continue
        if pw != getpass.getpass("Repeat password: "):
            print("Passwords didn't match.")
            continue
        return pw


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("add", "passwd", "remove", "list", "resetadmin"):
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "resetadmin":
        user, pw = server.DEFAULT_ADMIN
        server.init_users()                      # makes sure the admin account exists
        server.set_user_password(user, pw)       # also logs out any old admin sessions
        print(f"Admin reset: log in as '{user}' with password '{pw}', then change it.")
        return
    if cmd == "list":
        users = server.list_users(include_admin=True)
        print("\n".join(f'{u["username"]}  ({u["role"]})' for u in users) if users else "(no accounts yet)")
        return
    if len(sys.argv) < 3:
        print("Missing <username>.\n" + __doc__)
        return
    name = sys.argv[2].strip()
    if cmd == "add":
        pw = ask_password()
        try:
            if name.lower() == server.DEFAULT_ADMIN[0]:
                print("'admin' is the built-in admin account and already exists.")
            else:
                server.create_user(name, pw, "user")
                print(f"Account '{name}' created.")
        except sqlite3.IntegrityError:
            print(f"'{name}' already exists. Use: python manage_users.py passwd {name}")
    elif cmd == "passwd":
        print("Password changed." if server.set_user_password(name, ask_password()) else f"No account named '{name}'.")
    elif cmd == "remove":
        if server.get_role(name) == "admin":
            print("The admin account can't be removed.")
        else:
            print("Removed." if server.delete_user(name) else f"No account named '{name}'.")


if __name__ == "__main__":
    main()