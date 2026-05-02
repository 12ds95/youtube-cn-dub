#!/usr/bin/env python3
"""
TTS 时长估算器校准脚本 (v2)

从已有的 TTS 音频和 segments_cache.json 中提取 (text_zh, actual_duration_ms) 对,
用 Ridge 回归拟合 _estimate_duration_jieba 的 8 个时长参数 + 1 个截距。
v2: 修正 rate 去混淆（移除过时的 *1.3）、支持嵌套目录扫描、更大训练集。

用法:
  python calibrate_tts_duration.py                    # 自动扫描 output/*/ 和 output/*/*/
  python calibrate_tts_duration.py output/zjMuIxRvygQ  # 指定单个视频目录
  python calibrate_tts_duration.py --apply             # 拟合后自动写入 pipeline.py

输出:
  - 校准后的参数（替换 _estimate_duration_jieba 中的硬编码值）
  - 拟合质量指标（R², MAE, MAPE, 15%/20% 阈值内比例）
  - 校准前后 Phase 2 触发率对比
"""
import sys
import os
import json
import re
import glob
import warnings

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── 特征提取（与 _estimate_duration_jieba 相同的分解逻辑）──

_URL_PATTERN = re.compile(
    r'(?:https?://)?(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}'
    r'(?:/[^\s]*)?'
)


def extract_features(text_zh: str) -> dict:
    """从文本中提取与 _estimate_duration_jieba 相同的 8 维特征向量"""
    import jieba
    import unicodedata

    # URL 特征
    url_chars = 0
    clean_text = text_zh
    for m in _URL_PATTERN.finditer(text_zh):
        url_str = m.group()
        url_chars += sum(1 for c in url_str if c.isalnum() or c in './-_:')
        clean_text = clean_text.replace(url_str, '', 1)

    words = jieba.lcut(clean_text)
    n_1char = 0   # 单字中文词
    n_2char = 0   # 双字中文词
    n_3char = 0   # 三字中文词
    n_4plus = 0   # 四字及以上中文字符总数
    n_letters = 0 # 英文字母
    n_digits = 0  # 数字
    n_punct = 0   # 标点/停顿

    for word in words:
        meaningful = [c for c in word
                      if not unicodedata.category(c).startswith(('P', 'Z', 'C'))]
        if not meaningful:
            n_punct += 1
            continue

        zh_count = sum(1 for c in meaningful if '\u4e00' <= c <= '\u9fff')
        other_count = len(meaningful) - zh_count

        if zh_count > 0:
            if zh_count == 1:
                n_1char += 1
            elif zh_count == 2:
                n_2char += 1
            elif zh_count == 3:
                n_3char += 1
            else:
                n_4plus += zh_count

        if other_count > 0:
            for c in meaningful:
                if c.isdigit():
                    n_digits += 1
                elif not ('\u4e00' <= c <= '\u9fff'):
                    n_letters += 1

    return {
        "n_1char": n_1char,
        "n_2char": n_2char,
        "n_3char": n_3char,
        "n_4plus": n_4plus,
        "n_letters": n_letters,
        "n_digits": n_digits,
        "n_url_chars": url_chars,
        "n_punct": n_punct,
    }


FEATURE_NAMES = ["n_1char", "n_2char", "n_3char", "n_4plus",
                 "n_letters", "n_digits", "n_url_chars", "n_punct"]

# 当前部署的参数（v2 校准值）
BASELINE_PARAMS = {
    "n_1char": 138, "n_2char": 361, "n_3char": 506, "n_4plus": 223,
    "n_letters": 31, "n_digits": 311, "n_url_chars": 16, "n_punct": 197,
    "intercept": 1210,
}

# ── 数据收集 ──

