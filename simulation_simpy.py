"""
RHI Magnesita — SimPy Tabanlı Stokastik Simülasyon Motoru v3
- Mixed-Poisson: Günlük palet hacmi gerçek veriden, gün içi Poisson
- TIR gelişleri: Poisson λ=1.46 TIR/saat (234 TIR / 20 gün)
- Gerçek min/max çevrim süreleri (üçgen dağılım)
- Aging priority (eşit öncelikte FIFO)
- PriorityResource ile kuyruk yönetimi
"""

import simpy
import random
import statistics
import sqlite3
import os
import math

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "forklift.db")

# ─── Helpers ───────────────────────────────────────────────────

def parse_hm(s):
    parts = s.split(":")
    return int(parts[0]) * 60 + int(parts[1])

def hm(minutes):
    h = int(minutes) // 60
    m = int(minutes) % 60
    return f"{h:02d}:{m:02d}"

def to_dk(val, birim):
    if val is None:
        return None
    return val * 60 if birim == "saat" else val

def sample_cycle_time(act):
    """Gerçek min/max/mode ile üçgen dağılım.
    Min veya max yoksa ±20% varsayılır."""
    mode = act.get("cevrim_suresi")
    if mode is None:
        return 1  # fallback

    low = act.get("cevrim_min")
    high = act.get("cevrim_max")

    if low is None:
        low = mode * 0.8
    if high is None:
        high = mode * 1.2

    # Güvenlik: low <= mode <= high
    low = min(low, mode)
    high = max(high, mode)

    return max(0.1, random.triangular(low, high, mode))

def break_time_between(t1, t2, breaks):
    total = 0
    for b_start, b_end in breaks:
        overlap_start = max(t1, b_start)
        overlap_end = min(t2, b_end)
        if overlap_start < overlap_end:
            total += overlap_end - overlap_start
    return total


# ─── Empirical Production Data ─────────────────────────────────
# 58 günlük gerçek üretim verisi (paketlemeden geçen toplam palet/gün)
# Kaynak: Kullanıcıdan alınan düzenli tablo (Ocak-Mart 2026)
EMPIRICAL_DAILY_PALLETS = [
    # Ocak 2026
    89, 80, 90, 75, 41, 165, 110, 92, 6, 166, 71, 137, 46,
    78, 101, 87, 81, 47, 47, 97, 153, 81, 72, 100,
    # Şubat 2026
    70, 87, 89, 101, 35, 169, 123, 115, 62, 73, 21,
    39, 90, 88, 120, 111, 145, 184, 199, 132, 139,
    # Mart 2026
    45, 67, 115, 25, 136, 157, 88, 71, 95, 90, 126, 104, 88,
]
# Ortalama: ~97 palet/gün, λ_saat≈12.1
PRODUCTION_LAMBDA_SAAT = statistics.mean(EMPIRICAL_DAILY_PALLETS) / 8


# ─── Data Loading ──────────────────────────────────────────────

def load_data():
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

    # TIR Poisson config
    tir_row = conn.execute("SELECT * FROM tir_config LIMIT 1").fetchone()
    tir_config = dict(tir_row) if tir_row else {"lambda_saat": 1.46}

    conn.close()
    return {"vardiya": vardiya, "molalar": molalar, "forkliftler": forkliftler, "tir_config": tir_config}


# ─── SimPy Simulation ─────────────────────────────────────────

PRIORITY_MAP = {"yuksek": 1, "normal": 2, "dusuk": 3}
# Dinamik Önceliklendirme: bekleme süresi toleransın X%'ini aştığında öncelik yükselmeye başlar
AGING_THRESHOLD_PCT = 0.5   # Toleransın %50'sinden sonra aging başlar
AGING_MAX_ESCALATION = 1.0  # Maks 1 kademe yükselme (düşük→normal, normal→yüksek)


