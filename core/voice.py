"""
墨溯 · 语音提取与转码

实测（微信 4.1.15.13）：
  语音**不落磁盘文件**，而是存在 message/media_0.db 的 VoiceInfo.voice_data 里，
  格式是 SILK_V3（字节头 ... 23 21 53 49 4C 4B 5F 56 33 = "#!SILK_V3"）。
  VoiceInfo 字段: chat_name_id, create_time, local_id, svr_id, voice_data, data_index
  与消息的关联键: create_time（同一秒，实测唯一）。

转码链: silk-wasm(Node) 解码 -> PCM s16le -> ffmpeg -> mp3
  浏览器不能直接放 silk，所以必须转；ffmpeg 本身不认 silk，靠 silk-wasm 解码。
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

DEFAULT_RATE = 24000


class VoiceStore:
    def __init__(self, media0_plain: str | os.PathLike):
        self.db = str(media0_plain)
        self._con: sqlite3.Connection | None = None
        self.available = os.path.exists(self.db)

    def _connect(self) -> sqlite3.Connection:
        if self._con is None:
            self._con = sqlite3.connect(self.db)
            self._con.row_factory = sqlite3.Row
        return self._con

    def count(self) -> int:
        if not self.available:
            return 0
        try:
            return self._connect().execute("SELECT count(*) FROM VoiceInfo").fetchone()[0]
        except Exception:
            return 0

    def fetch(self, create_time: int) -> bytes | None:
        """按 create_time 取 silk 原始数据。"""
        if not self.available:
            return None
        try:
            r = self._connect().execute(
                "SELECT voice_data FROM VoiceInfo WHERE create_time=? ORDER BY rowid LIMIT 1",
                (int(create_time),),
            ).fetchone()
        except Exception:
            return None
        return bytes(r[0]) if r and r[0] is not None else None

    @staticmethod
    def _find_node() -> str | None:
        import shutil
        env = os.environ.get("NODE_BIN", "").strip()
        if env and os.path.exists(env):
            return env
        found = shutil.which("node")
        if found:
            return found
        for c in (r"C:\Program Files\nodejs\node.exe",
                  os.path.expanduser(r"~\AppData\Roaming\npm\node.exe"),
                  "/usr/bin/node", "/usr/local/bin/node"):
            if os.path.exists(c):
                return c
        return "node"

    def prepare(self, rows, out_dir: str | os.PathLike, script: str | os.PathLike) -> dict[int, str]:
        """把 rows 里所有语音转码为 mp3，返回 {create_time: 相对路径}。"""
        out = Path(out_dir)
        vdir = out / "media" / "voice"
        vdir.mkdir(parents=True, exist_ok=True)
        mapping: dict[int, str] = {}

        from msgparse import T_VOICE, parse_message

        jobs = []
        for r in rows:
            d = dict(r)
            m = parse_message(d.get("local_type", 0), d.get("message_content"), d.get("packed_info_data"))
            if m["type"] != T_VOICE:
                continue
            ts = int(d.get("create_time") or 0)
            if not ts or ts in mapping:
                continue
            # 增量导出：上次已转码好的 mp3 直接复用
            if (vdir / f"v{ts}.mp3").exists():
                mapping[ts] = f"media/voice/v{ts}.mp3"
                continue
            data = self.fetch(ts)
            if not data:
                continue
            sid = f"v{ts}"
            sp = vdir / f"{sid}.silk"
            sp.write_bytes(data)
            jobs.append({
                "id": sid,
                "silk": str(sp).replace("\\", "/"),
                "out": str(vdir / f"{sid}.mp3").replace("\\", "/"),
                "rate": DEFAULT_RATE,
            })
            mapping[ts] = f"media/voice/{sid}.mp3"

        if not jobs:
            return mapping

        node = self._find_node()
        if not node:
            print("  [!] 未找到 node，语音无法转码（silk 已保留）")
            return mapping

        mf = vdir / "_manifest.json"
        mf.write_text(json.dumps(jobs, ensure_ascii=False), encoding="utf-8")
        env = dict(os.environ)
        env["NODE_OPTIONS"] = ""   # 绕开 WorkBuddy 注入的 shim
        try:
            subprocess.run([node, str(script), str(mf)], capture_output=True,
                           text=True, timeout=900, env=env, cwd=str(Path(script).parent.parent))
        except Exception as e:
            print(f"  [!] 语音转码失败: {str(e)[:80]}")
        # 没转成功的从映射里剔除，HTML 退化为「仅有文字」
        for j in jobs:
            if os.path.exists(j["out"]):
                try:
                    os.remove(j["silk"])          # 转成功就删掉 silk，只留 mp3
                except Exception:
                    pass
            else:
                mapping.pop(int(j["id"][1:]), None)
        try:
            mf.unlink()
        except Exception:
            pass
        return mapping
