import os
import logging
from supabase import create_client, Client
from . import settings as cfg

log = logging.getLogger("lifeguard.storage")

def get_supabase_client() -> Client:
    """Initialize the Supabase client using environment variables."""
    if not cfg.SUPABASE_URL or not cfg.SUPABASE_SERVICE_ROLE_KEY:
        raise ValueError("Missing Supabase credentials (SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY).")
    return create_client(cfg.SUPABASE_URL, cfg.SUPABASE_SERVICE_ROLE_KEY)

def upload_incident_video(file_path: str, destination_name: str) -> str:
    """Uploads a local video to the private Supabase Storage bucket."""
    supabase = get_supabase_client()
    bucket_name = "incident-footage"
    
    try:
        with open(file_path, "rb") as f:
            supabase.storage.from_(bucket_name).upload(
                file=f,
                path=destination_name,
                file_options={"content-type": "video/mp4"}
            )
        log.info(f"Successfully uploaded {destination_name} to cloud storage.")
        return destination_name
    except Exception as e:
        log.error(f"Failed to upload video to Supabase: {e}")
        return None

def get_secure_video_url(storage_path: str, expiration_sec: int = 3600) -> str:
    """Generates a temporary, signed URL for authorized dashboard playback."""
    if not storage_path:
        return None
        
    try:
        supabase = get_supabase_client()
        bucket_name = "incident-footage"
        
        response = supabase.storage.from_(bucket_name).create_signed_url(
            path=storage_path, 
            expires_in=expiration_sec
        )
        return response.get("signedURL")
    except Exception as e:
        log.error(f"Failed to generate secure video URL: {e}")
        return None