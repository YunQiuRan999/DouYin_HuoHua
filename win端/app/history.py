from __future__ import annotations

import json
import logging
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LOGGER = logging.getLogger("douyin_sender")

# reserve() 写入 unknown 后，超过该天数仍未确认成功则回收（允许重发）
UNKNOWN_RECYCLE_DAYS = 3


class AlreadyRunningError(RuntimeError):
    pass


class History:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries = self._load()

    def run_date(self, timezone: str) -> str:
        try:
            return datetime.now(ZoneInfo(timezone)).date().isoformat()
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"未知时区: {timezone}，请安装 tzdata 或修正配置") from exc

    def key(self, task_id: str, run_date: str, target: str, message_id: str) -> str:
        return f"{task_id}:{run_date}:{target}:{message_id}"

    def contains(self, key: str) -> bool:
        return key in self.entries

    def reserve(self, key: str) -> None:
        self.entries[key] = {"status": "unknown", "started_at": datetime.now().astimezone().isoformat()}
        self._save()

    def mark_success(self, key: str) -> None:
        self.entries[key] = {"status": "success", "finished_at": datetime.now().astimezone().isoformat()}
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.entries, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def _load(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError:
            # 历史文件损坏：备份后重置，避免因损坏而停止任务（无人值守服务器不能卡死）
            backup = self.path.with_suffix(".json.bak")
            try:
                self.path.rename(backup)
                LOGGER.warning("发送历史损坏，已备份到 %s 并重置", backup)
            except Exception:
                pass
            return {}
        if not isinstance(value, dict):
            return {}
        return self._recycle_stale_unknown(value)

    @staticmethod
    def _recycle_stale_unknown(entries: dict) -> dict:
        """对账回收超时的 unknown 记录。

        reserve() 在发送前写入 status=unknown（结果不确定），若进程在发送中途
        崩溃，该记录会让 contains() 恒真，导致当天该消息再也发不出去（防重复
        的副作用）。这里回收超过 UNKNOWN_RECYCLE_DAYS 仍未确认成功的 unknown
        记录：时间已足够久，不再有"重复发送"的实际风险，删除以允许重发。
        """
        stale = []
        now = datetime.now().astimezone()
        for key, entry in entries.items():
            if not isinstance(entry, dict) or entry.get("status") != "unknown":
                continue
            started = entry.get("started_at")
            try:
                started_dt = datetime.fromisoformat(str(started))
            except (TypeError, ValueError):
                continue
            if now - started_dt > timedelta(days=UNKNOWN_RECYCLE_DAYS):
                stale.append(key)
        for key in stale:
            del entries[key]
        if stale:
            LOGGER.warning("回收 %d 条超时未确认的发送记录（超过 %d 天）", len(stale), UNKNOWN_RECYCLE_DAYS)
        return entries


@contextmanager
def run_lock(path: Path) -> Iterator[None]:
    """基于文件的互斥锁；崩溃残留的锁文件会自动检测并清理。

    锁文件中写入进程 PID，获取锁时若文件已存在，检查该 PID 是否仍在运行；
    若进程已不存在（崩溃残留），自动删除旧锁并重新获取。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # 锁文件已存在，检查是否为崩溃残留
        if _is_lock_stale(path):
            LOGGER.warning("检测到崩溃残留的锁文件 %s，已自动清理", path)
            try:
                path.unlink()
            except OSError:
                pass
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        else:
            raise AlreadyRunningError(f"已有任务正在运行；如确认没有进程，请删除 {path}")
    try:
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        os.close(descriptor)
        yield
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _is_lock_stale(path: Path) -> bool:
    """检查锁文件中的 PID 对应的进程是否仍在运行。"""
    try:
        content = path.read_text(encoding="ascii").strip()
        if not content:
            return True
        pid = int(content)
    except (OSError, ValueError):
        return True
    if pid <= 0:
        return True
    return not _pid_running(pid)


def _pid_running(pid: int) -> bool:
    """判断 PID 对应的进程是否仍在运行（跨平台）。

    注意：POSIX 的 ``os.kill(pid, 0)`` 是"仅探测是否存在"的无害信号；
    但 Windows 上 ``os.kill(pid, 0)`` 会调用 TerminateProcess 直接终止进程，
    绝不能用于探测存活。故 Windows 改用 OpenProcess + GetExitCodeProcess。
    """
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # 打开失败（进程不存在或访问拒绝）。保守认为仍存活，避免误回收
            # 一个其实仍在运行的锁；崩溃残留的锁文件可手动删除。
            return True
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)

    # POSIX：signal 0 只探测、不发送信号
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但无权限，保守认为存活
    except OSError:
        return True
