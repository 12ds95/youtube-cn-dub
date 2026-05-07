#!/usr/bin/env python3
"""Duration estimator v3+ 实验脚本 — 数据驱动校准 + 多模型对比。

阶段:
  v0: 数据预处理 (调速异常 + MAD outlier 过滤)
  v5': 净数据 Ridge 回归 baseline
  v3: 加音节级特征 (pypinyin 声调/韵母/声母)
  v4: 加韵律特征 (标点类型 / 句末位置 / 语气词)
  v6: GBDT (LightGBM/XGBoost) 或 MLP 对比

用法:
  venv/bin/python3 scripts/calibrate_v3.py [--stage v0|v5|v3|v4|v6|all]
                                          [--save-result PATH]

输出:
  - 净数据集大小 / 过滤前后对比
  - 各阶段 R² / MAE / MAPE / 95% within / Phase2 触发数
  - JSON 实验结果到 audit/duration_estimator_experiments.json
"""
import os
import sys
import json
import re
import glob
import argparse
import warnings
from typing import List, Dict, Tuple, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

# ── 与现有 estimator 一致的特征提取 ──────────────────────────────
_URL_PATTERN = re.compile(
    r'(?:https?://)?(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}'
    r'(?:/[^\s]*)?'
)

BASE_FEATURES = [
    "n_1char", "n_2char", "n_3char", "n_4plus",
    "n_letters", "n_digits", "n_url_chars", "n_punct",
]

# v3 音节级特征 (pypinyin)
SYLLABLE_FEATURES = [
    "n_tone1", "n_tone2", "n_tone3", "n_tone4", "n_tone_neutral",
    "n_final_simple", "n_final_complex",  # 单元音 vs 复合韵母
    "n_no_initial",                        # 零声母
]

# v4 韵律级特征
PROSODY_FEATURES = [
    "n_comma", "n_period", "n_question", "n_exclaim", "n_ellipsis",
    "n_sentence_final",  # 句末 (近 . ! ?) 的字数
    "n_filler_word",      # 语气词数量 (吧/呢/啊/嘛)
]

# v7 token 特征 (针对 d4Eg/kCc atempo 失效模式: 数学符号 + 音译名 + 单字母变量)
V7_TOKEN_FEATURES = [
    "n_capital_word",   # 音译名 (Linus / Felix / TTS) — edge-tts 朗读较慢
    "n_math_op",         # 数学运算符 (×, ÷, ·, −, √, ∑, ∫, →, ⊥, ∞ ...)
    "n_ascii_isolated",  # 单 ASCII 字母变量 (i, j, k, x, y, z 等数学变量)
]

FILLER_WORDS = {'吧', '呢', '啊', '嘛', '哦', '哎', '嗯', '哈', '呀'}

# 数学运算符 (不含 ASCII +-*/= 防中文文本误伤)
MATH_OPS = set("×÷·−√∑∫⊥∞→←⇒⇐∂∇≤≥≠≈⊆⊇∈∉∪∩⊕⊗∥‖")
CAPITAL_WORD_RE = re.compile(r'[A-Z][a-zA-Z]+')  # ≥2 字母, 首字母大写
ASCII_ISOLATED_RE = re.compile(r'(?<![a-zA-Z])[a-zA-Z](?![a-zA-Z])')


def extract_base_features(text_zh: str) -> Dict[str, int]:
    """v2 baseline 8 维特征 (与 _estimate_duration_jieba 一致)."""
    import jieba
    import unicodedata

    url_chars = 0
    clean_text = text_zh
    for m in _URL_PATTERN.finditer(text_zh):
        url_str = m.group()
        url_chars += sum(1 for c in url_str if c.isalnum() or c in './-_:')
        clean_text = clean_text.replace(url_str, '', 1)

    words = jieba.lcut(clean_text)
    feat = {k: 0 for k in BASE_FEATURES}

    for word in words:
        meaningful = [c for c in word
                      if not unicodedata.category(c).startswith(('P', 'Z', 'C'))]
        if not meaningful:
            feat["n_punct"] += 1
            continue
        zh_count = sum(1 for c in meaningful if '一' <= c <= '鿿')
        other_count = len(meaningful) - zh_count
        if zh_count > 0:
            if zh_count == 1: feat["n_1char"] += 1
            elif zh_count == 2: feat["n_2char"] += 1
            elif zh_count == 3: feat["n_3char"] += 1
            else: feat["n_4plus"] += zh_count
        if other_count > 0:
            for c in meaningful:
                if c.isdigit():
                    feat["n_digits"] += 1
                elif not ('一' <= c <= '鿿'):
                    feat["n_letters"] += 1
    feat["n_url_chars"] = url_chars
    return feat


