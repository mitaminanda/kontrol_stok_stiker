"""
Pencocokan kemiripan gambar stiker.

Algoritma utama: SIFT (Scale-Invariant Feature Transform) + FLANN/BFMatcher
+ RANSAC homography. Pendekatan ini tahan terhadap perbedaan ukuran, sudut
pengambilan foto, pencahayaan, kompresi, dan latar belakang yang ramai.

Jika OpenCV tidak tersedia, modul otomatis memakai fallback berbasis
perceptual hash (pHash + dHash + grid warna + histogram) memakai Pillow/NumPy.

Semua proses berjalan lokal, tanpa layanan eksternal.
"""

import io
import math
import os
import pickle
import threading

import numpy as np
from PIL import Image, ImageOps

try:
    import cv2
except Exception:  # pragma: no cover - OpenCV opsional
    cv2 = None


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(BASE_DIR, '.cache_gambar_stiker.pkl')
VERSI_CACHE = 3

# Batas ukuran gambar yang diproses (px sisi terpanjang) agar cepat
MAKS_SISI = 900
JUMLAH_FITUR = 500
RASIO_LOWE = 0.72

# Ambang skor kemiripan untuk dianggap cocok (0.0 - 1.0)
AMBANG_COCOK = 0.35


# ==========================================================
# CACHE DESKRIPTOR GAMBAR DATABASE
# ==========================================================
_cache = {}
_cache_siap = False
_cache_kotor = False
_cache_lock = threading.RLock()


def _muat_cache():
    global _cache, _cache_siap
    with _cache_lock:
        if _cache_siap:
            return
        _cache_siap = True
        try:
            with open(CACHE_FILE, 'rb') as f:
                data = pickle.load(f)
            if isinstance(data, dict) and data.get('versi') == VERSI_CACHE:
                _cache = data.get('items', {})
        except Exception:
            _cache = {}


def _simpan_cache():
    global _cache_kotor
    with _cache_lock:
        if not _cache_kotor:
            return
        data = {'versi': VERSI_CACHE, 'items': _cache}
        tmp = f"{CACHE_FILE}.tmp"
        try:
            with open(tmp, 'wb') as f:
                pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, CACHE_FILE)
            _cache_kotor = False
        except Exception:
            pass


# ==========================================================
# UTIL GAMBAR
# ==========================================================
def siapkan_gambar(img):
    """Normalisasi mode warna & orientasi EXIF menjadi RGB."""
    img = ImageOps.exif_transpose(img)
    if img.mode in ('RGBA', 'LA', 'P'):
        img = img.convert('RGBA')
        latar = Image.new('RGB', img.size, (255, 255, 255))
        latar.paste(img, mask=img.split()[-1])
        return latar
    return img.convert('RGB')


