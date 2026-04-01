"""
RHI Magnesita — Forklift Süreç Simülasyonu
Flask backend + SQLite veritabanı
"""

import sqlite3
import os
import json
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "forklift.db")

# ─── Database Setup ────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Veritabanını oluştur ve başlangıç verilerini ekle."""
    conn = get_db()
    cur = conn.cursor()

    # Vardiya tablosu
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vardiya (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            baslangic TEXT NOT NULL DEFAULT '08:00',
            bitis TEXT NOT NULL DEFAULT '16:00'
        )
    """)

    # Mola tablosu
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mola (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ad TEXT NOT NULL,
            baslangic TEXT NOT NULL,
            bitis TEXT NOT NULL
        )
    """)

    # Forklift tablosu
    cur.execute("""
        CREATE TABLE IF NOT EXISTS forklift (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ad TEXT NOT NULL,
            bolge TEXT NOT NULL
        )
    """)

    # Faaliyet tablosu — min/max destekli şema
    cur.execute("""
        CREATE TABLE IF NOT EXISTS faaliyet (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            forklift_id INTEGER NOT NULL,
            ad TEXT NOT NULL,
            cevrim_suresi REAL,
            cevrim_min REAL,
            cevrim_max REAL,
            tekrar_suresi REAL,
            tekrar_birimi TEXT DEFAULT 'dk',
            oncelik TEXT CHECK(oncelik IN ('yuksek','normal','dusuk')),
            gecikme_toleransi REAL,
            gecikme_birimi TEXT DEFAULT 'dk',
            gece_vardiyasi INTEGER DEFAULT 0,
            poisson_mode INTEGER DEFAULT 0,
            FOREIGN KEY (forklift_id) REFERENCES forklift(id) ON DELETE CASCADE
        )
    """)

    # TIR yapılandırması (Poisson dağılımı)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tir_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lambda_saat REAL DEFAULT 1.46,
            ort_gunluk INTEGER DEFAULT 12,
            min_gunluk INTEGER DEFAULT 5,
            max_gunluk INTEGER DEFAULT 20,
            f1_paketleme_orani REAL DEFAULT 80
        )
    """)
    
    try:
        cur.execute("ALTER TABLE tir_config ADD COLUMN f1_paketleme_orani REAL DEFAULT 80")
    except sqlite3.OperationalError:
        pass

    # Başlangıç verileri — sadece boşsa ekle
    if cur.execute("SELECT COUNT(*) FROM forklift").fetchone()[0] == 0:
        # Vardiya
        cur.execute("INSERT INTO vardiya (baslangic, bitis) VALUES ('08:00','16:00')")

        # Molalar
        molalar = [
            ("1. Çay Molası", "09:45", "10:00"),
            ("Öğle Arası", "11:30", "12:00"),
            ("2. Çay Molası", "13:45", "14:00"),
        ]
        cur.executemany("INSERT INTO mola (ad, baslangic, bitis) VALUES (?,?,?)", molalar)

        # Forkliftler
        cur.execute("INSERT INTO forklift (ad, bolge) VALUES ('Forklift 1','Tuğla Boşaltma')")
        cur.execute("INSERT INTO forklift (ad, bolge) VALUES ('Forklift 2','Paketleme')")
        cur.execute("INSERT INTO forklift (ad, bolge) VALUES ('Forklift 3','Sevkiyat')")

        # ── Forklift 1 — Tuğla Boşaltma ──
        #  (fk_id, ad, cevrim_ort, cevrim_min, cevrim_max, tekrar, t_birim, oncelik, gec_tol, g_birim, gece, poisson)
        f1 = [
            (1, "Tuğla Dolu Paleti Alma",  1,  0.5, 3,   4,  "dk",   "yuksek", 30,  "dk",   0, 1),
            (1, "Boş Palet Alma",           1,  0.5, 3,   2,  "saat", "normal", 30,  "dk",   1, 0),
            (1, "Iskarta Boşaltma",         7,  5,   10,  48, "saat", "dusuk",  24,  "saat", 1, 0),
        ]

        # ── Forklift 2 — Paketleme ──
        f2 = [
            (2, "Tuğla Boşaltmadan Makineye", 1,   0.5, 2,  8,  "dk",  "yuksek", 24, "dk",  0, 1),
            (2, "Tuğla Boşaltmadan Alana",    1,   0.5, 2,  8,  "dk",  "yuksek", 24, "dk",  0, 1),
            (2, "Makinadan Stoğa",            1.5, 0.5, 3,  40, "dk",  "yuksek", 24, "dk",  0, 0),
            (2, "Alandan Stoğa",              1.5, 0.5, 3,  30, "dk",  "dusuk",  5,  "saat",0, 0),
        ]

        # ── Forklift 3 — Sevkiyat (Poisson mode) ──
        f3 = [
            (3, "Stoktaki tuğla dolu paletlerin TIR'a yüklenmesi", 30, 20, 40, 1, "saat", "yuksek", 1, "saat", 0, 1),
        ]

        all_fa = f1 + f2 + f3
        cur.executemany("""
            INSERT INTO faaliyet
            (forklift_id, ad, cevrim_suresi, cevrim_min, cevrim_max, tekrar_suresi, tekrar_birimi,
             oncelik, gecikme_toleransi, gecikme_birimi, gece_vardiyasi, poisson_mode)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, all_fa)

        # TIR Poisson yapılandırması ve Paketleme Oranı
        cur.execute("INSERT INTO tir_config (lambda_saat, ort_gunluk, min_gunluk, max_gunluk, f1_paketleme_orani) VALUES (1.46, 12, 5, 20, 80)")

    conn.commit()
    conn.close()