def extract_syllable_features(text_zh: str) -> Dict[str, int]:
    """v3 音节级特征: 声调/韵母/声母."""
    from pypinyin import pinyin, Style
    feat = {k: 0 for k in SYLLABLE_FEATURES}
    if not text_zh:
        return feat

    # 只对汉字提取拼音
    chinese_chars = [c for c in text_zh if '一' <= c <= '鿿']
    if not chinese_chars:
        return feat

    chinese_text = ''.join(chinese_chars)
    tones = pinyin(chinese_text, style=Style.TONE3, heteronym=False)
    finals = pinyin(chinese_text, style=Style.FINALS_TONE3, heteronym=False)
    initials = pinyin(chinese_text, style=Style.INITIALS, heteronym=False)

    for tp, fp, ip in zip(tones, finals, initials):
        py = tp[0] if tp else ""
        # 声调: 末尾数字 1/2/3/4, 否则 0 (轻声)
        if py and py[-1].isdigit():
            t = int(py[-1])
            if t == 1: feat["n_tone1"] += 1
            elif t == 2: feat["n_tone2"] += 1
            elif t == 3: feat["n_tone3"] += 1
            elif t == 4: feat["n_tone4"] += 1
        else:
            feat["n_tone_neutral"] += 1
        # 韵母长度: > 2 字符 (如 iang/uang) 算复合
        final = fp[0] if fp else ""
        final_letters = re.sub(r'\d', '', final)
        if len(final_letters) >= 3:
            feat["n_final_complex"] += 1
        else:
            feat["n_final_simple"] += 1
        # 零声母 (initials 为空)
        ini = ip[0] if ip else ""
        if not ini:
            feat["n_no_initial"] += 1

    return feat


def extract_prosody_features(text_zh: str) -> Dict[str, int]:
    """v4 韵律级特征: 标点类型, 句末位置, 语气词."""
    feat = {k: 0 for k in PROSODY_FEATURES}
    if not text_zh:
        return feat

    feat["n_comma"] = text_zh.count(',') + text_zh.count(',') + text_zh.count(';') + text_zh.count(';')
    feat["n_period"] = text_zh.count('.') + text_zh.count('。')
    feat["n_question"] = text_zh.count('?') + text_zh.count('?')
    feat["n_exclaim"] = text_zh.count('!') + text_zh.count('!')
    feat["n_ellipsis"] = text_zh.count('…') + text_zh.count('...')

    # 句末位置: 句末标点前的最后 2 个汉字
    sentence_end_re = re.compile(r'[。.!?！？]')
    cnt = 0
    for m in sentence_end_re.finditer(text_zh):
        end_pos = m.start()
        # 取末标点前的 2 字 (如果是汉字)
        for c in text_zh[max(0, end_pos - 2):end_pos]:
            if '一' <= c <= '鿿':
                cnt += 1
    feat["n_sentence_final"] = cnt

    # 语气词
    feat["n_filler_word"] = sum(1 for c in text_zh if c in FILLER_WORDS)
    return feat


def extract_token_features(text_zh: str) -> Dict[str, int]:
    """v7 token 特征: 数学符号/音译名/单字母变量 (TTS 朗读较慢)."""
    feat = {k: 0 for k in V7_TOKEN_FEATURES}
    if not text_zh:
        return feat
    feat["n_capital_word"] = len(CAPITAL_WORD_RE.findall(text_zh))
    feat["n_math_op"] = sum(1 for c in text_zh if c in MATH_OPS)
    feat["n_ascii_isolated"] = len(ASCII_ISOLATED_RE.findall(text_zh))
    return feat


def extract_all_features(text_zh: str, include_v7: bool = True) -> Dict[str, int]:
    """提取全部特征 (v2 + v3 + v4 [+ v7]).

    include_v7=False 用于复现 v6 23 维模型 (向后兼容).
    """
    f = extract_base_features(text_zh)
    f.update(extract_syllable_features(text_zh))
    f.update(extract_prosody_features(text_zh))
    if include_v7:
        f.update(extract_token_features(text_zh))
    return f


