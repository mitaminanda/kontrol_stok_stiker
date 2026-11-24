import os
import io
import zipfile
import sqlite3
import threading
import xml.etree.ElementTree as ET
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, session, send_from_directory, jsonify, send_file
import pandas as pd
import image_match

# ==========================================
# 1. KONFIGURASI DATABASE & DIREKTORI
# ==========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, 'stok_stiker.db')
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
UPLOAD_PRODUKSI_FOLDER = os.path.join(UPLOAD_FOLDER, 'produksi')
os.makedirs(UPLOAD_PRODUKSI_FOLDER, exist_ok=True)

# ==========================================
# 2. INISIALISASI FLASK
# ==========================================
app = Flask(__name__, static_url_path='/uploads', static_folder=UPLOAD_FOLDER)
app.secret_key = 'kunci-rahasia-stiker-produksi'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
FITUR_STOCK_OPNAME = True

def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL;')
    return conn

def check_db_migrations():
    conn = get_db()
    cursor = conn.cursor()

    # Migrasi kolom tambahan pada tabel yang sudah ada agar tidak error SQLite
    alter_queries = [
        "ALTER TABLE master_produk ADD COLUMN foto_stiker_produksi TEXT",
        "ALTER TABLE master_produk ADD COLUMN saldo_awal INTEGER DEFAULT 0",
        "ALTER TABLE master_produk ADD COLUMN waktu_saldo_awal TEXT",
        "ALTER TABLE master_produk ADD COLUMN petugas_saldo_awal TEXT",
        "ALTER TABLE incoming_stikers ADD COLUMN nama_barang TEXT",
        "ALTER TABLE incoming_stikers ADD COLUMN no_sap TEXT",
        "ALTER TABLE incoming_stikers ADD COLUMN no_lot TEXT",
        "ALTER TABLE incoming_stikers ADD COLUMN lokasi_rak TEXT",
        "ALTER TABLE pemakaian_stikers ADD COLUMN buyer TEXT",
        "ALTER TABLE permintaan_produksi ADD COLUMN waktu_diterima_produksi TEXT",
        "ALTER TABLE permintaan_produksi ADD COLUMN penerima_produksi TEXT"
    ]
    for q in alter_queries:
        try:
            cursor.execute(q)
            conn.commit()
        except Exception:
            pass

    # Tabel khusus menyimpan banyak foto per stiker (multi-supplier / multi-versi) di Produksi
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS foto_stiker_produksi_list (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            master_id INTEGER,
            nama_file TEXT,
            keterangan TEXT,
            waktu_upload TEXT,
            FOREIGN KEY (master_id) REFERENCES master_produk(id)
        )
    ''')

    # Migrasi foto lama dari kolom master_produk ke tabel list (jika ada)
    try:
        old_photos = cursor.execute('''
            SELECT id, foto_stiker_produksi FROM master_produk 
            WHERE foto_stiker_produksi IS NOT NULL AND foto_stiker_produksi != ''
        ''').fetchall()
        for row in old_photos:
            cek = cursor.execute(
                'SELECT id FROM foto_stiker_produksi_list WHERE master_id = ? AND nama_file = ?',
                (row['id'], row['foto_stiker_produksi'])
            ).fetchone()
            if not cek:
                cursor.execute('''
                    INSERT INTO foto_stiker_produksi_list (master_id, nama_file, keterangan, waktu_upload)
                    VALUES (?, ?, ?, ?)
                ''', (row['id'], row['foto_stiker_produksi'], 'Versi 1', datetime.now().strftime('%Y-%m-%d %H:%M')))
    except Exception:
        pass

    conn.commit()
    conn.close()

# ==========================================
# 3. INISIALISASI TABEL & SINKRONISASI EXCEL
# ==========================================
def init_clean_db():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password TEXT,
            nama_lengkap TEXT,
            role TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS master_produk (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            buyer TEXT,
            sku TEXT,
            nama_produk TEXT,
            karton TEXT,
            packaging_3 TEXT,
            packaging_2 TEXT,
            packaging_1 TEXT,
            jenis_stiker TEXT,
            no_sas_sap TEXT,
            foto_stiker_produksi TEXT,
            saldo_awal INTEGER DEFAULT 0
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS incoming_stikers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tanggal_datang TEXT,
            urutan_mobil INTEGER,
            no_qan TEXT,
            supplier TEXT,
            nama_barang TEXT,
            no_sap TEXT,
            no_lot TEXT,
            lokasi_rak TEXT,
            jumlah INTEGER,
            foto_bukti TEXT,
            diinput_oleh TEXT,
            waktu_input TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS permintaan_produksi (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            incoming_id INTEGER,
            no_qan TEXT,
            supplier TEXT,
            buyer TEXT,
            nama_produk TEXT,
            sku TEXT,
            jenis_stiker TEXT,
            no_sas_sap TEXT,
            jumlah_diminta INTEGER,
            pengaju TEXT,
            diajukan_pada TEXT,
            status TEXT DEFAULT 'Menunggu Persetujuan',
            alasan_tolak TEXT,
            waktu_respon TEXT,
            petugas_respon TEXT,
            FOREIGN KEY (incoming_id) REFERENCES incoming_stikers(id)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS penyerahan_stikers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            permintaan_id INTEGER,
            jumlah_diserahkan INTEGER,
            waktu_penyerahan TEXT,
            petugas_gudang TEXT,
            FOREIGN KEY (permintaan_id) REFERENCES permintaan_produksi(id)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pemakaian_stikers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tanggal TEXT,
            waktu TEXT,
            shift TEXT,
            buyer TEXT,
            nama_produk TEXT,
            sku TEXT,
            jenis_stiker TEXT,
            jumlah_digunakan INTEGER,
            jumlah_bs INTEGER,
            total_digunakan INTEGER,
            petugas_produksi TEXT
        )
    ''')

    # Akun Login Sistem
    daftar_users = [
        ('gudang', 'gudang123!', 'Petugas Gudang', 'gudang'),
        ('produksi', 'Produksi231', 'Operator Produksi', 'produksi'),
        ('ppic', 'TorabikaJaya', 'PPIC', 'ppic')
    ]
    for username, password, nama, role in daftar_users:
        user_exist = cursor.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
        if user_exist:
            cursor.execute('UPDATE users SET password = ?, nama_lengkap = ?, role = ? WHERE username = ?',
                           (password, nama, role, username))
        else:
            cursor.execute('INSERT INTO users (username, password, nama_lengkap, role) VALUES (?, ?, ?, ?)',
                           (username, password, nama, role))

    conn.commit()
    conn.close()

init_clean_db()
check_db_migrations()

