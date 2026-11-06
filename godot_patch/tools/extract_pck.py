#!/usr/bin/env python3
"""从 Godot 可执行文件提取内嵌 PCK，或导出为独立 .pck。

示例：
  python3 extract_pck.py "Buckshot Roulette.x86_64" --out project --scripts-only
  python3 extract_pck.py "Buckshot Roulette.x86_64" --pack-out buckshot.pck

支持 PCK 2/3/4，未加密、非 sparse。
"""
from __future__ import annotations

import argparse
import struct
from pathlib import Path

MAGIC = b"GDPC"
SCRIPT_EXTS = {".gd", ".tscn", ".tres", ".json", ".cfg", ".godot", ".remap", ".import", ".txt"}


def locate(path: Path) -> tuple[int, int, int]:
    size = path.stat().st_size
    with path.open("rb") as handle:
        if handle.read(4) == MAGIC:
            return 0, size, size
        if size < 12:
            raise SystemExit("不是 PCK")
        handle.seek(size - 12)
        trailer = handle.read(12)
        if trailer[8:12] != MAGIC:
            raise SystemExit("没有找到 PCK 头部或 EOF trailer")
        pack_size = struct.unpack("<Q", trailer[:8])[0]
        end = size - 12
        start = end - pack_size
        handle.seek(start)
        if handle.read(4) != MAGIC:
            raise SystemExit("trailer 位置不是 GDPC")
        return start, end, pack_size


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("--out", help="提取目录")
    parser.add_argument("--scripts-only", action="store_true",
                        help="只提取脚本、场景、配置等小文件")
    parser.add_argument("--pack-out", help="导出为独立 .pck（不改内容）")
    args = parser.parse_args()
    source = Path(args.source).expanduser().resolve()
    start, end, pack_size = locate(source)
    with source.open("rb") as handle:
        handle.seek(start)
        fixed = handle.read(40)
        version = struct.unpack("<I", fixed[4:8])[0]
        if version not in (2, 3, 4):
            raise SystemExit(f"不支持 PCK 版本 {version}")
        flags = struct.unpack("<I", fixed[20:24])[0]
        if flags & 1:
            raise SystemExit("加密 PCK 不支持")
        if flags & 4:
            raise SystemExit("sparse bundle 不支持")
        file_base = struct.unpack("<Q", fixed[24:32])[0]
        directory = struct.unpack("<Q", fixed[32:40])[0]
        data_base = start + file_base if (version >= 3 or (flags & 2)) else file_base
        dir_start = start + 96 if version < 3 else start + directory

        if args.pack_out:
            handle.seek(start)
            data = bytearray(handle.read(pack_size))
            struct.pack_into("<Q", data, 24, data_base - start)
            if version >= 3:
                struct.pack_into("<Q", data, 32, 96)
            Path(args.pack_out).expanduser().write_bytes(data)
            print(f"wrote standalone pack {args.pack_out}")
            return

        if not args.out:
            raise SystemExit("需要 --out 或 --pack-out")
        out_root = Path(args.out).expanduser().resolve()
        handle.seek(dir_start)
        count = struct.unpack("<I", handle.read(4))[0]
        written = 0
        for _ in range(count):
            length = struct.unpack("<I", handle.read(4))[0]
            path = handle.read(length).rstrip(b"\0").decode("utf-8")
            offset, size = struct.unpack("<QQ", handle.read(16))
            handle.read(16)  # md5
            entry_flags = struct.unpack("<I", handle.read(4))[0]
            if entry_flags & 1:
                continue
            rel = path[6:] if path.startswith("res://") else path
            if ".." in Path(rel).parts:
                continue
            if args.scripts_only and Path(rel).suffix.lower() not in SCRIPT_EXTS:
                continue
            dest = out_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            pos = handle.tell()
            handle.seek(data_base + offset)
            dest.write_bytes(handle.read(size))
            handle.seek(pos)
            written += 1
        print(f"extracted {written} files to {out_root}")


if __name__ == "__main__":
    main()