class ForkliftSimulation:
    """Tek bir simülasyon koşumu — bir senaryo için."""

    def __init__(self, activities, vardiya_start, vardiya_end, breaks, label="", tir_config=None):
        self.activities = activities
        self.v_start = vardiya_start
        self.v_end = vardiya_end
        self.breaks = breaks
        self.label = label
        self.tir_config = tir_config or {"lambda_saat": 1.46}

        # İstatistik toplama
        self.events = []
        self.delays = []
        self.wait_times = []
        self.violations = 0
        self.total_work = 0
        self.completed = 0
        self.missed = 0
        # Aging counter: her talep monoton artan sıra numarası alır
        self._aging_counter = 0

        # Lookahead: yüksek öncelikli Poisson faaliyetlerinin toplam λ'sını hesapla
        # Düşük öncelikli işler bu bilgiyi kullanarak erteleme kararı verir
        self._high_priority_lambda_dk = 0.0
        for act in activities:
            if act.get("poisson_mode") and PRIORITY_MAP.get(act.get("oncelik"), 2) <= 1:
                act_name = act.get("ad", "")
                is_tir = "TIR" in act_name.upper() or "tır" in act_name.lower()
                if is_tir:
                    lam = self.tir_config.get("lambda_saat", 1.46)
                else:
                    lam = PRODUCTION_LAMBDA_SAAT * act.get("_poisson_split", 1.0)
                self._high_priority_lambda_dk += lam / 60

    def _next_aging(self):
        """Aynı öncelikteki işlerde FIFO sıralama — daha önce gelen öne geçer."""
        self._aging_counter += 1
        return self._aging_counter

    def _dynamic_priority(self, base_priority, time_waiting, gecikme_tol):
        """Dinamik önceliklendirme — bekleme süresi arttıkça öncelik yükselir.
        
        Bekleme süresi toleransın %50'sini aşınca, kalan %50'lik dilimde
        öncelik kademeli olarak base_priority'den 1'e (en yüksek) doğru
        yükselir.
        
        Örnek (base=3, tol=40dk):
          0-20dk bekleme  → priority = 3 (değişmez)
          30dk bekleme    → priority = 2 (yarı yolda)
          40dk bekleme    → priority = 1 (en yüksek — artık acil)
        """
        if gecikme_tol is None or gecikme_tol <= 0 or time_waiting <= 0:
            return base_priority
        
        # Toleransın ne kadarı geçmiş?
        wait_ratio = time_waiting / gecikme_tol
        
        if wait_ratio <= AGING_THRESHOLD_PCT:
            return base_priority  # Henüz aging başlamadı
        
        # Aging bölgesinde: threshold_pct ile 1.0 arası lineer interpolasyon
        aging_progress = min(1.0, (wait_ratio - AGING_THRESHOLD_PCT) / (1.0 - AGING_THRESHOLD_PCT))
        escalation = aging_progress * min(AGING_MAX_ESCALATION, base_priority - 1)
        
        return max(1, base_priority - escalation)

    def is_in_break(self, t):
        for b_start, b_end in self.breaks:
            if b_start <= t < b_end:
                return True, b_end
        return False, None

    def wait_until_break_ends(self, env):
        in_break, break_end = self.is_in_break(env.now)
        while in_break:
            yield env.timeout(break_end - env.now)
            in_break, break_end = self.is_in_break(env.now)

    def _should_defer_for_lookahead(self, cevrim):
        """Düşük öncelikli iş için lookahead kontrolü.
        Yüksek öncelikli Poisson varışının beklenen süresini hesapla,
        çevrim süresinden kısaysa erteleme öner."""
        if self._high_priority_lambda_dk <= 0:
            return False
        # Beklenen varış arası süre (dk)
        expected_interarrival = 1.0 / self._high_priority_lambda_dk
        # Çevrim süresi beklenen varış aralığından uzunsa → çakışma olasılığı yüksek
        return cevrim > expected_interarrival

    def regular_activity_process(self, env, forklift, act):
        """Sabit aralıklarla tekrarlayan faaliyet süreci."""
        cevrim_mode = act.get("cevrim_suresi")
        tekrar_dk = to_dk(act.get("tekrar_suresi"), act.get("tekrar_birimi", "dk"))
        gecikme_tol = to_dk(act.get("gecikme_toleransi"), act.get("gecikme_birimi", "dk"))
        base_priority = PRIORITY_MAP.get(act.get("oncelik"), 2)
        is_low_priority = (act.get("oncelik") == "dusuk")

        if cevrim_mode is None or tekrar_dk is None:
            return

        # Tekrar sayısını hesapla — NET çalışma süresi kullan (mola düşülmüş)
        total_break = sum(b[1] - b[0] for b in self.breaks)
        net_work_time = self.v_end - self.v_start - total_break
        total_reps = max(1, int(net_work_time / tekrar_dk)) if tekrar_dk < net_work_time else 1

        next_schedule = self.v_start

        for rep in range(total_reps):
            # Sonraki planlanan zaman vardiya sonunu aşıyorsa dur
            if next_schedule >= self.v_end:
                break

            if env.now < next_schedule:
                yield env.timeout(next_schedule - env.now)

            scheduled_time = next_schedule
            yield from self.wait_until_break_ends(env)

            if env.now >= self.v_end:
                self.missed += 1
                break

            # Dinamik önceliklendirme + periyodik yeniden değerlendirme
            request_time = env.now
            time_waiting = max(0, request_time - scheduled_time)
            effective_priority = self._dynamic_priority(base_priority, time_waiting, gecikme_tol)
            aging = self._next_aging()
            req = forklift.request(priority=(effective_priority, aging))

            # Yeniden değerlendirme döngüsü: bekleme süresince önceliği yükselt
            reeval_interval = max(5, (gecikme_tol or 60) * 0.25)  # toleransın %25'i aralıklarla
            while True:
                result = yield req | env.timeout(reeval_interval)
                if req in result:
                    break  # Forklift alındı
                # Henüz alınamadı — önceliği yeniden hesapla
                total_wait = env.now - scheduled_time
                new_priority = self._dynamic_priority(base_priority, total_wait, gecikme_tol)
                if new_priority < effective_priority - 0.1:
                    # Öncelik anlamlı şekilde yükseldi — iptal et, yeniden talep et
                    if not req.triggered:
                        req.cancel()
                    else:
                        forklift.release(req)
                    effective_priority = new_priority
                    aging = self._next_aging()
                    req = forklift.request(priority=(effective_priority, aging))
                # Else: aynı öncelik, beklemeye devam

            yield from self.wait_until_break_ends(env)

            actual_start = env.now
            wait_time = actual_start - request_time

            # Stokastik çevrim süresi
            cevrim = sample_cycle_time(act)

            if actual_start + cevrim > self.v_end:
                forklift.release(req)
                self.missed += 1
                break

            # ── Lookahead: düşük öncelikli iş yüksek öncelikli işi engelleyecek mi? ──
            if is_low_priority and self._should_defer_for_lookahead(cevrim):
                # Kuyruktaki yüksek öncelikli bekleyen var mı kontrol et
                # PriorityResource queue'sunda her eleman .key = ((priority, aging), count, preempt)
                has_waiting_high = len(forklift.queue) > 0 and any(
                    getattr(r, 'key', ((99,),))[0][0] < base_priority for r in forklift.queue
                )
                if has_waiting_high:
                    # Zaten bekleyen yüksek öncelikli iş var → ertele
                    forklift.release(req)
                    defer_time = min(cevrim, 5.0)  # en fazla 5dk bekle
                    yield env.timeout(defer_time)
                    # Erteleme sonrası tekrar dene (tek seferlik)
                    request_time = env.now
                    aging = self._next_aging()
                    req = forklift.request(priority=(base_priority, aging))
                    yield req
                    yield from self.wait_until_break_ends(env)
                    actual_start = env.now
                    wait_time = actual_start - request_time
                    cevrim = sample_cycle_time(act)
                    if actual_start + cevrim > self.v_end:
                        forklift.release(req)
                        self.missed += 1
                        break

            yield env.timeout(cevrim)
            forklift.release(req)

            # Net gecikme
            gross_delay = actual_start - scheduled_time
            break_dur = break_time_between(scheduled_time, actual_start, self.breaks)
            net_delay = max(0, gross_delay - break_dur)

            status = "ok"
            if net_delay > 0.5:
                if gecikme_tol is not None and net_delay > gecikme_tol:
                    status = "violation"
                    self.violations += 1
                else:
                    status = "delayed"

            self._record_event(act, scheduled_time, actual_start, cevrim, net_delay, gross_delay, break_dur, wait_time, status)

            # Sonraki tekrar (±15% varyasyon)
            interval_noise = random.triangular(tekrar_dk * 0.85, tekrar_dk * 1.15, tekrar_dk)
            next_schedule = scheduled_time + interval_noise

    def poisson_arrival_generator(self, env, queue, act):
        """Bağımsız Poisson varış üreteci — varışları kuyruğa ekler.
        Forklift meşgul olsa bile varışlar üretilmeye devam eder."""
        act_name = act.get("ad", "")
        is_tir = "TIR" in act_name.upper() or "tır" in act_name.lower()

        if is_tir:
            lambda_saat = self.tir_config.get("lambda_saat", 1.46)
        else:
            daily_pallets = random.choice(EMPIRICAL_DAILY_PALLETS)
            lambda_saat = daily_pallets / 8

            split = act.get("_poisson_split", 1.0)
            lambda_saat *= split

        lambda_dk = lambda_saat / 60

        while env.now < self.v_end:
            inter_arrival = random.expovariate(lambda_dk) if lambda_dk > 0 else 60
            yield env.timeout(inter_arrival)

            if env.now >= self.v_end:
                break

            arrival_time = env.now
            # Kuyruğa varış zamanını ekle (forklift durumundan bağımsız)
            queue.put(arrival_time)

    def poisson_service_process(self, env, forklift, queue, act):
        """Kuyruktan varış alıp hizmet veren süreç."""
        gecikme_tol = to_dk(act.get("gecikme_toleransi"), act.get("gecikme_birimi", "dk"))
        base_priority = PRIORITY_MAP.get(act.get("oncelik"), 2)
        avg_cevrim = act.get("cevrim_suresi", 1)

        while env.now < self.v_end:
            # Kuyruktan bir varış al — vardiya sonuna kadar bekle
            remaining = self.v_end - env.now
            if remaining <= 0:
                break

            get_req = queue.get()
            result = yield get_req | env.timeout(remaining)

            if get_req not in result:
                # Vardiya bitti, kuyrukta bekleyen kalmadı
                break

            scheduled_time = get_req.value  # Varışın geldiği zaman

            # Mola kontrolü
            yield from self.wait_until_break_ends(env)

            if env.now >= self.v_end:
                self.missed += 1
                break

            # Kalan süre ortalama çevrimden çok azsa, missed say
            remaining = self.v_end - env.now
            if remaining < avg_cevrim * 0.3:
                self.missed += 1
                continue

            # Kaynak talep et — dinamik önceliklendirme ile
            request_time = env.now
            time_waiting = max(0, request_time - scheduled_time)  # Store kuyruğundaki bekleme
            effective_priority = self._dynamic_priority(base_priority, time_waiting, gecikme_tol)
            aging = self._next_aging()
            req = forklift.request(priority=(effective_priority, aging))

            remaining_for_wait = self.v_end - env.now
            if remaining_for_wait <= 0:
                req.cancel()
                self.missed += 1
                break

            result = yield req | env.timeout(remaining_for_wait)

            if req not in result:
                if req.triggered:
                    forklift.release(req)
                else:
                    req.cancel()
                self.missed += 1
                continue

            yield from self.wait_until_break_ends(env)

            actual_start = env.now
            wait_time = actual_start - request_time

            cevrim = sample_cycle_time(act)

            if actual_start + cevrim > self.v_end:
                forklift.release(req)
                self.missed += 1
                continue

            yield env.timeout(cevrim)
            forklift.release(req)

            # Net gecikme
            gross_delay = actual_start - scheduled_time
            break_dur = break_time_between(scheduled_time, actual_start, self.breaks)
            net_delay = max(0, gross_delay - break_dur)

            status = "ok"
            if net_delay > 0.5:
                if gecikme_tol is not None and net_delay > gecikme_tol:
                    status = "violation"
                    self.violations += 1
                else:
                    status = "delayed"

            self._record_event(act, scheduled_time, actual_start, cevrim, net_delay, gross_delay, break_dur, wait_time, status)

        # Vardiya bitti — kuyrukta kalan varışları missed olarak say
        while len(queue.items) > 0:
            queue.items.pop(0)
            self.missed += 1

    def _record_event(self, act, scheduled_time, actual_start, cevrim, net_delay, gross_delay, break_dur, wait_time, status):
        """Olayı kaydet."""
        self.events.append({
            "activity": act["ad"],
            "scheduled_start": hm(scheduled_time),
            "start": hm(actual_start),
            "end": hm(actual_start + cevrim),
            "start_dk": actual_start,
            "end_dk": actual_start + cevrim,
            "net_delay": round(net_delay, 1),
            "gross_delay": round(gross_delay, 1),
            "break_dur": round(break_dur, 1),
            "wait_time": round(wait_time, 1),
            "priority": act.get("oncelik"),
            "status": status,
        })
        self.delays.append(net_delay)
        self.wait_times.append(wait_time)
        self.total_work += cevrim
        self.completed += 1

    def run(self, seed=None):
        """Simülasyonu çalıştır."""
        if seed is not None:
            random.seed(seed)

        env = simpy.Environment(initial_time=self.v_start)
        forklift = simpy.PriorityResource(env, capacity=1)

        for act in self.activities:
            if act.get("poisson_mode"):
                # Bağımsız varış üreteci + hizmet süreci (Store kuyruğu ile)
                queue = simpy.Store(env)
                env.process(self.poisson_arrival_generator(env, queue, act))
                env.process(self.poisson_service_process(env, forklift, queue, act))
            else:
                env.process(self.regular_activity_process(env, forklift, act))

        env.run(until=self.v_end)

        total_break = sum(b[1] - b[0] for b in self.breaks)
        net_time = self.v_end - self.v_start - total_break
        utilization = (self.total_work / net_time * 100) if net_time > 0 else 0

        return {
            "label": self.label,
            "total_events": self.completed + self.missed,
            "completed": self.completed,
            "missed": self.missed,
            "total_work_dk": round(self.total_work, 1),
            "utilization_pct": round(utilization, 1),
            "idle_dk": round(net_time - self.total_work, 1),
            "violations": self.violations,
            "avg_delay": round(statistics.mean(self.delays), 1) if self.delays else 0,
            "max_delay": round(max(self.delays), 1) if self.delays else 0,
            "p95_delay": round(sorted(self.delays)[int(len(self.delays) * 0.95)] if len(self.delays) > 1 else (self.delays[0] if self.delays else 0), 1),
            "avg_wait": round(statistics.mean(self.wait_times), 1) if self.wait_times else 0,
            "max_wait": round(max(self.wait_times), 1) if self.wait_times else 0,
            "events": self.events,
            "activity_counts": self._count_per_activity(),
        }

    def _count_per_activity(self):
        """Faaliyet başına tekrar sayısı ve toplam çalışma süresi."""
        counts = {}
        for e in self.events:
            name = e["activity"]
            if name not in counts:
                counts[name] = {"count": 0, "total_work_dk": 0, "violations": 0, "delays": []}
            counts[name]["count"] += 1
            dur = e["end_dk"] - e["start_dk"]
            counts[name]["total_work_dk"] += dur
            if e["status"] == "violation":
                counts[name]["violations"] += 1
            counts[name]["delays"].append(e["net_delay"])
        # Compute avg delay per activity
        for name in counts:
            d = counts[name]["delays"]
            counts[name]["avg_delay"] = round(statistics.mean(d), 1) if d else 0
            counts[name]["max_delay"] = round(max(d), 1) if d else 0
            counts[name]["total_work_dk"] = round(counts[name]["total_work_dk"], 1)
            del counts[name]["delays"]
        return counts