# ── 数据收集 + v0 预处理 ──────────────────────────────────────────

def collect_raw_samples(video_dirs: List[str]) -> List[Dict]:
    """从视频目录收集原始 (text_zh, actual_ms, applied_rate) 样本."""
    from pydub import AudioSegment
    from pipeline import _estimate_duration_jieba

    samples = []
    for vdir in video_dirs:
        cache_path = os.path.join(vdir, "segments_cache.json")
        tts_dir = os.path.join(vdir, "tts_segments")
        if not os.path.exists(cache_path) or not os.path.exists(tts_dir):
            continue

        try:
            with open(cache_path, encoding="utf-8") as f:
                segments = json.load(f)
        except Exception:
            continue

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

        # 读 speed_report.json 看是否有 atempo 调速 (truncate 段)
        atempo_segs = set()
        sr_path = os.path.join(audit_dir, "speed_report.json")
        if os.path.exists(sr_path):
            try:
                with open(sr_path, encoding="utf-8") as f:
                    sr = json.load(f)
                for seg in sr.get("segments_atempo", []):
                    atempo_segs.add(seg.get("idx"))
            except Exception:
                pass

        for idx, seg in enumerate(segments):
            text_zh = seg.get("text_zh", seg.get("text", ""))
            if len(text_zh.strip()) < 2:
                continue
            if not re.search(r'[一-鿿㐀-䶿a-zA-Z0-9]', text_zh):
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

            start_ms = int(seg.get("start", 0) * 1000)
            end_ms = int(seg.get("end", 0) * 1000)
            target_dur_ms = end_ms - start_ms

            if idx in feedback_rates:
                applied_rate = feedback_rates[idx]
            elif target_dur_ms > 0:
                est_ms = _estimate_duration_jieba(text_zh)
                raw_ratio = est_ms / target_dur_ms if target_dur_ms > 0 else 1.0
                applied_rate = max(0.80, min(1.35, raw_ratio))
            else:
                applied_rate = 1.0

            natural_ms = actual_ms * applied_rate

            samples.append({
                "text_zh": text_zh,
                "actual_ms": actual_ms,
                "natural_ms": natural_ms,
                "target_dur_ms": target_dur_ms,
                "applied_rate": applied_rate,
                "video": os.path.basename(vdir.rstrip('/')),
                "idx": idx,
                "is_atempo": idx in atempo_segs,
            })

    return samples


def filter_v0(samples: List[Dict],
              clamp_eps: float = 0.01,
              mad_threshold: float = 3.5,
              verbose: bool = True) -> Tuple[List[Dict], Dict]:
    """v0 数据预处理: 移除调速异常 / 截断 / MAD outlier。

    过滤规则:
      1. applied_rate clamp 在边界 (≤ 0.81 或 ≥ 1.34): natural_ms 反推不可信
      2. 标记为 atempo 的段: 实际时长经过 ffmpeg 调速, 失真
      3. natural_ms / chinese_chars (单字时长) 用 MAD 检测离群

    Returns:
        (filtered_samples, stats)
    """
    n0 = len(samples)
    rejected = {"clamp_low": 0, "clamp_high": 0, "atempo": 0, "mad_outlier": 0}

    keep = []
    for s in samples:
        ar = s["applied_rate"]
        if ar <= 0.80 + clamp_eps:
            rejected["clamp_low"] += 1
            continue
        if ar >= 1.35 - clamp_eps:
            rejected["clamp_high"] += 1
            continue
        if s["is_atempo"]:
            rejected["atempo"] += 1
            continue
        keep.append(s)

    # MAD outlier on natural_ms / total_chars
    ratios = []
    for s in keep:
        total_chars = sum(1 for c in s["text_zh"] if '一' <= c <= '鿿')
        total_chars += sum(1 for c in s["text_zh"] if c.isalnum() and not ('一' <= c <= '鿿'))
        if total_chars >= 3:
            ratios.append((s, s["natural_ms"] / total_chars))

    if ratios:
        vals = np.array([r[1] for r in ratios])
        med = np.median(vals)
        mad = np.median(np.abs(vals - med))
        if mad > 0:
            modified_z = 0.6745 * (vals - med) / mad
            outlier_mask = np.abs(modified_z) > mad_threshold
            outlier_indices = set()
            for i, is_out in enumerate(outlier_mask):
                if is_out:
                    outlier_indices.add(id(ratios[i][0]))
            keep_after_mad = []
            for s in keep:
                if id(s) in outlier_indices:
                    rejected["mad_outlier"] += 1
                else:
                    keep_after_mad.append(s)
            keep = keep_after_mad

    stats = {
        "input": n0,
        "kept": len(keep),
        "rejected": rejected,
        "kept_pct": round(100 * len(keep) / max(n0, 1), 1),
    }

    if verbose:
        print(f"  v0 过滤: {n0} → {len(keep)} ({stats['kept_pct']}% 保留)")
        for k, v in rejected.items():
            if v > 0:
                print(f"    - {k}: {v} 段")

    return keep, stats


