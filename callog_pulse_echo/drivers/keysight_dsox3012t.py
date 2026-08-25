"""Keysight DSOX3012T (InfiniiVision 3000T X serisi) osiloskop sürücüsü.

SCPI kümesi 1200 X ailesiyle aynı; ortak davranış
`keysight_infiniivision.InfiniiVisionScope` içinde. Ses hızı ölçümü için
önemli olan farklar:

* **Çok daha yüksek örnekleme hızı ve bellek.** Darbe-yankı ölçümünde
  zamanlama çözünürlüğü doğrudan buna bağlı: 25 mm'lik blokta dört yankı
  ~32 µs'lik bir pencereye yayılır ve bu pencere en yüksek örnekleme
  hızında bile bellek sınırına yaklaşmaz. Gerçek hız veri sayfasından
  varsayılmıyor, `sample_rate()` ile cihazdan okunuyor.
* **Yüksek çözünürlük (HRESolution) kipi.** 3. ve 4. yansıma ilk yankının
  onda birine kadar iner; 8 bitlik dikey ızgarada bu birkaç basamağa
  sıkışır ve tepe seçimi kuantalama merdivenine takılır.
* **Harici tetikleme girişi.** Fonksiyon üretecinin SYNC çıkışı buraya
  bağlanır. Yankının kendisiyle tetiklemek zaman referansını ölçülen
  sinyale bağlar ve kararsızdır.

``*IDN?`` yanıtı: ``KEYSIGHT TECHNOLOGIES,DSO-X 3012T,MY00000000,07.xx.xxxx``
"""

from .keysight_infiniivision import InfiniiVisionScope


class KeysightDSOX3012T(InfiniiVisionScope):

    # "3012T" tek başına yetmiyor: bazı aygıt yazılımları model alanını
    # "DSO-X 3012T", bazıları "DSOX3012T" biçiminde veriyor. Ortak parça
    # "3012" olduğu için eşleşme onun üzerinden yapılıyor.
    MODEL_TOKENS = ("3012", "3000T", "DSO-X 3")
    MODEL_NAME = "DSOX3012T"

    CHANNELS = (("CHANnel1", "Kanal 1"), ("CHANnel2", "Kanal 2"))

    #: Harici tetikleme kaynağının SCPI adı. Fonksiyon üretecinin SYNC
    #: çıkışı bu girişe bağlandığında tetikleme kaynağı olarak seçilir.
    EXTERNAL_TRIGGER = "EXTernal"
