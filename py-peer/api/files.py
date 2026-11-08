"""
File sharing endpoints (Bitswap / MerkleDag).

GET  /api/v1/files/shared          - list files this node has shared
GET  /api/v1/files/shared/{cid}    - metadata for a specific shared file
POST /api/v1/files/share           - share a local file to a topic
POST /api/v1/files/download        - download a file by CID hex
POST /api/v1/files/upload          - upload via multipart and share to a topic
"""

import asyncio
import os

from .base import BaseHandler, parse_string_field

# Cap an individual upload. Tornado still has a global body limit, but this
# keeps the in-memory multipart buffer and the on-disk write bounded.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def _is_within(path, directory):
    """True when ``path`` resolves inside ``directory`` (symlinks followed)."""
    try:
        path_real = os.path.realpath(path)
        dir_real = os.path.realpath(directory)
        return os.path.commonpath([dir_real, path_real]) == dir_real
    except (OSError, ValueError):
        return False


def _write_bytes(path, data):
    """Blocking write, executed in a worker thread by the upload handler."""
    with open(path, "wb") as f:
        f.write(data)


class SharedFilesHandler(BaseHandler):
    """GET /api/v1/files/shared"""

    def get(self):
        if not self.require_ready():
            return
        files = self.service.get_shared_files()
        self.send_success({"shared_files": files, "count": len(files)})


class SharedFileDetailHandler(BaseHandler):
    """GET /api/v1/files/shared/{cid}"""

    def get(self, cid):
        if not self.require_ready():
            return
        meta = self.service.get_shared_file(cid)
        if not meta:
            self.send_error_response(f"No shared file with CID '{cid}'.", status=404)
            return
        self.send_success(meta)


class ShareFileHandler(BaseHandler):
    """POST /api/v1/files/share — share a file that already exists on disk"""

    def post(self):
        if not self.require_ready():
            return
        body = self.get_json_body()
        try:
            file_path = parse_string_field(body, "file_path", required=True)
            topic = parse_string_field(body, "topic", required=True)
        except ValueError as e:
            self.send_error_response(str(e))
            return

        file_path = os.path.expanduser(file_path)
        if not os.path.isfile(file_path):
            self.send_error_response(f"File not found: {file_path}", status=400)
            return
        # The API is unauthenticated and CORS-open. Never let a remote caller
        # name an arbitrary host file (e.g. ~/.ssh/id_ed25519) and have the
        # node publish it to a pubsub topic. The supported local-file sharing
        # path is the download directory (uploaded/downloaded files); local
        # desktop UIs call HeadlessService.share_file() directly instead.
        if not _is_within(file_path, self.service.download_dir):
            self.send_error_response(
                "'file_path' must be inside the peer's download directory.",
                status=400,
            )
            return

        subscribed = self.service.get_subscribed_topics()
        if topic not in subscribed:
            self.send_error_response(
                f"Not subscribed to topic '{topic}'. Subscribe first via POST /api/v1/topics.",
                status=400,
            )
            return

        queued = self.service.share_file(file_path, topic)
        if queued:
            filename = os.path.basename(file_path)
            self.send_success(
                {"message": "File share request queued", "filename": filename, "topic": topic},
                status=202,
            )
        else:
            self.send_error_response("Failed to queue file share — service not ready.", status=503)


class DownloadFileHandler(BaseHandler):
    """POST /api/v1/files/download — download a file by CID hex"""

    def post(self):
        if not self.require_ready():
            return
        body = self.get_json_body()
        try:
            cid = parse_string_field(body, "file_cid", required=True)
            name = parse_string_field(body, "file_name", default="unknown")
        except ValueError as e:
            self.send_error_response(str(e))
            return

        queued = self.service.download_file(cid, name)
        if queued:
            self.send_success(
                {"message": "Download request queued", "file_cid": cid, "file_name": name},
                status=202,
            )
        else:
            self.send_error_response("Failed to queue download — service not ready.", status=503)


class UploadAndShareHandler(BaseHandler):
    """
    POST /api/v1/files/upload
    Accepts multipart/form-data with fields:
      - file   : the file bytes
      - topic  : the topic to share to
    Saves the file to the service's download_dir and queues a share.
    """

    async def post(self):
        if not self.require_ready():
            return

        topic = self.get_argument("topic", "").strip()
        if not topic:
            self.send_error_response("'topic' form field is required.")
            return

        if "file" not in self.request.files:
            self.send_error_response("'file' form-data field is required.")
            return

        # Validate the topic before writing anything to disk.
        subscribed = self.service.get_subscribed_topics()
        if topic not in subscribed:
            self.send_error_response(
                f"Not subscribed to topic '{topic}'. Subscribe first via POST /api/v1/topics.",
                status=400,
            )
            return

        file_info = self.request.files["file"][0]
        # A multipart filename is attacker-controlled: strip any directory
        # components so it cannot escape the download directory.
        filename = os.path.basename(file_info["filename"] or "upload").strip()
        if filename in ("", ".", ".."):
            filename = "upload"
        file_data = file_info["body"]
        if len(file_data) > MAX_UPLOAD_BYTES:
            self.send_error_response(
                f"Upload is too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).",
                status=413,
            )
            return

        # Save to download_dir
        save_path = os.path.join(self.service.download_dir, filename)
        # Handle name collisions
        counter = 1
        base, ext = os.path.splitext(filename)
        while os.path.exists(save_path):
            save_path = os.path.join(self.service.download_dir, f"{base}_{counter}{ext}")
            counter += 1
        if not _is_within(save_path, self.service.download_dir):
            self.send_error_response("Invalid upload filename.", status=400)
            return

        # Offload the write: a synchronous multi-megabyte write would block
        # every API/WebSocket client on the single Tornado IOLoop thread.
        await asyncio.to_thread(_write_bytes, save_path, file_data)

        queued = self.service.share_file(save_path, topic)
        if queued:
            self.send_success(
                {
                    "message": "File uploaded and share request queued",
                    "filename": os.path.basename(save_path),
                    "size": len(file_data),
                    "topic": topic,
                },
                status=202,
            )
        else:
            self.send_error_response("Failed to queue file share — service not ready.", status=503)