# ─── Multi-Replication Runner ─────────────────────────────────

def run_multi_replication(activities, v_start, v_end, breaks, label="", n_reps=50, tir_config=None):
    """N replikasyon çalıştır ve istatistik hesapla."""
    results = []
    all_events = None

    for i in range(n_reps):
        sim = ForkliftSimulation(activities, v_start, v_end, breaks, label, tir_config)
        r = sim.run(seed=i * 42 + 7)
        results.append(r)
        if i == 0:
            all_events = r["events"]

    def ci95(values):
        n = len(values)
        if n < 2:
            return values[0] if values else 0, 0, 0
        mean = statistics.mean(values)
        std = statistics.stdev(values)
        margin = 1.96 * std / math.sqrt(n)
        return round(mean, 1), round(mean - margin, 1), round(mean + margin, 1)

    utils = [r["utilization_pct"] for r in results]
    delays = [r["avg_delay"] for r in results]
    max_delays = [r["max_delay"] for r in results]
    viols = [r["violations"] for r in results]
    waits = [r["avg_wait"] for r in results]
    works = [r["total_work_dk"] for r in results]

    util_mean, util_lo, util_hi = ci95(utils)
    delay_mean, delay_lo, delay_hi = ci95(delays)
    mdelay_mean, mdelay_lo, mdelay_hi = ci95(max_delays)
    viol_mean, viol_lo, viol_hi = ci95(viols)
    wait_mean, wait_lo, wait_hi = ci95(waits)
    work_mean, work_lo, work_hi = ci95(works)

    net_time = v_end - v_start - sum(b[1] - b[0] for b in breaks)

    return {
        "label": label,
        "n_reps": n_reps,
        "events": all_events,
        "stats": {
            "utilization": {"mean": util_mean, "lo": util_lo, "hi": util_hi, "unit": "%"},
            "avg_delay": {"mean": delay_mean, "lo": delay_lo, "hi": delay_hi, "unit": "dk"},
            "max_delay": {"mean": mdelay_mean, "lo": mdelay_lo, "hi": mdelay_hi, "unit": "dk"},
            "violations": {"mean": viol_mean, "lo": viol_lo, "hi": viol_hi, "unit": ""},
            "avg_wait": {"mean": wait_mean, "lo": wait_lo, "hi": wait_hi, "unit": "dk"},
            "total_work": {"mean": work_mean, "lo": work_lo, "hi": work_hi, "unit": "dk"},
            "idle": {"mean": round(net_time - work_mean, 1), "lo": round(net_time - work_hi, 1), "hi": round(net_time - work_lo, 1), "unit": "dk"},
        },
        "raw_distributions": {
            "utilization": utils,
            "violations": viols,
            "max_delay": max_delays,
        },
        "replications": [
            {
                "id": i + 1,
                "utilization_pct": r["utilization_pct"],
                "total_work_dk": r["total_work_dk"],
                "idle_dk": round(net_time - r["total_work_dk"], 1),
                "completed": r["completed"],
                "missed": r["missed"],
                "violations": r["violations"],
                "avg_delay": r["avg_delay"],
                "max_delay": r["max_delay"],
                "avg_wait": r["avg_wait"],
                "max_wait": r["max_wait"],
                "events": r["events"],
            }
            for i, r in enumerate(results)
        ],
        "activity_frequency": _aggregate_activity_counts(results, activities, v_start, v_end, breaks),
    }