def buka_gambar_dari_bytes(data):
    """Buka gambar dari bytes; mengembalikan PIL Image RGB atau None."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        return siapkan_gambar(img)
    except Exception:
        return None


def _ke_abu_terbatas(img):
    abu = cv2.cvtColor(np.asarray(img, dtype=np.uint8), cv2.COLOR_RGB2GRAY)
    h, w = abu.shape
    skala = min(1.0, MAKS_SISI / float(max(h, w)))
    if skala < 1.0:
        abu = cv2.resize(abu, None, fx=skala, fy=skala, interpolation=cv2.INTER_AREA)
    return abu


# ==========================================================
# FITUR SIFT
# ==========================================================
def _buat_sift():
    if cv2 is None:
        return None
    try:
        return cv2.SIFT_create(nfeatures=JUMLAH_FITUR)
    except Exception:
        return None


_sift = _buat_sift()


def _ekstrak_sift(img):
    """Kembalikan (titik_kunci Nx2, deskriptor Nx128) dari PIL Image."""
    if _sift is None:
        return None, None
    try:
        abu = _ke_abu_terbatas(img)
        kp, des = _sift.detectAndCompute(abu, None)
        if not kp or des is None:
            return np.empty((0, 2), np.float32), None
        pts = np.asarray([k.pt for k in kp], dtype=np.float32)
        return pts, np.asarray(des, dtype=np.float32)
    except Exception:
        return None, None


def _fitur_kandidat(path):
    """Ambil (pts, des) gambar kandidat dari cache, atau hitung baru."""
    global _cache_kotor
    _muat_cache()
    try:
        st = os.stat(path)
    except OSError:
        return None, None

    kunci = os.path.abspath(path)

    with _cache_lock:
        entri = _cache.get(kunci)
        if entri and entri.get('mtime') == st.st_mtime_ns and entri.get('size') == st.st_size:
            return entri.get('pts'), entri.get('des')

    try:
        with Image.open(path) as img:
            img.load()
            pts, des = _ekstrak_sift(siapkan_gambar(img))
    except Exception:
        pts, des = None, None

    if pts is None:
        return None, None

    with _cache_lock:
        _cache[kunci] = {
            'mtime': st.st_mtime_ns,
            'size': st.st_size,
            'pts': pts,
            'des': des,
        }
        _cache_kotor = True
    return pts, des


# ==========================================================
# SKOR KECOCOKAN
# ==========================================================
def _skor_sift(pts_q, des_q, pts_k, des_k):
    """Hitung skor kemiripan 0..1 memakai rasio inlier homography."""
    if des_q is None or des_k is None:
        return 0.0, 0
    if len(des_q) < 4 or len(des_k) < 4:
        return 0.0, 0

    try:
        matcher = cv2.BFMatcher(cv2.NORM_L2)
        pasangan = matcher.knnMatch(des_q, des_k, k=2)
    except Exception:
        return 0.0, 0

    baik = []
    for p in pasangan:
        if len(p) == 2 and p[0].distance < RASIO_LOWE * p[1].distance:
            baik.append(p[0])

    if len(baik) < 4:
        return 0.0, 0

    try:
        src = np.float32([pts_q[m.queryIdx] for m in baik]).reshape(-1, 1, 2)
        dst = np.float32([pts_k[m.trainIdx] for m in baik]).reshape(-1, 1, 2)
        _, mask = cv2.findHomography(src, dst, cv2.RANSAC, 5.0)
        inlier = int(mask.sum()) if mask is not None else 0
    except Exception:
        inlier = 0

    if inlier <= 0:
        return 0.0, 0

    # Skor gabungan: jumlah inlier absolut + rasio terhadap keypoint terkecil
    rasio = inlier / float(max(1, min(len(pts_q), len(pts_k))))
    skor = 0.72 * min(1.0, inlier / 30.0) + 0.28 * min(1.0, rasio / 0.45)

    # Penalti bila inlier sangat sedikit (mencegah false positive)
    if inlier < 8:
        skor *= inlier / 8.0

    return float(max(0.0, min(1.0, skor))), inlier


# ==========================================================
# FALLBACK TANPA OPENCV (pHash + dHash + warna)
# ==========================================================
def _buat_matriks_dct(n):
    m = np.zeros((n, n), dtype=np.float32)
    for i in range(n):
        skala = math.sqrt(1.0 / n) if i == 0 else math.sqrt(2.0 / n)
        for j in range(n):
            m[i, j] = skala * math.cos(math.pi * (2 * j + 1) * i / (2.0 * n))
    return m


_DCT32 = _buat_matriks_dct(32)


def _crop_konten(img):
    """Potong area kosong di tepi gambar (mis. latar putih) agar fokus ke objek."""
    a = np.asarray(img, dtype=np.float32)
    border = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]])
    latar = np.median(border, axis=0)
    mask = np.abs(a - latar).sum(axis=2) > 45
    if mask.sum() < 40:
        return img
    ys, xs = np.where(mask)
    h, w = a.shape[:2]
    y0, y1 = max(0, ys.min() - 2), min(h, ys.max() + 3)
    x0, x1 = max(0, xs.min() - 2), min(w, xs.max() + 3)
    if (y1 - y0) < 8 or (x1 - x0) < 8:
        return img
    return img.crop((x0, y0, x1, y1))


def _ciri_ink(img):
    """Peta 'tinta' 32x32: tata letak tulisan/bentuk, tahan terhadap warna & cahaya."""
    kecil = _crop_konten(img).convert('L').resize((32, 32), Image.LANCZOS)
    a = 1.0 - np.asarray(kecil, dtype=np.float32) / 255.0
    a = a - a.mean()
    norma = np.linalg.norm(a)
    a = a / norma if norma > 0 else a
    return a.reshape(32, 32)


def _ciri_hash(img):
    abu32 = img.convert('L').resize((32, 32), Image.LANCZOS)
    arr32 = np.asarray(abu32, dtype=np.float32)
    dct = _DCT32 @ arr32 @ _DCT32.T
    blok = dct[:8, :8].flatten()
    phash = (blok > np.median(blok[1:])).astype(np.uint8)

    abu9x8 = np.asarray(img.convert('L').resize((9, 8), Image.LANCZOS), dtype=np.int16)
    dhash = (abu9x8[:, 1:] > abu9x8[:, :-1]).astype(np.uint8).flatten()

    kecil = np.asarray(img.resize((8, 8), Image.LANCZOS), dtype=np.float32) / 255.0
    grid = kecil.flatten()
    norma = np.linalg.norm(grid)
    grid = grid / norma if norma > 0 else grid

    hist_img = np.asarray(img.resize((64, 64), Image.LANCZOS), dtype=np.uint8)
    idx = (hist_img[:, :, 0] // 64).astype(np.int32) * 16 \
        + (hist_img[:, :, 1] // 64).astype(np.int32) * 4 \
        + (hist_img[:, :, 2] // 64).astype(np.int32)
    hist = np.bincount(idx.flatten(), minlength=64).astype(np.float32)
    total = hist.sum()
    hist = hist / total if total > 0 else hist

    return {
        'phash': phash,
        'dhash': dhash,
        'grid': grid,
        'hist': hist,
        'ink': _ciri_ink(img),
    }


def _skor_ink(a, b, radius=3):
    """Korelasi peta tinta dengan toleransi geser kecil."""
    terbaik = -1.0
    b_flat = b.flatten()
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            nilai = float(np.dot(np.roll(np.roll(a, dy, 0), dx, 1).flatten(), b_flat))
            if nilai > terbaik:
                terbaik = nilai
    return float(np.clip(terbaik, 0.0, 1.0))


def _skor_hash(a, b):
    ph = 1.0 - np.count_nonzero(a['phash'] != b['phash']) / 64.0
    dh = 1.0 - np.count_nonzero(a['dhash'] != b['dhash']) / 64.0
    grid = float(np.clip(np.dot(a['grid'], b['grid']), 0.0, 1.0))
    hist = float(np.clip(np.minimum(a['hist'], b['hist']).sum(), 0.0, 1.0))
    ink = _skor_ink(a['ink'], b['ink'])
    return float(0.60 * ink + 0.18 * ph + 0.12 * dh + 0.10 * hist)


# ==========================================================
# API UTAMA
# ==========================================================
def cari_kemiripan(gambar, kandidat, maks_hasil=12):
    """
    Cari gambar kandidat yang mirip dengan gambar query.

    Parameters
    ----------
    gambar : PIL.Image
        Gambar query (foto kamera / upload).
    kandidat : list[dict]
        Setiap item minimal punya kunci 'id' dan 'path'.
    maks_hasil : int
        Jumlah hasil maksimum yang dikembalikan.

    Returns
    -------
    list[dict]
        Diurutkan dari skor tertinggi: {'id', 'skor', 'inlier'}.
    """
    gambar = siapkan_gambar(gambar)
    if cv2 is None or _sift is None:
        return _cari_kemiripan_hash(gambar, kandidat, maks_hasil)

    pts_q, des_q = _ekstrak_sift(gambar)
    if des_q is None or len(des_q) < 4:
        return []

    hasil = []
    for item in kandidat:
        path = item.get('path')
        if not path or not os.path.isfile(path):
            continue
        pts_k, des_k = _fitur_kandidat(path)
        skor, inlier = _skor_sift(pts_q, des_q, pts_k, des_k)
        if skor > 0:
            hasil.append({
                'id': item.get('id'),
                'skor': round(skor, 4),
                'inlier': inlier,
            })

    _simpan_cache()

    hasil.sort(key=lambda x: x['skor'], reverse=True)
    hasil = [h for h in hasil if h['skor'] >= AMBANG_COCOK]
    return hasil[:maks_hasil]


def _cari_kemiripan_hash(gambar, kandidat, maks_hasil):
    """Fallback bila OpenCV tidak terpasang."""
    ciri_q = _ciri_hash(gambar)
    hasil = []
    for item in kandidat:
        path = item.get('path')
        if not path or not os.path.isfile(path):
            continue
        try:
            with Image.open(path) as img:
                img.load()
                ciri_k = _ciri_hash(siapkan_gambar(img))
        except Exception:
            continue
        skor = _skor_hash(ciri_q, ciri_k)
        hasil.append({'id': item.get('id'), 'skor': round(skor, 4), 'inlier': 0})

    hasil.sort(key=lambda x: x['skor'], reverse=True)
    hasil = [h for h in hasil if h['skor'] >= 0.55]
    return hasil[:maks_hasil]


def hangatkan_cache(daftar_path):
    """Hitung deskriptor lebih awal di background agar pencarian pertama cepat."""
    if cv2 is None or _sift is None:
        return
    for path in daftar_path:
        try:
            _fitur_kandidat(path)
        except Exception:
            continue
    _simpan_cache()