def collect_samples(video_dirs: list) -> list:
    """从视频目录收集 (text_zh, natural_ms, features) 样本

    关键：TTS 生成时应用了 rate 参数调速，actual_ms 包含了 rate 效果。
    对 edge-tts（rate 有效）: natural_ms ≈ actual_ms * applied_rate
    这样回归学到的是 rate=1.0 下的自然时长参数。
    """
    from pydub import AudioSegment
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from pipeline import _estimate_duration_jieba

    samples = []
    for vdir in video_dirs:
        cache_path = os.path.join(vdir, "segments_cache.json")
        tts_dir = os.path.join(vdir, "tts_segments")
        if not os.path.exists(cache_path) or not os.path.exists(tts_dir):
            continue

        with open(cache_path, encoding="utf-8") as f:
            segments = json.load(f)

        # 读取反馈日志（如果有），获取实际使用的 corrected_rate
        feedback_rates = {}
        audit_dir = os.path.join(vdir, "audit")
        fb_path = os.path.join(audit_dir, "tts_feedback_log.json")
        if os.path.exists(fb_path):
            try:
                with open(fb_path, encoding="utf-8") as f:
                    for entry in json.load(f):
                        feedback_rates[entry["idx"]] = entry["corrected_rate"]
            except Exception:
                pass

        for idx, seg in enumerate(segments):
            text_zh = seg.get("text_zh", seg.get("text", ""))
            if len(text_zh.strip()) < 2:
                continue
            if not re.search(r'[\u4e00-\u9fff\u3400-\u4dbfa-zA-Z0-9]', text_zh):
                continue

            mp3_path = os.path.join(tts_dir, f"seg_{idx:04d}.mp3")
            if not os.path.exists(mp3_path) or os.path.getsize(mp3_path) < 100:
                continue

            try:
                audio = AudioSegment.from_mp3(mp3_path)
                actual_ms = len(audio)
            except Exception:
                continue

            if actual_ms < 200:
                continue

            # 反推 rate=1.0 下的自然时长
            start_ms = int(seg.get("start", 0) * 1000)
            end_ms = int(seg.get("end", 0) * 1000)
            target_dur_ms = end_ms - start_ms

            if idx in feedback_rates:
                # 经过反馈闭环的段，用实际 corrected_rate
                applied_rate = feedback_rates[idx]
            elif target_dur_ms > 0:
                # 初始 rate = _estimate_duration_jieba(text) / target_ms, clamped
                # (v1 校准已移除 * 1.3 韵律乘数)
                est_ms = _estimate_duration_jieba(text_zh)
                raw_ratio = est_ms / target_dur_ms if target_dur_ms > 0 else 1.0
                applied_rate = max(0.80, min(1.35, raw_ratio))
            else:
                applied_rate = 1.0

            # natural_ms = actual_ms * rate（rate>1 加速→实际更短，乘回去恢复自然时长）
            natural_ms = actual_ms * applied_rate

            features = extract_features(text_zh)
            samples.append({
                "text_zh": text_zh,
                "actual_ms": actual_ms,
                "natural_ms": natural_ms,
                "applied_rate": applied_rate,
                "video": os.path.basename(vdir),
                "idx": idx,
                **features,
            })

    return samples


# ── 估算函数（可替换参数）──

def estimate_with_params(features: dict, params: dict) -> float:
    """用给定参数估算时长"""
    total = params.get("intercept", 0)
    for name in FEATURE_NAMES:
        total += features[name] * params[name]
    return total


# ── 校准 ──