def _aggregate_activity_counts(results, activities, v_start, v_end, breaks):
    """Tüm replikasyonlardan faaliyet başına frekans istatistiği hesapla."""
    from collections import defaultdict

    # Teorik tekrar sayılarını hesapla — NET çalışma süresi (mola düşülmüş)
    net_time = v_end - v_start
    total_break = sum(b[1] - b[0] for b in breaks)
    net_work = net_time - total_break

    act_stats = {}
    for act in activities:
        name = act["ad"]
        tekrar_dk = to_dk(act.get("tekrar_suresi"), act.get("tekrar_birimi", "dk"))
        cevrim = act.get("cevrim_suresi", 1)
        is_poisson = bool(act.get("poisson_mode"))

        # Fiziksel kapasite sınırı: net çalışma süresinde max kaç iş sığar
        max_physical = int(net_work / cevrim) if cevrim > 0 else 9999

        if is_poisson:
            act_name = name
            is_tir = "TIR" in act_name.upper() or "tır" in act_name.lower()
            if is_tir:
                lambda_saat = 1.46
                # Net çalışma saati üzerinden teorik varış
                theoretical_arrivals = round(lambda_saat * (net_work / 60), 1)
                # Kapasite sınırı ile kısıtla
                theoretical = min(theoretical_arrivals, max_physical)
            else:
                # Mixed Poisson: ortalama günlük palet
                split = act.get("_poisson_split", 1.0)
                theoretical_arrivals = round(statistics.mean(EMPIRICAL_DAILY_PALLETS) * split, 1)
                theoretical = min(theoretical_arrivals, max_physical)
        elif tekrar_dk and tekrar_dk > 0:
            if tekrar_dk >= net_work:
                theoretical = 1
            else:
                theoretical = max(1, int(net_work / tekrar_dk))
            theoretical = min(theoretical, max_physical)
        else:
            theoretical = 0

        act_stats[name] = {
            "theoretical": theoretical,
            "cevrim_ort": cevrim,
            "cevrim_min": act.get("cevrim_min"),
            "cevrim_max": act.get("cevrim_max"),
            "tekrar_dk": tekrar_dk,
            "oncelik": act.get("oncelik"),
            "poisson": is_poisson,
            "simulated_counts": [],
            "simulated_work": [],
            "simulated_violations": [],
        }

    # Replikasyonlardan gerçek sayıları topla
    for r in results:
        ac = r.get("activity_counts", {})
        for name in act_stats:
            if name in ac:
                act_stats[name]["simulated_counts"].append(ac[name]["count"])
                act_stats[name]["simulated_work"].append(ac[name]["total_work_dk"])
                act_stats[name]["simulated_violations"].append(ac[name]["violations"])
            else:
                act_stats[name]["simulated_counts"].append(0)
                act_stats[name]["simulated_work"].append(0)
                act_stats[name]["simulated_violations"].append(0)

    # İstatistik hesapla
    result = []
    for name, s in act_stats.items():
        counts = s["simulated_counts"]
        avg_count = round(statistics.mean(counts), 1) if counts else 0
        min_count = min(counts) if counts else 0
        max_count = max(counts) if counts else 0
        avg_work = round(statistics.mean(s["simulated_work"]), 1) if s["simulated_work"] else 0
        avg_viol = round(statistics.mean(s["simulated_violations"]), 1) if s["simulated_violations"] else 0

        result.append({
            "activity": name,
            "theoretical_reps": s["theoretical"],
            "avg_reps": avg_count,
            "min_reps": min_count,
            "max_reps": max_count,
            "avg_work_dk": avg_work,
            "avg_violations": avg_viol,
            "cevrim_ort": s["cevrim_ort"],
            "cevrim_min": s["cevrim_min"],
            "cevrim_max": s["cevrim_max"],
            "tekrar_dk": s["tekrar_dk"],
            "oncelik": s["oncelik"],
            "poisson": s["poisson"],
        })

    return result


