#!/usr/bin/env python3
"""
NB Image Classifier Desktop App (Tkinter)
MVP + extensible architecture
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import queue
import random
import re
import shutil
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageOps, ImageEnhance, ImageTk, ImageFile

# Large scanner images can be huge. We avoid warning spam and safely downscale.
Image.MAX_IMAGE_PIXELS = None
ImageFile.LOAD_TRUNCATED_IMAGES = True

APP_NAME = "NB Image Classifier"
CONFIG_PATH = Path.home() / ".nb_image_classifier_config.json"
CACHE_PATH = Path.home() / ".nb_image_classifier_feature_cache.json"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
NB_PATTERN = re.compile(r"_0([123])_NB_", re.IGNORECASE)
NB_ANY_PATTERN = re.compile(r"NB", re.IGNORECASE)
OK_VIEW_PATTERN = re.compile(r"_0([123])_", re.IGNORECASE)


@dataclass
class TemplateRecord:
    path: str
    type_name: str


@dataclass
class FeatureRecord:
    path: str
    mtime: float
    size: int
    coarse: List[float]
    fine: List[float]


class FeatureEngine:
    """Fast + reasonably robust image feature extraction for dark images."""

    def __init__(self, max_dim: int = 512, preprocess: bool = True, gamma: float = 1.15):
        self.max_dim = max_dim
        self.preprocess = preprocess
        self.gamma = gamma

    def _safe_open(self, path: Path) -> Optional[Image.Image]:
        try:
            with Image.open(path) as im:
                im = im.convert("RGB")
                im.thumbnail((self.max_dim, self.max_dim), Image.Resampling.LANCZOS)
                return im.copy()
        except Exception:
            return None

    def _preprocess(self, img: Image.Image) -> Image.Image:
        if not self.preprocess:
            return img

        gray = ImageOps.grayscale(img)
        # Auto contrast + mild gamma boost improves dark scanner frames.
        gray = ImageOps.autocontrast(gray, cutoff=1)
        lut = [min(255, int((i / 255.0) ** (1.0 / max(self.gamma, 0.01)) * 255.0)) for i in range(256)]
        gray = gray.point(lut)
        gray = ImageEnhance.Contrast(gray).enhance(1.15)
        return gray

    def _avg_hash(self, img: Image.Image, size: int = 16) -> List[float]:
        small = img.resize((size, size), Image.Resampling.BILINEAR)
        pixels = list(small.getdata())
        avg = sum(pixels) / len(pixels)
        return [1.0 if p >= avg else 0.0 for p in pixels]

    def _block_stats(self, img: Image.Image, blocks: int = 4) -> List[float]:
        w, h = img.size
        bw = max(1, w // blocks)
        bh = max(1, h // blocks)
        out: List[float] = []
        for by in range(blocks):
            for bx in range(blocks):
                x0, y0 = bx * bw, by * bh
                x1 = w if bx == blocks - 1 else (bx + 1) * bw
                y1 = h if by == blocks - 1 else (by + 1) * bh
                crop = img.crop((x0, y0, x1, y1))
                vals = list(crop.getdata())
                if not vals:
                    out.extend([0.0, 0.0])
                    continue
                mu = sum(vals) / len(vals)
                var = sum((v - mu) ** 2 for v in vals) / len(vals)
                out.extend([mu / 255.0, math.sqrt(var) / 255.0])
        return out

    def extract(self, path: Path) -> Optional[Tuple[List[float], List[float]]]:
        img = self._safe_open(path)
        if img is None:
            return None
        proc = self._preprocess(img)
        if proc.mode != "L":
            proc = ImageOps.grayscale(proc)

        coarse = self._avg_hash(proc, size=8)  # 64-bit style hash

        # Fine feature: central and full-frame descriptors combined.
        full_hash = self._avg_hash(proc, size=16)
        full_stats = self._block_stats(proc, blocks=4)
        w, h = proc.size
        cx0, cy0 = int(w * 0.2), int(h * 0.2)
        cx1, cy1 = int(w * 0.8), int(h * 0.8)
        center = proc.crop((cx0, cy0, max(cx0 + 1, cx1), max(cy0 + 1, cy1)))
        center_hash = self._avg_hash(center, size=8)
        center_stats = self._block_stats(center, blocks=3)
        fine = full_hash + full_stats + center_hash + center_stats
        return coarse, fine


def l1_distance(a: List[float], b: List[float]) -> float:
    n = min(len(a), len(b))
    if n == 0:
        return 9999.0
    return sum(abs(a[i] - b[i]) for i in range(n)) / n


class TemplateStore:
    def __init__(self):
        self.templates: List[TemplateRecord] = []
        self.cache: Dict[str, FeatureRecord] = {}

    def load_cache(self):
        if CACHE_PATH.exists():
            try:
                data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
                for p, v in data.items():
                    self.cache[p] = FeatureRecord(**v)
            except Exception:
                self.cache = {}

    def save_cache(self):
        raw = {k: asdict(v) for k, v in self.cache.items()}
        CACHE_PATH.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

    def to_json(self) -> List[dict]:
        return [asdict(t) for t in self.templates]

    def from_json(self, data: List[dict]):
        self.templates = [TemplateRecord(**d) for d in data]

    def _derive_type_name(self, base_folder: Path, file_path: Path) -> str:
        rel = file_path.relative_to(base_folder)
        parts = list(rel.parts[:-1])
        if not parts:
            return "default"
        return "_".join(p.strip() for p in parts if p.strip())

    def register_from_folder(self, base_folder: Path, preview_limit: Optional[int] = None) -> Tuple[int, List[TemplateRecord]]:
        records: List[TemplateRecord] = []
        count = 0
        for path in base_folder.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in IMAGE_EXTS:
                continue
            type_name = self._derive_type_name(base_folder, path)
            records.append(TemplateRecord(path=str(path), type_name=type_name))
            count += 1
            if preview_limit is not None and count >= preview_limit:
                break
        return count, records

    def add_records(self, records: List[TemplateRecord]):
        existing = {(t.path, t.type_name) for t in self.templates}
        for r in records:
            key = (r.path, r.type_name)
            if key not in existing:
                self.templates.append(r)
                existing.add(key)

    def remove_indices(self, indices: List[int]):
        remove_set = set(indices)
        self.templates = [t for i, t in enumerate(self.templates) if i not in remove_set]

    def clear(self):
        self.templates.clear()

    def sample_per_type(self, max_per_type: int, seed: int = 17) -> List[TemplateRecord]:
        rng = random.Random(seed)
        by_type: Dict[str, List[TemplateRecord]] = defaultdict(list)
        for t in self.templates:
            by_type[t.type_name].append(t)
        sampled: List[TemplateRecord] = []
        for _, items in by_type.items():
            if len(items) <= max_per_type:
                sampled.extend(items)
            else:
                sampled.extend(rng.sample(items, max_per_type))
        return sampled

    def get_feature(self, path: Path, engine: FeatureEngine) -> Optional[FeatureRecord]:
        k = str(path)
        try:
            st = path.stat()
        except Exception:
            return None

        cached = self.cache.get(k)
        if cached and abs(cached.mtime - st.st_mtime) < 1e-6 and cached.size == st.st_size:
            return cached

        feat = engine.extract(path)
        if feat is None:
            return None
        rec = FeatureRecord(path=k, mtime=st.st_mtime, size=st.st_size, coarse=feat[0], fine=feat[1])
        self.cache[k] = rec
        return rec


def normalize_type_name(name: str) -> str:
    return re.sub(r"(?:_|\s)?\d+$", "", name).strip("_ ") or name


def group_key_from_name(filename: str) -> Optional[str]:
    if not NB_ANY_PATTERN.search(filename):
        return None
    return NB_PATTERN.sub("_NB_", filename)


def extract_view_tag(filename: str) -> str:
    m = NB_PATTERN.search(filename)
    if m:
        return f"0{m.group(1)}"
    m2 = OK_VIEW_PATTERN.search(filename)
    if m2:
        return f"0{m2.group(1)}"
    return "NA"


class NBClassifierApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1200x760")

        self.store = TemplateStore()
        self.store.load_cache()

        self.cancel_event = threading.Event()
        self.running_thread: Optional[threading.Thread] = None
        self.log_queue: "queue.Queue[str]" = queue.Queue()

        self.scan_folder_var = tk.StringVar()
        self.template_root_var = tk.StringVar()
        self.include_ok_var = tk.BooleanVar(value=False)
        self.preprocess_var = tk.BooleanVar(value=True)
        self.max_templates_var = tk.IntVar(value=120)
        self.top_k_var = tk.IntVar(value=8)
        self.unmatched_margin_var = tk.DoubleVar(value=0.04)

        self.preview_limit = 1000
        self.thumbnail_cache: Optional[ImageTk.PhotoImage] = None

        self._build_ui()
        self._load_config()
        self._refresh_template_list()
        self._poll_log_queue()

    def _build_ui(self):
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=8, pady=6)

        ttk.Label(top, text="검사 폴더").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.scan_folder_var, width=90).grid(row=0, column=1, padx=4)
        ttk.Button(top, text="폴더 선택", command=self.choose_scan_folder).grid(row=0, column=2)

        ttk.Label(top, text="템플릿 루트 폴더").grid(row=1, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.template_root_var, width=90).grid(row=1, column=1, padx=4)
        ttk.Button(top, text="폴더 선택", command=self.choose_template_root).grid(row=1, column=2)

        options = ttk.LabelFrame(self.root, text="옵션")
        options.pack(fill="x", padx=8, pady=6)
        ttk.Checkbutton(options, text="동일 그룹의 NB 없는(OK) 이미지도 포함", variable=self.include_ok_var).grid(row=0, column=0, sticky="w", padx=6)
        ttk.Checkbutton(options, text="전처리(auto-contrast/gamma) 사용", variable=self.preprocess_var).grid(row=0, column=1, sticky="w", padx=6)

        ttk.Label(options, text="유형당 최대 템플릿").grid(row=0, column=2, sticky="e")
        ttk.Spinbox(options, from_=20, to=300, textvariable=self.max_templates_var, width=7).grid(row=0, column=3, padx=4)
        ttk.Label(options, text="coarse 후보 Top-K").grid(row=0, column=4, sticky="e")
        ttk.Spinbox(options, from_=2, to=30, textvariable=self.top_k_var, width=7).grid(row=0, column=5, padx=4)
        ttk.Label(options, text="UNMATCHED 마진").grid(row=0, column=6, sticky="e")
        ttk.Spinbox(options, from_=0.0, to=1.0, increment=0.01, textvariable=self.unmatched_margin_var, width=7).grid(row=0, column=7, padx=4)

        main = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        main.pack(fill="both", expand=True, padx=8, pady=6)

        left = ttk.Frame(main)
        right = ttk.Frame(main)
        main.add(left, weight=2)
        main.add(right, weight=3)

        # Left: template list and controls
        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=4)
        ttk.Button(btns, text="미리보기 등록(최대 1000)", command=self.preview_register).pack(side="left", padx=2)
        ttk.Button(btns, text="폴더 전체 자동 등록", command=self.full_register).pack(side="left", padx=2)
        ttk.Button(btns, text="선택 삭제", command=self.delete_selected_templates).pack(side="left", padx=2)
        ttk.Button(btns, text="전체 삭제", command=self.clear_templates).pack(side="left", padx=2)

        self.template_list = tk.Listbox(left, selectmode=tk.EXTENDED)
        self.template_list.pack(fill="both", expand=True)
        self.template_list.bind("<<ListboxSelect>>", self.show_selected_thumbnail)

        # Right: preview + run controls + logs
        preview_frame = ttk.LabelFrame(right, text="선택 템플릿 미리보기")
        preview_frame.pack(fill="x", pady=4)
        self.preview_label = ttk.Label(preview_frame, text="(선택 없음)")
        self.preview_label.pack(padx=8, pady=8)

        run_frame = ttk.Frame(right)
        run_frame.pack(fill="x", pady=4)
        ttk.Button(run_frame, text="실행", command=self.start_run).pack(side="left", padx=2)
        ttk.Button(run_frame, text="취소", command=self.cancel_run).pack(side="left", padx=2)

        self.progress = ttk.Progressbar(right, orient="horizontal", mode="determinate")
        self.progress.pack(fill="x", pady=6)

        log_frame = ttk.LabelFrame(right, text="로그")
        log_frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_frame, height=18)
        self.log_text.pack(fill="both", expand=True)

    def _log(self, msg: str):
        self.log_queue.put(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def _poll_log_queue(self):
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_text.insert("end", line + "\n")
            self.log_text.see("end")
        self.root.after(150, self._poll_log_queue)

    def _refresh_template_list(self):
        self.template_list.delete(0, "end")
        for t in self.store.templates:
            self.template_list.insert("end", f"[{t.type_name}] {t.path}")

    def choose_scan_folder(self):
        p = filedialog.askdirectory(title="검사 폴더 선택")
        if p:
            self.scan_folder_var.set(p)

    def choose_template_root(self):
        p = filedialog.askdirectory(title="템플릿 루트 폴더 선택")
        if p:
            self.template_root_var.set(p)

    def preview_register(self):
        folder = self.template_root_var.get().strip()
        if not folder:
            messagebox.showwarning(APP_NAME, "템플릿 루트 폴더를 먼저 선택하세요.")
            return
        count, records = self.store.register_from_folder(Path(folder), preview_limit=self.preview_limit)
        self.store.add_records(records)
        self._refresh_template_list()
        self._save_config()
        self._log(f"미리보기 등록 완료: {count}개 탐색/추가")

    def full_register(self):
        folder = self.template_root_var.get().strip()
        if not folder:
            messagebox.showwarning(APP_NAME, "템플릿 루트 폴더를 먼저 선택하세요.")
            return
        count, records = self.store.register_from_folder(Path(folder), preview_limit=None)
        self.store.add_records(records)
        self._refresh_template_list()
        self._save_config()
        self._log(f"전체 자동 등록 완료: {count}개 탐색/추가")

    def delete_selected_templates(self):
        indices = list(self.template_list.curselection())
        if not indices:
            return
        self.store.remove_indices(indices)
        self._refresh_template_list()
        self._save_config()

    def clear_templates(self):
        if messagebox.askyesno(APP_NAME, "기준 이미지를 모두 삭제할까요?"):
            self.store.clear()
            self._refresh_template_list()
            self._save_config()

    def show_selected_thumbnail(self, _evt=None):
        indices = self.template_list.curselection()
        if not indices:
            self.preview_label.configure(image="", text="(선택 없음)")
            return
        rec = self.store.templates[indices[0]]
        p = Path(rec.path)
        try:
            with Image.open(p) as im:
                im = im.convert("RGB")
                im.thumbnail((240, 240), Image.Resampling.LANCZOS)
                tk_img = ImageTk.PhotoImage(im)
                self.thumbnail_cache = tk_img
                self.preview_label.configure(image=tk_img, text="")
        except Exception:
            self.preview_label.configure(image="", text="미리보기 실패")

    def cancel_run(self):
        if self.running_thread and self.running_thread.is_alive():
            self.cancel_event.set()
            self._log("취소 요청 수신. 현재 작업이 안전 지점에 도달하면 중단합니다.")

    def start_run(self):
        if self.running_thread and self.running_thread.is_alive():
            messagebox.showinfo(APP_NAME, "이미 실행 중입니다.")
            return

        scan = self.scan_folder_var.get().strip()
        if not scan or not Path(scan).exists():
            messagebox.showwarning(APP_NAME, "유효한 검사 폴더를 선택하세요.")
            return
        if not self.store.templates:
            messagebox.showwarning(APP_NAME, "최소 1개 이상 템플릿을 등록하세요.")
            return

        self.cancel_event.clear()
        self.progress["value"] = 0
        self.running_thread = threading.Thread(target=self._run_pipeline, daemon=True)
        self.running_thread.start()

    def _collect_groups(self, scan_dir: Path, include_ok: bool) -> Dict[str, Dict[str, List[Path]]]:
        groups: Dict[str, Dict[str, List[Path]]] = defaultdict(lambda: defaultdict(list))

        all_files = [p for p in scan_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
        nb_files = [p for p in all_files if NB_ANY_PATTERN.search(p.name)]

        for p in nb_files:
            gk = group_key_from_name(p.name)
            if gk:
                groups[gk][extract_view_tag(p.name)].append(p)

        if include_ok:
            # Bring matching non-NB view images into already-created NB groups.
            keyed_nb = set(groups.keys())
            for p in all_files:
                if NB_ANY_PATTERN.search(p.name):
                    continue
                candidate_keys = [
                    NB_PATTERN.sub("_NB_", p.name.replace("_01_", "_01_NB_").replace("_02_", "_02_NB_").replace("_03_", "_03_NB_")),
                ]
                for ck in candidate_keys:
                    if ck in keyed_nb:
                        groups[ck][extract_view_tag(p.name)].append(p)
                        break
        return groups

    def _run_pipeline(self):
        started = time.time()
        try:
            self._save_config()
            scan_dir = Path(self.scan_folder_var.get().strip())
            include_ok = self.include_ok_var.get()
            preprocess = self.preprocess_var.get()
            max_tpl = max(5, int(self.max_templates_var.get()))
            top_k = max(1, int(self.top_k_var.get()))
            unmatched_margin = max(0.0, float(self.unmatched_margin_var.get()))

            engine = FeatureEngine(preprocess=preprocess)
            templates = self.store.sample_per_type(max_tpl)
            self._log(f"템플릿 샘플 수: {len(templates)} (유형당 최대 {max_tpl})")

            # Build typed features with cache
            by_type_features: Dict[str, List[FeatureRecord]] = defaultdict(list)
            self._log("템플릿 특징 계산/캐시 확인 중...")
            for idx, t in enumerate(templates, 1):
                if self.cancel_event.is_set():
                    self._log("사용자 취소로 중단됨")
                    return
                rec = self.store.get_feature(Path(t.path), engine)
                if rec:
                    by_type_features[t.type_name].append(rec)
                if idx % 50 == 0:
                    self.progress["value"] = min(10, 10 * idx / max(1, len(templates)))
                    self.root.update_idletasks()

            if not by_type_features:
                self._log("유효한 템플릿 특징이 없습니다. 작업 중단")
                return

            groups = self._collect_groups(scan_dir, include_ok)
            group_items = list(groups.items())
            total = len(group_items)
            self._log(f"NB 그룹 수집 완료: {total}개 그룹")
            if total == 0:
                return

            details_rows = []
            summary_counter: Counter[str] = Counter()
            results_for_move: List[Tuple[str, List[Path], str]] = []

            # Precompute type centroids for coarse stage.
            type_centroids: Dict[str, List[float]] = {}
            for tname, feats in by_type_features.items():
                n = len(feats)
                dim = len(feats[0].coarse)
                mean = [0.0] * dim
                for f in feats:
                    for i, v in enumerate(f.coarse):
                        mean[i] += v
                type_centroids[tname] = [v / n for v in mean]

            max_workers = min(8, (os.cpu_count() or 4))
            self._log(f"그룹 분류 시작... worker={max_workers}")

            def classify_one(gkey: str, views: Dict[str, List[Path]]):
                if self.cancel_event.is_set():
                    return None

                view_best: Dict[str, Dict[str, float]] = {}
                used_paths: List[Path] = []

                for vtag, files in views.items():
                    if not files:
                        continue
                    path = files[0]
                    used_paths.append(path)
                    qf = self.store.get_feature(path, engine)
                    if not qf:
                        continue

                    coarse_scores = []
                    for tname, centroid in type_centroids.items():
                        d = l1_distance(qf.coarse, centroid)
                        coarse_scores.append((d, tname))
                    coarse_scores.sort(key=lambda x: x[0])
                    candidate_types = [t for _, t in coarse_scores[:top_k]]

                    fine_by_type: Dict[str, float] = {}
                    for tname in candidate_types:
                        feats = by_type_features.get(tname, [])
                        if not feats:
                            continue
                        best = min(l1_distance(qf.fine, tf.fine) for tf in feats)
                        fine_by_type[tname] = best
                    if fine_by_type:
                        view_best[vtag] = fine_by_type

                if not view_best:
                    return {
                        "group_key": gkey,
                        "result_type": "UNMATCHED",
                        "score": 9999.0,
                        "used_paths": used_paths,
                        "views": len(used_paths),
                        "second_score": 9999.0,
                    }

                agg: Dict[str, List[float]] = defaultdict(list)
                for _, m in view_best.items():
                    for tname, score in m.items():
                        agg[tname].append(score)

                mean_scores = []
                for tname, vals in agg.items():
                    mean_scores.append((sum(vals) / len(vals), tname))
                mean_scores.sort(key=lambda x: x[0])

                best_score, best_type = mean_scores[0]
                second_score = mean_scores[1][0] if len(mean_scores) > 1 else 9999.0
                result_type = "UNMATCHED" if (second_score - best_score) < unmatched_margin else best_type
                return {
                    "group_key": gkey,
                    "result_type": result_type,
                    "score": best_score,
                    "used_paths": used_paths,
                    "views": len(used_paths),
                    "second_score": second_score,
                }

            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                future_map = {ex.submit(classify_one, gk, vw): (gk, vw) for gk, vw in group_items}
                done = 0
                for fut in as_completed(future_map):
                    if self.cancel_event.is_set():
                        self._log("취소 요청으로 결과 수집 중단")
                        break
                    res = fut.result()
                    if not res:
                        continue
                    done += 1
                    label_raw = res["result_type"]
                    label_norm = normalize_type_name(label_raw) if label_raw != "UNMATCHED" else "UNMATCHED"
                    summary_counter[label_norm] += 1
                    used_paths = res["used_paths"]
                    details_rows.append({
                        "group_key": res["group_key"],
                        "result_type": label_norm,
                        "raw_result_type": label_raw,
                        "distance_mean": round(res["score"], 6),
                        "distance_second": round(res["second_score"], 6),
                        "used_view_count": res["views"],
                        "used_paths": " | ".join(str(p) for p in used_paths),
                    })
                    results_for_move.append((label_norm, used_paths, res["group_key"]))
                    self.progress["value"] = 10 + (90 * done / max(1, total))
                    if done % 20 == 0:
                        self.root.update_idletasks()

            if self.cancel_event.is_set():
                self._log("취소 완료")
                return

            run_date = datetime.now().strftime("%Y%m%d")
            out_root = scan_dir / run_date
            out_root.mkdir(parents=True, exist_ok=True)
            details_csv = out_root / "details.csv"
            summary_csv = out_root / "summary.csv"

            with details_csv.open("w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=[
                        "group_key",
                        "result_type",
                        "raw_result_type",
                        "distance_mean",
                        "distance_second",
                        "used_view_count",
                        "used_paths",
                    ],
                )
                writer.writeheader()
                writer.writerows(details_rows)

            with summary_csv.open("w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["result_type", "count"])
                for tname, cnt in sorted(summary_counter.items(), key=lambda x: x[1], reverse=True):
                    writer.writerow([tname, cnt])

            moved = 0
            for label, paths, _gk in results_for_move:
                target_dir = out_root / label
                target_dir.mkdir(parents=True, exist_ok=True)
                for p in paths:
                    if not p.exists():
                        continue
                    dst = target_dir / p.name
                    if dst.exists():
                        stem = p.stem
                        suffix = p.suffix
                        digest = hashlib.sha1(str(p).encode("utf-8")).hexdigest()[:8]
                        dst = target_dir / f"{stem}_{digest}{suffix}"
                    try:
                        shutil.move(str(p), str(dst))
                        moved += 1
                    except Exception:
                        pass

            self.store.save_cache()
            self._save_config()

            elapsed = time.time() - started
            self._log(f"완료: 그룹 {len(details_rows)}건, 파일 이동 {moved}건, 소요 {elapsed:.1f}s")
            self._log(f"결과: {details_csv}")
            self._log(f"결과: {summary_csv}")
            messagebox.showinfo(APP_NAME, f"분류 완료\n\n{details_csv}\n{summary_csv}")
        except Exception as e:
            self._log(f"오류: {e}")
            messagebox.showerror(APP_NAME, str(e))

    def _save_config(self):
        data = {
            "scan_folder": self.scan_folder_var.get(),
            "template_root": self.template_root_var.get(),
            "include_ok": self.include_ok_var.get(),
            "preprocess": self.preprocess_var.get(),
            "max_templates": self.max_templates_var.get(),
            "top_k": self.top_k_var.get(),
            "unmatched_margin": self.unmatched_margin_var.get(),
            "templates": self.store.to_json(),
        }
        CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_config(self):
        if not CONFIG_PATH.exists():
            return
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            self.scan_folder_var.set(data.get("scan_folder", ""))
            self.template_root_var.set(data.get("template_root", ""))
            self.include_ok_var.set(data.get("include_ok", False))
            self.preprocess_var.set(data.get("preprocess", True))
            self.max_templates_var.set(data.get("max_templates", 120))
            self.top_k_var.set(data.get("top_k", 8))
            self.unmatched_margin_var.set(data.get("unmatched_margin", 0.04))
            self.store.from_json(data.get("templates", []))
        except Exception:
            pass


def main():
    root = tk.Tk()
    app = NBClassifierApp(root)
    root.protocol("WM_DELETE_WINDOW", lambda: (app._save_config(), app.store.save_cache(), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    main()
