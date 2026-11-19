import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, 'stok_stiker.db')

conn = sqlite3.connect(DB_NAME)
cursor = conn.cursor()

# 1. Kosongkan seluruh tabel transaksi percobaan & reset nomor urut ID ke 1
tabel_transaksi = [
    'incoming_stikers',
    'permintaan_produksi',
    'penyerahan_stikers',
    'pemakaian_stikers',
    'foto_stiker_produksi_list'
]

for tabel in tabel_transaksi:
    cursor.execute(f"DELETE FROM {tabel}")
    cursor.execute("DELETE FROM sqlite_sequence WHERE name = ?", (tabel,))

# 2. Reset saldo awal percobaan di master_produk menjadi 0 (daftar produk tetap utuh)
cursor.execute("""
    UPDATE master_produk 
    SET saldo_awal = 0, 
        waktu_saldo_awal = NULL, 
        petugas_saldo_awal = NULL, 
        foto_stiker_produksi = NULL
""")

conn.commit()
conn.close()
print("✅ SELESAI! Seluruh data transaksi dummy berhasil dibersihkan dan ID transaksi direset ke #1.")
print("ℹ️ Data Master Produk & Akun Login (Gudang, Produksi, PPIC) tetap aman.")