# ─── Scenario Runner ──────────────────────────────────────────

def run_simpy_scenarios(data, n_reps=50):
    """Tüm senaryoları SimPy ile çalıştır."""
    v_start = parse_hm(data["vardiya"]["baslangic"])
    v_end = parse_hm(data["vardiya"]["bitis"])
    breaks = [(parse_hm(m["baslangic"]), parse_hm(m["bitis"])) for m in data["molalar"]]
    forkliftler = data["forkliftler"]
    tir_config = data.get("tir_config", {"lambda_saat": 1.46})

    def get_acts(fk_ids, exclude_night=False):
        acts = []
        for f in forkliftler:
            if f["id"] in fk_ids:
                for a in f["faaliyetler"]:
                    if exclude_night and a.get("gece_vardiyasi"):
                        continue
                    act_copy = dict(a)
                    
                    # FK1 "Tuğla Dolu Paleti Alma" faaliyeti, belirlenen oran kadar iş alır (örn: %80)
                    if f["id"] == 1 and act_copy.get("poisson_mode") and "Tuğla Dolu" in act_copy.get("ad", ""):
                        act_copy["_poisson_split"] = tir_config.get("f1_paketleme_orani", 80) / 100.0
                        
                    # FK2 "Makineye" ve "Alana" faaliyetleri toplam üretimin %50'sini alır
                    if f["id"] == 2:
                        if "Makineye" in act_copy.get("ad", "") or "Alana" in act_copy.get("ad", ""):
                            act_copy["_poisson_split"] = 0.5
                        elif "Makinadan Stoğa" in act_copy.get("ad", ""):
                            act_copy["poisson_mode"] = 1
                            act_copy["_poisson_split"] = 0.5
                        elif "Alandan Stoğa" in act_copy.get("ad", ""):
                            act_copy["poisson_mode"] = 1
                            act_copy["_poisson_split"] = 0.5
                    acts.append(act_copy)
        return acts

    scenarios = {}

    # ── Mevcut Durum ──
    mevcut_fks = {}
    for f in forkliftler:
        acts = get_acts([f["id"]])
        result = run_multi_replication(acts, v_start, v_end, breaks, f["ad"], n_reps, tir_config)
        mevcut_fks[f["ad"]] = result

    scenarios["mevcut"] = {
        "name": "Mevcut Durum (3 Forklift)",
        "description": "Her forklift sadece kendi bölgesinin işlerini yapar",
        "forklift_count": 3,
        "forkliftler": mevcut_fks,
    }

    # ── Birleşim senaryoları ──
    merges = [
        ("f1_f2", [1, 2], [3], "F1+F2 Birleşim", "Boşaltma+Paketleme tek FK, Sevkiyat ayrı"),
        ("f1_f3", [1, 3], [2], "F1+F3 Birleşim", "Boşaltma+Sevkiyat tek FK, Paketleme ayrı"),
        ("f2_f3", [2, 3], [1], "F2+F3 Birleşim", "Paketleme+Sevkiyat tek FK, Boşaltma ayrı"),
    ]

    for key, merged, solos, name, desc in merges:
        fks = {}
        merged_acts = get_acts(merged)
        merged_name = "+".join(f["ad"] for f in forkliftler if f["id"] in merged)
        fks[merged_name] = run_multi_replication(merged_acts, v_start, v_end, breaks, merged_name, n_reps, tir_config)

        for sid in solos:
            sname = next(f["ad"] for f in forkliftler if f["id"] == sid)
            fks[sname] = run_multi_replication(get_acts([sid]), v_start, v_end, breaks, sname, n_reps, tir_config)

        scenarios[key] = {"name": name, "description": desc, "forklift_count": 2, "forkliftler": fks}

    # ── Özel Senaryo: F1+F2 Birleşim + Boş Palet & Iskarta → F3 ──
    # F1+F2'nin tüm faaliyetleri tek forklift'e, AMA "Boş Palet Alma" ve "Iskarta Boşaltma" F3'e devredilir
    fks_special = {}
    # FK_A: F1+F2 faaliyetleri — "Boş Palet Alma" ve "Iskarta Boşaltma" hariç
    merged_acts_filtered = []
    delegated_to_f3 = []  # F3'e devredilecek faaliyetler
    for f in forkliftler:
        if f["id"] in [1, 2]:
            for a in f["faaliyetler"]:
                act_copy = dict(a)
                if "Boş Palet" in act_copy.get("ad", "") or "Iskarta" in act_copy.get("ad", ""):
                    delegated_to_f3.append(act_copy)  # Bu F3'e gidecek
                    continue
                if act_copy.get("poisson_mode") and ("Makineye" in act_copy.get("ad", "") or "Alana" in act_copy.get("ad", "")):
                    act_copy["_poisson_split"] = 0.5
                merged_acts_filtered.append(act_copy)

    merged_name_a = "Forklift 1+Forklift 2"
    fks_special[merged_name_a] = run_multi_replication(merged_acts_filtered, v_start, v_end, breaks, merged_name_a, n_reps, tir_config)

    # FK_B: F3 faaliyetleri + Boş Palet Alma + Iskarta Boşaltma
    f3_acts = get_acts([3])
    for delegated_act in delegated_to_f3:
        f3_acts.append(delegated_act)
    fks_special["Forklift 3"] = run_multi_replication(f3_acts, v_start, v_end, breaks, "Forklift 3", n_reps, tir_config)

    scenarios["f1_f2_bos_f3"] = {
        "name": "F1+F2 Birleşim + Boş Palet & Iskarta→F3",
        "description": "Boşaltma+Paketleme tek FK, Sevkiyat + Boş Palet + Iskarta ayrı FK",
        "forklift_count": 2,
        "forkliftler": fks_special,
    }

    # ── Gece Delegasyonu ──
    gece_fks = {}
    night_delegated = []
    for f in forkliftler:
        night_acts = [a for a in f["faaliyetler"] if a.get("gece_vardiyasi")]
        for a in night_acts:
            night_delegated.append({"forklift": f["ad"], "activity": a["ad"]})
        # get_acts kullan — _poisson_split doğru uygulanması için
        all_acts = get_acts([f["id"]])
        day_acts = [a for a in all_acts if not a.get("gece_vardiyasi")]
        gece_fks[f["ad"]] = run_multi_replication(day_acts, v_start, v_end, breaks, f["ad"], n_reps, tir_config)

    scenarios["gece_delegasyonu"] = {
        "name": "Gece Vardiyası Delegasyonu",
        "description": "Gece'ye atanabilir faaliyetler gündüzden çıkarılır",
        "forklift_count": 3,
        "forkliftler": gece_fks,
        "night_delegated": night_delegated,
    }

    # ── Gece + Birleşim ──
    for key, merged, solos, name, desc in merges:
        fks = {}
        merged_acts = get_acts(merged, exclude_night=True)
        merged_name = "+".join(f["ad"] for f in forkliftler if f["id"] in merged)
        fks[merged_name] = run_multi_replication(merged_acts, v_start, v_end, breaks, merged_name, n_reps, tir_config)

        for sid in solos:
            sname = next(f["ad"] for f in forkliftler if f["id"] == sid)
            fks[sname] = run_multi_replication(get_acts([sid], True), v_start, v_end, breaks, sname, n_reps, tir_config)

        scenarios[f"gece_{key}"] = {
            "name": f"Gece + {name}",
            "description": f"{desc} + gece faaliyetleri devredilir",
            "forklift_count": 2,
            "forkliftler": fks,
        }

    return scenarios


