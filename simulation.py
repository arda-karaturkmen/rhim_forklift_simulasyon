"""
RHI Magnesita — Forklift Simülasyon Motoru
Discrete-event simulation: zamanlama, çakışma analizi, ortaklaştırma senaryoları
"""

import json
import sqlite3
import os
from datetime import datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "forklift.db")


# ─── Helpers ───────────────────────────────────────────────────

def hm(minutes):
    """Dakikayı HH:MM string'e çevir."""
    h = int(minutes) // 60
    m = int(minutes) % 60
    return f"{h:02d}:{m:02d}"


def parse_hm(s):
    """HH:MM string'i dakika cinsine çevir."""
    parts = s.split(":")
    return int(parts[0]) * 60 + int(parts[1])


def to_dk(val, birim):
    """Değeri dakikaya çevir."""
    if val is None:
        return None
    return val * 60 if birim == "saat" else val


# ─── Data Loading ──────────────────────────────────────────────

def load_data():
    """SQLite'dan tüm veriyi yükle."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    vardiya = dict(conn.execute("SELECT * FROM vardiya LIMIT 1").fetchone())
    molalar = [dict(m) for m in conn.execute("SELECT * FROM mola ORDER BY baslangic").fetchall()]
    forkliftler = []

    for f in conn.execute("SELECT * FROM forklift ORDER BY id").fetchall():
        faaliyetler = [dict(fa) for fa in conn.execute(
            "SELECT * FROM faaliyet WHERE forklift_id=? ORDER BY id", (f["id"],)
        ).fetchall()]
        forkliftler.append({**dict(f), "faaliyetler": faaliyetler})

    conn.close()
    return {"vardiya": vardiya, "molalar": molalar, "forkliftler": forkliftler}


# ─── Simulation Engine ────────────────────────────────────────

PRIORITY_ORDER = {"yuksek": 0, "normal": 1, "dusuk": 2, None: 3}


def is_in_break(t, cevrim, breaks):
    """t anında başlayan cevrim süreli iş herhangi bir molayla çakışıyor mu?"""
    for b_start, b_end in breaks:
        if t < b_end and (t + cevrim) > b_start:
            return True, b_end  # Çakışıyor → mola bitişini döndür
    return False, None


def break_time_between(t1, t2, breaks):
    """
    t1 ile t2 arasında geçen toplam mola süresini hesapla (dakika).
    Sadece [t1, t2] aralığına denk gelen kısımları sayar.
    """
    total = 0
    for b_start, b_end in breaks:
        # Mola [t1, t2] aralığıyla kesişiyor mu?
        overlap_start = max(t1, b_start)
        overlap_end = min(t2, b_end)
        if overlap_start < overlap_end:
            total += overlap_end - overlap_start
    return total


def effective_start(sched, breaks):
    """
    Planlanan zaman bir molanın içindeyse,
    gerçek beklenen başlangıcı mola sonrasına taşı.
    Mola dışındaysa aynen döndür.
    """
    for b_start, b_end in breaks:
        if b_start <= sched < b_end:
            return b_end
    return sched


def skip_breaks(t, breaks):
    """
    t zamanı bir molanın içindeyse, mola bitişine taşı.
    """
    for b_start, b_end in breaks:
        if b_start <= t < b_end:
            return b_end
    return t


def simulate_forklift(activities, vardiya_start, vardiya_end, breaks, label=""):
    """
    Tek bir forklift için zaman çizelgesi üret.

    Her faaliyet tekrar_suresi aralığında planlanır.
    Çakışmalar önceliğe göre geciktirilir.

    Returns:
        events: [{activity, start, end, scheduled_start, delay, priority, gecikme_tol_dk, status}]
        stats: {utilization, total_delay, max_delay, violations, idle_time}
    """
    # Tüm planlanan olayları oluştur
    planned_events = []

    for act in activities:
        cevrim = act["cevrim_suresi"]
        tekrar_dk = to_dk(act["tekrar_suresi"], act.get("tekrar_birimi", "dk"))
        gecikme_tol = to_dk(act.get("gecikme_toleransi"), act.get("gecikme_birimi", "dk"))

        if cevrim is None or tekrar_dk is None:
            continue

        # Kaç kez tekrar edecek?
        if tekrar_dk >= (vardiya_end - vardiya_start):
            # Günde 1 veya daha az → 1 kez planla
            occurrences = 1
        else:
            net_time = vardiya_end - vardiya_start
            occurrences = int(net_time / tekrar_dk)
            if occurrences < 1:
                occurrences = 1

        t = vardiya_start
        count = 0
        while count < occurrences and t + cevrim <= vardiya_end:
            # Mola kontrolü — mola içindeyse sonrasına kaydır
            in_break, break_end = is_in_break(t, cevrim, breaks)
            if in_break:
                t = break_end
                continue

            if t + cevrim > vardiya_end:
                break

            planned_events.append({
                "activity": act["ad"],
                "activity_id": act["id"],
                "scheduled_start": t,
                "cevrim": cevrim,
                "priority": act.get("oncelik"),
                "priority_order": PRIORITY_ORDER.get(act.get("oncelik"), 3),
                "gecikme_tol_dk": gecikme_tol,
                "gece": act.get("gece_vardiyasi", 0),
            })
            count += 1
            t += tekrar_dk

    # Önceliğe ve zamana göre sırala
    planned_events.sort(key=lambda x: (x["scheduled_start"], x["priority_order"]))

    # Sıralı çalıştır — çakışmaları geciktir
    events = []
    busy_until = vardiya_start
    total_delay = 0
    max_delay = 0
    violations = 0
    total_work = 0

    for evt in planned_events:
        sched = evt["scheduled_start"]
        cevrim = evt["cevrim"]

        # Forkliftin müsait olduğu zaman
        # busy_until molanın içindeyse, mola sonrasına kaydır
        busy_until = skip_breaks(busy_until, breaks)
        actual_start = max(sched, busy_until)

        # Mola kontrolü — actual_start moladaysa sonrasına kaydır
        in_break, brk_end = is_in_break(actual_start, cevrim, breaks)
        while in_break:
            actual_start = brk_end
            in_break, brk_end = is_in_break(actual_start, cevrim, breaks)

        # Vardiya bitti mi?
        if actual_start + cevrim > vardiya_end:
            events.append({
                "activity": evt["activity"],
                "activity_id": evt["activity_id"],
                "scheduled_start": hm(sched),
                "start": None,
                "end": None,
                "delay": None,
                "net_delay": None,
                "priority": evt["priority"],
                "gecikme_tol_dk": evt["gecikme_tol_dk"],
                "status": "missed",
                "gece": evt["gece"],
            })
            violations += 1
            continue

        # ── NET GECİKME HESABI ──
        # Brüt gecikme = actual_start - sched
        # Mola süresi = aradaki mola dakikaları
        # Net gecikme = brüt - mola süresi
        gross_delay = actual_start - sched
        break_dur = break_time_between(sched, actual_start, breaks)
        net_delay = max(0, gross_delay - break_dur)

        total_delay += net_delay
        max_delay = max(max_delay, net_delay)

        # Gecikme toleransı kontrolü — NET gecikmeye göre
        status = "ok"
        if net_delay > 0:
            if evt["gecikme_tol_dk"] is not None and net_delay > evt["gecikme_tol_dk"]:
                status = "violation"
                violations += 1
            else:
                status = "delayed"

        busy_until = actual_start + cevrim
        total_work += cevrim

        events.append({
            "activity": evt["activity"],
            "activity_id": evt["activity_id"],
            "scheduled_start": hm(sched),
            "start": hm(actual_start),
            "end": hm(actual_start + cevrim),
            "start_dk": actual_start,
            "end_dk": actual_start + cevrim,
            "delay": round(net_delay, 1),
            "gross_delay": round(gross_delay, 1),
            "break_in_delay": round(break_dur, 1),
            "priority": evt["priority"],
            "gecikme_tol_dk": evt["gecikme_tol_dk"],
            "status": status,
            "gece": evt["gece"],
        })

    net_time = vardiya_end - vardiya_start - sum(b[1] - b[0] for b in breaks)
    utilization = (total_work / net_time * 100) if net_time > 0 else 0

    stats = {
        "label": label,
        "total_events": len(planned_events),
        "completed_events": len([e for e in events if e["status"] != "missed"]),
        "total_work_dk": total_work,
        "utilization_pct": round(utilization, 1),
        "idle_dk": round(net_time - total_work, 1),
        "total_delay_dk": round(total_delay, 1),
        "max_delay_dk": round(max_delay, 1),
        "violations": violations,
        "ok_count": len([e for e in events if e["status"] == "ok"]),
        "delayed_count": len([e for e in events if e["status"] == "delayed"]),
        "violation_count": len([e for e in events if e["status"] == "violation"]),
        "missed_count": len([e for e in events if e["status"] == "missed"]),
    }

    return events, stats


# ─── Scenarios ─────────────────────────────────────────────────

def run_scenarios(data, exclude_night=False):
    """
    Tüm senaryoları çalıştır:
    1. Mevcut Durum (3 forklift, her biri kendi işini yapar)
    2. F1+F2 birleşim (Forklift 1 ve 2'nin işlerini tek forklift yapar)
    3. F1+F3 birleşim
    4. F2+F3 birleşim
    5. Gece vardiyası delegasyonu (gece=True olanlar çıkarılır)
    """
    v_start = parse_hm(data["vardiya"]["baslangic"])
    v_end = parse_hm(data["vardiya"]["bitis"])
    breaks = [(parse_hm(m["baslangic"]), parse_hm(m["bitis"])) for m in data["molalar"]]

    forkliftler = data["forkliftler"]

    def get_activities(forklift_ids, exclude_night_acts=False):
        acts = []
        for f in forkliftler:
            if f["id"] in forklift_ids:
                for a in f["faaliyetler"]:
                    if exclude_night_acts and a.get("gece_vardiyasi"):
                        continue
                    acts.append(a)
        return acts

    scenarios = {}

    # ── Senaryo 1: Mevcut Durum ──
    s1_events = {}
    s1_stats = {}
    for f in forkliftler:
        acts = get_activities([f["id"]], exclude_night)
        evts, st = simulate_forklift(acts, v_start, v_end, breaks, f["ad"])
        s1_events[f["ad"]] = evts
        s1_stats[f["ad"]] = st

    scenarios["mevcut"] = {
        "name": "Mevcut Durum (3 Forklift)",
        "description": "Her forklift sadece kendi bölgesinin işlerini yapar",
        "forklift_count": 3,
        "events": s1_events,
        "stats": s1_stats,
        "total_violations": sum(s["violations"] for s in s1_stats.values()),
        "feasible": all(s["violations"] == 0 for s in s1_stats.values()),
    }

    # ── Ortaklaştırma senaryoları ──
    merge_scenarios = [
        ("f1_f2", [1, 2], [3], "F1+F2 Birleşim",
         "Tuğla Boşaltma + Paketleme tek forklift, Sevkiyat ayrı"),
        ("f1_f3", [1, 3], [2], "F1+F3 Birleşim",
         "Tuğla Boşaltma + Sevkiyat tek forklift, Paketleme ayrı"),
        ("f2_f3", [2, 3], [1], "F2+F3 Birleşim",
         "Paketleme + Sevkiyat tek forklift, Tuğla Boşaltma ayrı"),
    ]

    for key, merged_ids, solo_ids, name, desc in merge_scenarios:
        events = {}
        stats = {}

        # Birleşen forklift
        merged_acts = get_activities(merged_ids, exclude_night)
        merged_names = "+".join(f["ad"] for f in forkliftler if f["id"] in merged_ids)
        evts, st = simulate_forklift(merged_acts, v_start, v_end, breaks, merged_names)
        events[merged_names] = evts
        stats[merged_names] = st

        # Ayrı kalan forklift(ler)
        for sid in solo_ids:
            solo_acts = get_activities([sid], exclude_night)
            solo_name = next(f["ad"] for f in forkliftler if f["id"] == sid)
            evts, st = simulate_forklift(solo_acts, v_start, v_end, breaks, solo_name)
            events[solo_name] = evts
            stats[solo_name] = st

        total_viols = sum(s["violations"] for s in stats.values())
        scenarios[key] = {
            "name": name,
            "description": desc,
            "forklift_count": 2,
            "events": events,
            "stats": stats,
            "total_violations": total_viols,
            "feasible": total_viols == 0,
        }

    # ── Gece Vardiyası Senaryosu ──
    s_night_events = {}
    s_night_stats = {}
    night_delegated = []
    for f in forkliftler:
        acts_day = []
        for a in f["faaliyetler"]:
            if a.get("gece_vardiyasi"):
                night_delegated.append({"forklift": f["ad"], "activity": a["ad"]})
            else:
                acts_day.append(a)
        evts, st = simulate_forklift(acts_day, v_start, v_end, breaks, f["ad"])
        s_night_events[f["ad"]] = evts
        s_night_stats[f["ad"]] = st

    scenarios["gece_delegasyonu"] = {
        "name": "Gece Vardiyası Delegasyonu",
        "description": "Gece vardiyasına atanabilir faaliyetler gündüzden çıkarılır",
        "forklift_count": 3,
        "events": s_night_events,
        "stats": s_night_stats,
        "total_violations": sum(s["violations"] for s in s_night_stats.values()),
        "feasible": True,
        "night_delegated": night_delegated,
    }

    # ── Gece + Birleşim Senaryoları ──
    for key, merged_ids, solo_ids, name, desc in merge_scenarios:
        events = {}
        stats = {}

        merged_acts = get_activities(merged_ids, exclude_night_acts=True)  # gece çıkar
        merged_names = "+".join(f["ad"] for f in forkliftler if f["id"] in merged_ids)
        evts, st = simulate_forklift(merged_acts, v_start, v_end, breaks, merged_names)
        events[merged_names] = evts
        stats[merged_names] = st

        for sid in solo_ids:
            solo_acts = get_activities([sid], exclude_night_acts=True)
            solo_name = next(f["ad"] for f in forkliftler if f["id"] == sid)
            evts, st = simulate_forklift(solo_acts, v_start, v_end, breaks, solo_name)
            events[solo_name] = evts
            stats[solo_name] = st

        total_viols = sum(s["violations"] for s in stats.values())
        scenarios[f"gece_{key}"] = {
            "name": f"Gece + {name}",
            "description": f"{desc} + gece faaliyetleri devredilir",
            "forklift_count": 2,
            "events": events,
            "stats": stats,
            "total_violations": total_viols,
            "feasible": total_viols == 0,
        }

    return scenarios


# ─── Özet Tablosu ──────────────────────────────────────────────

def summary_table(scenarios):
    """Tüm senaryoları karşılaştırmalı tablo formatında döndür."""
    rows = []
    for key, sc in scenarios.items():
        total_work = sum(s["total_work_dk"] for s in sc["stats"].values())
        total_idle = sum(s["idle_dk"] for s in sc["stats"].values())
        avg_util = sum(s["utilization_pct"] for s in sc["stats"].values()) / len(sc["stats"]) if sc["stats"] else 0
        max_delay = max((s["max_delay_dk"] for s in sc["stats"].values()), default=0)

        rows.append({
            "key": key,
            "name": sc["name"],
            "forklift_count": sc["forklift_count"],
            "total_work_dk": round(total_work, 1),
            "total_idle_dk": round(total_idle, 1),
            "avg_utilization_pct": round(avg_util, 1),
            "max_delay_dk": round(max_delay, 1),
            "total_violations": sc["total_violations"],
            "feasible": sc["feasible"],
        })

    return rows


# ─── CLI Test ──────────────────────────────────────────────────

if __name__ == "__main__":
    data = load_data()
    scenarios = run_scenarios(data)
    summary = summary_table(scenarios)

    print("\n" + "=" * 80)
    print("  SENARYO KARŞILAŞTIRMA TABLOSU")
    print("=" * 80)
    print(f"{'Senaryo':<35} {'FK':>3} {'İş(dk)':>8} {'Boş(dk)':>9} {'Kul.%':>6} {'MxGec':>6} {'İhlal':>6} {'Uygun':>6}")
    print("-" * 80)
    for r in summary:
        feasible_str = "✅" if r["feasible"] else "❌"
        print(f"{r['name']:<35} {r['forklift_count']:>3} {r['total_work_dk']:>8} {r['total_idle_dk']:>9} "
              f"{r['avg_utilization_pct']:>5}% {r['max_delay_dk']:>6} {r['total_violations']:>6} {feasible_str:>6}")

    print("\n" + "=" * 80)
    print("  DETAY: Mevcut Durum")
    print("=" * 80)
    for fname, st in scenarios["mevcut"]["stats"].items():
        print(f"\n  {fname}:")
        print(f"    Toplam iş: {st['total_work_dk']} dk | Boş: {st['idle_dk']} dk | Kullanım: {st['utilization_pct']}%")
        print(f"    Toplam olay: {st['total_events']} | Tamamlanan: {st['completed_events']}")
        print(f"    Gecikmeler: maks {st['max_delay_dk']} dk | İhlal: {st['violations']}")
