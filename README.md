# CalLog Ses Hızı

Darbe-yankı ultrasonik yöntemiyle çelik blokta ses hızı / kalınlık ölçümü
ve ML kalınlık kestirimi. Kalibrasyon laboratuvarları için bağımsız bir
ölçüm kayıt sistemi — kurulum ve depo tek başına yeterli, başka bir
depoya ihtiyaç duymaz.

Geliştiren: **Cem Girgin**  ·  Lisans: [Özel Kullanım Lisansı](LICENSE)

Kurum adı, birim adı ve logo kaynak kodda gömülü değildir; kurulumdan
sonra Yönetim → Laboratuvar sayfasından girilir ve veritabanında saklanır
(bkz. [`callog_common/branding.py`](callog_common/branding.py)). Bu depo
hiçbir kuruma özgü bilgi içermez.

Donanım kurulumu, DPR300 pulser/receiver ayarları ve doğrulama adımları
için: [`docs/ses-hizi-kurulum.pdf`](docs/ses-hizi-kurulum.pdf) — uygulama
içinden de **Yardım → Ses hızı kurulum kılavuzu (PDF)**.

---

## Ne yapar

1. Kullanıcı kendi hesabıyla giriş yapar (kayıt kime ait — izlenebilirliğin temeli)
2. Kalibrasyona gelen cihazın (DUT) bilgileri elle girilir
3. Referans osiloskop (Keysight DSOX3012T) otomatik tespit edilir veya elle adres girilir
4. **Ses hızı** sayfasında cihaz sürekli okunur, her kare hem klasik DSP
   (paket/zarf tespiti, çapraz korelasyon + faz eğimi) hem eğitilmiş bir
   ML modeliyle çözümlenir; "Durdur ve ölç" o kareyi ölçüm olarak kaydeder.
   Cihaz bağlı olmadan da çalışılabilir: "CSV'den yükle…" daha önce
   kaydedilmiş ham dalga CSV'sini okuyup canlı bir kareymiş gibi çözümler
5. Her şey hash zinciriyle korunan denetim kaydına yazılır; geçmiş
   kayıtlarda arama, lab sorumlusu onayı, Excel'e aktarım

**İkincil/deneysel ölçüm yolu:** defibrilatör akışının aksine resmî bir
sertifika değil, DSP ve ML tahminini yan yana gösteren bir analiz ekranı.

## Kurulum

Windows 10/11 + Python 3.12.

```bash
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python run.py
```

İlk açılışta kullanıcı olmadığı için yönetici hesabı oluşturma penceresi gelir.

**Cihaz olmadan denemek için:** referans cihaz listesinden
`[SİMÜLASYON] Keysight DSOX3012T` seçin. Sentetik yankı dizisi üretilir;
kalınlığı değiştirdikçe yankı aralığı da değişir.

