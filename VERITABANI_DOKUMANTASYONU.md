# 📦 RHI Magnesita Forklift Simülasyon — Veritabanı Dokümantasyonu

**Veritabanı:** `forklift.db` (SQLite3)
**Konum:** Proje kök dizini
**Oluşturma:** Uygulama ilk çalıştırıldığında `app.py` tarafından otomatik oluşturulur

---

## 📊 Tablo Yapısı

### 1. `vardiya` — Çalışma Saatleri

Forklift operatörlerinin çalışma saatlerini tanımlar. Simülasyon bu saat aralığında çalışır.

| Sütun | Tip | Varsayılan | Açıklama |
|-------|-----|-----------|----------|
| `id` | INTEGER (PK) | Otomatik | Benzersiz kimlik |
| `baslangic` | TEXT | `'08:00'` | Vardiya başlangıç saati (SS:DD) |
| `bitis` | TEXT | `'16:00'` | Vardiya bitiş saati (SS:DD) |

**Kullanım:** Simülasyon motoru (`simulation_simpy.py`) bu değerleri `v_start` ve `v_end` olarak dakikaya çevirir (ör: 08:00 → 480dk). Tüm faaliyet süreçleri bu zaman penceresi içinde çalışır.

---

### 2. `mola` — Mola Zamanları

Vardiya içindeki mola aralıklarını tanımlar. Mola sırasında forklift iş kabul etmez.

| Sütun | Tip | Açıklama |
|-------|-----|----------|
| `id` | INTEGER (PK) | Benzersiz kimlik |
| `ad` | TEXT | Mola adı (ör: "Öğle Arası") |
| `baslangic` | TEXT | Mola başlangıç saati (SS:DD) |
| `bitis` | TEXT | Mola bitiş saati (SS:DD) |

**Varsayılan veriler:**

| Mola | Başlangıç | Bitiş | Süre |
|------|-----------|-------|------|
| 1. Çay Molası | 09:45 | 10:00 | 15 dk |
| Öğle Arası | 11:30 | 12:00 | 30 dk |
| 2. Çay Molası | 13:45 | 14:00 | 15 dk |

**Toplam mola:** 60 dk → **Net çalışma süresi:** 420 dk (7 saat)

**Kullanım:** Simülasyon motoru mola aralıklarında `wait_until_break_ends()` fonksiyonunu çağırır. Gecikme hesabından mola süreleri çıkarılır (`break_time_between()`).

---

### 3. `forklift` — Forklift Araçları

Simülasyondaki forklift araçlarını tanımlar. Her forklift bir `PriorityResource` olarak modellenir.

| Sütun | Tip | Açıklama |
|-------|-----|----------|
| `id` | INTEGER (PK) | Benzersiz kimlik |
| `ad` | TEXT | Forklift adı (ör: "Forklift 1") |
| `bolge` | TEXT | Çalışma bölgesi |

**Varsayılan veriler:**

| ID | Ad | Bölge | Rol |
|----|----|-------|-----|
| 1 | Forklift 1 | Tuğla Boşaltma | Fırından gelen paletleri alma/taşıma |
| 2 | Forklift 2 | Paketleme | Paletleri makineye/alana/stoka taşıma |
| 3 | Forklift 3 | Sevkiyat | Stoktan TIR'a yükleme |

**Kullanım:** Her forklift SimPy'de `PriorityResource(capacity=1)` olarak oluşturulur. Bir forklift aynı anda yalnızca bir iş yapabilir; diğer işler kuyrukta bekler.

---

### 4. `faaliyet` — Forklift Faaliyetleri ⭐

Sistemin en kritik tablosu. Her forklift'in yapması gereken işleri tanımlar.

| Sütun | Tip | Varsayılan | Açıklama |
|-------|-----|-----------|----------|
| `id` | INTEGER (PK) | Otomatik | Benzersiz kimlik |
| `forklift_id` | INTEGER (FK) | — | Hangi forklift'e ait |
| `ad` | TEXT | — | Faaliyet adı |
| `cevrim_suresi` | REAL | — | ¹ Ortalama çevrim süresi (dk) |
| `cevrim_min` | REAL | — | ² Minimum çevrim süresi (dk) |
| `cevrim_max` | REAL | — | ² Maksimum çevrim süresi (dk) |
| `tekrar_suresi` | REAL | — | ³ Tekrar aralığı (birime göre) |
| `tekrar_birimi` | TEXT | `'dk'` | `'dk'` veya `'saat'` |
| `oncelik` | TEXT | — | `'yuksek'` / `'normal'` / `'dusuk'` |
| `gecikme_toleransi` | REAL | — | ⁴ Tolerans süresi (birime göre) |
| `gecikme_birimi` | TEXT | `'dk'` | `'dk'` veya `'saat'` |
| `gece_vardiyasi` | INTEGER | `0` | ⁵ `1` = sadece gece vardiyasında |
| `poisson_mode` | INTEGER | `0` | ⁶ `1` = Poisson süreciyle tetiklenir |