# ── 训练 + 评估 ─────────────────────────────────────────────────

def fit_ridge(samples: List[Dict], features: List[str], alpha: float = 1.0,
              with_intercept: bool = True) -> Tuple[Dict, Dict]:
    """Ridge 回归拟合 (numpy 手写, 避免 sklearn 依赖)."""
    n = len(samples)
    X = np.zeros((n, len(features)))
    y = np.zeros(n)

    for i, s in enumerate(samples):
        feat = extract_all_features(s["text_zh"])
        for j, name in enumerate(features):
            X[i, j] = feat.get(name, 0)
        y[i] = s["natural_ms"]

    if with_intercept:
        X_full = np.column_stack([X, np.ones(n)])
        XtX = X_full.T @ X_full + alpha * np.eye(X_full.shape[1])
        XtX[-1, -1] = alpha * 0.01  # 截距少正则化
        Xty = X_full.T @ y
        w = np.linalg.solve(XtX, Xty)
    else:
        XtX = X.T @ X + alpha * np.eye(X.shape[1])
        Xty = X.T @ y
        w = np.linalg.solve(XtX, Xty)
        w = np.append(w, 0.0)  # intercept = 0

    params = {features[j]: float(w[j]) for j in range(len(features))}
    if with_intercept:
        params["intercept"] = float(w[-1])
    else:
        params["intercept"] = 0.0

    # 评估
    y_pred = X @ w[:-1] + w[-1]
    metrics = compute_metrics(y, y_pred)
    metrics["alpha"] = alpha
    metrics["n_samples"] = n
    return params, metrics


def compute_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> Dict:
    """R², MAE, MAPE, within-X% 比例."""
    err_abs = np.abs(y_true - y_pred)
    err_pct = err_abs / np.maximum(y_true, 1)
    ss_res = float(np.sum((y_true - y_pred) ** 2))
    ss_tot = float(np.sum((y_true - np.mean(y_true)) ** 2))
    return {
        "r2": round(1 - ss_res / max(ss_tot, 1), 4),
        "mae_ms": round(float(np.mean(err_abs)), 1),
        "mape_pct": round(float(np.mean(err_pct)) * 100, 2),
        "within_10pct": round(float(np.mean(err_pct < 0.10)) * 100, 1),
        "within_15pct": round(float(np.mean(err_pct < 0.15)) * 100, 1),
        "within_20pct": round(float(np.mean(err_pct < 0.20)) * 100, 1),
    }