# ─── Helpers ───────────────────────────────────────────────────

def sure_to_dk(val, birim):
    """Saat/dk değerini dakikaya çevir."""
    if val is None:
        return None
    if birim == "saat":
        return val * 60
    return val  # dk


def dk_to_display(dk):
    """Dakikayı okunabilir string'e çevir."""
    if dk is None:
        return "—"
    if dk >= 60:
        saat = dk / 60
        if saat == int(saat):
            return f"{int(saat)} saat"
        return f"{saat:.1f} saat"
    return f"{dk:.0f} dk" if dk == int(dk) else f"{dk:.1f} dk"


# ─── API Routes ────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/vardiya", methods=["GET"])
def get_vardiya():
    conn = get_db()
    vardiya = conn.execute("SELECT * FROM vardiya LIMIT 1").fetchone()
    molalar = conn.execute("SELECT * FROM mola ORDER BY baslangic").fetchall()
    conn.close()
    return jsonify({
        "vardiya": dict(vardiya) if vardiya else {},
        "molalar": [dict(m) for m in molalar]
    })


@app.route("/api/vardiya", methods=["PUT"])
def update_vardiya():
    data = request.json
    conn = get_db()
    conn.execute("UPDATE vardiya SET baslangic=?, bitis=? WHERE id=1",
                 (data["baslangic"], data["bitis"]))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/molalar", methods=["PUT"])
def update_molalar():
    data = request.json
    conn = get_db()
    for m in data:
        conn.execute("UPDATE mola SET ad=?, baslangic=?, bitis=? WHERE id=?",
                     (m["ad"], m["baslangic"], m["bitis"], m["id"]))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/config", methods=["GET"])
def get_config():
    conn = get_db()
    tir_row = conn.execute("SELECT * FROM tir_config LIMIT 1").fetchone()
    conn.close()
    return jsonify(dict(tir_row) if tir_row else {})

@app.route("/api/config", methods=["PUT"])
def update_config():
    data = request.json
    conn = get_db()
    conn.execute("""
        UPDATE tir_config
        SET f1_paketleme_orani=?
        WHERE id=1
    """, (data.get("f1_paketleme_orani", 80),))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/forkliftler", methods=["GET"])
