#!/usr/bin/env python3
"""从含内嵌 PCK 的 Godot 可执行文件重打一个独立 .pck。

用于在 macOS 上用官方 Godot 4.1.1 运行补丁后的 Linux PCK：
  python3 repack_pck.py "Buckshot Roulette.x86_64" out.pck \
      --replace-dir /path/to/patched_project

replace-dir 里的相对路径会覆盖同名 res:// 文件，未出现过的文件会追加。
只支持未加密、非 sparse 的 PCK 2/3/4。
"""
from __future__ import annotations

import argparse
import hashlib
import struct
from pathlib import Path

MAGIC = b"GDPC"
HEADER = 96


def locate(path: Path) -> tuple[int, int]:
    size = path.stat().st_size
    with path.open("rb") as handle:
        head = handle.read(4)
        if head == MAGIC:
            return 0, size
        if size < 12:
            raise SystemExit("文件太小，不是 PCK")
        handle.seek(size - 12)
        trailer = handle.read(12)
        if trailer[8:12] != MAGIC:
            raise SystemExit("没有找到 Godot PCK 头部或 EOF trailer")
        pack_size = struct.unpack("<Q", trailer[:8])[0]
        end = size - 12
        start = end - pack_size
        if start < 0:
            raise SystemExit("PCK trailer 指向非法范围")
        handle.seek(start)
        if handle.read(4) != MAGIC:
            raise SystemExit("PCK trailer 指向的位置没有 GDPC")
        return start, end


def read_entries(path: Path, start: int):
    with path.open("rb") as handle:
        handle.seek(start)
        fixed = handle.read(40)
        if fixed[:4] != MAGIC:
            raise SystemExit("PCK 头部损坏")
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
        handle.seek(dir_start)
        count = struct.unpack("<I", handle.read(4))[0]
        entries = []
        for _ in range(count):
            length = struct.unpack("<I", handle.read(4))[0]
            raw = handle.read(length)
            path_text = raw.rstrip(b"\x00").decode("utf-8")
            offset, size = struct.unpack("<QQ", handle.read(16))
            md5 = handle.read(16)
            entry_flags = struct.unpack("<I", handle.read(4))[0]
            if entry_flags & 1:
                raise SystemExit(f"加密条目不支持：{path_text}")
            entries.append({
                "path": path_text,
                "offset": offset,
                "size": size,
                "md5": md5,
                "flags": entry_flags,
            })
        return version, flags, directory, data_base, entries


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("output")
    parser.add_argument("--replace-dir", required=True)
    args = parser.parse_args()
    source = Path(args.source).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    replace_root = Path(args.replace_dir).expanduser().resolve()

    start, _end = locate(source)
    version, flags, _directory, data_base, entries = read_entries(source, start)
    paths = {entry["path"] for entry in entries}
    replacements: dict[str, Path] = {}
    for file in replace_root.rglob("*"):
        if not file.is_file():
            continue
        rel = file.relative_to(replace_root).as_posix()
        target = f"res://{rel}" if not rel.startswith("res://") else rel
        if target in paths:
            replacements[target] = file
    additions = []
    for file in replace_root.rglob("*"):
        if not file.is_file():
            continue
        rel = file.relative_to(replace_root).as_posix()
        target = f"res://{rel}" if not rel.startswith("res://") else rel
        if target not in paths:
            additions.append((target, file))

    final_entries = []
    for entry in entries:
        replacement = replacements.get(entry["path"])
        if replacement is None:
            final_entries.append({**entry, "source": None,
                                  "source_offset": entry["offset"]})
        else:
            data = replacement.read_bytes()
            final_entries.append({
                "path": entry["path"], "size": len(data),
                "md5": hashlib.md5(data).digest(), "flags": 0, "source": data,
                "source_offset": None,
            })
    for target, file in additions:
        data = file.read_bytes()
        final_entries.append({
            "path": target, "size": len(data),
            "md5": hashlib.md5(data).digest(), "flags": 0, "source": data,
            "source_offset": None,
        })

    directory = bytearray(struct.pack("<I", len(final_entries)))
    offset = 0
    for entry in final_entries:
        path_bytes = entry["path"].encode("utf-8")
        directory += struct.pack("<I", len(path_bytes)) + path_bytes
        directory += struct.pack("<QQ", offset, entry["size"])
        directory += entry["md5"] + struct.pack("<I", entry["flags"])
        entry["offset"] = offset
        offset += entry["size"]
    data_start = HEADER + len(directory)

    with source.open("rb") as handle:
        handle.seek(start)
        header = bytearray(handle.read(HEADER))
        struct.pack_into("<Q", header, 24, data_start)
        if version >= 3:
            struct.pack_into("<Q", header, 32, HEADER)
        else:
            struct.pack_into("<Q", header, 32, 0)

        temp = output.with_suffix(output.suffix + ".tmp")
        with temp.open("wb") as out:
            out.write(header)
            out.write(directory)
            for entry in final_entries:
                data = entry["source"]
                if data is not None:
                    out.write(data)
                else:
                    handle.seek(data_base + entry["source_offset"])
                    remaining = entry["size"]
                    while remaining:
                        chunk = handle.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise SystemExit(f"源 PCK 数据截断：{entry['path']}")
                        out.write(chunk)
                        remaining -= len(chunk)
        temp.replace(output)
    print(f"wrote {output} ({output.stat().st_size} bytes), "
          f"{len(final_entries)} entries, replaced {len(replacements)}, added {len(additions)}")


if __name__ == "__main__":
    main()
