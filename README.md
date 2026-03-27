# 🚜 RHI Magnesita — Forklift Süreç Simülasyonu

Tuğla boşaltma, paketleme ve sevkiyat bölgesindeki 3 forkliftin operasyonel verimliliğini analiz eden ve ortaklaştırma senaryolarını simüle eden web tabanlı araç.

## Amaç

- Forklift kullanım oranlarını ölçmek
- Faaliyetlerin çakışma ve gecikme analizini yapmak
- Ortaklaştırma senaryolarıyla kiralık forklift maliyetini azaltmak
- Gece vardiyasına devredilebilecek faaliyetleri belirlemek

## Kurulum

```bash
# Gereksinimler
pip install flask

# Çalıştırma
python3 app.py
```

Sunucu başladıktan sonra:
- **Veri Girişi:** http://localhost:5050
- **Simülasyon:** http://localhost:5050/timeline

## Proje Yapısı

```
SİMÜLASYON/
├── app.py              # Flask backend + SQLite API
├── simulation.py       # Discrete-event simülasyon motoru
├── forklift.db         # SQLite veritabanı (otomatik oluşur)
├── templates/
│   ├── index.html      # Veri girişi arayüzü
│   └── timeline.html   # Gantt chart & senaryo karşılaştırma
└── README.md
```

## Operasyonel Veriler

| Vardiya | Molalar |
|---|---|
| 08:00 – 16:00 | 09:45–10:00, 11:30–12:00, 13:45–14:00 |

### Forkliftler

| Forklift | Bölge | Faaliyet Sayısı |
|---|---|---|
| 🧱 Forklift 1 | Tuğla Boşaltma | 4 |
| 📦 Forklift 2 | Paketleme | 4 |
| 🚛 Forklift 3 | Sevkiyat | 1 |

## Simülasyon Senaryoları

Sistem 8 farklı senaryoyu otomatik olarak simüle eder:

| # | Senaryo | FK | Açıklama |
|---|---|---|---|
| 1 | Mevcut Durum | 3 | Her forklift kendi işini yapar |
| 2 | Gece Delegasyonu | 3 | Düşük öncelikli işler gece vardiyasına |
| 3 | F1+F2 Birleşim | 2 | Boşaltma + Paketleme tek forklift |
| 4 | F1+F3 Birleşim | 2 | Boşaltma + Sevkiyat tek forklift |
| 5 | F2+F3 Birleşim | 2 | Paketleme + Sevkiyat tek forklift |
| 6-8 | Gece + Birleşim | 2 | Gece delegasyonu + birleşim kombinasyonları |

## Gecikme Hesabı

Sistem **net gecikme** hesaplar:

```
Net Gecikme = Brüt Gecikme − Mola Süresi
```

Örnek: Faaliyet 11:28'de planlandı, öğle arası nedeniyle 12:01'de yapıldı:
- Brüt: 33 dk
- Mola: 30 dk (11:30–12:00)
- **Net: 3 dk** ✅

## Özellikler

### Veri Girişi (index.html)
- Çevrim süresi, tekrar süresi (dk/saat), öncelik, gecikme toleransı
- 🌙 Gece vardiyası toggle
- `Cmd+S` ile hızlı kaydetme
- JSON dışa aktarma

### Simülasyon (timeline.html)
- İnteraktif Plotly.js Gantt chart
- Senaryo sekmeleri ile anında geçiş
- KPI kartları (kullanım, gecikme, ihlal)
- Forklift detay istatistikleri + kullanım çubukları
- Karşılaştırma tablosu

## Teknolojiler

- **Backend:** Python, Flask, SQLite
- **Frontend:** HTML/CSS/JS, Plotly.js
- **Simülasyon:** Discrete-event scheduling
