
def list_users():
    """List registered accounts and their current login-lock status."""
    client = _client()
    if client is None:
        raise RuntimeError("Supabase database is unavailable.")

    try:
        users_result = (
            client.table("users")
            .select("id,username,role,approved,created_at")
            .execute()
        )

        throttle_result = (
            client.table("login_throttle")
            .select("username,failed_attempts,locked_until,last_attempt")
            .execute()
        )

        now = time.time()
        throttle_by_account = {}

        # Account throttle keys may be either the username itself or
        # username|IP. Ignore address-only keys such as @ip|...
        for item in throttle_result.data or []:
            key = str(item.get("username") or "")

            if not key or key.startswith("@ip|"):
                continue

            account_name = key.split("|", 1)[0].casefold()
            locked_until = _timestamp(item.get("locked_until"))

            previous = throttle_by_account.get(account_name)

            entry = {
                "failed_attempts": int(item.get("failed_attempts") or 0),
                "locked_until": locked_until,
            }

            if previous is None:
                throttle_by_account[account_name] = entry
            else:
                previous["failed_attempts"] = max(
                    previous["failed_attempts"],
                    entry["failed_attempts"],
                )
                previous["locked_until"] = max(
                    previous["locked_until"],
                    entry["locked_until"],
                )

        output = []

        for user in users_result.data or []:
            username = str(user.get("username") or "")
            throttle = throttle_by_account.get(username.casefold(), {})
            locked_until = float(throttle.get("locked_until") or 0)
            seconds_left = max(0, int(locked_until - now))

            output.append({
                "id": user.get("id"),
                "username": username,
                "role": user.get("role") or "User",
                "approved": bool(user.get("approved")),
                "created_at": user.get("created_at"),
                "failed_attempts": int(
                    throttle.get("failed_attempts") or 0
                ),
                "locked_until": locked_until,
                "locked": seconds_left > 0,
                "locked_seconds_left": seconds_left,
            })

        output.sort(
            key=lambda item: (
                item["approved"],
                str(item.get("created_at") or ""),
            )
        )
        return output

    except Exception:
        logger.exception("Failed to list user accounts")
        raise


def approve_user(user_id):
    """Approve a pending account without changing its role."""
    client = _client()
    if client is None or not user_id:
        return 0

    try:
        result = (
            client.table("users")
            .update({"approved": 1})
            .eq("id", str(user_id))
            .select("id")
            .execute()
        )
        return len(result.data or [])
    except Exception:
        logger.exception("Failed to approve user")
        raise


def delete_user(user_id):
    """Delete an account while protecting the last approved administrator."""
    client = _client()
    if client is None:
        return False, "Database is unavailable."

    if not user_id:
        return False, "Missing user ID."

    try:
        target_result = (
            client.table("users")
            .select("id,username,role,approved")
            .eq("id", str(user_id))
            .limit(1)
            .execute()
        )

        if not target_result.data:
            return False, "User not found."

        target = target_result.data[0]
        username = str(target.get("username") or "")

        if (
            target.get("role") == "Administrator"
            and bool(target.get("approved"))
        ):
            admins_result = (
                client.table("users")
                .select("id")
                .eq("role", "Administrator")
                .eq("approved", 1)
                .execute()
            )

            if len(admins_result.data or []) <= 1:
                return False, "You can't remove the last administrator."

        # Delete the user first. Do not delete any camera, footage,
        # incident, or uplink records.
        deleted = (
            client.table("users")
            .delete()
            .eq("id", str(user_id))
            .select("id")
            .execute()
        )

        if not deleted.data:
            return False, "The account could not be deleted."

        # Clean up throttle rows associated with this username.
        client.table("login_throttle").delete().eq(
            "username", username
        ).execute()

        client.table("login_throttle").delete().like(
            "username", username + "|%"
        ).execute()

        return True, username

    except Exception:
        logger.exception("Failed to delete user")
        return False, "Database error while deleting the account."
