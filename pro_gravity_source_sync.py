"""User-triggered, non-blocking acquisition of the public institutional research archive.

Only the Pro source-sync routes call this manager. It never runs at application
startup and never blocks the Lite dashboard during normal operation.
"""
from __future__ import annotations

import json
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DATASET_URL = "https://data.mendeley.com/datasets/xtx7tg6jpx/2"
ARCHIVE_NAME = "Dahlquist_Ibert_replication_JFE-lkATwi.zip"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SourceSyncManager:
    def __init__(self, project_root: Path):
        self.project_root = Path(project_root)
        self.source_dir = self.project_root / "data" / "gravity" / "source"
        self.archive_path = self.source_dir / ARCHIVE_NAME
        self.unpacked_dir = self.source_dir / "unpacked"
        self.state_path = self.source_dir / "source_sync_status.json"
        self.manifest_path = self.source_dir / "institution_archive_schema_manifest.json"
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = self._load_state()
        if self._state.get("status") in {"DOWNLOADING", "SCANNING"}:
            self._state.update({"status": "INTERRUPTED_AFTER_RESTART", "updated_at": _now(),
                                "message": "上次下载在进程重启前未完成；可重新启动下载。"})
            self._save_state()

    def _load_state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"status": "NOT_STARTED", "message": "尚未请求下载机构原始研究包。", "source_url": DATASET_URL}

    def _save_state(self) -> None:
        self.source_dir.mkdir(parents=True, exist_ok=True)
        self._state["updated_at"] = _now()
        self.state_path.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
        valid = self.archive_path.exists() and zipfile.is_zipfile(self.archive_path)
        state.update({
            "source_url": DATASET_URL,
            "archive_path": str(self.archive_path.relative_to(self.project_root)),
            "archive_exists": self.archive_path.exists(),
            "archive_zip_valid": valid,
            "archive_size_bytes": self.archive_path.stat().st_size if self.archive_path.exists() else 0,
            "extracted_dir_exists": self.unpacked_dir.exists() and any(self.unpacked_dir.rglob("*")),
            "extracted_dir": str(self.unpacked_dir.relative_to(self.project_root)),
            "manifest_exists": self.manifest_path.exists(),
            "mapping_status": "AWAITING_SOURCE_SCHEMA_REVIEW" if valid else "NOT_AVAILABLE",
        })
        return state

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {"ok": True, "status": "IN_PROGRESS", "message": "机构研究包正在下载或检查，不会重复启动。"}
            if (self.archive_path.exists() and zipfile.is_zipfile(self.archive_path)
                    and self.manifest_path.exists() and self.unpacked_dir.exists()
                    and any(self.unpacked_dir.rglob("*"))):
                return self.status_unlocked() | {"ok": True, "status": "ALREADY_DOWNLOADED", "message": "原始研究包、解压文件与结构清单均已存在；未重复下载。"}
            self._state = {"ok": True, "status": "DOWNLOADING", "started_at": _now(),
                           "source_url": DATASET_URL,
                           "message": "正在从论文官方 Mendeley Data 来源获取原始 ZIP；这只在用户点击时运行。"}
            self._save_state()
            self._thread = threading.Thread(target=self._run, name="ael-gravity-source-sync", daemon=True)
            self._thread.start()
            return {"ok": True, "status": "DOWNLOADING", "message": "已启动后台下载。请轮询状态；不会阻塞 Lite 或其他 Pro 页面。"}

    def status_unlocked(self) -> dict[str, Any]:
        # The caller already holds _lock. Return only persisted state, not file probes.
        return dict(self._state)

    def _run(self) -> None:
        try:
            # Delayed imports keep archive tooling out of normal Lite start-up work.
            from scripts.download_institution_research_data import fetch_archive
            from scripts.inspect_institution_archive import scan

            if self.archive_path.exists() and zipfile.is_zipfile(self.archive_path):
                # Reuse a valid local archive rather than downloading it again when only
                # the schema manifest or extracted files are missing.
                with zipfile.ZipFile(self.archive_path) as existing:
                    bad = existing.testzip()
                    if bad:
                        raise RuntimeError(f"现有 ZIP CRC 校验失败：{bad}")
                    if not (self.unpacked_dir.exists() and any(self.unpacked_dir.rglob("*"))):
                        root = self.unpacked_dir.resolve()
                        root.mkdir(parents=True, exist_ok=True)
                        for member in existing.infolist():
                            if not (root / member.filename).resolve().is_relative_to(root):
                                raise RuntimeError(f"ZIP 成员路径越界，拒绝解压：{member.filename}")
                        existing.extractall(root)
                from scripts.download_institution_research_data import sha256_file
                result = {"output": str(self.archive_path), "size_bytes": self.archive_path.stat().st_size,
                          "sha256": sha256_file(self.archive_path), "zip_valid": True,
                          "extracted_to": str(self.unpacked_dir)}
            else:
                result = fetch_archive(self.archive_path, self.unpacked_dir)
            with self._lock:
                self._state = {"ok": True, "status": "SCANNING", "started_at": self._state.get("started_at"),
                               "download_result": result, "source_url": DATASET_URL,
                               "message": "ZIP 下载与基本完整性检查通过；正在读取文件清单和字段结构。"}
                self._save_state()
            manifest = scan(self.archive_path)
            self.source_dir.mkdir(parents=True, exist_ok=True)
            self.manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            files = manifest.get("inspectable_entries", [])
            table_files = [x for x in files if x.get("header") or x.get("sheets") or x.get("format") in {"stata_dta", "legacy_excel"}]
            with self._lock:
                self._state = {
                    "ok": True, "status": "DOWNLOADED_SCHEMA_INSPECTED", "started_at": self._state.get("started_at"),
                    "completed_at": _now(), "source_url": DATASET_URL,
                    "download_result": result,
                    "archive_members": manifest.get("archive_members", 0),
                    "schema_inspected_files": len(table_files),
                    "manifest_path": str(self.manifest_path.relative_to(self.project_root)),
                    "message": "原始 ZIP 已下载并检查结构。字段映射仍需按照论文定义审计；不会自动把总回报误标为 ERP，也不会对缺失 g 插值。"
                }
                self._save_state()
        except Exception as exc:
            with self._lock:
                self._state = {"ok": False, "status": "DOWNLOAD_FAILED", "started_at": self._state.get("started_at"),
                               "completed_at": _now(), "source_url": DATASET_URL,
                               "message": "下载失败。检查服务端外网访问与 DNS 后可重试。",
                               "error": str(exc)[:1200]}
                self._save_state()

    def manifest(self) -> dict[str, Any] | None:
        if not self.manifest_path.exists():
            return None
        try:
            return json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