# ==========================================
# DECORATOR AKSES ROLE
# ==========================================
def login_required(role_required=None):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'username' not in session:
                flash('Silakan login terlebih dahulu.', 'warning')
                return redirect(url_for('login'))
            if role_required and session.get('role') != role_required:
                flash('Akses ditolak untuk peran Anda.', 'danger')
                return redirect(url_for('dashboard_router'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator

# ==========================================
# RUTE AUTH & ROUTER
# ==========================================
@app.route('/', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        conn = get_db()
        user = conn.execute('SELECT * FROM users WHERE username = ? AND password = ?', (username, password)).fetchone()
        conn.close()

        if user:
            session['username'] = user['username']
            session['nama_lengkap'] = user['nama_lengkap']
            session['role'] = user['role']
            flash('Selamat datang!', 'success')
            return redirect(url_for('dashboard_router'))
        else:
            flash('Username atau password salah!', 'danger')

    return render_template('login.html')

@app.route('/dashboard')
def dashboard_router():
    role = session.get('role')
    if role == 'gudang':
        return redirect(url_for('gudang_incoming'))
    elif role == 'produksi':
        return redirect(url_for('produksi_pengajuan'))
    elif role == 'ppic':
        return redirect(url_for('ppic_monitoring'))
    return redirect(url_for('login'))

@app.route('/logout')
def logout():
    session.clear()
    flash('Berhasil keluar dari sistem.', 'info')
    return redirect(url_for('login'))

# ==========================================
# FITUR POV GUDANG
# ==========================================
@app.route('/gudang/incoming', methods=['GET', 'POST'])
@login_required(role_required='gudang')
def gudang_incoming():
    conn = get_db()
    cursor = conn.cursor()

    if request.method == 'POST':
        # Mendukung back date dari input form tanggal_datang
        tanggal_datang = request.form.get('tanggal_datang') or datetime.now().strftime('%Y-%m-%d')
        urutan_mobil = int(request.form.get('urutan_mobil', 1))
        supplier = request.form.get('supplier', '').strip()
        nama_barang = request.form.get('nama_barang', '').strip()
        no_sap = request.form.get('no_sap', '').strip()
        no_lot = request.form.get('no_lot', '').strip().upper()
        kelompok_rak = request.form.get('kelompok_rak', '').strip().upper()
        detail_lokasi = request.form.get('detail_lokasi', '').strip().upper()
        if kelompok_rak and detail_lokasi:
            lokasi_rak = f"{kelompok_rak} - {detail_lokasi}"
        else:
            lokasi_rak = kelompok_rak or detail_lokasi or "-"
        jumlah = int(request.form.get('jumlah'))
        diinput_oleh = session.get('nama_lengkap', 'Petugas Gudang')
        waktu_input = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        tgl_obj = datetime.strptime(tanggal_datang, '%Y-%m-%d')
        no_qan = f"{tgl_obj.strftime('%d')} {tgl_obj.strftime('%m')} {urutan_mobil:02d}"

        foto = request.files.get('foto')
        foto_name = None
        if foto and foto.filename != '':
            foto_name = f"qan_{no_qan.replace(' ', '_')}_{int(datetime.now().timestamp())}_{foto.filename}"
            foto.save(os.path.join(app.config['UPLOAD_FOLDER'], foto_name))

        with conn:
            cursor.execute('''
                INSERT INTO incoming_stikers 
                (tanggal_datang, urutan_mobil, no_qan, supplier, nama_barang, no_sap, no_lot, lokasi_rak, jumlah, foto_bukti, diinput_oleh, waktu_input)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (tanggal_datang, urutan_mobil, no_qan, supplier, nama_barang, no_sap, no_lot, lokasi_rak, jumlah, foto_name, diinput_oleh, waktu_input))

        flash(f'Incoming No. QAN {no_qan} ({nama_barang}) berhasil dicatat sebanyak {jumlah:,} PCS di {lokasi_rak}.', 'success')
        conn.close()
        return redirect(url_for('gudang_incoming'))

    cursor.execute('SELECT * FROM incoming_stikers ORDER BY id DESC')
    incoming_list = cursor.fetchall()
    conn.close()

    return render_template('gudang_incoming.html',
                           incoming_list=incoming_list,
                           today=datetime.now().strftime('%Y-%m-%d'))

@app.route('/gudang/permintaan')
@login_required(role_required='gudang')
def gudang_permintaan():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM permintaan_produksi ORDER BY id DESC')
    daftar_permintaan = cursor.fetchall()
    conn.close()
    return render_template('gudang_approval.html', permintaan_list=daftar_permintaan)

@app.route('/gudang/permintaan/approval', methods=['POST'])
@login_required(role_required='gudang')
def gudang_approval():
    permintaan_id = request.form.get('permintaan_id')
    aksi = request.form.get('aksi')
    alasan_tolak = request.form.get('alasan_tolak', '-')
    petugas = session.get('nama_lengkap', 'Petugas Gudang')
    waktu_respon = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    conn = get_db()
    cursor = conn.cursor()

    if aksi == 'setujui':
        cursor.execute('''
            UPDATE permintaan_produksi 
            SET status = 'Disetujui', waktu_respon = ?, petugas_respon = ?
            WHERE id = ?
        ''', (waktu_respon, petugas, permintaan_id))
        flash('Permintaan produksi telah DISETUJUI.', 'success')
    else:
        cursor.execute('''
            UPDATE permintaan_produksi 
            SET status = 'Ditolak', alasan_tolak = ?, waktu_respon = ?, petugas_respon = ?
            WHERE id = ?
        ''', (alasan_tolak, waktu_respon, petugas, permintaan_id))
        flash('Permintaan produksi telah DITOLAK.', 'warning')

    conn.commit()
    conn.close()
    return redirect(url_for('gudang_permintaan'))

@app.route('/gudang/penyerahan/eksekusi', methods=['POST'])
@login_required(role_required='gudang')
def gudang_eksekusi_penyerahan():
    permintaan_id = request.form.get('permintaan_id')
    petugas = session.get('nama_lengkap', 'Petugas Gudang')
    waktu = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    conn = get_db()
    cursor = conn.cursor()
    req = cursor.execute('SELECT * FROM permintaan_produksi WHERE id = ?', (permintaan_id,)).fetchone()

    if req:
        with conn:
            cursor.execute('''
                INSERT INTO penyerahan_stikers (permintaan_id, jumlah_diserahkan, waktu_penyerahan, petugas_gudang)
                VALUES (?, ?, ?, ?)
            ''', (permintaan_id, req['jumlah_diminta'], waktu, petugas))
            cursor.execute("UPDATE permintaan_produksi SET status = 'Menunggu Konfirmasi Produksi' WHERE id = ?", (permintaan_id,))
        flash('Stiker diserahkan fisik. Menunggu konfirmasi "Diterima" dari pihak Produksi.', 'success')
    conn.close()
    return redirect(url_for('gudang_permintaan'))

@app.route('/produksi/pengajuan/terima', methods=['POST'])
@login_required(role_required='produksi')
def produksi_konfirmasi_terima():
    permintaan_id = request.form.get('permintaan_id')
    penerima = session.get('nama_lengkap', 'Operator Produksi')
    waktu_terima = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    conn = get_db()
    cursor = conn.cursor()
    req = cursor.execute('SELECT * FROM permintaan_produksi WHERE id = ?', (permintaan_id,)).fetchone()

    if req and req['status'] == 'Menunggu Konfirmasi Produksi':
        cursor.execute('''
            UPDATE permintaan_produksi 
            SET status = 'Close / Selesai', waktu_diterima_produksi = ?, penerima_produksi = ?
            WHERE id = ?
        ''', (waktu_terima, penerima, permintaan_id))
        conn.commit()
        flash(f'Penerimaan stiker {req["nama_produk"]} ({req["jumlah_diminta"]:,} PCS) telah dikonfirmasi. Status transaksi: Close / Selesai.', 'success')
    conn.close()
    return redirect(url_for('produksi_pengajuan'))

# ==========================================
# RUTE STOK & LAPORAN + PENCARIAN LOKASI RAK
# ==========================================
@app.route('/stok/katalog')
@login_required()
def stok_katalog():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute('''
        SELECT 
            inc.id,
            inc.no_qan,
            inc.tanggal_datang,
            inc.supplier,
            inc.nama_barang,
            inc.no_sap,
            inc.lokasi_rak,
            inc.jumlah AS total_incoming,
            inc.foto_bukti,
            COALESCE((
                SELECT SUM(pen.jumlah_diserahkan) 
                FROM penyerahan_stikers pen
                JOIN permintaan_produksi p ON pen.permintaan_id = p.id
                WHERE p.incoming_id = inc.id
            ), 0) AS total_keluar,
            (inc.jumlah - COALESCE((
                SELECT SUM(pen.jumlah_diserahkan) 
                FROM penyerahan_stikers pen
                JOIN permintaan_produksi p ON pen.permintaan_id = p.id
                WHERE p.incoming_id = inc.id
            ), 0)) AS sisa_stok_gudang
        FROM incoming_stikers inc
        ORDER BY inc.id DESC
    ''')
    stok_items = cursor.fetchall()
    conn.close()
    return render_template('stok_katalog.html', stok_items=stok_items)

@app.route('/gudang/pencarian')
@login_required()
def gudang_pencarian():
    _mulai_warmup_gambar()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT 
            inc.id,
            inc.no_qan,
            inc.tanggal_datang,
            inc.supplier,
            inc.nama_barang,
            inc.no_sap,
            inc.no_lot,
            inc.lokasi_rak,
            inc.foto_bukti,
            inc.jumlah AS total_incoming,
            (inc.jumlah - COALESCE((
                SELECT SUM(pen.jumlah_diserahkan) 
                FROM penyerahan_stikers pen
                JOIN permintaan_produksi p ON pen.permintaan_id = p.id
                WHERE p.incoming_id = inc.id
            ), 0)) AS sisa_stok_gudang
        FROM incoming_stikers inc
        ORDER BY inc.id DESC
    ''')
    daftar_stiker = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return render_template('gudang_pencarian.html', daftar_stiker=daftar_stiker)


# ==========================================
# PENCARIAN STIKER BERDASARKAN FOTO (IMAGE MATCHING)
# ==========================================
_warmup_gambar_dimulai = False
_warmup_gambar_lock = threading.Lock()


def _mulai_warmup_gambar():
    """Hitung deskriptor foto di background agar pencarian gambar pertama cepat."""
    global _warmup_gambar_dimulai
    with _warmup_gambar_lock:
        if _warmup_gambar_dimulai:
            return
        _warmup_gambar_dimulai = True

    def kerja():
        try:
            conn = get_db()
            rows = conn.execute(
                "SELECT foto_bukti FROM incoming_stikers "
                "WHERE foto_bukti IS NOT NULL AND foto_bukti != ''"
            ).fetchall()
            conn.close()
            daftar_path = [os.path.join(UPLOAD_FOLDER, r['foto_bukti']) for r in rows]
            image_match.hangatkan_cache(daftar_path)
        except Exception:
            pass

    threading.Thread(target=kerja, daemon=True).start()


@app.route('/gudang/pencarian-gambar', methods=['POST'])
@login_required(role_required='gudang')
def gudang_pencarian_gambar():
    """Cocokkan foto kiriman pengguna dengan foto stiker yang ada di database."""
    berkas = request.files.get('gambar')
    if not berkas or not berkas.filename:
        return jsonify({'ok': False, 'pesan': 'Tidak ada gambar yang dikirim.'}), 400

    data = berkas.read()
    if not data:
        return jsonify({'ok': False, 'pesan': 'File gambar kosong.'}), 400
    if len(data) > 15 * 1024 * 1024:
        return jsonify({'ok': False, 'pesan': 'Ukuran gambar maksimal 15 MB.'}), 400

    gambar = image_match.buka_gambar_dari_bytes(data)
    if gambar is None:
        return jsonify({'ok': False, 'pesan': 'File bukan gambar yang valid.'}), 400

    conn = get_db()
    rows = conn.execute('''
        SELECT id, nama_barang, foto_bukti
        FROM incoming_stikers
        WHERE foto_bukti IS NOT NULL AND foto_bukti != ''
        ORDER BY id DESC
    ''').fetchall()
    conn.close()

    kandidat = []
    for row in rows:
        path = os.path.join(UPLOAD_FOLDER, row['foto_bukti'])
        if os.path.isfile(path):
            kandidat.append({'id': row['id'], 'path': path})

    if not kandidat:
        return jsonify({
            'ok': True,
            'hasil': [],
            'jumlah_kandidat': 0,
            'terbaik': 0,
            'pesan': 'Belum ada foto stiker di database untuk dibandingkan.'
        })

    hasil = image_match.cari_kemiripan(gambar, kandidat)
    return jsonify({
        'ok': True,
        'hasil': hasil,
        'jumlah_kandidat': len(kandidat),
        'terbaik': hasil[0]['skor'] if hasil else 0,
    })

@app.route('/gudang/update-lokasi', methods=['POST'])
@login_required()
def gudang_update_lokasi():
    incoming_id = request.form.get('incoming_id')
    kelompok_rak = request.form.get('kelompok_rak', '').strip().upper()
    detail_lokasi = request.form.get('detail_lokasi', '').strip().upper()
    lokasi_rak = f"{kelompok_rak} - {detail_lokasi}" if (kelompok_rak and detail_lokasi) else (kelompok_rak or detail_lokasi or "-")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE incoming_stikers SET lokasi_rak = ? WHERE id = ?", (lokasi_rak, incoming_id))
    conn.commit()
    conn.close()
    flash(f'Lokasi penyimpanan berhasil diperbarui ke {lokasi_rak}.', 'success')
    return redirect(url_for('gudang_pencarian'))

@app.route('/api/incoming/<int:incoming_id>/history')
@login_required()
def api_incoming_history(incoming_id):
    conn = get_db()
    cursor = conn.cursor()
    incoming = cursor.execute('SELECT * FROM incoming_stikers WHERE id = ?', (incoming_id,)).fetchone()
    if not incoming:
        conn.close()
        return {"error": "Data incoming tidak ditemukan"}, 404

    penyerahan = cursor.execute('''
        SELECT 
            pen.waktu_penyerahan,
            pen.jumlah_diserahkan,
            pen.petugas_gudang,
            p.buyer,
            p.nama_produk,
            p.sku,
            p.jenis_stiker,
            p.pengaju
        FROM penyerahan_stikers pen
        JOIN permintaan_produksi p ON pen.permintaan_id = p.id
        WHERE p.incoming_id = ?
        ORDER BY pen.id DESC
    ''', (incoming_id,)).fetchall()

    total_keluar = cursor.execute('''
        SELECT COALESCE(SUM(pen.jumlah_diserahkan), 0) 
        FROM penyerahan_stikers pen
        JOIN permintaan_produksi p ON pen.permintaan_id = p.id
        WHERE p.incoming_id = ?
    ''', (incoming_id,)).fetchone()[0]
    conn.close()

    return {
        "incoming": dict(incoming),
        "total_incoming": incoming['jumlah'],
        "total_keluar": total_keluar,
        "sisa_stok": incoming['jumlah'] - total_keluar,
        "penyerahan": [dict(row) for row in penyerahan]
    }

# ==========================================
# RUTE POV PRODUKSI
# ==========================================
@app.route('/produksi/pengajuan')
@login_required(role_required='produksi')
def produksi_pengajuan():
    conn = get_db()
    cursor = conn.cursor()

    incomings = cursor.execute('''
        SELECT 
            inc.*,
            COALESCE((
                SELECT SUM(jumlah_diminta) 
                FROM permintaan_produksi 
                WHERE incoming_id = inc.id AND status != 'Ditolak'
            ), 0) AS total_diajukan,
            (inc.jumlah - COALESCE((
                SELECT SUM(jumlah_diminta) 
                FROM permintaan_produksi 
                WHERE incoming_id = inc.id AND status != 'Ditolak'
            ), 0)) AS sisa_tersedia
        FROM incoming_stikers inc 
        ORDER BY inc.id DESC
    ''').fetchall()

    master_produk = cursor.execute('''
        SELECT DISTINCT sku, nama_produk, buyer 
        FROM master_produk 
        ORDER BY nama_produk ASC
    ''').fetchall()

    conn.close()
    return render_template('produksi_pengajuan.html', incomings=incomings, master_produk=master_produk)

@app.route('/api/produk/<sku>/stiker')
@login_required()
def api_produk_stiker(sku):
    conn = get_db()
    cursor = conn.cursor()
    items = cursor.execute('''
        SELECT jenis_stiker, no_sas_sap, buyer, nama_produk 
        FROM master_produk 
        WHERE sku = ?
    ''', (sku,)).fetchall()
    conn.close()
    return jsonify([dict(row) for row in items])

@app.route('/produksi/pengajuan/submit', methods=['POST'])
@login_required(role_required='produksi')
def produksi_pengajuan_submit():
    incoming_id = request.form.get('incoming_id')
    no_qan = request.form.get('no_qan')
    supplier = request.form.get('supplier')
    buyer = request.form.get('buyer')
    nama_produk = request.form.get('nama_produk')
    sku = request.form.get('sku')
    jenis_stiker = request.form.get('jenis_stiker')
    no_sas_sap = request.form.get('no_sas_sap')
    jumlah_diminta = int(request.form.get('jumlah_diminta'))
    pengaju = session.get('nama_lengkap', 'Operator Produksi')
    diajukan_pada = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO permintaan_produksi 
        (incoming_id, no_qan, supplier, buyer, nama_produk, sku, jenis_stiker, no_sas_sap, jumlah_diminta, pengaju, diajukan_pada)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (incoming_id, no_qan, supplier, buyer, nama_produk, sku, jenis_stiker, no_sas_sap, jumlah_diminta, pengaju, diajukan_pada))
    conn.commit()
    conn.close()

    flash(f'Pengajuan stiker untuk {nama_produk} berhasil dikirim ke Gudang.', 'success')
    return redirect(url_for('produksi_pengajuan'))

@app.route('/produksi/pemakaian', methods=['GET', 'POST'])
@login_required(role_required='produksi')
def produksi_pemakaian():
    conn = get_db()
    cursor = conn.cursor()

    if request.method == 'POST':
        tanggal = datetime.now().strftime('%Y-%m-%d')
        waktu = datetime.now().strftime('%H:%M:%S')
        shift = request.form.get('shift')
        buyer = request.form.get('buyer')
        sku = request.form.get('sku')
        nama_produk = request.form.get('nama_produk')
        jenis_stiker = request.form.get('jenis_stiker')
        jumlah_digunakan = int(request.form.get('jumlah_digunakan', 0))
        jumlah_bs = int(request.form.get('jumlah_bs', 0))
        total_digunakan = jumlah_digunakan + jumlah_bs
        petugas = session.get('nama_lengkap', 'Operator Produksi')

        cursor.execute('''
            INSERT INTO pemakaian_stikers 
            (tanggal, waktu, shift, buyer, nama_produk, sku, jenis_stiker, jumlah_digunakan, jumlah_bs, total_digunakan, petugas_produksi)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (tanggal, waktu, shift, buyer, nama_produk, sku, jenis_stiker, jumlah_digunakan, jumlah_bs, total_digunakan, petugas))
        conn.commit()
        flash('Data pemakaian stiker berhasil disimpan.', 'success')
        conn.close()
        return redirect(url_for('produksi_pemakaian'))

    riwayat_pemakaian = cursor.execute('SELECT * FROM pemakaian_stikers ORDER BY id DESC').fetchall()
    master_produk = cursor.execute('''
        SELECT DISTINCT sku, nama_produk, buyer 
        FROM master_produk 
        ORDER BY nama_produk ASC
    ''').fetchall()

    conn.close()
    return render_template('produksi_pemakaian.html', riwayat=riwayat_pemakaian, master_produk=master_produk)

@app.route('/produksi/stok')
@login_required(role_required='produksi')
def produksi_stok():
    conn = get_db()
    cursor = conn.cursor()
    rows = cursor.execute('''
        SELECT 
            mp.id,
            mp.buyer,
            mp.sku,
            mp.nama_produk,
            mp.jenis_stiker,
            mp.no_sas_sap,
            COALESCE(mp.saldo_awal, 0) as saldo_awal,
            COALESCE((
                SELECT SUM(p.jumlah_diminta) 
                FROM permintaan_produksi p 
                WHERE p.sku = mp.sku 
                  AND p.jenis_stiker = mp.jenis_stiker 
                  AND (COALESCE(p.buyer, '') = '' OR UPPER(TRIM(p.buyer)) = UPPER(TRIM(COALESCE(mp.buyer, ''))))
                  AND p.status IN ('Selesai', 'Close / Selesai')
            ), 0) as diterima_produksi,
            COALESCE((
                SELECT SUM(pm.total_digunakan) 
                FROM pemakaian_stikers pm 
                WHERE pm.sku = mp.sku 
                  AND pm.jenis_stiker = mp.jenis_stiker
                  AND (COALESCE(pm.buyer, '') = '' OR UPPER(TRIM(pm.buyer)) = UPPER(TRIM(COALESCE(mp.buyer, ''))))
            ), 0) as total_dipakai,
            (COALESCE(mp.saldo_awal, 0) +
             COALESCE((
                SELECT SUM(p.jumlah_diminta) 
                FROM permintaan_produksi p 
                WHERE p.sku = mp.sku 
                  AND p.jenis_stiker = mp.jenis_stiker 
                  AND (COALESCE(p.buyer, '') = '' OR UPPER(TRIM(p.buyer)) = UPPER(TRIM(COALESCE(mp.buyer, ''))))
                  AND p.status IN ('Selesai', 'Close / Selesai')
             ), 0) - 
             COALESCE((
                SELECT SUM(pm.total_digunakan) 
                FROM pemakaian_stikers pm 
                WHERE pm.sku = mp.sku 
                  AND pm.jenis_stiker = mp.jenis_stiker
                  AND (COALESCE(pm.buyer, '') = '' OR UPPER(TRIM(pm.buyer)) = UPPER(TRIM(COALESCE(mp.buyer, ''))))
             ), 0)) as sisa_di_produksi
        FROM master_produk mp
        ORDER BY mp.buyer ASC, mp.nama_produk ASC
    ''').fetchall()

    all_fotos = cursor.execute('SELECT * FROM foto_stiker_produksi_list ORDER BY id ASC').fetchall()
    foto_map = {}
    for f in all_fotos:
        foto_map.setdefault(f['master_id'], []).append(dict(f))

    laporan_stok = []
    for r in rows:
        item_dict = dict(r)
        item_dict['daftar_foto'] = foto_map.get(r['id'], [])
        laporan_stok.append(item_dict)

    conn.close()
    return render_template('produksi_stok.html', laporan=laporan_stok, fitur_opname=FITUR_STOCK_OPNAME)

@app.route('/produksi/set-saldo-awal', methods=['POST'])
@login_required(role_required='produksi')
def produksi_set_saldo_awal():
    stiker_id = request.form.get('stiker_id')
    jumlah_awal = int(request.form.get('saldo_awal', 0))

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE master_produk SET saldo_awal = ? WHERE id = ?", (max(0, jumlah_awal), stiker_id))
    conn.commit()
    conn.close()

    flash(f'Saldo awal stok produksi berhasil diperbarui menjadi {jumlah_awal:,} PCS.', 'success')
    return redirect(url_for('produksi_stok'))

@app.route('/produksi/stock-opname/submit', methods=['POST'])
@login_required(role_required='produksi')
def produksi_stock_opname_submit():
    if not FITUR_STOCK_OPNAME:
        flash('Fitur input Stock Opname sudah dinonaktifkan.', 'warning')
        return redirect(url_for('produksi_stok'))

    stiker_id = request.form.get('stiker_id', '').strip()
    sku = request.form.get('sku', '').strip()
    buyer = request.form.get('buyer', '').strip().upper()
    jenis_stiker = request.form.get('jenis_stiker', '').strip().upper()
    jumlah_fisik = int(request.form.get('jumlah_fisik', 0))
    petugas = session.get('nama_lengkap', 'Operator Produksi')
    waktu_opname = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    conn = get_db()
    cursor = conn.cursor()

    mp = None
    if stiker_id and stiker_id.isdigit():
        mp = cursor.execute(
            'SELECT id, buyer, nama_produk FROM master_produk WHERE id = ?',
            (int(stiker_id),)
        ).fetchone()

    if not mp and buyer and buyer != '-':
        mp = cursor.execute(
            'SELECT id, buyer, nama_produk FROM master_produk WHERE UPPER(TRIM(buyer)) = ? AND TRIM(sku) = ? AND UPPER(TRIM(jenis_stiker)) = ?',
            (buyer, sku, jenis_stiker)
        ).fetchone()

    if not mp:
        mp = cursor.execute(
            'SELECT id, buyer, nama_produk FROM master_produk WHERE TRIM(sku) = ? AND UPPER(TRIM(jenis_stiker)) = ?',
            (sku, jenis_stiker)
        ).fetchone()

    if not mp:
        conn.close()
        flash('Data produk, buyer, dan jenis stiker tidak ditemukan di master produk!', 'danger')
        return redirect(url_for('produksi_stok'))

    master_id = mp['id']
    buyer_terpilih = mp['buyer'] or '-'

    # Simpan saldo opname ke saldo_awal beserta waktu & petugas input
    cursor.execute('''
        UPDATE master_produk 
        SET saldo_awal = ?, waktu_saldo_awal = ?, petugas_saldo_awal = ?
        WHERE id = ?
    ''', (max(0, jumlah_fisik), waktu_opname, petugas, master_id))

    # Jika mengunggah foto stiker sekalian saat Stock Opname
    file = request.files.get('foto')
    if file and file.filename != '':
        allowed_ext = {'png', 'jpg', 'jpeg', 'webp'}
        ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else ''
        if ext in allowed_ext:
            nama_file = f"prod_stiker_{master_id}_{int(datetime.now().timestamp())}.{ext}"
            file.save(os.path.join(UPLOAD_PRODUKSI_FOLDER, nama_file))
            cursor.execute('''
                INSERT INTO foto_stiker_produksi_list (master_id, nama_file, keterangan, waktu_upload)
                VALUES (?, ?, ?, ?)
            ''', (master_id, nama_file, 'Stock Opname Produksi', datetime.now().strftime('%Y-%m-%d %H:%M')))

    conn.commit()
    conn.close()
    flash(f'Hasil Stock Opname [{buyer_terpilih}] {mp["nama_produk"]} ({jenis_stiker}) sebanyak {jumlah_fisik:,} PCS berhasil disimpan.', 'success')
    return redirect(url_for('produksi_stok'))

    master_id = mp['id']

    # Simpan saldo opname ke saldo_awal beserta waktu & petugas input
    cursor.execute('''
        UPDATE master_produk 
        SET saldo_awal = ?, waktu_saldo_awal = ?, petugas_saldo_awal = ?
        WHERE id = ?
    ''', (max(0, jumlah_fisik), waktu_opname, petugas, master_id))

    # Jika mengunggah foto stiker sekalian saat Stock Opname
    file = request.files.get('foto')
    if file and file.filename != '':
        allowed_ext = {'png', 'jpg', 'jpeg', 'webp'}
        ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else ''
        if ext in allowed_ext:
            nama_file = f"prod_stiker_{master_id}_{int(datetime.now().timestamp())}.{ext}"
            file.save(os.path.join(UPLOAD_PRODUKSI_FOLDER, nama_file))
            cursor.execute('''
                INSERT INTO foto_stiker_produksi_list (master_id, nama_file, keterangan, waktu_upload)
                VALUES (?, ?, ?, ?)
            ''', (master_id, nama_file, 'Stock Opname Produksi', datetime.now().strftime('%Y-%m-%d %H:%M')))

    conn.commit()
    conn.close()
    flash(f'Hasil Stock Opname {mp["nama_produk"]} ({jenis_stiker}) sebanyak {jumlah_fisik:,} PCS berhasil disimpan.', 'success')
    return redirect(url_for('produksi_stok'))

@app.route('/uploads/<path:filename>')
def view_uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/uploads/produksi/<path:filename>')
def serve_uploads_produksi(filename):
    return send_from_directory(UPLOAD_PRODUKSI_FOLDER, filename)

@app.route('/produksi/upload-foto-stiker', methods=['POST'])
@login_required(role_required='produksi')
def produksi_upload_foto_stiker():
    stiker_id = request.form.get('stiker_id')
    keterangan = request.form.get('keterangan', '').strip()
    if not keterangan:
        keterangan = f"Upload {datetime.now().strftime('%d/%m/%Y')}"

    if 'foto' not in request.files:
        flash('Tidak ada file foto yang dipilih!', 'danger')
        return redirect(url_for('produksi_stok'))

    file = request.files['foto']
    if file.filename == '':
        flash('Silakan pilih file gambar!', 'danger')
        return redirect(url_for('produksi_stok'))

    allowed_ext = {'png', 'jpg', 'jpeg', 'webp'}
    ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else ''

    if file and ext in allowed_ext:
        nama_file = f"prod_stiker_{stiker_id}_{int(datetime.now().timestamp())}.{ext}"
        jalur_simpan = os.path.join(UPLOAD_PRODUKSI_FOLDER, nama_file)
        file.save(jalur_simpan)

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO foto_stiker_produksi_list (master_id, nama_file, keterangan, waktu_upload)
            VALUES (?, ?, ?, ?)
        ''', (stiker_id, nama_file, keterangan, datetime.now().strftime('%Y-%m-%d %H:%M')))
        conn.commit()
        conn.close()

        flash('Foto stiker baru berhasil ditambahkan ke daftar!', 'success')
    else:
        flash('Format file harus berupa gambar (JPG, PNG, atau WEBP)!', 'danger')

    return redirect(url_for('produksi_stok'))

@app.route('/produksi/hapus-foto-stiker/<int:foto_id>', methods=['POST'])
@login_required(role_required='produksi')
def produksi_hapus_foto_stiker(foto_id):
    conn = get_db()
    cursor = conn.cursor()
    foto = cursor.execute('SELECT nama_file FROM foto_stiker_produksi_list WHERE id = ?', (foto_id,)).fetchone()
    if foto:
        file_path = os.path.join(UPLOAD_PRODUKSI_FOLDER, foto['nama_file'])
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass
        cursor.execute('DELETE FROM foto_stiker_produksi_list WHERE id = ?', (foto_id,))
        conn.commit()
        flash('Foto stiker berhasil dihapus.', 'info')
    conn.close()
    return redirect(url_for('produksi_stok'))

@app.route('/api/incoming/<int:incoming_id>/status_produksi')
@login_required()
def api_status_produksi(incoming_id):
    conn = get_db()
    cursor = conn.cursor()
    permintaan = cursor.execute('SELECT * FROM permintaan_produksi WHERE incoming_id = ? ORDER BY id DESC', (incoming_id,)).fetchall()
    conn.close()
    return jsonify([dict(row) for row in permintaan])

@app.route('/api/produksi/mutasi/<sku>/<path:jenis_stiker>')
@login_required()
def api_produksi_mutasi(sku, jenis_stiker):
    conn = get_db()
    cursor = conn.cursor()

    mp_row = cursor.execute('''
        SELECT COALESCE(saldo_awal, 0) as saldo_awal, waktu_saldo_awal, petugas_saldo_awal 
        FROM master_produk WHERE sku = ? AND jenis_stiker = ?
    ''', (sku, jenis_stiker)).fetchone()

    saldo_awal = mp_row['saldo_awal'] if mp_row else 0
    waktu_saldo_awal = mp_row['waktu_saldo_awal'] if (mp_row and mp_row['waktu_saldo_awal']) else '-'
    petugas_saldo_awal = mp_row['petugas_saldo_awal'] if (mp_row and mp_row['petugas_saldo_awal']) else 'Operator Produksi'

    masuk = cursor.execute('''
        SELECT 
            pen.waktu_penyerahan AS waktu,
            p.no_qan,
            p.supplier,
            pen.jumlah_diserahkan AS jumlah,
            pen.petugas_gudang,
            p.pengaju
        FROM penyerahan_stikers pen
        JOIN permintaan_produksi p ON pen.permintaan_id = p.id
        WHERE p.sku = ? AND p.jenis_stiker = ?
        ORDER BY pen.id DESC
    ''', (sku, jenis_stiker)).fetchall()

    keluar = cursor.execute('''
        SELECT 
            (tanggal || ' ' || waktu) AS waktu,
            shift,
            jumlah_digunakan,
            jumlah_bs,
            total_digunakan AS jumlah,
            petugas_produksi
        FROM pemakaian_stikers
        WHERE sku = ? AND jenis_stiker = ?
        ORDER BY id DESC
    ''', (sku, jenis_stiker)).fetchall()

    conn.close()

    return jsonify({
        "saldo_awal": saldo_awal,
        "waktu_saldo_awal": waktu_saldo_awal,
        "petugas_saldo_awal": petugas_saldo_awal,
        "masuk": [dict(row) for row in masuk],
        "keluar": [dict(row) for row in keluar]
    })

# ==========================================
# FITUR POV PPIC
# ==========================================
@app.route('/ppic/monitoring')
@login_required(role_required='ppic')
def ppic_monitoring():
    bulan = request.args.get('bulan', datetime.now().strftime('%m'))
    tahun = request.args.get('tahun', datetime.now().strftime('%Y'))
    periode = f"{tahun}-{bulan}"

    conn = get_db()
    cursor = conn.cursor()

    # Sinkronisasi real-time dari Excel jika ada
    excel_path = os.path.join(BASE_DIR, 'Control Stock Stiker.xlsx')
    if os.path.exists(excel_path):
        try:
            df = pd.read_excel(excel_path, sheet_name='Data Mapping Sticker', dtype=str)
            for _, row in df.iterrows():
                buyer_xl = str(row.get('Nama Buyer', '')).strip().upper()
                sku_xl = str(row.get('SKU', '')).strip().replace('.0', '')
                nama_xl = str(row.get('Nama Produk', '')).strip()
                karton_xl = str(row.get('Karton', '')).strip()
                pack3_xl = str(row.get('Packaging 3', '')).strip()
                pack2_xl = str(row.get('Packaging 2', '')).strip()
                pack1_xl = str(row.get('Packaging 1', '')).strip()
                jenis_xl = str(row.get('Jenis Stiker', '')).strip().upper()
                sas_sap_xl = str(row.get('NO. SAS/ SAP', '')).strip()

                if not sku_xl or sku_xl == 'nan':
                    continue

                existing = cursor.execute('''
                    SELECT id, no_sas_sap FROM master_produk 
                    WHERE UPPER(TRIM(buyer)) = ? AND TRIM(sku) = ? AND UPPER(TRIM(jenis_stiker)) = ?
                ''', (buyer_xl, sku_xl, jenis_xl)).fetchone()

                if existing:
                    if str(existing['no_sas_sap']).strip() != sas_sap_xl:
                        cursor.execute('''
                            UPDATE master_produk 
                            SET nama_produk = ?, karton = ?, packaging_3 = ?, packaging_2 = ?, packaging_1 = ?, no_sas_sap = ?
                            WHERE id = ?
                        ''', (nama_xl, karton_xl, pack3_xl, pack2_xl, pack1_xl, sas_sap_xl, existing['id']))

                        cursor.execute('''
                            UPDATE permintaan_produksi 
                            SET no_sas_sap = ? 
                            WHERE TRIM(sku) = ? AND UPPER(TRIM(jenis_stiker)) = ?
                        ''', (sas_sap_xl, sku_xl, jenis_xl))
                else:
                    cursor.execute('''
                        INSERT INTO master_produk 
                        (buyer, sku, nama_produk, karton, packaging_3, packaging_2, packaging_1, jenis_stiker, no_sas_sap)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (buyer_xl, sku_xl, nama_xl, karton_xl, pack3_xl, pack2_xl, pack1_xl, jenis_xl, sas_sap_xl))
            conn.commit()
        except Exception as e:
            print(f"Info sinkronisasi Excel: {e}")

    # 1. Data Stok Gudang
    cursor.execute('''
        SELECT 
            inc.id,
            inc.no_qan,
            inc.tanggal_datang,
            inc.supplier,
            inc.nama_barang,
            inc.no_sap,
            inc.lokasi_rak,
            inc.jumlah AS total_incoming,
            inc.foto_bukti,
            COALESCE((
                SELECT SUM(pen.jumlah_diserahkan) 
                FROM penyerahan_stikers pen
                JOIN permintaan_produksi p ON pen.permintaan_id = p.id
                WHERE p.incoming_id = inc.id
            ), 0) AS total_keluar,
            (inc.jumlah - COALESCE((
                SELECT SUM(pen.jumlah_diserahkan) 
                FROM penyerahan_stikers pen
                JOIN permintaan_produksi p ON pen.permintaan_id = p.id
                WHERE p.incoming_id = inc.id
            ), 0)) AS sisa_stok_gudang
        FROM incoming_stikers inc
        ORDER BY inc.id DESC
    ''')
    stok_gudang_items = [dict(row) for row in cursor.fetchall()]

    # 2. Data Stok Produksi
    master_stiker = cursor.execute('''
        SELECT id, buyer, sku, nama_produk, jenis_stiker, no_sas_sap, COALESCE(saldo_awal, 0) as saldo_awal
        FROM master_produk
        ORDER BY buyer ASC, nama_produk ASC, jenis_stiker ASC
    ''').fetchall()

    laporan_produksi = []
    for s in master_stiker:
        sku = s['sku']
        jenis = s['jenis_stiker']

        serah_row = cursor.execute('''
            SELECT COALESCE(SUM(pen.jumlah_diserahkan), 0) as total
            FROM penyerahan_stikers pen
            JOIN permintaan_produksi p ON pen.permintaan_id = p.id
            WHERE p.sku = ? AND p.jenis_stiker = ?
        ''', (sku, jenis)).fetchone()
        total_terima_produksi = serah_row['total'] if serah_row else 0

        pakai_row = cursor.execute('''
            SELECT COALESCE(SUM(total_digunakan), 0) as total,
                   COALESCE(SUM(jumlah_digunakan), 0) as digunakan,
                   COALESCE(SUM(jumlah_bs), 0) as bs
            FROM pemakaian_stikers
            WHERE sku = ? AND jenis_stiker = ? AND strftime('%Y-%m', tanggal) = ?
        ''', (sku, jenis, periode)).fetchone()

        pakai_all = cursor.execute('''
            SELECT COALESCE(SUM(total_digunakan), 0) as total
            FROM pemakaian_stikers
            WHERE sku = ? AND jenis_stiker = ?
        ''', (sku, jenis)).fetchone()
        total_pakai_all = pakai_all['total'] if pakai_all else 0

        stok_fisik_produksi = max(0, (s['saldo_awal'] or 0) + total_terima_produksi - total_pakai_all)

        laporan_produksi.append({
            'id': s['id'],
            'buyer': s['buyer'],
            'sku': s['sku'],
            'nama_produk': s['nama_produk'],
            'jenis_stiker': s['jenis_stiker'],
            'no_sas_sap': s['no_sas_sap'],
            'stok_produksi': stok_fisik_produksi,
            'total_pemakaian': pakai_row['total'] if pakai_row else 0,
            'pakai_mesin': pakai_row['digunakan'] if pakai_row else 0,
            'bs_mesin': pakai_row['bs'] if pakai_row else 0
        })

    conn.close()
    return render_template('ppic_monitoring.html',
                           stok_gudang=stok_gudang_items,
                           laporan=laporan_produksi,
                           bulan_aktif=bulan,
                           tahun_aktif=tahun)

@app.route('/ppic/master/save', methods=['POST'])
@login_required(role_required='ppic')
def ppic_master_save():
    stiker_id = request.form.get('stiker_id')
    buyer = request.form.get('buyer', '').strip().upper()
    sku = request.form.get('sku', '').strip()
    nama_produk = request.form.get('nama_produk', '').strip()
    jenis_stiker = request.form.get('jenis_stiker', '').strip().upper()
    no_sas_sap = request.form.get('no_sas_sap', '').strip()

    conn = get_db()
    cursor = conn.cursor()

    if not no_sas_sap:
        rows = cursor.execute("SELECT no_sas_sap FROM master_produk WHERE no_sas_sap LIKE '3000%'").fetchall()
        max_sas = 3000000000
        for r in rows:
            val = str(r['no_sas_sap']).strip()
            if val.isdigit():
                num = int(val)
                if 3000000000 <= num < 4000000000 and num > max_sas:
                    max_sas = num
        no_sas_sap = str(max_sas + 1)

    if stiker_id:
        cursor.execute('''
            UPDATE master_produk
            SET buyer = ?, sku = ?, nama_produk = ?, jenis_stiker = ?, no_sas_sap = ?
            WHERE id = ?
        ''', (buyer, sku, nama_produk, jenis_stiker, no_sas_sap, stiker_id))

        cursor.execute('''
            UPDATE permintaan_produksi
            SET no_sas_sap = ?
            WHERE sku = ? AND jenis_stiker = ?
        ''', (no_sas_sap, sku, jenis_stiker))

        flash(f'Master No. SAS/SAP untuk {nama_produk} berhasil diperbarui ({no_sas_sap}).', 'success')
    else:
        cursor.execute('''
            INSERT INTO master_produk (buyer, sku, nama_produk, jenis_stiker, no_sas_sap)
            VALUES (?, ?, ?, ?, ?)
        ''', (buyer, sku, nama_produk, jenis_stiker, no_sas_sap))
        flash(f'Data stiker baru berhasil ditambahkan dengan No. SAS otomatis: {no_sas_sap}.', 'success')

    conn.commit()
    conn.close()
    return redirect(url_for('ppic_monitoring'))

@app.route('/ppic/master/delete/<int:stiker_id>', methods=['POST'])
@login_required(role_required='ppic')
def ppic_master_delete(stiker_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM master_produk WHERE id = ?', (stiker_id,))
    conn.commit()
    conn.close()
    flash('Data master produk & jenis stiker berhasil dihapus.', 'warning')
    return redirect(url_for('ppic_monitoring'))

@app.route('/ppic/export-excel/gudang')
@login_required(role_required='ppic')
def ppic_export_excel_gudang():
    from openpyxl import Workbook  # type: ignore[import-not-found]
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side  # type: ignore[import-not-found]
    from openpyxl.drawing.image import Image as XLImage # type: ignore[import-not-found]

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT 
            inc.id,
            inc.no_qan,
            inc.tanggal_datang,
            inc.supplier,
            inc.nama_barang,
            inc.no_sap,
            inc.jumlah AS total_incoming,
            inc.foto_bukti,
            COALESCE((
                SELECT SUM(pen.jumlah_diserahkan) 
                FROM penyerahan_stikers pen
                JOIN permintaan_produksi p ON pen.permintaan_id = p.id
                WHERE p.incoming_id = inc.id
            ), 0) AS total_keluar
        FROM incoming_stikers inc
        ORDER BY inc.id DESC
    ''')
    rows = cursor.fetchall()
    conn.close()

    wb = Workbook()
    ws = wb.active
    ws.title = "Laporan Stok Gudang"

    ws.merge_cells('A1:G1')
    ws['A1'] = "LAPORAN STOK STIKER (GUDANG) - PPIC"
    ws['A1'].font = Font(name='Calibri', size=13, bold=True)
    ws['A1'].alignment = Alignment(horizontal='center', vertical='center')
    ws.row_dimensions[1].height = 24

    ws.merge_cells('A2:G2')
    ws['A2'] = f"Dicetak Tanggal: {datetime.now().strftime('%d-%m-%Y %H:%M')} | Petugas: {session.get('nama_lengkap', 'PPIC')}"
    ws['A2'].font = Font(name='Calibri', size=10, italic=True)
    ws['A2'].alignment = Alignment(horizontal='center', vertical='center')
    ws.row_dimensions[2].height = 18

    headers = ['NO', 'FOTO', 'NAMA SUPPLIER', 'NAMA BARANG', 'NO. SAP', 'NO. QAN', 'STOK DI GUDANG (PCS)']
    thin_side = Side(border_style='thin', color='000000')
    border_all = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)
    header_fill = PatternFill(start_color='F1F5F9', end_color='F1F5F9', fill_type='solid')

    ws.row_dimensions[4].height = 25
    for col_idx, text in enumerate(headers, start=1):
        c = ws.cell(row=4, column=col_idx, value=text)
        c.font = Font(name='Calibri', size=10, bold=True)
        c.fill = header_fill
        c.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        c.border = border_all

    row_num = 5
    for idx, r in enumerate(rows, start=1):
        sisa = max(0, (r['total_incoming'] or 0) - (r['total_keluar'] or 0))
        stok_label = f"{sisa:,} PCS" if sisa > 0 else "0 PCS (HABIS)"

        values = [
            idx,
            '-',
            r['supplier'] or '-',
            r['nama_barang'] or '-',
            r['no_sap'] or '-',
            r['no_qan'] or '-',
            stok_label
        ]

        for col_idx, val in enumerate(values, start=1):
            c = ws.cell(row=row_num, column=col_idx, value=val)
            c.font = Font(name='Calibri', size=10, bold=(col_idx in [3, 7]))
            c.border = border_all
            if col_idx == 4:
                c.alignment = Alignment(horizontal='left', vertical='center', wrap_text=True)
            else:
                c.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)

        gambar_ada = False
        if r['foto_bukti']:
            img_path = os.path.join(UPLOAD_FOLDER, r['foto_bukti'])
            if os.path.exists(img_path):
                try:
                    ws.cell(row=row_num, column=2, value='')
                    img = XLImage(img_path)
                    max_w, max_h = 135, 70
                    ratio = min(max_w / img.width, max_h / img.height)
                    img.width = int(img.width * ratio)
                    img.height = int(img.height * ratio)
                    ws.add_image(img, f'B{row_num}')
                    ws.row_dimensions[row_num].height = 60
                    gambar_ada = True
                except Exception:
                    pass

        if not gambar_ada:
            ws.row_dimensions[row_num].height = 26

        row_num += 1

    col_widths = {'A': 6, 'B': 22, 'C': 20, 'D': 45, 'E': 16, 'F': 14, 'G': 22}
    for col_letter, width in col_widths.items():
        ws.column_dimensions[col_letter].width = width

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    nama_file = f"Laporan_Stok_Gudang_PPIC_{datetime.now().strftime('%d-%m-%Y')}.xlsx"
    return send_file(
        output,
        as_attachment=True,
        download_name=nama_file,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )

@app.route('/ppic/export-excel/produksi')
@login_required(role_required='ppic')
def ppic_export_excel_produksi():
    from openpyxl import Workbook # type: ignore[import-not-found]
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side # type: ignore[import-not-found]

    conn = get_db()
    cursor = conn.cursor()

    master_stiker = cursor.execute('''
        SELECT id, buyer, sku, nama_produk, jenis_stiker, no_sas_sap, COALESCE(saldo_awal, 0) as saldo_awal
        FROM master_produk
        ORDER BY buyer ASC, nama_produk ASC, jenis_stiker ASC
    ''').fetchall()

    wb = Workbook()
    ws = wb.active
    ws.title = "Laporan Stok Produksi"

    ws.merge_cells('A1:G1')
    ws['A1'] = "LAPORAN STOK STIKER (PRODUKSI) - PPIC"
    ws['A1'].font = Font(name='Calibri', size=13, bold=True)
    ws['A1'].alignment = Alignment(horizontal='center', vertical='center')
    ws.row_dimensions[1].height = 24

    ws.merge_cells('A2:G2')
    ws['A2'] = f"Dicetak Tanggal: {datetime.now().strftime('%d-%m-%Y %H:%M')} | Petugas: {session.get('nama_lengkap', 'PPIC')}"
    ws['A2'].font = Font(name='Calibri', size=10, italic=True)
    ws['A2'].alignment = Alignment(horizontal='center', vertical='center')
    ws.row_dimensions[2].height = 18

    headers = ['NO', 'BUYER', 'SKU', 'NAMA PRODUK', 'JENIS STIKER', 'NO. SAS/SAP', 'SISA STOK PRODUKSI']
    thin_side = Side(border_style='thin', color='000000')
    border_all = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)
    header_fill = PatternFill(start_color='F1F5F9', end_color='F1F5F9', fill_type='solid')

    ws.row_dimensions[4].height = 25
    for col_idx, text in enumerate(headers, start=1):
        c = ws.cell(row=4, column=col_idx, value=text)
        c.font = Font(name='Calibri', size=10, bold=True)
        c.fill = header_fill
        c.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        c.border = border_all

    row_num = 5
    nomor = 1
    for s in master_stiker:
        sku = s['sku']
        jenis = s['jenis_stiker']

        serah_row = cursor.execute('''
            SELECT COALESCE(SUM(pen.jumlah_diserahkan), 0) as total
            FROM penyerahan_stikers pen
            JOIN permintaan_produksi p ON pen.permintaan_id = p.id
            WHERE p.sku = ? AND p.jenis_stiker = ?
        ''', (sku, jenis)).fetchone()
        total_terima = serah_row['total'] if serah_row else 0

        pakai_all = cursor.execute('''
            SELECT COALESCE(SUM(total_digunakan), 0) as total
            FROM pemakaian_stikers
            WHERE sku = ? AND jenis_stiker = ?
        ''', (sku, jenis)).fetchone()
        total_pakai = pakai_all['total'] if pakai_all else 0

        sisa_stok = max(0, (s['saldo_awal'] or 0) + total_terima - total_pakai)

        if sisa_stok <= 0:
            continue

        stok_label = f"{sisa_stok:,} PCS"

        values = [
            nomor,
            s['buyer'] or '-',
            sku or '-',
            s['nama_produk'] or '-',
            jenis or '-',
            s['no_sas_sap'] or '-',
            stok_label
        ]

        for col_idx, val in enumerate(values, start=1):
            c = ws.cell(row=row_num, column=col_idx, value=val)
            c.font = Font(name='Calibri', size=10, bold=(col_idx in [2, 7]))
            c.border = border_all
            if col_idx == 4:
                c.alignment = Alignment(horizontal='left', vertical='center', wrap_text=True)
            else:
                c.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)

        ws.row_dimensions[row_num].height = 24
        row_num += 1
        nomor += 1

    conn.close()

    col_widths = {'A': 6, 'B': 24, 'C': 12, 'D': 45, 'E': 22, 'F': 16, 'G': 22}
    for col_letter, width in col_widths.items():
        ws.column_dimensions[col_letter].width = width

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    nama_file = f"Laporan_Stok_Produksi_PPIC_{datetime.now().strftime('%d-%m-%Y')}.xlsx"
    return send_file(
        output,
        as_attachment=True,
        download_name=nama_file,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )

