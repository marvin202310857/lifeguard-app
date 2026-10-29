
"""Supabase-backed database helpers for LifeGuard authentication and audit logging.

This module intentionally uses the existing public tables. It does not create,
truncate, or migrate tables, and it does not touch incident or camera-uplink data.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone

from werkzeug.security import check_password_hash, generate_password_hash

logger = logging.getLogger(__name__)

try:
    from . import settings as cfg
except ImportError:
    cfg = None


def get_supabase_client():
    """Create a Supabase client from server-side environment variables."""
    url = (
        os.environ.get("SUPABASE_URL")
        or os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
        or ""
    ).strip()

    key = (
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_KEY")
        or os.environ.get("NEXT_PUBLIC_SUPABASE_ANON_KEY")
        or ""
    ).strip()

    if not url or not key:
        logger.error(
            "Supabase configuration is missing; check deployment environment variables."
        )
        return None

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        from supabase import create_client
        return create_client(url, key)
    except Exception:
        logger.exception("Could not initialize Supabase client")
        return None


def init_db():
    """Compatibility entry point; Supabase tables are managed in Supabase."""
    return get_supabase_client()


supabase = get_supabase_client()


def _client():
    return get_supabase_client()


def _as_bool(value) -> bool:
    """Handle boolean and integer values returned by Supabase."""
    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return value != 0

    if isinstance(value, str):
        return value.strip().casefold() in {
            "1", "true", "t", "yes", "y"
        }

    return False


def _timestamp(value) -> float:
    """Convert numeric or ISO-8601 timestamps to Unix seconds."""
    if value is None or value == "":
        return 0.0

    try:
        return float(value)
    except (TypeError, ValueError):
        pass

    try:
        text = str(value).strip()

        if text.endswith("Z"):
            text = text[:-1] + "+00:00"

        parsed = datetime.fromisoformat(text)

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.timestamp()

    except (TypeError, ValueError, OverflowError):
        return 0.0


def get_user(username: str):
    if not username:
        return None

    client = _client()
    if client is None:
        return None

    try:
        result = (
            client.table("users")
            .select("*")
            .eq("username", username)
            .limit(1)
            .execute()
        )

        return result.data[0] if result.data else None

    except Exception:
        logger.exception("Failed to retrieve user")
        return None


def get_user_by_id(user_id):
    if not user_id:
        return None

    client = _client()
    if client is None:
        return None

    try:
        result = (
            client.table("users")
            .select("*")
            .eq("id", str(user_id))
            .limit(1)
            .execute()
        )

        return result.data[0] if result.data else None

    except Exception:
        logger.exception("Failed to retrieve user by ID")
        return None


def is_legacy_hash(hash_str: str) -> bool:
    if not isinstance(hash_str, str) or not hash_str:
        return False

    if re.fullmatch(
        r"[a-fA-F0-9]{32}|[a-fA-F0-9]{40}|[a-fA-F0-9]{64}",
        hash_str,
    ):
        return True

    return hash_str.startswith(
        ("pbkdf2:sha256:", "sha256$", "md5$", "sha1$")
    )


def verify_password(user, password: str) -> bool:
    """Verify Werkzeug hashes and recognized legacy MD5/SHA1/SHA256 hashes."""
    if not user or not isinstance(password, str) or not password:
        return False

    stored = (
        user.get("password_hash")
        if isinstance(user, dict)
        else getattr(user, "password_hash", None)
    )

    if not isinstance(stored, str) or not stored:
        return False

    try:
        if check_password_hash(stored, password):
            return True
    except (ValueError, TypeError):
        pass

    try:
        stored_value = stored

        if re.fullmatch(r"[a-fA-F0-9]{32}", stored_value):
            candidate = hashlib.md5(
                password.encode("utf-8")
            ).hexdigest()

        elif re.fullmatch(r"[a-fA-F0-9]{40}", stored_value):
            candidate = hashlib.sha1(
                password.encode("utf-8")
            ).hexdigest()

        elif re.fullmatch(r"[a-fA-F0-9]{64}", stored_value):
            candidate = hashlib.sha256(
                password.encode("utf-8")
            ).hexdigest()

        elif stored_value.startswith("sha256$"):
            candidate = hashlib.sha256(
                password.encode("utf-8")
            ).hexdigest()
            stored_value = stored_value.split("$", 1)[1]

        elif stored_value.startswith("sha1$"):
            candidate = hashlib.sha1(
                password.encode("utf-8")
            ).hexdigest()
            stored_value = stored_value.split("$", 1)[1]

        elif stored_value.startswith("md5$"):
            candidate = hashlib.md5(
                password.encode("utf-8")
            ).hexdigest()
            stored_value = stored_value.split("$", 1)[1]

        else:
            return False

        return hmac.compare_digest(
            candidate.lower(), stored_value.lower()
        )

    except Exception:
        logger.exception("Password verification failed")
        return False


def set_password(user_id, password: str) -> bool:
    return rehash_password(user_id, password)


def rehash_password(user_id, password: str) -> bool:
    client = _client()

    if (
        client is None
        or not user_id
        or not isinstance(password, str)
        or not password
    ):
        return False

    try:
        result = (
            client.table("users")
            .update({
                "password_hash": generate_password_hash(password)
            })
            .eq("id", str(user_id))
            .select("id")
            .execute()
        )

        return bool(result.data)

    except Exception:
        logger.exception("Failed to upgrade password hash")
        return False


def create_user(username: str, password: str, role: str = "User"):
    """Create a pending account; registration cannot grant admin role."""
    return register_user(username, password, role)[0]


def register_user(username: str, password: str, role: str = "User"):
    username = (username or "").strip()[:80]

    if len(username) < 3:
        return False, "Username must contain at least 3 characters."

    if not isinstance(password, str) or len(password) < 8:
        return False, "Password must contain at least 8 characters."

    client = _client()
    if client is None:
        return False, "Database is unavailable. Please try again later."

    try:
        existing = (
            client.table("users")
            .select("id")
            .eq("username", username)
            .limit(1)
            .execute()
        )

        if existing.data:
            return False, "That username is already registered."

        payload = {
            "id": str(uuid.uuid4()),
            "username": username,
            "password_hash": generate_password_hash(password),
            "role": "User",
            "approved": 0,
        }

        result = client.table("users").insert(payload).execute()

        if not result.data:
            return False, "The database did not confirm account creation."

        return True, None

    except Exception:
        logger.exception("Failed to register user")
        return (
            False,
            "Could not create the account. Check the database connection and try again.",
        )


def update_last_login(user_id) -> None:
    """The existing users table has no last_login column."""
    return None


def _throttle_settings(args, kwargs):
    max_attempts = kwargs.get("max_attempts")
    lock_seconds = kwargs.get("lock_seconds")

    if args:
        if len(args) >= 1 and isinstance(args[0], (int, float)):
            max_attempts = args[0]

        if len(args) >= 2 and isinstance(args[1], (int, float)):
            lock_seconds = args[1]

    if max_attempts is None:
        max_attempts = (
            getattr(cfg, "MAX_FAILED_ATTEMPTS", 5) if cfg else 5
        )

    if lock_seconds is None:
        lock_seconds = (
            getattr(cfg, "LOCK_SECONDS", 900) if cfg else 900
        )

    return max(1, int(max_attempts)), max(1, int(lock_seconds))


def _get_throttle(key: str):
    client = _client()

    if client is None or not key:
        return None

    try:
        result = (
            client.table("login_throttle")
            .select(
                "username,failed_attempts,locked_until,last_attempt"
            )
            .eq("username", key)
            .limit(1)
            .execute()
        )

        return result.data[0] if result.data else None

    except Exception:
        logger.exception("Could not read login throttle")
        return None


def get_lock_remaining(key: str, *args, **kwargs) -> int:
    row = _get_throttle(key)

    if not row:
        return 0

    try:
        return max(
            0,
            int(_timestamp(row.get("locked_until")) - time.time()),
        )

    except (TypeError, ValueError):
        return 0


def is_locked(key: str, *args, **kwargs) -> bool:
    return get_lock_remaining(key, *args, **kwargs) > 0


def record_failed_attempt(key: str, *args, **kwargs):
    """Increment persistent failed attempts; return (attempts_left, just_locked)."""
    max_attempts, lock_seconds = _throttle_settings(args, kwargs)
    client = _client()

    if client is None or not key:
        return max_attempts, False

    now = time.time()

    try:
        row = _get_throttle(key)

        if row and _timestamp(row.get("locked_until")) > now:
            return 0, False

        prior_lock = (
            _timestamp(row.get("locked_until")) if row else 0
        )

        if row and prior_lock > 0 and prior_lock <= now:
            attempts = 0
        else:
            attempts = (
                int(row.get("failed_attempts") or 0) if row else 0
            )

        attempts += 1
        just_locked = attempts >= max_attempts

        payload = {
            "username": key,
            "failed_attempts": attempts,
            "locked_until": (
                now + lock_seconds if just_locked else 0
            ),
            "last_attempt": now,
        }

        client.table("login_throttle").upsert(
            payload,
            on_conflict="username",
        ).execute()

        return max(0, max_attempts - attempts), just_locked

    except Exception:
        logger.exception("Could not record failed login attempt")
        return 0, False


def record_failed_login(key: str, *args, **kwargs):
    return record_failed_attempt(key, *args, **kwargs)


def reset_failed_attempts(key: str, *args, **kwargs) -> None:
    client = _client()

    if client is None or not key:
        return

    try:
        client.table("login_throttle").upsert(
            {
                "username": key,
                "failed_attempts": 0,
                "locked_until": 0,
                "last_attempt": 0,
            },
            on_conflict="username",
        ).execute()

    except Exception:
        logger.exception("Could not reset login throttle")


def reset_throttle(key: str, *args, **kwargs) -> None:
    reset_failed_attempts(key, *args, **kwargs)


def clear_failed_login(key: str, *args, **kwargs) -> None:
    reset_failed_attempts(key, *args, **kwargs)


def clear_attempts(key: str, *args, **kwargs) -> None:
    reset_failed_attempts(key, *args, **kwargs)


def add_admin_alert(
    kind: str,
    message: str,
    username=None,
    ip_address=None,
) -> bool:
    client = _client()

    if client is None:
        return False

    try:
        result = client.table("admin_alerts").insert({
            "kind": str(kind or "SECURITY_ALERT")[:100],
            "username": str(username)[:80] if username else None,
            "message": str(message or "")[:4000],
            "ip_address": str(ip_address)[:45] if ip_address else None,
        }).execute()

        return bool(result.data)

    except Exception:
        logger.exception("Failed to create administrator alert")
        return False


def log_event(*args, **kwargs) -> None:
    """Write authentication/audit events to the existing login_logs table."""
    client = _client()

    if client is None:
        return

    try:
        username = kwargs.get("username")
        event_type = kwargs.get("event_type") or kwargs.get("event")
        success = kwargs.get("success", False)
        ip = kwargs.get("ip_address") or kwargs.get("ip")
        agent = kwargs.get("user_agent") or kwargs.get("agent")
        details = kwargs.get("detail") or kwargs.get("details") or ""

        if len(args) >= 1:
            username = args[0]

        if len(args) >= 2:
            event_type = args[1]

        if len(args) >= 3:
            success = args[2]

        if len(args) >= 4:
            ip = args[3]

        if len(args) >= 5:
            agent = args[4]

        if len(args) >= 6:
            details = args[5]

        payload = {
            "username": str(username)[:80] if username else None,
            "event": str(event_type or "SYSTEM_EVENT")[:100],
            "success": 1 if _as_bool(success) else 0,
            "ip_address": str(ip)[:45] if ip else None,
            "user_agent": str(agent)[:1000] if agent else None,
            "detail": str(details or "")[:4000] or None,
        }

        client.table("login_logs").insert(payload).execute()

    except Exception:
        logger.exception("Failed inserting login audit event")


def log_login_attempt(*args, **kwargs) -> None:
    log_event(*args, **kwargs)


def get_login_logs(limit: int = 100):
    """Retrieve recent login and audit events for the admin Activity Log."""
    client = _client()

    if client is None:
        raise RuntimeError("Supabase database is unavailable.")

    safe_limit = max(1, min(int(limit), 500))

    try:
        result = (
            client.table("login_logs")
            .select("*")
            .order("created_at", desc=True)
            .limit(safe_limit)
            .execute()
        )

        return result.data or []

    except Exception:
        logger.exception(
            "Failed to retrieve login logs. Check the login_logs table "
            "and its created_at column."
        )
        raise


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
            .select(
                "username,failed_attempts,locked_until,last_attempt"
            )
            .execute()
        )

        now = time.time()
        throttle_by_account = {}

        # Throttle keys may be username or username|IP.
        # Ignore address-only keys such as @ip|...
        for item in throttle_result.data or []:
            key = str(item.get("username") or "")

            if not key or key.startswith("@ip|"):
                continue

            account_name = key.split("|", 1)[0].casefold()
            locked_until = _timestamp(item.get("locked_until"))

            entry = {
                "failed_attempts": int(
                    item.get("failed_attempts") or 0
                ),
                "locked_until": locked_until,
            }

            previous = throttle_by_account.get(account_name)

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
            throttle = throttle_by_account.get(
                username.casefold(), {}
            )

            locked_until = float(
                throttle.get("locked_until") or 0
            )

            seconds_left = max(
                0,
                int(locked_until - now),
            )

            output.append({
                "id": user.get("id"),
                "username": username,
                "role": user.get("role") or "User",
                "approved": _as_bool(user.get("approved")),
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
            and _as_bool(target.get("approved"))
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

        # Delete the user only. Do not delete camera, footage,
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
