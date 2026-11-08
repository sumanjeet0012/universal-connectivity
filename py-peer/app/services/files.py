"""Bitswap-backed file sharing operations."""

from __future__ import annotations

import os

from ..errors import NotReadyError, NotFoundError, OperationError, ValidationError


def is_within(path: str, directory: str) -> bool:
    """True when ``path`` resolves inside ``directory`` (symlinks followed)."""
    try:
        path_real = os.path.realpath(path)
        dir_real = os.path.realpath(directory)
        return os.path.commonpath([dir_real, path_real]) == dir_real
    except (OSError, ValueError):
        return False


class FileService:
    """Share and download files; enforces the download-directory boundary."""

    def __init__(self, engine):
        self._engine = engine

    @property
    def engine(self):
        return self._engine

    @property
    def download_dir(self) -> str:
        return self._engine.download_dir

    def shared_files(self) -> list[dict]:
        return self._engine.get_shared_files()

    def shared_file(self, cid: str) -> dict:
        meta = self._engine.get_shared_file(cid)
        if not meta:
            raise NotFoundError(f"No shared file with CID '{cid}'.")
        return meta

    def share_local_file(
        self,
        file_path: str,
        topic: str,
        *,
        allow_outside_download_dir: bool = False,
    ) -> dict:
        """Queue a share for a file that already exists on disk.

        Remote/HTTP callers must not be able to publish arbitrary host files, so
        the file must live inside the download directory unless the caller is a
        trusted local adapter that explicitly opts out.
        """
        if not getattr(self._engine, "ready", False):
            raise NotReadyError("Service not ready — cannot queue a file share.")
        if not isinstance(file_path, str) or not file_path.strip():
            raise ValidationError("'file_path' field is required.")
        if not isinstance(topic, str) or not topic.strip():
            raise ValidationError("'topic' field is required.")
        path = os.path.expanduser(file_path.strip())
        if not os.path.isfile(path):
            raise ValidationError(f"File not found: {path}")
        if not allow_outside_download_dir and not is_within(path, self.download_dir):
            raise ValidationError(
                "'file_path' must be inside the peer's download directory."
            )
        if topic not in self._engine.get_subscribed_topics():
            raise ValidationError(
                f"Not subscribed to topic '{topic}'. Subscribe first via POST "
                "/api/v1/topics."
            )
        if not self._engine.share_file(path, topic):
            raise OperationError("Failed to queue file share.")
        return {
            "message": "File share request queued",
            "filename": os.path.basename(path),
            "topic": topic,
        }

    def download(self, cid: str, file_name: str = "unknown") -> dict:
        if not getattr(self._engine, "ready", False):
            raise NotReadyError("Service not ready — cannot queue a download.")
        if not isinstance(cid, str) or not cid.strip():
            raise ValidationError("'file_cid' field is required.")
        cid = cid.strip()
        name = (file_name or "unknown").strip() or "unknown"
        if not self._engine.download_file(cid, name):
            raise OperationError("Failed to queue download.")
        return {"message": "Download request queued", "file_cid": cid, "file_name": name}

    def store_upload(self, filename: str, data: bytes) -> dict:
        """Persist uploaded bytes inside the download directory.

        The multipart filename is attacker-controlled, so directory components
        are stripped and containment is re-checked after collision handling.
        """
        if not getattr(self._engine, "ready", False):
            raise NotReadyError("Service not ready — cannot store an upload.")
        name = os.path.basename(filename or "upload").strip()
        if name in ("", ".", ".."):
            name = "upload"
        directory = self.download_dir
        save_path = os.path.join(directory, name)
        base, ext = os.path.splitext(name)
        counter = 1
        while os.path.exists(save_path):
            save_path = os.path.join(directory, f"{base}_{counter}{ext}")
            counter += 1
        if not is_within(save_path, directory):
            raise ValidationError("Invalid upload filename.")
        with open(save_path, "wb") as handle:
            handle.write(data)
        return {
            "path": save_path,
            "filename": os.path.basename(save_path),
            "size": len(data),
        }
