"""识别结果的噪音过滤。

语音识别模型（尤其 Whisper / SenseVoice）对纯噪声片段会产生"幻觉文本"：
嗯、啊、四、三、Here.、Shoes. 之类。这里在入库/推送前统一拦掉。
"""
import re
from typing import Optional

# 常见语气词单字：由它们拼出来的整句（嗯、啊啊、呵呵呵）都视为无效
FILLER_CHARS = set("嗯啊哦呃唉哎呵哈嘿唔噢喂呀呐呗嘛额呣嘻嘘咯喽嘞呐噢")

# 标点与空白
PUNCT_RE = re.compile(
    r"[\s，。！？、,.!?~…:：;；\"'“”‘’()（）\[\]【】<>《》\-—_/\\|]+"
)

# 内置兜底幻觉词（用户可在设置里追加）
BUILTIN_IGNORE = {
    "嗯", "啊", "哦", "呃", "唉", "哎", "呵", "哈", "嘿", "唔", "噢",
    "嗯嗯", "啊啊", "呵呵", "哈哈", "呵呵呵", "哈哈哈", "不不不",
    "四", "三", "二", "一", "十",
    "here", "shoes", "foot", "eye", "i love you", "thank you",
    "thanks", "bye", "okay", "oh", "wow", "hmm",
}


def noise_reason(text: str, ignore_words: str = "") -> Optional[str]:
    """返回丢弃原因；返回 None 表示是有效文本。"""
    raw = (text or "").strip()
    if not raw:
        return "空结果"

    cleaned = PUNCT_RE.sub("", raw)
    if not cleaned:
        return "纯标点"

    low = cleaned.lower()
    # 黑名单条目同样归一化：忽略标点与空格，避免 "I love you." 匹配不上
    words = {
        PUNCT_RE.sub("", w.strip()).lower()
        for w in (ignore_words or "").replace("，", ",").replace("\n", ",").split(",")
        if w.strip()
    }
    words |= {PUNCT_RE.sub("", w).lower() for w in BUILTIN_IGNORE}
    if low in words:
        return "噪音词黑名单"

    # 单个汉字/字母/数字：监控场景下几乎都是噪声幻觉
    if len(cleaned) <= 1:
        return "单字结果"

    # 整句全是语气词
    if all(ch in FILLER_CHARS for ch in cleaned):
        return "纯语气词"

    # 三个及以上同一字重复（不不不、啊啊啊）；两字叠词如"爸爸"是正常词
    if len(cleaned) >= 3 and len(set(cleaned)) == 1:
        return "重复单字"

    return None