@app.route('/api/ppic/detail_transaksi/<sku>/<path:jenis_stiker>')
@login_required()
def api_ppic_detail(sku, jenis_stiker):
    conn = get_db()
    cursor = conn.cursor()

    master = cursor.execute('SELECT no_sas_sap FROM master_produk WHERE sku = ? AND jenis_stiker = ?', (sku, jenis_stiker)).fetchone()
    no_sas_sap = master['no_sas_sap'] if master else ''

    kedatangan = cursor.execute('''
        SELECT waktu_input AS waktu, tanggal_datang, no_qan, supplier AS penyerah, diinput_oleh AS penerima, jumlah
        FROM incoming_stikers
        WHERE no_sap = ? OR nama_barang LIKE ? OR no_qan IN (
            SELECT no_qan FROM permintaan_produksi WHERE sku = ? AND jenis_stiker = ?
        )
        ORDER BY id ASC
    ''', (no_sas_sap, f"%{sku}%", sku, jenis_stiker)).fetchall()

    pengiriman = cursor.execute('''
        SELECT pen.waktu_penyerahan AS waktu, p.no_qan, pen.petugas_gudang AS penyerah, p.pengaju AS penerima, pen.jumlah_diserahkan AS jumlah
        FROM penyerahan_stikers pen
        JOIN permintaan_produksi p ON pen.permintaan_id = p.id
        WHERE p.sku = ? AND p.jenis_stiker = ?
        ORDER BY pen.id ASC
    ''', (sku, jenis_stiker)).fetchall()

    pemakaian = cursor.execute('''
        SELECT tanggal, waktu, shift, jumlah_digunakan, jumlah_bs, total_digunakan, petugas_produksi
        FROM pemakaian_stikers
        WHERE sku = ? AND jenis_stiker = ?
        ORDER BY id DESC
    ''', (sku, jenis_stiker)).fetchall()

    conn.close()
    return jsonify({
        'kedatangan': [dict(k) for k in kedatangan],
        'pengiriman': [dict(p) for p in pengiriman],
        'pemakaian': [dict(m) for m in pemakaian]
    })

