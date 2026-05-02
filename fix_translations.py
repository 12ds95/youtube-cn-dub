#!/usr/bin/env python3
"""
修复 segments_cache.json 的翻译错位问题。
使用配置文件中的 LLM 逐条重新翻译所有 segments。
"""
import json
import re
import sys
import time
from pathlib import Path

try:
    import httpx
except ImportError:
    print("需要安装 httpx: pip install httpx")
    sys.exit(1)


def strip_think_block(content: str) -> str:
    return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()


def strip_numbered_prefix(text: str) -> str:
    return re.sub(r"^\[\d+\]\s*", "", text)


def parse_numbered_translations(content: str, expected_count: int):
    """解析 LLM 返回的编号格式翻译"""
    content = strip_think_block(content)
    lines = content.strip().split("\n")
    results = [""] * expected_count
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        m = re.match(r"\[(\d+)\]\s*(.*)", line)
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < expected_count:
                results[idx] = m.group(2).strip()
    
    return results


def translate_batch(batch_texts, batch_durations, endpoint, headers, model, 
                    system_prompt, temperature):
    """批量翻译，返回翻译列表"""
    CHARS_PER_SEC = 4.5
    lines = []
    char_hints = []
    for j, (text, dur) in enumerate(zip(batch_texts, batch_durations)):
        lines.append(f"[{j+1}] {text}")
        target_chars = max(2, int(dur * CHARS_PER_SEC))
        char_hints.append(f"[{j+1}]≈{target_chars}字")
    
    user_msg = "\n".join(lines)
    hint_line = f"各句参考字数：{', '.join(char_hints)}"
    
    batch_prompt = (
        f"{system_prompt}\n\n"
        f"请翻译以下 {len(batch_texts)} 句话，每句保持 [编号] 格式，"
        f"一行一句，不要合并或拆分。\n"
        f"{hint_line}\n"
        f"注意：参考字数仅供控制译文长度，不要在译文中输出字数标注。\n\n{user_msg}"
    )
    
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": batch_prompt},
        ],
        "temperature": temperature,
        "max_tokens": 4096,
    }
    
    with httpx.Client(timeout=120.0) as client:
        resp = client.post(endpoint, json=payload, headers=headers)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()
    
    return parse_numbered_translations(content, len(batch_texts))


def translate_single(text, duration, endpoint, headers, model, system_prompt, temperature):
    """逐条翻译"""
    CHARS_PER_SEC = 4.5
    target_chars = max(2, int(duration * CHARS_PER_SEC))
    user_content = f"（请将译文控制在约{target_chars}字，不要在译文中输出字数标注）\n{text}"
    
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": temperature,
        "max_tokens": 512,
    }
    
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(endpoint, json=payload, headers=headers)
        resp.raise_for_status()
        zh = resp.json()["choices"][0]["message"]["content"].strip()
        zh = strip_think_block(zh)
        zh = strip_numbered_prefix(zh)
        return zh if zh and len(zh.strip()) >= 2 else ""


def main():
    # 加载配置
    config_path = Path("config.json")
    if not config_path.exists():
        print("找不到 config.json")
        sys.exit(1)
    
    with open(config_path) as f:
        config = json.load(f)
    
    llm = config["llm"]
    api_url = llm["api_url"].rstrip("/")
    api_key = llm["api_key"]
    model = llm["model"]
    temperature = llm.get("temperature", 0.3)
    batch_size = llm.get("batch_size", 10)
    
    if "/chat/completions" not in api_url:
        endpoint = f"{api_url}/chat/completions"
    else:
        endpoint = api_url
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    
    system_prompt = (
        "你是专业的英中翻译引擎。将以下英文文本翻译为简体中文。"
        "要求：1)翻译准确流畅，符合中文表达习惯；"
        "2)保持技术术语的专业性；"
        "3)翻译要适合做视频配音朗读，语句通顺自然；"
        "4)只输出翻译结果，不要解释。"
    )
    
    # 加载 segments
    seg_path = Path("output/kCc8FmEb1nY/segments_cache.json")
    if not seg_path.exists():
        print(f"找不到 {seg_path}")
        sys.exit(1)
    
    with open(seg_path) as f:
        segments = json.load(f)
    
    print(f"共 {len(segments)} 个片段需要重新翻译")
    print(f"使用模型: {model}")
    print(f"批大小: {batch_size}")
    print()
    
    # 备份原文件
    backup_path = seg_path.with_suffix(".json.bak")
    if not backup_path.exists():
        import shutil
        shutil.copy2(seg_path, backup_path)
        print(f"已备份原文件到 {backup_path}")
    
    # 批量翻译
    total = len(segments)
    translated = 0
    failed = 0
    
    for i in range(0, total, batch_size):
        batch = segments[i:i + batch_size]
        batch_texts = [s["text_en"] for s in batch]
        batch_durations = [s["end"] - s["start"] for s in batch]
        
        # 尝试批量翻译
        try:
            results = translate_batch(
                batch_texts, batch_durations, endpoint, headers, 
                model, system_prompt, temperature
            )
            
            # 验证对齐：检查有多少非空结果
            non_empty = sum(1 for r in results if r.strip())
            if non_empty < len(batch) * 0.7:
                raise ValueError(f"对齐失败: {non_empty}/{len(batch)}")
            
            # 对空结果逐条补翻
            for j, (seg, zh) in enumerate(zip(batch, results)):
                if zh and len(zh.strip()) >= 2:
                    seg["text_zh"] = zh.strip()
                    translated += 1
                else:
                    # 逐条补翻
                    try:
                        zh_single = translate_single(
                            seg["text_en"], seg["end"] - seg["start"],
                            endpoint, headers, model, system_prompt, temperature
                        )
                        if zh_single:
                            seg["text_zh"] = zh_single
                            translated += 1
                        else:
                            failed += 1
                    except Exception as e:
                        print(f"  逐条翻译失败 [{i+j}]: {e}")
                        failed += 1
                    time.sleep(0.5)
                        
        except Exception as e:
            print(f"  批量翻译失败 [{i}-{i+len(batch)}]: {e}，降级逐条翻译...")
            for j, seg in enumerate(batch):
                try:
                    zh = translate_single(
                        seg["text_en"], seg["end"] - seg["start"],
                        endpoint, headers, model, system_prompt, temperature
                    )
                    if zh:
                        seg["text_zh"] = zh
                        translated += 1
                    else:
                        failed += 1
                except Exception as e2:
                    print(f"  逐条翻译也失败 [{i+j}]: {e2}")
                    failed += 1
                time.sleep(0.5)
        
        done = min(i + batch_size, total)
        print(f"  进度: {done}/{total} ({done*100//total}%) | 成功: {translated} 失败: {failed}")
        
        # 每 50 段保存一次进度
        if done % 50 == 0 or done == total:
            with open(seg_path, "w", encoding="utf-8") as f:
                json.dump(segments, f, ensure_ascii=False, indent=2)
        
        # 控制请求频率
        if i + batch_size < total:
            time.sleep(0.3)
    
    # 最终保存
    with open(seg_path, "w", encoding="utf-8") as f:
        json.dump(segments, f, ensure_ascii=False, indent=2)
    
    print(f"\n翻译完成！成功: {translated}, 失败: {failed}")
    print(f"结果已保存到 {seg_path}")


if __name__ == "__main__":
    main()