def get_forkliftler():
    conn = get_db()
    forkliftler = conn.execute("SELECT * FROM forklift ORDER BY id").fetchall()
    result = []
    for f in forkliftler:
        faaliyetler = conn.execute(
            "SELECT * FROM faaliyet WHERE forklift_id=? ORDER BY id", (f["id"],)
        ).fetchall()
        result.append({
            **dict(f),
            "faaliyetler": [dict(fa) for fa in faaliyetler]
        })
    conn.close()
    return jsonify(result)


@app.route("/api/faaliyet/<int:faaliyet_id>", methods=["PUT"])
def update_faaliyet(faaliyet_id):
    data = request.json
    conn = get_db()
    conn.execute("""
        UPDATE faaliyet
        SET ad=?, cevrim_suresi=?, cevrim_min=?, cevrim_max=?,
            tekrar_suresi=?, tekrar_birimi=?,
            oncelik=?, gecikme_toleransi=?, gecikme_birimi=?,
            gece_vardiyasi=?, poisson_mode=?
        WHERE id=?
    """, (
        data.get("ad"),
        data.get("cevrim_suresi"),
        data.get("cevrim_min"),
        data.get("cevrim_max"),
        data.get("tekrar_suresi"),
        data.get("tekrar_birimi", "dk"),
        data.get("oncelik"),
        data.get("gecikme_toleransi"),
        data.get("gecikme_birimi", "dk"),
        1 if data.get("gece_vardiyasi") else 0,
        1 if data.get("poisson_mode") else 0,
        faaliyet_id
    ))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/faaliyet", methods=["POST"])