def summary_table(scenarios):
    """Karşılaştırma tablosu."""
    rows = []
    for key, sc in scenarios.items():
        fks = sc["forkliftler"]
        avg_util = statistics.mean(fk["stats"]["utilization"]["mean"] for fk in fks.values())
        max_delay = max(fk["stats"]["max_delay"]["mean"] for fk in fks.values())
        total_viols = sum(fk["stats"]["violations"]["mean"] for fk in fks.values())
        total_work = sum(fk["stats"]["total_work"]["mean"] for fk in fks.values())
        total_idle = sum(fk["stats"]["idle"]["mean"] for fk in fks.values())

        rows.append({
            "key": key,
            "name": sc["name"],
            "forklift_count": sc["forklift_count"],
            "total_work_dk": round(total_work, 1),
            "total_idle_dk": round(total_idle, 1),
            "avg_utilization_pct": round(avg_util, 1),
            "max_delay_dk": round(max_delay, 1),
            "total_violations": round(total_viols, 1),
            "feasible": total_viols < 0.5,
        })
    return rows


# ─── Sensitivity Analysis ─────────────────────────────────────

def run_sensitivity_analysis(data, scenario_key, tolerance_overrides, n_reps=30):
    """Tolerans duyarlılık analizi.
    
    Args:
        data: load_data() çıktısı
        scenario_key: senaryo anahtarı (ör: 'f1_f2', 'mevcut', ...)
        tolerance_overrides: {faaliyet_adı: yeni_tolerans_dk, ...}
        n_reps: replikasyon sayısı
    
    Returns:
        dict: before/after karşılaştırma verileri
    """
    # 1. Önce mevcut toleranslarla çalıştır
    original_scenarios = run_simpy_scenarios(data, n_reps=n_reps)
    
    if scenario_key not in original_scenarios:
        return {"error": f"Senaryo bulunamadı: {scenario_key}"}
    
    original = original_scenarios[scenario_key]
    
    # 2. Toleransları override et
    modified_data = json.loads(json.dumps(data))  # Deep copy
    for f in modified_data["forkliftler"]:
        for a in f["faaliyetler"]:
            if a["ad"] in tolerance_overrides:
                new_tol = tolerance_overrides[a["ad"]]
                a["gecikme_toleransi"] = new_tol
                a["gecikme_birimi"] = "dk"
    
    # 3. Yeni toleranslarla çalıştır
    modified_scenarios = run_simpy_scenarios(modified_data, n_reps=n_reps)
    modified = modified_scenarios[scenario_key]
    
    # 4. Karşılaştırma oluştur
    comparison = {
        "scenario_name": original["name"],
        "scenario_key": scenario_key,
        "n_reps": n_reps,
        "tolerance_changes": [],
        "before": _extract_summary(original),
        "after": _extract_summary(modified),
        "forklift_comparison": {},
    }
    
    # Tolerans değişikliklerini raporla
    for f in data["forkliftler"]:
        for a in f["faaliyetler"]:
            name = a["ad"]
            old_tol = a.get("gecikme_toleransi", 0)
            old_birimi = a.get("gecikme_birimi", "dk")
            old_dk = old_tol * 60 if old_birimi == "saat" else old_tol
            new_dk = tolerance_overrides.get(name, old_dk)
            comparison["tolerance_changes"].append({
                "activity": name,
                "old_tolerance_dk": round(old_dk, 1),
                "new_tolerance_dk": round(new_dk, 1),
                "changed": abs(new_dk - old_dk) > 0.1,
            })
    
    # Forklift bazlı karşılaştırma
    for fk_name in original["forkliftler"]:
        orig_fk = original["forkliftler"][fk_name]
        mod_fk = modified["forkliftler"][fk_name]
        
        comparison["forklift_comparison"][fk_name] = {
            "before": {
                "utilization": orig_fk["stats"]["utilization"]["mean"],
                "violations": orig_fk["stats"]["violations"]["mean"],
                "max_delay": orig_fk["stats"]["max_delay"]["mean"],
                "avg_delay": orig_fk["stats"]["avg_delay"]["mean"],
            },
            "after": {
                "utilization": mod_fk["stats"]["utilization"]["mean"],
                "violations": mod_fk["stats"]["violations"]["mean"],
                "max_delay": mod_fk["stats"]["max_delay"]["mean"],
                "avg_delay": mod_fk["stats"]["avg_delay"]["mean"],
            },
            "activity_comparison": [],
        }
        
        # Faaliyet bazlı before/after
        orig_freq = {af["activity"]: af for af in orig_fk.get("activity_frequency", [])}
        mod_freq = {af["activity"]: af for af in mod_fk.get("activity_frequency", [])}
        
        for act_name in orig_freq:
            o = orig_freq[act_name]
            m = mod_freq.get(act_name, o)
            
            # Faaliyet bazlı ihlalleri say
            orig_viols = sum(
                1 for rep in orig_fk.get("replications", [])
                for e in rep.get("events", [])
                if e.get("status") == "violation" and e.get("activity") == act_name
            ) / max(n_reps, 1)
            
            mod_viols = sum(
                1 for rep in mod_fk.get("replications", [])
                for e in rep.get("events", [])
                if e.get("status") == "violation" and e.get("activity") == act_name
            ) / max(n_reps, 1)
            
            comparison["forklift_comparison"][fk_name]["activity_comparison"].append({
                "activity": act_name,
                "violations_before": round(orig_viols, 1),
                "violations_after": round(mod_viols, 1),
                "avg_reps_before": o["avg_reps"],
                "avg_reps_after": m["avg_reps"],
            })
    
    return comparison