def calibrate(samples: list, prosody_multiplier: float = 1.0) -> dict:
    """Ridge 回归拟合最优参数

    Args:
        samples: collect_samples 的输出
        prosody_multiplier: 应用于估算结果的韵律乘数（校准后此值应接近 1.0）

    Returns:
        dict with calibrated params, metrics, comparison
    """
    import numpy as np

    n = len(samples)
    X = np.zeros((n, len(FEATURE_NAMES)))
    y = np.zeros(n)

    for i, s in enumerate(samples):
        for j, name in enumerate(FEATURE_NAMES):
            X[i, j] = s[name]
        y[i] = s["natural_ms"]

    # Ridge 回归（小正则化防止过拟合）
    # 手动实现避免 sklearn 依赖
    alphas = [0.1, 1.0, 10.0, 100.0]
    best_alpha = 1.0
    best_score = -1e18

    for alpha in alphas:
        # Ridge: (X^T X + alpha I)^-1 X^T y
        XtX = X.T @ X + alpha * np.eye(X.shape[1])
        Xty = X.T @ y
        w = np.linalg.solve(XtX, Xty)
        # 加截距: y_mean - w @ X_mean
        y_pred = X @ w
        ss_res = np.sum((y - y_pred) ** 2)
        ss_tot = np.sum((y - np.mean(y)) ** 2)
        r2 = 1 - ss_res / ss_tot
        if r2 > best_score:
            best_score = r2
            best_alpha = alpha

    # 用最佳 alpha 带截距重新拟合
    X_bias = np.column_stack([X, np.ones(n)])
    XtX = X_bias.T @ X_bias + best_alpha * np.eye(X_bias.shape[1])
    XtX[-1, -1] = best_alpha * 0.01  # 截距少正则化
    Xty = X_bias.T @ y
    w_full = np.linalg.solve(XtX, Xty)

    calibrated = {}
    for j, name in enumerate(FEATURE_NAMES):
        calibrated[name] = round(float(w_full[j]), 1)
    calibrated["intercept"] = round(float(w_full[-1]), 1)

    # ── 评估：校准前 vs 校准后 ──
    baseline_errors = []
    calibrated_errors = []
    baseline_deviations = []
    calibrated_deviations = []

    for s in samples:
        natural = s["natural_ms"]
        feat = {name: s[name] for name in FEATURE_NAMES}

        est_baseline = estimate_with_params(feat, BASELINE_PARAMS)  # v1 校准参数（已无乘数）
        est_calibrated = estimate_with_params(feat, calibrated)  # 校准后不需要乘数

        dev_baseline = abs(est_baseline - natural) / natural
        dev_calibrated = abs(est_calibrated - natural) / natural

        baseline_errors.append(abs(est_baseline - natural))
        calibrated_errors.append(abs(est_calibrated - natural))
        baseline_deviations.append(dev_baseline)
        calibrated_deviations.append(dev_calibrated)

    baseline_deviations = np.array(baseline_deviations)
    calibrated_deviations = np.array(calibrated_deviations)

    metrics = {
        "n_samples": n,
        "alpha": best_alpha,
        "r2": round(best_score, 4),
        "baseline": {
            "mae_ms": round(float(np.mean(baseline_errors)), 1),
            "mape": round(float(np.mean(baseline_deviations)) * 100, 1),
            "within_15pct": round(float(np.mean(baseline_deviations < 0.15)) * 100, 1),
            "within_20pct": round(float(np.mean(baseline_deviations < 0.20)) * 100, 1),
            "phase2_triggers": int(np.sum(baseline_deviations >= 0.15)),
        },
        "calibrated": {
            "mae_ms": round(float(np.mean(calibrated_errors)), 1),
            "mape": round(float(np.mean(calibrated_deviations)) * 100, 1),
            "within_15pct": round(float(np.mean(calibrated_deviations < 0.15)) * 100, 1),
            "within_20pct": round(float(np.mean(calibrated_deviations < 0.20)) * 100, 1),
            "phase2_triggers": int(np.sum(calibrated_deviations >= 0.15)),
        },
    }

    return {
        "params": calibrated,
        "metrics": metrics,
    }


# ── 参数写入 pipeline.py ──

def apply_to_pipeline(params: dict, pipeline_path: str = "pipeline.py"):
    """将校准后的参数写入 pipeline.py 的 _estimate_duration_jieba 函数"""
    with open(pipeline_path, encoding="utf-8") as f:
        content = f.read()

    replacements = [
        # 中文词时长
        (r"(if zh_count == 1:\s*\n\s*total_ms \+= )\d+", rf"\g<1>{int(params['n_1char'])}"),
        (r"(elif zh_count == 2:\s*\n\s*total_ms \+= )\d+", rf"\g<1>{int(params['n_2char'])}"),
        (r"(elif zh_count == 3:\s*\n\s*total_ms \+= )\d+", rf"\g<1>{int(params['n_3char'])}"),
        (r"(total_ms \+= zh_count \* )\d+", rf"\g<1>{int(params['n_4plus'])}"),
        # 英文/数字
        (r"(total_ms \+= letters \* )\d+( \+ digits \* )\d+",
         rf"\g<1>{int(params['n_letters'])}\g<2>{int(params['n_digits'])}"),
        # URL
        (r"(url_ms \+= url_chars \* )\d+", rf"\g<1>{int(params['n_url_chars'])}"),
        # 标点停顿
        (r"(total_ms \+= )\d+(  # 标点停顿)", rf"\g<1>{int(params['n_punct'])}\2"),
        # 截距 (替换 'total_ms - 63' 或 'total_ms + 42' 模式)
        (r"total_ms [+-] \d+\)  # 校准截距.*",
         f"total_ms {'+' if params['intercept'] >= 0 else '-'} {abs(int(params['intercept']))})  # 校准截距: {int(params['intercept'])}ms (Ridge v2 拟合)"),
    ]

    new_content = content
    for pattern, repl in replacements:
        new_content = re.sub(pattern, repl, new_content)

    if new_content == content:
        print("  ⚠️  未找到可替换的参数模式，pipeline.py 未修改")
        return False

    # 更新 docstring 中的经验值
    old_doc_values = {
        "单字词": params["n_1char"], "双字词": params["n_2char"],
        "三字词": params["n_3char"], "四字及以上": params["n_4plus"],
    }
    for label, val in old_doc_values.items():
        new_content = re.sub(
            rf"({label}.*?): ~\d+ms",
            rf"\1: ~{int(val)}ms",
            new_content,
        )
    new_content = re.sub(r"(英文单词: ~)\d+(ms)", rf"\g<1>{int(params['n_letters'])}\2", new_content)
    new_content = re.sub(r"(URL/域名.*?): ~\d+(ms)", rf"\1: ~{int(params['n_url_chars'])}\2", new_content)
    new_content = re.sub(r"(数字: ~)\d+(ms)", rf"\g<1>{int(params['n_digits'])}\2", new_content)

    with open(pipeline_path, "w", encoding="utf-8") as f:
        f.write(new_content)

    print(f"  ✅ 已将校准参数写入 {pipeline_path}")
    return True