# ========================================================
# UNDUH KARTU STOCK JALUR
# ========================================================
@app.route('/stok/kartu-jalur/<int:incoming_id>')
def cetak_kartu_jalur(incoming_id):
    if 'username' not in session:
        return redirect(url_for('login'))

    conn = get_db()
    cursor = conn.cursor()

    incoming = cursor.execute('SELECT * FROM incoming_stikers WHERE id = ?', (incoming_id,)).fetchone()
    if not incoming:
        conn.close()
        flash('Data stiker tidak ditemukan!', 'danger')
        return redirect(url_for('stok_katalog'))

    pengiriman_rows = cursor.execute('''
        SELECT pen.waktu_penyerahan, pen.jumlah_diserahkan
        FROM penyerahan_stikers pen
        JOIN permintaan_produksi p ON pen.permintaan_id = p.id
        WHERE p.incoming_id = ?
        ORDER BY pen.id ASC
    ''', (incoming_id,)).fetchall()
    conn.close()

    template_path = os.path.join(BASE_DIR, 'template_kartu_stock_jalur.xlsx')
    if not os.path.exists(template_path):
        flash('File template_kartu_stock_jalur.xlsx belum ada di folder website_stok_stiker!', 'danger')
        return redirect(url_for('stok_katalog'))

    try:
        tgl_datang_formatted = str(incoming['tanggal_datang'])
        periode_str = '-'
        try:
            tgl_obj = datetime.strptime(incoming['tanggal_datang'], '%Y-%m-%d')
            tgl_datang_formatted = tgl_obj.strftime('%d-%m-%Y')

            nama_bulan_id = {
                '01': 'Januari', '02': 'Februari', '03': 'Maret', '04': 'April',
                '05': 'Mei', '06': 'Juni', '07': 'Juli', '08': 'Agustus',
                '09': 'September', '10': 'Oktober', '11': 'November', '12': 'Desember'
            }
            periode_str = f"{nama_bulan_id.get(tgl_obj.strftime('%m'), '')} {tgl_obj.strftime('%Y')}"
        except Exception:
            pass

        cell_updates = {
            'D8': f": {incoming['nama_barang'] or '-'}",
            'D9': f": {incoming['no_sap'] or '-'}",
            'D10': f": {periode_str}",
            'B14': tgl_datang_formatted,
            'C14': str(incoming['no_qan']),
            'E14': str(incoming['supplier']),
            'F14': f"{incoming['jumlah']:,}"
        }

        col_letters = ['H', 'I', 'J', 'K', 'L', 'M', 'N', 'O', 'P']
        sisa_stok = incoming['jumlah']

        for idx, p in enumerate(pengiriman_rows):
            if idx >= 9:
                break
            sisa_stok -= p['jumlah_diserahkan']
            col = col_letters[idx]

            tgl_k = str(p['waktu_penyerahan'])[:10]
            try:
                tgl_dt = datetime.strptime(tgl_k, '%Y-%m-%d')
                tgl_k = tgl_dt.strftime('%d/%m/%y')
            except Exception:
                pass

            cell_updates[f'{col}14'] = tgl_k
            cell_updates[f'{col}15'] = f"{p['jumlah_diserahkan']:,}"
            cell_updates[f'{col}16'] = f"{max(0, sisa_stok):,}"

        output_buffer = io.BytesIO()
        with zipfile.ZipFile(template_path, 'r') as zin:
            with zipfile.ZipFile(output_buffer, 'w', compression=zipfile.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    content = zin.read(item.filename)

                    if item.filename == 'xl/styles.xml':
                        ns_styles = 'http://purl.oclc.org/ooxml/spreadsheetml/main'
                        ET.register_namespace('', ns_styles)
                        root_styles = ET.fromstring(content)
                        cellXfs = root_styles.find('{*}cellXfs')

                        if cellXfs is not None:
                            xfs_list = list(cellXfs.findall('{*}xf'))
                            for s_idx in [7, 8, 12, 36, 37, 38]:
                                if s_idx < len(xfs_list):
                                    xf = xfs_list[s_idx]
                                    xf.attrib['applyAlignment'] = '1'
                                    align = xf.find('{*}alignment')
                                    if align is None:
                                        align = ET.SubElement(xf, f"{{{ns_styles}}}alignment")
                                    align.attrib['horizontal'] = 'center'
                                    align.attrib['vertical'] = 'center'

                        content = ET.tostring(root_styles, encoding='utf-8', xml_declaration=True)

                    elif item.filename == 'xl/worksheets/sheet1.xml':
                        ns_sheet = 'http://purl.oclc.org/ooxml/spreadsheetml/main'
                        ET.register_namespace('', ns_sheet)
                        root_sheet = ET.fromstring(content)

                        sheetData = None
                        for elem in root_sheet.iter():
                            if elem.tag.endswith('sheetData'):
                                sheetData = elem
                                break

                        if sheetData is not None:
                            for ref, val in cell_updates.items():
                                row_idx = ''.join(filter(str.isdigit, ref))
                                row_elem = None
                                for r in sheetData:
                                    if r.attrib.get('r') == row_idx:
                                        row_elem = r
                                        break

                                if row_elem is not None:
                                    cell_elem = None
                                    for c in row_elem:
                                        if c.attrib.get('r') == ref:
                                            cell_elem = c
                                            break

                                    if cell_elem is None:
                                        cell_elem = ET.SubElement(row_elem, f"{{{ns_sheet}}}c", {'r': ref})

                                    cell_elem.attrib['t'] = 'inlineStr'
                                    for child in list(cell_elem):
                                        cell_elem.remove(child)

                                    is_elem = ET.SubElement(cell_elem, f"{{{ns_sheet}}}is")
                                    t_elem = ET.SubElement(is_elem, f"{{{ns_sheet}}}t")
                                    t_elem.text = str(val)

                        content = ET.tostring(root_sheet, encoding='utf-8', xml_declaration=True)

                    zout.writestr(item, content)

        output_buffer.seek(0)
        nama_file_unduh = f"Kartu_Stock_Jalur_{incoming['no_qan'].replace(' ', '_')}.xlsx"

        return send_file(
            output_buffer,
            as_attachment=True,
            download_name=nama_file_unduh,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )

    except Exception as e:
        print(f"Error memproses file Excel: {e}")
        flash(f"Terjadi kesalahan saat memproses file Excel: {e}", "danger")
        return redirect(url_for('stok_katalog'))

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)