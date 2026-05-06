"""g2pW 兜底实战验证 (独立脚本, 避开 pytest+multiprocessing 死锁)。

用法:
    venv/bin/python3 scripts/smoke_test_polyphone_g2pw.py

验证点:
  1. g2pW 模型能加载
  2. 我们已知 g2pM false positive case 在 g2pW 上正确 (严重/权重/所处/处于)
  3. g2pW 修复用户报的"了解" 误判 (g2pM 把"了解" 错判 le, g2pW 对)
  4. 主路径 jieba 白名单 + g2pW 兜底 联合工作
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    from pipeline import _fix_polyphones, _get_g2pw

    print("Loading g2pW (this may take ~10s after first download)...")
    nlp = _get_g2pw()
    if nlp is None:
        print("❌ g2pW load failed (model not downloaded? need internet)")
        return 1
    print("✓ g2pW loaded\n")

    # 验证 case: 文本 → 期望关键替换是否符合
    cases = [
        # (text, must_NOT_contain, must_contain, comment)
        ("严重的损失",            "严虫",  "严重",  "g2pM 误判 chóng, g2pW 应保留 zhòng"),
        ("注意力权重计算",        "权虫",  "权重",  "g2pM 误判, g2pW 应保留"),
        ("它知道自己所处的位置",  "所触",  "所处",  "g2pM 误判 chù, g2pW 应保留 chǔ"),
        ("不关心是否处于训练",    "触于",  "处于",  "g2pM 误判, g2pW 应保留"),
        ("这构成了当前实现",      "瞭",    "构成了", "助词 le, 不应替换"),
        ("我了解你的意思",        "了解",  "瞭解",  "动词 liǎo, jieba 白名单替换"),
        ("我有直觉",              "直觉",  "直决",  "jué, jieba 白名单替换"),
        ("银行办事",              "银行",  "银杭",  "háng, jieba 白名单替换"),
        ("三重注意力机制",        "三重",  "三虫",  "g2pW 兜底覆盖 (jieba 白名单未含)"),
    ]

    pass_n = 0
    for text, must_not, must_have, comment in cases:
        out = _fix_polyphones(text, use_g2pw_fallback=True)
        ok = (must_not not in out) and (must_have in out)
        status = "✓" if ok else "✗"
        if ok:
            pass_n += 1
        print(f"  {status} '{text}' → '{out}'")
        print(f"      期望: 不含 '{must_not}', 含 '{must_have}'")
        print(f"      注释: {comment}")
        print()

    print(f"\n{'='*60}")
    print(f"smoke test: {pass_n}/{len(cases)} passed")
    return 0 if pass_n == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