def _extract_summary(sc):
    """Senaryo özet istatistikleri."""
    fks = sc["forkliftler"]
    return {
        "total_violations": round(sum(fk["stats"]["violations"]["mean"] for fk in fks.values()), 1),
        "avg_utilization": round(statistics.mean(fk["stats"]["utilization"]["mean"] for fk in fks.values()), 1),
        "max_delay": round(max(fk["stats"]["max_delay"]["mean"] for fk in fks.values()), 1),
        "feasible": sum(fk["stats"]["violations"]["mean"] for fk in fks.values()) < 0.5,
    }


import json  # Ensure json is imported for deep copy


# ─── CLI Test ──────────────────────────────────────────────────

if __name__ == "__main__":
    data = load_data()
    print("🔄 SimPy v2 simülasyonu çalışıyor (Poisson TIR + gerçek min/max)...")
    scenarios = run_simpy_scenarios(data, n_reps=50)
    summary = summary_table(scenarios)

    print("\n" + "=" * 90)
    print("  SIMPY v2 — Poisson TIR + Gerçek Min/Max + Aging Priority")
    print("=" * 90)
    print(f"{'Senaryo':<35} {'FK':>3} {'İş(dk)':>8} {'Boş(dk)':>9} {'Kul.%':>6} {'MxGec':>6} {'İhlal':>6} {'Uyg.':>5}")
    print("-" * 90)
    for r in summary:
        f = "✅" if r["feasible"] else "❌"
        print(f"{r['name']:<35} {r['forklift_count']:>3} {r['total_work_dk']:>8} {r['total_idle_dk']:>9} "
              f"{r['avg_utilization_pct']:>5}% {r['max_delay_dk']:>6} {r['total_violations']:>6} {f:>5}")

    print("\n── DETAY: Mevcut Durum ──")
    for fname, fdata in scenarios["mevcut"]["forkliftler"].items():
        st = fdata["stats"]
        print(f"\n  {fname}:")
        print(f"    Kullanım: {st['utilization']['mean']}% [{st['utilization']['lo']}—{st['utilization']['hi']}]")
        print(f"    Ort. Gecikme: {st['avg_delay']['mean']} dk [{st['avg_delay']['lo']}—{st['avg_delay']['hi']}]")
        print(f"    Maks Gecikme: {st['max_delay']['mean']} dk [{st['max_delay']['lo']}—{st['max_delay']['hi']}]")
        print(f"    İhlal: {st['violations']['mean']} [{st['violations']['lo']}—{st['violations']['hi']}]")
        print(f"    Kaynak Bekleme: {st['avg_wait']['mean']} dk")
