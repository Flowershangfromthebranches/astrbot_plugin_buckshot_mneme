"""时间、ID、中文检索分词与游戏 API 路径提取。核心只依赖标准库。"""
from __future__ import annotations

import math
import re
import unicodedata
import uuid
from datetime import datetime, timezone

try:  # 可选：装了 jieba 就额外加入词级 token，提高精度
    import jieba  # type: ignore

    jieba.setLogLevel(60)
except Exception:  # pragma: no cover - 没装 jieba 是正常情况
    jieba = None

_CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af"
CJK_RUN = re.compile(f"[{_CJK}]+")
TOKEN_RUN = re.compile(f"[{_CJK}]+|[A-Za-z0-9]+")
CJK_CHAR = re.compile(f"[{_CJK}]")

STOP_CHARS = set("的了是我你他她它们在和就也都很吗呢吧啊呀哦嗯着过这那有个")

GENERIC_BIGRAMS = set("""
什么 怎么 怎样 么样 为什 这个 那个 这些 那些 这样 那样 这么 那么 一个 一下 一些 一点 一起 一直 一样
我们 你们 他们 她们 它们 咱们 大家 自己 现在 今天 明天 昨天 刚才 刚刚 时候 以后 以前 之前 之后
然后 后来 因为 所以 但是 不过 可是 而且 如果 虽然 还是 或者 就是 只是 也是 都是 不是 是不 没有
有没 不要 不会 不能 不用 可以 可能 应该 需要 觉得 感觉 知道 认为 好像 真的 其实 已经 还有 一定
当然 确实 比较 非常 特别 有点 有些 东西 事情 你好 谢谢 哈哈 好的 好吧 对了 记得 告诉 说说 聊聊
看看 想想 最近 怎么 如何 哪里 哪个 多少 为啥 干嘛 还记 得我 记不
""".split())

GAME_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")
_DOTTED_PATH = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b")
_PATH_STOP = {
    "int", "float", "double", "bool", "string", "void", "object", "byte", "long", "short",
    "System", "UnityEngine", "Microsoft", "BepInEx", "HarmonyLib",
}


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    """统一存成 UTC、精确到秒的 ISO 字符串，保证字符串比较等于时间比较。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_time(value) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return to_iso(value)
    if not isinstance(value, str):
        raise ValueError(f"无法识别的时间：{value!r}")
    text = value.strip().replace("Z", "+00:00").replace("z", "+00:00")
    text = re.sub(r"(T?\d{2}:\d{2}:\d{2})[.,]\d+", r"\1", text)
    return to_iso(datetime.fromisoformat(text))


def from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def squash(text: str) -> str:
    """全角半角统一、引号统一、去掉所有空白；用于文本包含校验。"""
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"}))
    return re.sub(r"\s+", "", text)


def norm_statement(text: str) -> str:
    return squash(text).lower().rstrip("。.!！~～")


def single_line(text: str, limit: int = 500) -> str:
    """记忆陈述必须是一行：换行折叠成空格，连续空白压缩。"""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text[:limit]


def valid_game_id(game_id: str) -> bool:
    return bool(isinstance(game_id, str) and GAME_ID.fullmatch(game_id))


def extract_watch_paths(text: str, limit: int = 8) -> list[str]:
    """从 API 陈述里提取可直读的点分路径，例如 PlayerData.Instance.money。

    后面紧跟 "(" 的是方法签名，不是可读路径；System / UnityEngine 等基础命名空间排除。
    """
    out: list[str] = []
    for match in _DOTTED_PATH.finditer(text or ""):
        path = match.group(0)
        if (text or "")[match.end():match.end() + 1] == "(":
            continue
        parts = path.split(".")
        if len(parts) < 2 or any(p in _PATH_STOP for p in parts):
            continue
        if path not in out:
            out.append(path)
        if len(out) >= limit:
            break
    return out


def _jieba_words(text: str) -> list[str]:
    if jieba is None:
        return []
    return [w for w in jieba.lcut_for_search(text) if len(w) >= 2 and CJK_RUN.fullmatch(w)]


def index_tokens(text: str) -> list[str]:
    """写入索引的 token：中文单字 + 相邻双字，英文数字按词小写。"""
    out: list[str] = []
    for match in TOKEN_RUN.finditer(unicodedata.normalize("NFKC", text)):
        run = match.group(0)
        if CJK_RUN.fullmatch(run):
            out.extend(ch for ch in run if ch not in STOP_CHARS)
            out.extend(run[i:i + 2] for i in range(len(run) - 1))
        else:
            out.append(run.lower())
    out.extend(_jieba_words(text))
    return out


def query_tokens(text: str, cap: int = 32, *, drop_generic: bool = False) -> list[str]:
    out: list[str] = []
    for match in TOKEN_RUN.finditer(unicodedata.normalize("NFKC", text)):
        run = match.group(0)
        if CJK_RUN.fullmatch(run):
            if len(run) == 1:
                if run not in STOP_CHARS:
                    out.append(run)
            else:
                for i in range(len(run) - 1):
                    pair = run[i:i + 2]
                    if not (pair[0] in STOP_CHARS and pair[1] in STOP_CHARS):
                        out.append(pair)
        else:
            out.append(run.lower())
    out.extend(_jieba_words(text))
    if drop_generic:
        out = [t for t in out if t not in GENERIC_BIGRAMS]
    seen: set[str] = set()
    uniq = [t for t in out if not (t in seen or seen.add(t))]
    return uniq[:cap]


def unigram_tokens(text: str, cap: int = 32) -> list[str]:
    out: list[str] = []
    for match in TOKEN_RUN.finditer(unicodedata.normalize("NFKC", text)):
        run = match.group(0)
        if CJK_RUN.fullmatch(run):
            out.extend(ch for ch in run if ch not in STOP_CHARS)
        else:
            out.append(run.lower())
    seen: set[str] = set()
    return [t for t in out if not (t in seen or seen.add(t))][:cap]


def estimate_tokens(text: str) -> int:
    """粗估 token：中文按一字一个，其余按四个字符一个。"""
    cjk = len(CJK_CHAR.findall(text))
    return cjk + math.ceil((len(text) - cjk) / 4)