**ML modeli:** `callog_seshizi/ml_models.py`, `dataset/models/*.pkl`
dosyalarını çalışma zamanında yükler. Bu depoda henüz eğitilmiş bir model
yok — model dosyaları ayrı bir depoda ([`pulse_echo-ml`](https://github.com/cemmgrgn/pulse_echo-ml))
eğitilip buradaki `dataset/models/` altına elle kopyalanmalı; yoksa arayüz
sessizce Klasik DSP'ye geri döner.

## Dosya düzeni

```
callog_common/          Paylaşılan altyapı — bkz. aşağıdaki "Bağımlılık" bölümü
callog_seshizi/
├── ultrasonic.py         DSP çözümleme: paket tespiti, çapraz korelasyon + faz eğimi
├── feature_extraction.py Öznitelik çıkarımı (ML modeli için)
├── ml_models.py          Eğitilmiş model yükleme + tahmin
├── measure.py             Ekran ölçüm matematiği (Vpp, frekans...)
├── setupadvice.py         Canlı ayar önerileri (kırpma, düşük SNR...)
├── seshizi_modes.py       Ses hızı test modunu callog_common.testmodes'a kaydeder
├── drivers/
│   ├── keysight_dsox3012t.py, keysight_infiniivision.py
│   └── simulated_ultrasonic.py
└── ui/
    ├── main_window.py     callog_common'ın BaseMainWindow'unu genişletir
    ├── velocity_page.py, velocity_results.py   Ses hızı sayfası
    └── scope_view.py       Osiloskop ekranı görünümü (bölme ızgarası, imleçler)
```

## `callog_common/`

Kullanıcı/rol yönetimi, sertifika üretimi, denetim kaydı, veritabanı
şeması, yedekleme, tema ve dil, ölçüm oturumu akışı gibi laboratuvar
altyapısının tamamı `callog_common/` altında — bu paylaşılan altyapı,
kardeş bir uygulamada da kullanılan ayrı bir kopya olarak burada duruyor
(canlı bir bağlantı/bağımlılık değil).

**Bunun bedeli:** `callog_common`'da ileride bir hata düzeltilirse bu
depodaki kopya kendiliğinden güncellenmez, elle senkronize edilmeli.

`callog_common/ui/approvals_page.py` ve `devices_page.py`, bu depoda
bulunmayan bir rapor koduna (`seriesreport`, `summaryreport`) **isteğe
bağlı** olarak içe aktarmaya çalışır (`try/except ImportError`) — ilgili
ekran nazikçe "bu kurulumda görüntülenemiyor" der; onay kuyruğunun
kendisi (aynı veritabanı paylaşılıyorsa) yine de çalışır.

## Test

```bash
python tests/smoke_test.py       # pyvisa/reportlab gerektirmez, ama PySide6 kurulu olmalı
python tests/gui_smoke_test.py   # ekransız (offscreen) çalışır
```

Windows'ta ekransız Qt platformu bazen sistem fontlarını bulamaz; bu durumda
her iki testten önce şunu ayarlayın:

```bash
set QT_QPA_FONTDIR=C:\Windows\Fonts
```

(`gui_smoke_test.py` `QT_QPA_PLATFORM=offscreen`'i kendi içinde ayarlar,
elle set etmeye gerek yok.)

`smoke_test.py`: 188/188. `gui_smoke_test.py`: 312/313 — kalan tek başarısızlık
(`kararlilik seridi en genis satir degil`), bu depoda hiç değiştirilmemiş
`acquire_page.py` kodunda, piksel genişliği karşılaştıran bir kontrol;
ekransız (offscreen) test ortamının font metrikleriyle ilgili görünüyor.
Gerçek ekranda ayrıca doğrulanmadı.

Bu test dosyaları CalLog'un `callog_defib`/`callog_seshizi` ayrımından
önceki, tek-uygulama dönemine ait; bu depoda **var olmayan** defibrilatör
işlevini (`WaveformPage`, şok/seri/toplu rapor) sınayan bölümler tamamen
çıkarıldı — geri kalanı (ortak altyapı: kimlik doğrulama, denetim kaydı,
sertifika, tema/dil, ölçüm planı, arama...) import yolları düzeltilerek
korundu. **Ses hızı / ultrasonik özelliğine (`velocity_page.py`,
`ultrasonic.py`, ML model) özgü hiçbir test yok** — bu dosyalar CalLog'a
ses hızı özelliği eklenmeden önce yazıldığı için hiç yoktu, buraya
taşınırken de eklenmedi. Yeni test yazımı bekliyor.

## Yeni cihaz eklemek

1. `callog_seshizi/drivers/` altında `callog_common.drivers.base.Driver`'ı
   miras alan bir modül yaz
2. `callog_seshizi/__init__.py`'den `drivers.register_driver()` ile kaydet
3. Otomatik tespit için `callog_common/drivers/discovery.py`'deki
   `KNOWN_MODELS`'e marka/model desenini ekle