# ── 主流程 ──

def main():
    import argparse
    parser = argparse.ArgumentParser(description="TTS 时长估算器校准")
    parser.add_argument("video_dirs", nargs="*", help="视频输出目录（默认扫描 output/*/）")
    parser.add_argument("--apply", action="store_true", help="自动将校准参数写入 pipeline.py")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式结果")
    args = parser.parse_args()

    # 发现视频目录
    if args.video_dirs:
        video_dirs = args.video_dirs
    else:
        video_dirs = sorted(glob.glob("output/*/") + glob.glob("output/*/*/"))
    video_dirs = [d.rstrip("/") for d in video_dirs if os.path.isdir(d)]

    if not video_dirs:
        print("❌ 未找到视频目录。用法: python calibrate_tts_duration.py [output/VIDEO_ID/...]")
        sys.exit(1)

    print(f"📊 TTS 时长估算器校准")
    print(f"   数据源: {len(video_dirs)} 个视频目录")

    # 收集样本
    warnings.filterwarnings("ignore", category=UserWarning)
    samples = collect_samples(video_dirs)
    if len(samples) < 20:
        print(f"❌ 样本不足: {len(samples)} 个（至少需要 20 个）")
        sys.exit(1)
    print(f"   样本数: {len(samples)}")

    # 校准
    result = calibrate(samples)
    params = result["params"]
    metrics = result["metrics"]

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    # 打印结果
    print(f"\n{'='*60}")
    print(f"校准结果 (Ridge alpha={metrics['alpha']}, R²={metrics['r2']})")
    print(f"{'='*60}")

    print(f"\n参数对比:")
    print(f"  {'特征':<12} {'原值':>8} {'校准值':>8} {'变化':>8}")
    print(f"  {'-'*40}")
    for name in FEATURE_NAMES:
        old = BASELINE_PARAMS[name]
        new = params[name]
        delta = new - old
        print(f"  {name:<12} {old:>8.0f} {new:>8.1f} {delta:>+8.1f}")
    print(f"  {'intercept':<12} {'0':>8} {params['intercept']:>8.1f} {params['intercept']:>+8.1f}")

    print(f"\n精度对比:")
    print(f"  {'指标':<24} {'原始(*1.3)':>12} {'校准后':>12}")
    print(f"  {'-'*50}")
    b, c = metrics["baseline"], metrics["calibrated"]
    print(f"  {'MAE (ms)':<24} {b['mae_ms']:>12.1f} {c['mae_ms']:>12.1f}")
    print(f"  {'MAPE (%)':<24} {b['mape']:>12.1f} {c['mape']:>12.1f}")
    print(f"  {'偏差 <15% 比例':<24} {b['within_15pct']:>11.1f}% {c['within_15pct']:>11.1f}%")
    print(f"  {'偏差 <20% 比例':<24} {b['within_20pct']:>11.1f}% {c['within_20pct']:>11.1f}%")
    print(f"  {'Phase2 触发数':<24} {b['phase2_triggers']:>12} {c['phase2_triggers']:>12}")

    reduction = (1 - c["phase2_triggers"] / max(b["phase2_triggers"], 1)) * 100
    print(f"\n  📉 Phase2 触发减少: {reduction:.0f}% ({b['phase2_triggers']} → {c['phase2_triggers']})")

    if args.apply:
        print()
        apply_to_pipeline(params)

    # 保存校准结果
    cal_path = "calibration_result.json"
    with open(cal_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"\n  💾 校准结果: {cal_path}")


if __name__ == "__main__":
    main()