def cross_validate(samples: List[Dict], features: List[str],
                   alpha: float = 1.0, k_folds: int = 5) -> Dict:
    """K-fold CV by video (防止泄漏: 同视频段不跨折)."""
    by_video = {}
    for s in samples:
        by_video.setdefault(s["video"], []).append(s)
    videos = list(by_video.keys())
    np.random.seed(42)
    np.random.shuffle(videos)

    fold_size = max(1, len(videos) // k_folds)
    fold_metrics = []
    for k in range(k_folds):
        test_vids = set(videos[k * fold_size: (k + 1) * fold_size])
        train_set = [s for s in samples if s["video"] not in test_vids]
        test_set = [s for s in samples if s["video"] in test_vids]
        if not train_set or not test_set:
            continue
        params, _ = fit_ridge(train_set, features, alpha=alpha)
        # 在 test_set 上评估
        n_test = len(test_set)
        X_test = np.zeros((n_test, len(features)))
        y_test = np.zeros(n_test)
        for i, s in enumerate(test_set):
            feat = extract_all_features(s["text_zh"])
            for j, name in enumerate(features):
                X_test[i, j] = feat.get(name, 0)
            y_test[i] = s["natural_ms"]
        w = np.array([params[f] for f in features])
        y_pred = X_test @ w + params["intercept"]
        m = compute_metrics(y_test, y_pred)
        fold_metrics.append(m)

    if not fold_metrics:
        return {}
    avg = {}
    for key in fold_metrics[0]:
        avg[key] = round(float(np.mean([m[key] for m in fold_metrics])), 4)
    avg["k_folds"] = len(fold_metrics)
    return avg


# ── 主流程 ──────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", default="all",
                        choices=["v0", "v5", "v3", "v4", "v6", "v7", "v8", "all"])
    parser.add_argument("--save-result", help="保存实验结果 JSON 到指定路径")
    args = parser.parse_args()

    warnings.filterwarnings("ignore", category=UserWarning)

    # 收集样本 (磁盘缓存避免重新加载 mp3)
    cache_path = "/tmp/duration_estimator_samples_cache.json"
    if os.path.exists(cache_path):
        print(f"📊 从缓存加载样本 ({cache_path})...")
        with open(cache_path) as f:
            raw_samples = json.load(f)
    else:
        print("📊 首次收集样本 (将缓存到 /tmp)...")
        video_dirs = sorted(glob.glob("output/*/") + glob.glob("output/*/*/"))
        video_dirs = [d.rstrip("/") for d in video_dirs if os.path.isdir(d)]
        raw_samples = collect_raw_samples(video_dirs)
        with open(cache_path, 'w') as f:
            json.dump(raw_samples, f, ensure_ascii=False)
    print(f"  原始样本数: {len(raw_samples)}")

    # ── v0: 数据预处理 ──
    print("\n🧹 v0 数据预处理...")
    clean_samples, v0_stats = filter_v0(raw_samples)

    results = {"v0": v0_stats, "stages": {}}

    # ── v5': 净数据 baseline (BASE_FEATURES only) ──
    if args.stage in ("v5", "all"):
        print("\n📐 v5: 净数据 Ridge baseline (BASE_FEATURES)...")
        params, metrics = fit_ridge(clean_samples, BASE_FEATURES, alpha=1.0)
        cv = cross_validate(clean_samples, BASE_FEATURES, alpha=1.0)
        results["stages"]["v5"] = {
            "features": BASE_FEATURES,
            "params": params,
            "in_sample": metrics,
            "cv_5fold": cv,
        }
        print(f"  in-sample: R²={metrics['r2']}, MAE={metrics['mae_ms']}ms, MAPE={metrics['mape_pct']}%")
        print(f"  5-fold CV: R²={cv.get('r2','?')}, MAE={cv.get('mae_ms','?')}ms")

    # ── v3: 加音节特征 ──
    if args.stage in ("v3", "all"):
        print("\n🎵 v3: 加音节级特征...")
        feat_v3 = BASE_FEATURES + SYLLABLE_FEATURES
        params, metrics = fit_ridge(clean_samples, feat_v3, alpha=1.0)
        cv = cross_validate(clean_samples, feat_v3, alpha=1.0)
        results["stages"]["v3"] = {
            "features": feat_v3,
            "params": params,
            "in_sample": metrics,
            "cv_5fold": cv,
        }
        print(f"  in-sample: R²={metrics['r2']}, MAE={metrics['mae_ms']}ms")
        print(f"  5-fold CV: R²={cv.get('r2','?')}, MAE={cv.get('mae_ms','?')}ms")

    # ── v4: 加韵律特征 ──
    if args.stage in ("v4", "all"):
        print("\n🎼 v4: 加韵律级特征...")
        feat_v4 = BASE_FEATURES + SYLLABLE_FEATURES + PROSODY_FEATURES
        params, metrics = fit_ridge(clean_samples, feat_v4, alpha=1.0)
        cv = cross_validate(clean_samples, feat_v4, alpha=1.0)
        results["stages"]["v4"] = {
            "features": feat_v4,
            "params": params,
            "in_sample": metrics,
            "cv_5fold": cv,
        }
        print(f"  in-sample: R²={metrics['r2']}, MAE={metrics['mae_ms']}ms")
        print(f"  5-fold CV: R²={cv.get('r2','?')}, MAE={cv.get('mae_ms','?')}ms")

    # ── v8: 替代 loss / 超参 调优 v6 (同 23 维特征) ──
    # 学习自 v7 失败: 不改特征架构, 仅调 loss/超参. 试探:
    #   - L1 (MAE) vs L2 (v6 baseline) vs Huber: 哪种 loss 对 outlier 更鲁棒
    #   - 不同 num_leaves / n_estimators 是否过/欠拟合
    if args.stage in ("v8", "all"):
        try:
            import lightgbm as lgb
            print("\n🛡️  v8: 替代 loss/超参 调优 (23 维, 同 v6 特征)...")
            feat_v8 = BASE_FEATURES + SYLLABLE_FEATURES + PROSODY_FEATURES
            n = len(clean_samples)
            X = np.zeros((n, len(feat_v8)))
            y = np.zeros(n)
            for i, s in enumerate(clean_samples):
                f = extract_all_features(s["text_zh"], include_v7=False)
                for j, name in enumerate(feat_v8):
                    X[i, j] = f.get(name, 0)
                y[i] = s["natural_ms"]
            by_video = {}
            for s in clean_samples:
                by_video.setdefault(s["video"], []).append(s)
            videos = sorted(by_video.keys())
            np.random.seed(42)
            np.random.shuffle(videos)
            fold_size = max(1, len(videos) // 5)

            def cv_metrics(model_kwargs):
                cv_mae, cv_r2 = [], []
                for k in range(5):
                    test_vids = set(videos[k * fold_size: (k + 1) * fold_size])
                    train_idx = [i for i, s in enumerate(clean_samples) if s["video"] not in test_vids]
                    test_idx = [i for i, s in enumerate(clean_samples) if s["video"] in test_vids]
                    if not train_idx or not test_idx:
                        continue
                    model = lgb.LGBMRegressor(verbosity=-1, **model_kwargs)
                    model.fit(X[train_idx], y[train_idx])
                    pred = model.predict(X[test_idx])
                    m = compute_metrics(y[test_idx], pred)
                    cv_mae.append(m["mae_ms"])
                    cv_r2.append(m["r2"])
                return {"r2_mean": round(float(np.mean(cv_r2)), 4),
                        "mae_mean": round(float(np.mean(cv_mae)), 1)}

            v8_results = {}
            configs = [
                ("L2_v6_repro",       {"objective": "regression",    "n_estimators": 200, "learning_rate": 0.05, "num_leaves": 15, "min_child_samples": 20}),
                ("L1_MAE",            {"objective": "regression_l1", "n_estimators": 200, "learning_rate": 0.05, "num_leaves": 15, "min_child_samples": 20}),
                ("L2_more_trees",     {"objective": "regression",    "n_estimators": 400, "learning_rate": 0.03, "num_leaves": 15, "min_child_samples": 20}),
                ("L2_deeper",         {"objective": "regression",    "n_estimators": 200, "learning_rate": 0.05, "num_leaves": 31, "min_child_samples": 20}),
                ("L2_more_min_child", {"objective": "regression",    "n_estimators": 200, "learning_rate": 0.05, "num_leaves": 15, "min_child_samples": 50}),
            ]
            for name, kw in configs:
                m = cv_metrics(kw)
                v8_results[name] = m
                print(f"  {name:22s}: CV R²={m['r2_mean']}, MAE={m['mae_mean']}ms")
            results["stages"]["v8"] = v8_results

            # 选最佳 (按 MAE) 训练全量并保存
            best_name = min(v8_results, key=lambda k: v8_results[k]['mae_mean'])
            best_kw = dict(configs)[best_name]
            print(f"  最佳: {best_name} (MAE={v8_results[best_name]['mae_mean']}ms, R²={v8_results[best_name]['r2_mean']})")
            model_full = lgb.LGBMRegressor(verbosity=-1, **best_kw)
            model_full.fit(X, y)
            os.makedirs("models", exist_ok=True)
            model_full.booster_.save_model("models/duration_estimator_lgbm_v8.txt")
            with open("models/duration_estimator_lgbm_v8_features.json", 'w') as fout:
                json.dump(feat_v8, fout)
            print(f"  💾 v8 LightGBM 模型: models/duration_estimator_lgbm_v8.txt ({best_name})")
        except ImportError:
            print(f"  ⚠️  lightgbm 未安装")

    # ── v7: Ridge + GBDT 加 token 特征 (音译名/数学符号/单字母) ──
    if args.stage in ("v7", "all"):
        print("\n🔬 v7: 加 token 特征 (Ridge)...")
        feat_v7 = BASE_FEATURES + SYLLABLE_FEATURES + PROSODY_FEATURES + V7_TOKEN_FEATURES
        params, metrics = fit_ridge(clean_samples, feat_v7, alpha=1.0)
        cv = cross_validate(clean_samples, feat_v7, alpha=1.0)
        results["stages"]["v7_ridge"] = {
            "features": feat_v7,
            "params": params,
            "in_sample": metrics,
            "cv_5fold": cv,
        }
        print(f"  Ridge in-sample: R²={metrics['r2']}, MAE={metrics['mae_ms']}ms")
        print(f"  Ridge 5-fold CV: R²={cv.get('r2','?')}, MAE={cv.get('mae_ms','?')}ms")
        # 检查 v7 token 系数是否显著
        for tk in V7_TOKEN_FEATURES:
            print(f"  系数[{tk}]: {params.get(tk, 0):.1f} ms/token")

        try:
            import lightgbm as lgb
            print("\n🌲 v7: GBDT (LightGBM 26 维)...")
            n = len(clean_samples)
            X = np.zeros((n, len(feat_v7)))
            y = np.zeros(n)
            for i, s in enumerate(clean_samples):
                f = extract_all_features(s["text_zh"], include_v7=True)
                for j, name in enumerate(feat_v7):
                    X[i, j] = f.get(name, 0)
                y[i] = s["natural_ms"]
            by_video = {}
            for s in clean_samples:
                by_video.setdefault(s["video"], []).append(s)
            videos = sorted(by_video.keys())
            np.random.seed(42)
            np.random.shuffle(videos)
            fold_size = max(1, len(videos) // 5)
            cv_mae, cv_r2 = [], []
            for k in range(5):
                test_vids = set(videos[k * fold_size: (k + 1) * fold_size])
                train_idx = [i for i, s in enumerate(clean_samples) if s["video"] not in test_vids]
                test_idx = [i for i, s in enumerate(clean_samples) if s["video"] in test_vids]
                if not train_idx or not test_idx:
                    continue
                model = lgb.LGBMRegressor(n_estimators=200, learning_rate=0.05,
                                          num_leaves=15, min_child_samples=20,
                                          verbosity=-1)
                model.fit(X[train_idx], y[train_idx])
                pred = model.predict(X[test_idx])
                m = compute_metrics(y[test_idx], pred)
                cv_mae.append(m["mae_ms"])
                cv_r2.append(m["r2"])
            cv_lgbm = {"r2_mean": round(float(np.mean(cv_r2)), 4),
                       "mae_mean": round(float(np.mean(cv_mae)), 1),
                       "k_folds": len(cv_r2)}
            results["stages"]["v7_lgbm"] = {"cv_5fold_lgbm": cv_lgbm}
            print(f"  LightGBM CV: R²={cv_lgbm['r2_mean']}, MAE={cv_lgbm['mae_mean']}ms")

            # 训练全量并保存 v7 模型 (单独文件, 不覆盖 v6)
            model_full = lgb.LGBMRegressor(n_estimators=200, learning_rate=0.05,
                                           num_leaves=15, min_child_samples=20,
                                           verbosity=-1)
            model_full.fit(X, y)
            os.makedirs("models", exist_ok=True)
            model_full.booster_.save_model("models/duration_estimator_lgbm_v7.txt")
            with open("models/duration_estimator_lgbm_v7_features.json", 'w') as fout:
                json.dump(feat_v7, fout)
            print(f"  💾 v7 LightGBM 模型: models/duration_estimator_lgbm_v7.txt")

            # 保存 v7 Ridge 参数
            v7_path = "audit/duration_estimator_v7_params.json"
            os.makedirs(os.path.dirname(v7_path), exist_ok=True)
            with open(v7_path, 'w', encoding='utf-8') as fout:
                json.dump({"params": params, "features": feat_v7,
                           "metrics": metrics, "cv": cv,
                           "lgbm_cv": cv_lgbm}, fout, indent=2, ensure_ascii=False)
            print(f"  💾 v7 Ridge 参数: {v7_path}")
        except ImportError:
            print(f"  ⚠️  lightgbm 未安装, 仅训练 Ridge")

    # ── v6: GBDT (LightGBM) ──
    if args.stage in ("v6", "all"):
        print("\n🌲 v6: GBDT (LightGBM 试探)...")
        try:
            import lightgbm as lgb
            feat_all = BASE_FEATURES + SYLLABLE_FEATURES + PROSODY_FEATURES
            n = len(clean_samples)
            X = np.zeros((n, len(feat_all)))
            y = np.zeros(n)
            for i, s in enumerate(clean_samples):
                f = extract_all_features(s["text_zh"])
                for j, name in enumerate(feat_all):
                    X[i, j] = f.get(name, 0)
                y[i] = s["natural_ms"]
            # 5-fold CV by video
            by_video = {}
            for s in clean_samples:
                by_video.setdefault(s["video"], []).append(s)
            videos = sorted(by_video.keys())
            np.random.seed(42)
            np.random.shuffle(videos)
            fold_size = max(1, len(videos) // 5)
            cv_mae = []
            cv_r2 = []
            for k in range(5):
                test_vids = set(videos[k * fold_size: (k + 1) * fold_size])
                train_idx = [i for i, s in enumerate(clean_samples) if s["video"] not in test_vids]
                test_idx = [i for i, s in enumerate(clean_samples) if s["video"] in test_vids]
                if not train_idx or not test_idx:
                    continue
                model = lgb.LGBMRegressor(n_estimators=200, learning_rate=0.05,
                                          num_leaves=15, min_child_samples=20,
                                          verbosity=-1)
                model.fit(X[train_idx], y[train_idx])
                pred = model.predict(X[test_idx])
                m = compute_metrics(y[test_idx], pred)
                cv_mae.append(m["mae_ms"])
                cv_r2.append(m["r2"])
            cv = {"r2_mean": round(float(np.mean(cv_r2)), 4),
                  "mae_mean": round(float(np.mean(cv_mae)), 1),
                  "k_folds": len(cv_r2)}
            results["stages"]["v6"] = {"cv_5fold_lgbm": cv}
            print(f"  LightGBM CV: R²={cv['r2_mean']}, MAE={cv['mae_mean']}ms")
        except ImportError:
            print(f"  ⚠️  lightgbm 未安装, 跳过 (pip install lightgbm)")
            results["stages"]["v6"] = {"skipped": "lightgbm not installed"}

    # 保存结果
    if args.save_result:
        os.makedirs(os.path.dirname(args.save_result) or '.', exist_ok=True)
        with open(args.save_result, 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"\n  💾 已保存到 {args.save_result}")

    # 同时单独保存 v4 参数 (用于线性集成) 和 v6 LightGBM 模型 (用于神经集成)
    if "v4" in results.get("stages", {}):
        v4_path = "audit/duration_estimator_v4_params.json"
        os.makedirs(os.path.dirname(v4_path), exist_ok=True)
        v4 = results["stages"]["v4"]
        with open(v4_path, 'w', encoding='utf-8') as f:
            json.dump({"params": v4["params"], "features": v4["features"],
                       "metrics": v4["in_sample"], "cv": v4["cv_5fold"]},
                      f, indent=2, ensure_ascii=False)
        print(f"  💾 v4 参数: {v4_path}")

    if args.stage in ("v6", "all"):
        try:
            import lightgbm as lgb
            feat_all = BASE_FEATURES + SYLLABLE_FEATURES + PROSODY_FEATURES
            n = len(clean_samples)
            X = np.zeros((n, len(feat_all)))
            y = np.zeros(n)
            for i, s in enumerate(clean_samples):
                f = extract_all_features(s["text_zh"])
                for j, name in enumerate(feat_all):
                    X[i, j] = f.get(name, 0)
                y[i] = s["natural_ms"]
            model = lgb.LGBMRegressor(n_estimators=200, learning_rate=0.05,
                                      num_leaves=15, min_child_samples=20,
                                      verbosity=-1)
            model.fit(X, y)
            os.makedirs("models", exist_ok=True)
            model.booster_.save_model("models/duration_estimator_lgbm.txt")
            with open("models/duration_estimator_lgbm_features.json", 'w') as fout:
                json.dump(feat_all, fout)
            print(f"  💾 v6 LightGBM 模型: models/duration_estimator_lgbm.txt")
        except Exception as e:
            print(f"  ⚠️  v6 模型保存失败: {e}")

    if not args.save_result:
        print("\n" + "=" * 60)
        print(json.dumps(results, indent=2, ensure_ascii=False)[:2000])


if __name__ == "__main__":
    main()