**Notlar:**

¹ `cevrim_suresi`: İşin tek seferde ne kadar sürdüğü. Üçgen dağılımda "mode" (en olası değer) olarak kullanılır.

² `cevrim_min/max`: Gerçek saha verisinden elde edilmiş. Simülasyonda `random.triangular(min, max, mode)` ile stokastik süre üretilir. Boşsa ±%20 varsayılır.

³ `tekrar_suresi`: Sabit aralıklı (non-Poisson) faaliyetlerde iki iş arası bekleme. Ör: `tekrar=40dk` → her 40dk'da bir tetiklenir.

⁴ `gecikme_toleransi`: Bu süreyi aşan gecikmeler "ihlal" olarak sayılır.

⁵ `gece_vardiyasi`: Bazı faaliyetler (Boş Palet, Iskarta) sadece geceleri yapılır. Gündüz senaryolarında bu faaliyetler hariç tutulur.

⁶ `poisson_mode`: `1` ise faaliyet sabit aralıklarla değil, rastgele Poisson süreciyle tetiklenir. Arageliş süreleri üstel dağılımla belirlenir.

**Varsayılan veriler:**

#### FK1 — Tuğla Boşaltma

| Faaliyet | Çevrim (dk) | Min-Max | Tekrar | Öncelik | Poisson | Gece |
|----------|-------------|---------|--------|---------|---------|------|
| Tuğla Dolu Paleti Alma | 1 | 0.5—3 | 4 dk | Yüksek | ✅ | — |
| Boş Palet Alma | 1 | 0.5—3 | 2 saat | Normal | — | Gece |
| Iskarta Boşaltma | 7 | 5—10 | 48 saat | Düşük | — | Gece |

#### FK2 — Paketleme

| Faaliyet | Çevrim (dk) | Min-Max | Tekrar | Öncelik | Poisson | Gece |
|----------|-------------|---------|--------|---------|---------|------|
| Tuğla Boşaltmadan Makineye | 1 | 0.5—2 | 8 dk | Yüksek | ✅ | — |
| Tuğla Boşaltmadan Alana | 1 | 0.5—2 | 8 dk | Yüksek | ✅ | — |
| Makinadan Stoğa | 1.5 | 0.5—3 | 40 dk | Yüksek | — | — |
| Alandan Stoğa | 1.5 | 0.5—3 | 30 dk | Düşük | — | — |

#### FK3 — Sevkiyat

| Faaliyet | Çevrim (dk) | Min-Max | Tekrar | Öncelik | Poisson | Gece |
|----------|-------------|---------|--------|---------|---------|------|
| TIR'a Yükleme | 30 | 20—40 | 1 saat | Yüksek | ✅ | — |

---

### 5. `tir_config` — TIR Poisson Yapılandırması

TIR gelişlerinin Poisson dağılım parametrelerini tanımlar.

| Sütun | Tip | Varsayılan | Açıklama |
|-------|-----|-----------|----------|
| `id` | INTEGER (PK) | Otomatik | Benzersiz kimlik |
| `lambda_saat` | REAL | `1.46` | Saatlik ortalama TIR sayısı (λ) |
| `ort_gunluk` | INTEGER | `12` | Günlük ortalama TIR |
| `min_gunluk` | INTEGER | `5` | Günlük minimum TIR |
| `max_gunluk` | INTEGER | `20` | Günlük maksimum TIR |

**Kaynak:** 1 aylık sevkiyat raporu — 234 TIR / 20 iş günü = 11.7 TIR/gün → **λ = 1.46 TIR/saat**

**Kullanım:** FK3 "TIR'a Yükleme" faaliyeti bu λ değeriyle `random.expovariate(λ/60)` kullanarak üstel dağılımdan arageliş süresi üretir.

---

## 🔬 Simülasyonda Kullanılan Ek Veriler

### Ampirik Üretim Verisi (Kod İçi Sabit)

Veritabanı dışında, `simulation_simpy.py` dosyasında **58 günlük gerçek üretim verisi** sabit olarak tutulur:

```python
EMPIRICAL_DAILY_PALLETS = [
    # Ocak 2026 (24 gün)
    89, 80, 90, 75, 41, 165, 110, 92, 6, 166, 71, 137, 46,
    78, 101, 87, 81, 47, 47, 97, 153, 81, 72, 100,
    # Şubat 2026 (21 gün)
    70, 87, 89, 101, 35, 169, 123, 115, 62, 73, 21,
    39, 90, 88, 120, 111, 145, 184, 199, 132, 139,
    # Mart 2026 (13 gün)
    45, 67, 115, 25, 136, 157, 88, 71, 95, 90, 126, 104, 88,
]
```

**İstatistikler:**

| Parametre | Değer |
|-----------|-------|
| Gün sayısı | 58 |
| Ortalama | 95.0 palet/gün |
| λ (saatlik) | 11.9 palet/saat |
| Min | 6 palet/gün |
| Max | 199 palet/gün |
| VMR | 18.2 (overdispersed) |

**Kullanım — Mixed-Poisson:**
Saf Poisson (VMR≈1) yerine **Mixed-Poisson** yaklaşımı:
1. Her simülasyon günü (replikasyon) için listedeki günlerden rastgele biri seçilir → o günün palet sayısı alınır
2. Bu sayı 8 saate bölünerek günlük λ hesaplanır
3. Gün içinde paletler bu λ ile Poisson süreciyle (üstel arageliş) tetiklenir

Bu yöntem, günden güne büyük farkları (6 vs 199) doğru yakalamayı sağlar.

**FK2 Split:** Makineye ve Alana faaliyetleri toplam paletin %50'sini alır (`_poisson_split = 0.5`).

---

## 🔄 Veri Akışı (Simülasyon Süreci)

```
┌─────────────┐     ┌──────────────┐     ┌──────────────────────┐
│  forklift.db │────▶│  load_data() │────▶│  ForkliftSimulation  │
│              │     │              │     │                      │
│ • vardiya    │     │ vardiya_start│     │ • PriorityResource   │
│ • mola       │     │ vardiya_end  │     │ • regular_process    │
│ • forklift   │     │ breaks[]     │     │ • poisson_process    │
│ • faaliyet   │     │ activities[] │     │ • break handling     │
│ • tir_config │     │ tir_config   │     │ • aging priority     │
└─────────────┘     └──────────────┘     └──────────┬───────────┘
                                                     │
                    ┌──────────────────────────────────┘
                    ▼
          ┌─────────────────┐     ┌──────────────────┐
          │ run_multi_rep() │────▶│   JSON Response    │
          │                 │     │                    │
          │ × 50 replikasyon│     │ • events[]         │
          │ × 8 senaryo     │     │ • stats{}          │
          │                 │     │ • replications[]   │
          └─────────────────┘     │ • activity_freq[]  │
                                  └────────┬───────────┘
                                           │
                    ┌──────────────────────┘
                    ▼
          ┌──────────────────────────────────────┐
          │            Web Arayüzü                │
          │                                       │
          │ • /timeline-simpy  → Gantt + KPI       │
          │ • /replications    → 50 koşum detayı   │
          │ • /                → Veri düzenleme     │
          │ • /timeline        → Deterministik     │
          └──────────────────────────────────────┘
```

---

## 🎛 Senaryolar

Simülasyon motoru 8 farklı senaryo çalıştırır:

| # | Senaryo | Açıklama | Forkliftler |
|---|---------|----------|-------------|
| 1 | Mevcut | Her FK kendi bölgesinde | FK1, FK2, FK3 ayrı |
| 2 | Gece Delegasyonu | Gece işleri havuzda | Gece faaliyetleri paylaşımlı |
| 3 | FK1+FK2 | İki FK birleşik çalışır | FK1 & FK2 paylaşımlı |
| 4 | FK1+FK3 | İki FK birleşik çalışır | FK1 & FK3 paylaşımlı |
| 5 | FK2+FK3 | İki FK birleşik çalışır | FK2 & FK3 paylaşımlı |
| 6-8 | Gece + Birleşik | Gece delegasyonu + birleşme | Kombinasyonlar |

---

## 📝 Veritabanını Düzenleme

- **Web arayüzü:** `http://localhost:5050` → Veri Girişi sayfası ile tüm tablolar düzenlenebilir
- **Sıfırlama:** `forklift.db` dosyasını silip uygulamayı yeniden başlatın → varsayılan veriler oluşturulur
- **Yedekleme:** `forklift.db` dosyasını kopyalayın
