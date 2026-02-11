#!/usr/bin/env python3
"""NB Image Classifier Desktop App (Tkinter, Windows-friendly)."""

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
import subprocess
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageEnhance, ImageFile, ImageOps, ImageTk

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


@dataclass
class TypeTuning:
    threshold_mult: float = 1.0
    rescue_mult: float = 1.0
    margin_bias: float = 0.0


class FeatureEngine:
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
        gray = ImageOps.autocontrast(gray, cutoff=1)
        lut = [min(255, int((i / 255.0) ** (1.0 / max(self.gamma, 0.01)) * 255.0)) for i in range(256)]
        gray = gray.point(lut)
        gray = ImageEnhance.Contrast(gray).enhance(1.15)
        return gray

    def _avg_hash(self, img: Image.Image, size: int) -> List[float]:
        small = img.resize((size, size), Image.Resampling.BILINEAR)
        px = list(small.getdata())
        avg = sum(px) / max(1, len(px))
        return [1.0 if p >= avg else 0.0 for p in px]

    def _block_stats(self, img: Image.Image, blocks: int) -> List[float]:
        w, h = img.size
        bw = max(1, w // blocks)
        bh = max(1, h // blocks)
        out: List[float] = []
        for by in range(blocks):
            for bx in range(blocks):
                x0, y0 = bx * bw, by * bh
                x1 = w if bx == blocks - 1 else (bx + 1) * bw
                y1 = h if by == blocks - 1 else (by + 1) * bh
                vals = list(img.crop((x0, y0, x1, y1)).getdata())
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

        coarse = self._avg_hash(proc, 8)
        full_hash = self._avg_hash(proc, 16)
        full_stats = self._block_stats(proc, 4)

        w, h = proc.size
        cx0, cy0 = int(w * 0.2), int(h * 0.2)
        cx1, cy1 = int(w * 0.8), int(h * 0.8)
        center = proc.crop((cx0, cy0, max(cx0 + 1, cx1), max(cy0 + 1, cy1)))
        center_hash = self._avg_hash(center, 8)
        center_stats = self._block_stats(center, 3)

        fine = full_hash + full_stats + center_hash + center_stats
        return coarse, fine


def l1_distance(a: List[float], b: List[float]) -> float:
    n = min(len(a), len(b))
    if n == 0:
        return 9999.0
    return sum(abs(a[i] - b[i]) for i in range(n)) / n


def mean_std(vals: List[float]) -> Tuple[float, float]:
    if not vals:
        return 0.0, 1.0
    mu = sum(vals) / len(vals)
    var = sum((v - mu) ** 2 for v in vals) / len(vals)
    return mu, max(1e-6, math.sqrt(var))


class TemplateStore:
    def __init__(self):
        self.templates: List[TemplateRecord] = []
        self.cache: Dict[str, FeatureRecord] = {}

    def load_cache(self):
        if not CACHE_PATH.exists():
            return
        try:
            raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            self.cache = {k: FeatureRecord(**v) for k, v in raw.items()}
        except Exception:
            self.cache = {}

    def save_cache(self):
        CACHE_PATH.write_text(
            json.dumps({k: asdict(v) for k, v in self.cache.items()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def to_json(self) -> List[dict]:
        return [asdict(t) for t in self.templates]

    def from_json(self, data: List[dict]):
        self.templates = [TemplateRecord(**d) for d in data]

    def _derive_type_name(self, base_folder: Path, file_path: Path) -> str:
        rel = file_path.relative_to(base_folder)
        parts = [p.strip() for p in rel.parts[:-1] if p.strip()]
        return "_".join(parts) if parts else "default"

    def register_from_folder(self, base_folder: Path, preview_limit: Optional[int]) -> Tuple[int, List[TemplateRecord]]:
        count = 0
        out: List[TemplateRecord] = []
        for p in base_folder.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in IMAGE_EXTS:
                continue
            out.append(TemplateRecord(path=str(p), type_name=self._derive_type_name(base_folder, p)))
            count += 1
            if preview_limit is not None and count >= preview_limit:
                break
        return count, out

    def add_records(self, records: List[TemplateRecord]):
        existing = {(t.path, t.type_name) for t in self.templates}
        for r in records:
            k = (r.path, r.type_name)
            if k not in existing:
                self.templates.append(r)
                existing.add(k)

    def remove_indices(self, indices: List[int]):
        drop = set(indices)
        self.templates = [t for i, t in enumerate(self.templates) if i not in drop]

    def clear(self):
        self.templates.clear()

    def sample_per_type(self, max_per_type: int, seed: int = 17) -> List[TemplateRecord]:
        rng = random.Random(seed)
        by_type: Dict[str, List[TemplateRecord]] = defaultdict(list)
        for t in self.templates:
            by_type[t.type_name].append(t)
        sampled: List[TemplateRecord] = []
        for items in by_type.values():
            sampled.extend(items if len(items) <= max_per_type else rng.sample(items, max_per_type))
        return sampled

    def get_feature(self, path: Path, engine: FeatureEngine) -> Optional[FeatureRecord]:
        k = str(path)
        try:
            st = path.stat()
        except Exception:
            return None

        c = self.cache.get(k)
        if c and abs(c.mtime - st.st_mtime) < 1e-6 and c.size == st.st_size:
            return c

        feat = engine.extract(path)
        if feat is None:
            return None

        rec = FeatureRecord(path=k, mtime=st.st_mtime, size=st.st_size, coarse=feat[0], fine=feat[1])
        self.cache[k] = rec
        return rec


def normalize_type_name(name: str) -> str:
    return re.sub(r"(?:_|\s)?\d+$", "", name).strip("_ ") or name


def group_key_from_name(name: str) -> Optional[str]:
    if not NB_ANY_PATTERN.search(name):
        return None
    return NB_PATTERN.sub("_NB_", name)


def extract_view_tag(name: str) -> str:
    m = NB_PATTERN.search(name)
    if m:
        return f"0{m.group(1)}"
    m2 = OK_VIEW_PATTERN.search(name)
    if m2:
        return f"0{m2.group(1)}"
    return "NA"


def try_open_file(path: Path):
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", str(path)])
        elif shutil.which("open"):
            subprocess.Popen(["open", str(path)])
    except Exception:
        pass


class NBClassifierApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1240x790")

        self.store = TemplateStore()
        self.store.load_cache()

        self.cancel_event = threading.Event()
        self.running_thread: Optional[threading.Thread] = None
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.preview_limit = 1000
        self.thumbnail_cache: Optional[ImageTk.PhotoImage] = None

        self.scan_folder_var = tk.StringVar()
        self.template_root_var = tk.StringVar()
        self.include_ok_var = tk.BooleanVar(value=False)
        self.preprocess_var = tk.BooleanVar(value=True)
        self.max_templates_var = tk.IntVar(value=120)
        self.top_k_var = tk.IntVar(value=8)
        self.unmatched_margin_var = tk.DoubleVar(value=0.04)
        self.min_support_views_var = tk.IntVar(value=2)
        self.rescue_factor_var = tk.DoubleVar(value=1.12)

        self._build_ui()
        self._load_config()
        self._refresh_template_list()
        self._poll_log_queue()

    def _build_ui(self):
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=8, pady=6)

        ttk.Label(top, text="검사 폴더").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.scan_folder_var, width=92).grid(row=0, column=1, padx=4)
        ttk.Button(top, text="폴더 선택", command=self.choose_scan_folder).grid(row=0, column=2)

        ttk.Label(top, text="템플릿 루트 폴더").grid(row=1, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.template_root_var, width=92).grid(row=1, column=1, padx=4)
        ttk.Button(top, text="폴더 선택", command=self.choose_template_root).grid(row=1, column=2)

        opt = ttk.LabelFrame(self.root, text="옵션")
        opt.pack(fill="x", padx=8, pady=6)

        ttk.Checkbutton(opt, text="동일 그룹의 NB 없는(OK) 이미지도 포함", variable=self.include_ok_var).grid(row=0, column=0, sticky="w", padx=6)
        ttk.Checkbutton(opt, text="전처리(auto-contrast/gamma) 사용", variable=self.preprocess_var).grid(row=0, column=1, sticky="w", padx=6)

        ttk.Label(opt, text="유형당 최대 템플릿").grid(row=0, column=2, sticky="e")
        ttk.Spinbox(opt, from_=20, to=300, textvariable=self.max_templates_var, width=7).grid(row=0, column=3, padx=4)
        ttk.Label(opt, text="coarse 후보 Top-K").grid(row=0, column=4, sticky="e")
        ttk.Spinbox(opt, from_=2, to=30, textvariable=self.top_k_var, width=7).grid(row=0, column=5, padx=4)
        ttk.Label(opt, text="UNMATCHED 마진").grid(row=0, column=6, sticky="e")
        ttk.Spinbox(opt, from_=0.0, to=1.0, increment=0.01, textvariable=self.unmatched_margin_var, width=7).grid(row=0, column=7, padx=4)

        ttk.Label(opt, text="Rescue 최소뷰").grid(row=1, column=2, sticky="e")
        ttk.Spinbox(opt, from_=1, to=3, textvariable=self.min_support_views_var, width=7).grid(row=1, column=3, padx=4)
        ttk.Label(opt, text="Rescue 허용배수").grid(row=1, column=4, sticky="e")
        ttk.Spinbox(opt, from_=1.0, to=1.5, increment=0.01, textvariable=self.rescue_factor_var, width=7).grid(row=1, column=5, padx=4)

        ttk.Button(opt, text="유형 튜닝 CSV 열기/생성", command=self.open_or_create_tuning_csv).grid(row=1, column=6, columnspan=2, padx=6)

        main = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        main.pack(fill="both", expand=True, padx=8, pady=6)

        left = ttk.Frame(main)
        right = ttk.Frame(main)
        main.add(left, weight=2)
        main.add(right, weight=3)

        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=4)
        ttk.Button(btns, text="미리보기 등록(최대 1000)", command=self.preview_register).pack(side="left", padx=2)
        ttk.Button(btns, text="폴더 전체 자동 등록", command=self.full_register).pack(side="left", padx=2)
        ttk.Button(btns, text="선택 삭제", command=self.delete_selected_templates).pack(side="left", padx=2)
        ttk.Button(btns, text="전체 삭제", command=self.clear_templates).pack(side="left", padx=2)

        self.template_list = tk.Listbox(left, selectmode=tk.EXTENDED)
        self.template_list.pack(fill="both", expand=True)
        self.template_list.bind("<<ListboxSelect>>", self.show_selected_thumbnail)

        preview_box = ttk.LabelFrame(right, text="선택 템플릿 미리보기")
        preview_box.pack(fill="x", pady=4)
        self.preview_label = ttk.Label(preview_box, text="(선택 없음)")
        self.preview_label.pack(padx=8, pady=8)

        run = ttk.Frame(right)
        run.pack(fill="x", pady=4)
        ttk.Button(run, text="실행", command=self.start_run).pack(side="left", padx=2)
        ttk.Button(run, text="취소", command=self.cancel_run).pack(side="left", padx=2)

        self.progress = ttk.Progressbar(right, orient="horizontal", mode="determinate")
        self.progress.pack(fill="x", pady=4)

        log_box = ttk.LabelFrame(right, text="로그")
        log_box.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_box, height=20)
        self.log_text.pack(fill="both", expand=True)

    def _log(self, text: str):
        self.log_queue.put(f"[{time.strftime('%H:%M:%S')}] {text}")

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

    def _tuning_csv_path(self) -> Optional[Path]:
        root = self.template_root_var.get().strip()
        if not root:
            return None
        return Path(root) / "type_tuning.csv"

    def open_or_create_tuning_csv(self):
        p = self._tuning_csv_path()
        if p is None:
            messagebox.showwarning(APP_NAME, "먼저 템플릿 루트 폴더를 선택하세요.")
            return
        if not p.exists():
            with p.open("w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["type_name", "threshold_mult", "rescue_mult", "margin_bias"])
                w.writerow(["예시유형", 1.00, 1.00, 0.00])
        try_open_file(p)
        self._log(f"유형 튜닝 CSV 경로: {p}")

    def preview_register(self):
        folder = self.template_root_var.get().strip()
        if not folder:
            messagebox.showwarning(APP_NAME, "템플릿 루트 폴더를 먼저 선택하세요.")
            return
        cnt, recs = self.store.register_from_folder(Path(folder), preview_limit=self.preview_limit)
        self.store.add_records(recs)
        self._refresh_template_list()
        self._save_config()
        self._log(f"미리보기 등록 완료: {cnt}개 탐색/추가")

    def full_register(self):
        folder = self.template_root_var.get().strip()
        if not folder:
            messagebox.showwarning(APP_NAME, "템플릿 루트 폴더를 먼저 선택하세요.")
            return
        cnt, recs = self.store.register_from_folder(Path(folder), preview_limit=None)
        self.store.add_records(recs)
        self._refresh_template_list()
        self._save_config()
        self._log(f"전체 자동 등록 완료: {cnt}개 탐색/추가")

    def delete_selected_templates(self):
        sel = list(self.template_list.curselection())
        if not sel:
            return
        self.store.remove_indices(sel)
        self._refresh_template_list()
        self._save_config()

    def clear_templates(self):
        if messagebox.askyesno(APP_NAME, "기준 이미지를 모두 삭제할까요?"):
            self.store.clear()
            self._refresh_template_list()
            self._save_config()

    def show_selected_thumbnail(self, _evt=None):
        sel = self.template_list.curselection()
        if not sel:
            self.preview_label.configure(image="", text="(선택 없음)")
            return
        rec = self.store.templates[sel[0]]
        try:
            with Image.open(rec.path) as im:
                im = im.convert("RGB")
                im.thumbnail((240, 240), Image.Resampling.LANCZOS)
                tkimg = ImageTk.PhotoImage(im)
                self.thumbnail_cache = tkimg
                self.preview_label.configure(image=tkimg, text="")
        except Exception:
            self.preview_label.configure(image="", text="미리보기 실패")

    def cancel_run(self):
        if self.running_thread and self.running_thread.is_alive():
            self.cancel_event.set()
            self._log("취소 요청 수신. 안전 지점에서 중단됩니다.")

    def start_run(self):
        if self.running_thread and self.running_thread.is_alive():
            messagebox.showinfo(APP_NAME, "이미 실행 중입니다.")
            return
        if not self.store.templates:
            messagebox.showwarning(APP_NAME, "최소 1개 이상 템플릿을 등록하세요.")
            return
        scan = Path(self.scan_folder_var.get().strip())
        if not scan.exists():
            messagebox.showwarning(APP_NAME, "유효한 검사 폴더를 선택하세요.")
            return

        self.cancel_event.clear()
        self.progress["value"] = 0
        self.running_thread = threading.Thread(target=self._run_pipeline, daemon=True)
        self.running_thread.start()

    def _collect_groups(self, scan_dir: Path, include_ok: bool) -> Dict[str, Dict[str, List[Path]]]:
        groups: Dict[str, Dict[str, List[Path]]] = defaultdict(lambda: defaultdict(list))
        files = [p for p in scan_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS]

        for p in files:
            if not NB_ANY_PATTERN.search(p.name):
                continue
            gk = group_key_from_name(p.name)
            if gk:
                groups[gk][extract_view_tag(p.name)].append(p)

        if include_ok:
            keyed = set(groups.keys())
            for p in files:
                if NB_ANY_PATTERN.search(p.name):
                    continue
                pseudo = p.name.replace("_01_", "_01_NB_").replace("_02_", "_02_NB_").replace("_03_", "_03_NB_")
                ck = NB_PATTERN.sub("_NB_", pseudo)
                if ck in keyed:
                    groups[ck][extract_view_tag(p.name)].append(p)
        return groups

    def _load_type_tuning(self, template_root: Path) -> Dict[str, TypeTuning]:
        out: Dict[str, TypeTuning] = {}
        p = template_root / "type_tuning.csv"
        if not p.exists():
            return out
        try:
            with p.open("r", newline="", encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    name = (row.get("type_name") or "").strip()
                    if not name:
                        continue
                    out[name] = TypeTuning(
                        threshold_mult=float(row.get("threshold_mult", 1.0) or 1.0),
                        rescue_mult=float(row.get("rescue_mult", 1.0) or 1.0),
                        margin_bias=float(row.get("margin_bias", 0.0) or 0.0),
                    )
        except Exception as e:
            self._log(f"유형 튜닝 CSV 로드 실패: {e}")
        return out

    def _build_type_profiles(self, by_type: Dict[str, List[FeatureRecord]], tuning_map: Dict[str, TypeTuning]) -> Dict[str, dict]:
        profiles: Dict[str, dict] = {}
        for tname, feats in by_type.items():
            n = len(feats)
            cdim = len(feats[0].coarse)
            fdim = len(feats[0].fine)
            cmean = [0.0] * cdim
            fmean = [0.0] * fdim
            for f in feats:
                for i, v in enumerate(f.coarse):
                    cmean[i] += v
                for i, v in enumerate(f.fine):
                    fmean[i] += v
            cmean = [x / n for x in cmean]
            fmean = [x / n for x in fmean]
            dists = [l1_distance(f.fine, fmean) for f in feats]
            mu, sd = mean_std(dists)

            tune = tuning_map.get(tname, TypeTuning())
            base_threshold = mu + 2.2 * sd
            profiles[tname] = {
                "coarse_centroid": cmean,
                "features": feats,
                "fine_mu": mu,
                "fine_sd": sd,
                "accept_threshold": base_threshold * max(0.2, tune.threshold_mult),
                "rescue_threshold": base_threshold * max(0.2, tune.rescue_mult),
                "margin_bias": tune.margin_bias,
                "tuning": tune,
            }
        return profiles

    def _run_pipeline(self):
        started = time.time()
        try:
            self._save_config()
            scan_dir = Path(self.scan_folder_var.get().strip())
            template_root = Path(self.template_root_var.get().strip()) if self.template_root_var.get().strip() else scan_dir
            include_ok = self.include_ok_var.get()
            preprocess = self.preprocess_var.get()
            max_tpl = max(5, int(self.max_templates_var.get()))
            top_k = max(1, int(self.top_k_var.get()))
            margin = max(0.0, float(self.unmatched_margin_var.get()))
            min_support = max(1, int(self.min_support_views_var.get()))
            rescue_factor = max(1.0, float(self.rescue_factor_var.get()))

            tuning_map = self._load_type_tuning(template_root)
            if tuning_map:
                self._log(f"유형 튜닝 로드: {len(tuning_map)}개")

            engine = FeatureEngine(preprocess=preprocess)
            sampled = self.store.sample_per_type(max_tpl)
            self._log(f"템플릿 샘플 수: {len(sampled)} (유형당 최대 {max_tpl})")

            by_type_feats: Dict[str, List[FeatureRecord]] = defaultdict(list)
            self._log("템플릿 특징 계산/캐시 확인 중...")
            for idx, t in enumerate(sampled, 1):
                if self.cancel_event.is_set():
                    self._log("사용자 취소로 중단됨")
                    return
                rec = self.store.get_feature(Path(t.path), engine)
                if rec:
                    by_type_feats[t.type_name].append(rec)
                if idx % 50 == 0:
                    self.progress["value"] = min(10, 10 * idx / max(1, len(sampled)))
                    self.root.update_idletasks()

            if not by_type_feats:
                self._log("유효한 템플릿 특징이 없습니다.")
                return

            profiles = self._build_type_profiles(by_type_feats, tuning_map)
            self._log(f"유형 프로파일 생성 완료: {len(profiles)}개")

            groups = self._collect_groups(scan_dir, include_ok)
            items = list(groups.items())
            if not items:
                self._log("NB 그룹이 없습니다.")
                return
            self._log(f"NB 그룹 수집 완료: {len(items)}개")

            details_rows = []
            summary = Counter()
            moves: List[Tuple[str, List[Path], str]] = []

            max_workers = min(8, (os.cpu_count() or 4))

            def classify_one(group_key: str, views: Dict[str, List[Path]]) -> Optional[dict]:
                if self.cancel_event.is_set():
                    return None

                view_best_type: Dict[str, str] = {}
                agg: Dict[str, List[float]] = defaultdict(list)
                used_paths: List[Path] = []

                for _vtag, files in views.items():
                    if not files:
                        continue
                    qpath = files[0]
                    used_paths.append(qpath)
                    qf = self.store.get_feature(qpath, engine)
                    if not qf:
                        continue

                    coarse_rank = sorted((l1_distance(qf.coarse, p["coarse_centroid"]), t) for t, p in profiles.items())
                    candidate_types = [t for _, t in coarse_rank[:top_k]]

                    fine_scores = []
                    for t in candidate_types:
                        feats = profiles[t]["features"]
                        best = min(l1_distance(qf.fine, tf.fine) for tf in feats)
                        fine_scores.append((best, t))
                    if not fine_scores:
                        continue
                    fine_scores.sort()
                    best_score, best_type = fine_scores[0]
                    view_best_type[str(qpath)] = best_type
                    for score, t in fine_scores:
                        agg[t].append(score)

                if not used_paths or not agg:
                    return {
                        "group_key": group_key,
                        "result_type": "UNMATCHED",
                        "raw_result_type": "UNMATCHED",
                        "score": 9999.0,
                        "second_score": 9999.0,
                        "used_paths": used_paths,
                        "used_view_count": len(used_paths),
                        "decision_mode": "no_feature",
                        "unmatched_reason": "이미지 특징 추출 실패 또는 비교 후보 없음",
                        "best_type": "",
                        "best_threshold": "",
                        "margin_value": "",
                        "top_vote_type": "",
                        "top_vote_count": 0,
                        "type_tuning": "",
                    }

                ranked = sorted((sum(v) / len(v), t) for t, v in agg.items())
                best_score, best_type = ranked[0]
                second_score = ranked[1][0] if len(ranked) > 1 else 9999.0
                local_margin = second_score - best_score

                p = profiles[best_type]
                type_threshold = p["accept_threshold"]
                rescue_threshold = p["rescue_threshold"] * rescue_factor
                local_margin_need = margin + p["margin_bias"]
                strict_pass = local_margin >= local_margin_need and best_score <= type_threshold

                vote = Counter(view_best_type.values())
                top_vote_type, top_vote_count = ("", 0)
                if vote:
                    top_vote_type, top_vote_count = vote.most_common(1)[0]
                rescue_pass = top_vote_type == best_type and top_vote_count >= min_support and best_score <= rescue_threshold

                unmatched_reason = ""
                mode = ""
                if strict_pass:
                    result = best_type
                    mode = "strict"
                elif rescue_pass:
                    result = best_type
                    mode = "rescue_consensus"
                else:
                    result = "UNMATCHED"
                    mode = "unmatched"
                    reasons = []
                    if local_margin < local_margin_need:
                        reasons.append(f"마진부족({local_margin:.4f} < 필요 {local_margin_need:.4f})")
                    if best_score > type_threshold:
                        reasons.append(f"유형임계초과(score {best_score:.4f} > thr {type_threshold:.4f})")
                    if top_vote_type != best_type:
                        reasons.append("뷰합의불충분(최다득표 유형 불일치)")
                    elif top_vote_count < min_support:
                        reasons.append(f"뷰합의수 부족({top_vote_count} < {min_support})")
                    if best_score > rescue_threshold:
                        reasons.append(f"rescue임계초과(score {best_score:.4f} > rescue_thr {rescue_threshold:.4f})")
                    unmatched_reason = " | ".join(reasons) if reasons else "조건 미충족"

                tune = p["tuning"]
                type_tuning = f"thr*{tune.threshold_mult:.3f},rescue*{tune.rescue_mult:.3f},margin_bias={tune.margin_bias:.4f}"

                return {
                    "group_key": group_key,
                    "result_type": normalize_type_name(result) if result != "UNMATCHED" else "UNMATCHED",
                    "raw_result_type": result,
                    "score": best_score,
                    "second_score": second_score,
                    "used_paths": used_paths,
                    "used_view_count": len(used_paths),
                    "decision_mode": mode,
                    "unmatched_reason": unmatched_reason,
                    "best_type": best_type,
                    "best_threshold": round(type_threshold, 6),
                    "margin_value": round(local_margin, 6),
                    "top_vote_type": top_vote_type,
                    "top_vote_count": top_vote_count,
                    "type_tuning": type_tuning,
                }

            self._log(f"그룹 분류 시작... worker={max_workers}")
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                futs = {ex.submit(classify_one, gk, vw): gk for gk, vw in items}
                for done, fut in enumerate(as_completed(futs), 1):
                    if self.cancel_event.is_set():
                        self._log("취소 요청으로 결과 수집 중단")
                        break
                    res = fut.result()
                    if not res:
                        continue
                    summary[res["result_type"]] += 1
                    details_rows.append(
                        {
                            "group_key": res["group_key"],
                            "result_type": res["result_type"],
                            "raw_result_type": res["raw_result_type"],
                            "distance_mean": round(res["score"], 6),
                            "distance_second": round(res["second_score"], 6),
                            "used_view_count": res["used_view_count"],
                            "decision_mode": res["decision_mode"],
                            "unmatched_reason": res["unmatched_reason"],
                            "best_type": res["best_type"],
                            "best_threshold": res["best_threshold"],
                            "margin_value": res["margin_value"],
                            "top_vote_type": res["top_vote_type"],
                            "top_vote_count": res["top_vote_count"],
                            "type_tuning": res["type_tuning"],
                            "used_paths": " | ".join(str(p) for p in res["used_paths"]),
                        }
                    )
                    moves.append((res["result_type"], res["used_paths"], res["group_key"]))
                    self.progress["value"] = 10 + (90 * done / max(1, len(items)))
                    if done % 20 == 0:
                        self.root.update_idletasks()

            if self.cancel_event.is_set():
                self._log("취소 완료")
                return

            out_root = scan_dir / datetime.now().strftime("%Y%m%d")
            out_root.mkdir(parents=True, exist_ok=True)
            details_csv = out_root / "details.csv"
            summary_csv = out_root / "summary.csv"

            with details_csv.open("w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(
                    f,
                    fieldnames=[
                        "group_key",
                        "result_type",
                        "raw_result_type",
                        "distance_mean",
                        "distance_second",
                        "used_view_count",
                        "decision_mode",
                        "unmatched_reason",
                        "best_type",
                        "best_threshold",
                        "margin_value",
                        "top_vote_type",
                        "top_vote_count",
                        "type_tuning",
                        "used_paths",
                    ],
                )
                w.writeheader()
                w.writerows(details_rows)

            with summary_csv.open("w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["result_type", "count"])
                for t, c in sorted(summary.items(), key=lambda x: x[1], reverse=True):
                    w.writerow([t, c])

            moved = 0
            for label, paths, _ in moves:
                td = out_root / label
                td.mkdir(parents=True, exist_ok=True)
                for p in paths:
                    if not p.exists():
                        continue
                    dst = td / p.name
                    if dst.exists():
                        dst = td / f"{p.stem}_{hashlib.sha1(str(p).encode('utf-8')).hexdigest()[:8]}{p.suffix}"
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
            "min_support_views": self.min_support_views_var.get(),
            "rescue_factor": self.rescue_factor_var.get(),
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
            self.min_support_views_var.set(data.get("min_support_views", 2))
            self.rescue_factor_var.set(data.get("rescue_factor", 1.12))
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