def add_faaliyet():
    data = request.json
    conn = get_db()
    cur = conn.execute("""
        INSERT INTO faaliyet
        (forklift_id, ad, cevrim_suresi, tekrar_suresi, tekrar_birimi,
         oncelik, gecikme_toleransi, gecikme_birimi, gece_vardiyasi)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (
        data["forklift_id"], data["ad"],
        data.get("cevrim_suresi"),
        data.get("tekrar_suresi"),
        data.get("tekrar_birimi", "dk"),
        data.get("oncelik"),
        data.get("gecikme_toleransi"),
        data.get("gecikme_birimi", "dk"),
        1 if data.get("gece_vardiyasi") else 0
    ))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return jsonify({"ok": True, "id": new_id})


@app.route("/api/faaliyet/<int:faaliyet_id>", methods=["DELETE"])
def delete_faaliyet(faaliyet_id):
    conn = get_db()
    conn.execute("DELETE FROM faaliyet WHERE id=?", (faaliyet_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/export", methods=["GET"])
def export_data():
    """Tüm veriyi JSON olarak dışa aktar (simülasyon için)."""
    conn = get_db()
    vardiya = dict(conn.execute("SELECT * FROM vardiya LIMIT 1").fetchone())
    molalar = [dict(m) for m in conn.execute("SELECT * FROM mola ORDER BY baslangic").fetchall()]
    forkliftler = conn.execute("SELECT * FROM forklift ORDER BY id").fetchall()

    result = {
        "vardiya": vardiya,
        "molalar": molalar,
        "forkliftler": []
    }

    for f in forkliftler:
        faaliyetler = conn.execute(
            "SELECT * FROM faaliyet WHERE forklift_id=? ORDER BY id", (f["id"],)
        ).fetchall()
        fa_list = []
        for fa in faaliyetler:
            fa_dict = dict(fa)
            # Dakika cinsinden hesaplanmış değerleri de ekle
            fa_dict["tekrar_suresi_dk"] = sure_to_dk(fa["tekrar_suresi"], fa["tekrar_birimi"])
            fa_dict["gecikme_toleransi_dk"] = sure_to_dk(fa["gecikme_toleransi"], fa["gecikme_birimi"])
            fa_list.append(fa_dict)

        result["forkliftler"].append({
            **dict(f),
            "faaliyetler": fa_list
        })

    conn.close()
    return jsonify(result)


# ─── Simulation Routes ─────────────────────────────────────────

@app.route("/timeline")
def timeline_page():
    return render_template("timeline.html")


@app.route("/timeline-simpy")
def timeline_simpy_page():
    return render_template("timeline_simpy.html")


@app.route("/replications")
def replications_page():
    return render_template("replications.html")


@app.route("/api/simulate", methods=["GET"])
def api_simulate():
    """Deterministik simülasyonu çalıştır."""
    from simulation import load_data, run_scenarios, summary_table
    data = load_data()
    scenarios = run_scenarios(data)
    summary = summary_table(scenarios)

    return jsonify({
        "scenarios": scenarios,
        "summary": summary,
        "vardiya": data["vardiya"],
        "molalar": data["molalar"],
    })


@app.route("/api/simulate-simpy", methods=["GET"])
def api_simulate_simpy():
    """SimPy stokastik simülasyonu çalıştır."""
    from simulation_simpy import load_data, run_simpy_scenarios, summary_table
    n_reps = request.args.get("n", 50, type=int)
    n_reps = min(max(n_reps, 10), 200)  # 10-200 arası
    data = load_data()
    scenarios = run_simpy_scenarios(data, n_reps=n_reps)
    summary = summary_table(scenarios)

    return jsonify({
        "scenarios": scenarios,
        "summary": summary,
        "vardiya": data["vardiya"],
        "molalar": data["molalar"],
        "n_reps": n_reps,
    })


@app.route("/sensitivity")
def sensitivity_page():
    return render_template("sensitivity.html")


@app.route("/api/sensitivity", methods=["POST"])
def api_sensitivity():
    """Tolerans duyarlılık analizi çalıştır."""
    from simulation_simpy import load_data, run_sensitivity_analysis
    payload = request.get_json()
    scenario_key = payload.get("scenario_key", "mevcut")
    tolerance_overrides = payload.get("tolerance_overrides", {})
    n_reps = min(max(payload.get("n_reps", 30), 10), 100)

    data = load_data()
    result = run_sensitivity_analysis(data, scenario_key, tolerance_overrides, n_reps=n_reps)
    return jsonify(result)


@app.route("/api/activities", methods=["GET"])
def api_activities():
    """Tüm faaliyetlerin tolerans bilgilerini döndür."""
    from simulation_simpy import load_data
    data = load_data()
    activities = []
    seen = set()
    for f in data["forkliftler"]:
        for a in f["faaliyetler"]:
            name = a["ad"]
            if name in seen:
                continue
            seen.add(name)
            tol = a.get("gecikme_toleransi", 0)
            birimi = a.get("gecikme_birimi", "dk")
            tol_dk = tol * 60 if birimi == "saat" else tol
            activities.append({
                "name": name,
                "forklift": f["ad"],
                "tolerance_dk": round(tol_dk, 1),
                "cycle_time": a.get("cevrim_suresi", 0),
                "cycle_min": a.get("cevrim_min"),
                "cycle_max": a.get("cevrim_max"),
                "priority": a.get("oncelik", "normal"),
                "poisson": bool(a.get("poisson_mode", 0)),
            })
    return jsonify(activities)


# ─── Start ─────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    print("🚜 Forklift Veri Girişi     → http://localhost:5050")
    print("📊 Deterministik Timeline   → http://localhost:5050/timeline")
    print("🎲 SimPy Simülasyon         → http://localhost:5050/timeline-simpy")
    print("🔬 Replikasyonlar           → http://localhost:5050/replications")
    print("🎯 Duyarlılık Analizi       → http://localhost:5050/sensitivity")
    app.run(debug=False, port=5050, host="0.0.0.0")
