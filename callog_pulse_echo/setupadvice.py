"""Yakalanan kareye bakıp kurulum önerisi üretir.

Neden ayrı bir katman: `ultrasonic.analyze` "ölçebildim / ölçemedim" der,
sebebini de söyler; ama sebep genellikle **cihazdaki bir düğmedir** ve
operatörün bilmesi gereken şey hangisini ne yöne çevireceğidir. Sahada
geçen süre bunu deneme yanılmayla bulmakla geçiyordu: kazanç mı düşük,
filtre mi yanlış, darbe mi uzun.

Kurallar dalganın kendisinden hesaplanıyor; prob, malzeme ya da cihaz
markası hakkında varsayım yok. Tek dayanak darbe-yankı düzeninin fiziği:
ana darbe en başta gelir, yankılar **sönerek** ve eşit aralıklarla izler,
aralarında sessizlik olur. Bu, çeliğe de fantoma da aynı biçimde uyar.

Qt'siz, `defib.py` ve `ultrasonic.py` gibi: aynı öneri hem ekranda hem
testte üretilebilsin diye.
"""

import numpy as np

#: Önem düzeyleri. "engel" ölçümü imkânsız kılan, "uyarı" sonucu bozan,
#: "bilgi" iyileştirme önerisi.
BLOCK = "engel"
WARN = "uyarı"
INFO = "bilgi"

#: Ana darbeden sonra sinyalin gürültü tabanının kaç katı olması gerektiği.
#: Bunun altında yankı yok sayılıyor.
MIN_ECHO_OVER_NOISE = 3.0

#: Ölçülen bölgede bu oranda örnek tepe değerdeyse dalga kırpılıyor.
RAIL_FRACTION = 0.02

#: Zarfın medyanı tepesinin bu oranını aşıyorsa kayıtta sessizlik kalmamış:
#: ya kazanç çok yüksek ya önceki atımın yankıları sönmemiş.
NO_GAP_RATIO = 0.40

#: Sönme eğimi bunun üstündeyse genlik zamanla **artıyor** demektir.
GROWTH_SLOPE = 0.05


def advise(times, values, thickness_m=None, reference_velocity=None,
           result=None):
    """(önem, sorun, yapılacak) üçlülerinden oluşan liste döndürür.

    result: `ultrasonic.analyze` sonucu, varsa. Yoksa yalnızca dalgadan
    çıkarılabilen kurallar uygulanır.
    """
    n = min(len(times), len(values))
    if n < 32:
        return []
    times = np.asarray(times, dtype=np.float64)[:n]
    values = np.asarray(values, dtype=np.float64)[:n]
    values = values - float(np.median(values))

    peak = float(np.max(np.abs(values)))
    if peak <= 0:
        return [(BLOCK, "Kayıtta hiç sinyal yok.",
                 "Prob kablosunu ve pulser çıkışını kontrol edin.")]

    out = []
    step = _quantisation_step(values)
    bang_start, bang_end = _bang_span(times, values, step)
    tail = np.abs(values[times > bang_end])
    tail_peak = float(np.max(tail)) if tail.size else 0.0
    noise = max(step, _noise_floor(values))

    # --- yankı var mı ---------------------------------------------------
    #
    # Bakılan yer, ana darbeden hemen sonrası **değil**: orası darbenin
    # kendi çınlaması ve yankıdan ayırt edilemiyor. Kalınlık ve referans hız
    # biliniyorsa ilk yankının nereye düşmesi gerektiği de biliniyor;
    # denetim tam o pencereye bakıyor.
    spacing = None
    if thickness_m and reference_velocity:
        spacing = 2.0 * float(thickness_m) / float(reference_velocity)

    if spacing:
        window = ((times >= bang_start + 0.7 * spacing)
                  & (times <= bang_start + 1.3 * spacing))
        where = "ilk yankının beklendiği yerde (%s civarı)" % _s(spacing)
    else:
        # Kalınlık bilinmiyorsa darbenin bir boyu kadar sonrasına bakılıyor.
        guard = max(bang_end - bang_start, 0.0)
        window = times > bang_end + guard
        where = "ana darbeden sonra"
    region = np.abs(values[window])
    tail_peak = float(np.max(region)) if region.size else 0.0

    if region.size and tail_peak < MIN_ECHO_OVER_NOISE * noise:
        out.append((BLOCK,
                    "%s yankı yok (%.4f V, gürültü tabanı %.4f V)."
                    % (where.capitalize(), tail_peak, noise),
                    "Sırayla deneyin: kuplaj jelini yenileyip probu bastırın; "
                    "REL. GAIN'i yükseltin; LP FILTER'ı yükseltin — prob "
                    "bandının üstünü kesiyorsa yankılar doğrudan yok olur."))
    elif region.size and tail_peak < 8.0 * noise:
        out.append((WARN,
                    "Yankılar gürültünün ancak %.0f katı." % (tail_peak / noise),
                    "REL. GAIN'i yükseltin ya da donanım ortalamasını "
                    "artırın."))

    # --- kırpma ----------------------------------------------------------
    railed = float(np.mean(np.abs(values) >= 0.98 * peak))
    if railed > RAIL_FRACTION:
        out.append((WARN,
                    "Örneklerin yüzde %.1f'i tepe değerde — dalga kırpılıyor."
                    % (100 * railed),
                    "REL. GAIN'i düşürün; kırpılmış bir yankının zamanlaması "
                    "kayar ve bunu hesaptan anlamak mümkün değildir."))

    # --- kayıtta sessizlik kalmış mı --------------------------------------
    envelope = _rough_envelope(np.abs(values))
    top = float(np.max(envelope))
    gap_ratio = (float(np.median(envelope)) / top) if top > 0 else 0.0
    if gap_ratio > NO_GAP_RATIO:
        out.append((WARN,
                    "Kayıtta sessiz bölge kalmamış (zarf medyanı tepenin "
                    "yüzde %.0f'i)." % (100 * gap_ratio),
                    "PRF RATE'i düşürün — önceki atımın yankıları sönmeden "
                    "yenisi giriyor olabilir. Sürerse REL. GAIN'i düşürün."))

    # --- genlik sönüyor mu, büyüyor mü ------------------------------------
    slope = _decay_slope(times, values, bang_end)
    if slope is not None and slope > GROWTH_SLOPE:
        out.append((BLOCK,
                    "Genlik zamanla artıyor; yankı dizisi sönmüyor.",
                    "Bakılan şey bir yankı dizisi değil. PRF RATE'i düşürün "
                    "(önceki atımın kuyruğunu görüyor olabilirsiniz) ve "
                    "tetiklemenin ana darbeye kilitlendiğini doğrulayın."))

    # --- prob bandı ve filtre ---------------------------------------------
    band = _band(values, _sample_interval(times))
    if band:
        low, centre, high = band
        out.append((INFO,
                    "Ölçülen band %.2f – %.2f MHz (merkez %.2f MHz)."
                    % (low / 1e6, high / 1e6, centre / 1e6),
                    "HP FILTER %.1f MHz'in altında, LP FILTER %.1f MHz'in "
                    "üstünde olmalı." % (low / 1e6, high / 1e6)))

    # --- kalınlığa bağlı kurallar -----------------------------------------
    if spacing:
        bang_length = bang_end - bang_start
        if bang_length > 0.5 * spacing:
            out.append((WARN,
                        "Darbe süresi (%s) yankı aralığının (%s) yarısını "
                        "aşıyor." % (_s(bang_length), _s(spacing)),
                        "DAMPING'i yükseltin ve PULSE ENERGY'yi düşürün. "
                        "Yetmezse daha yüksek frekanslı prob ya da daha "
                        "kalın basamak gerekir."))

    if result is not None and result.get("found"):
        wanted = result.get("requested_echoes")
        found = len(result.get("packets", []))
        if wanted and found < wanted:
            out.append((INFO,
                        "%d yankı istendi, %d bulundu." % (wanted, found),
                        "REL. GAIN'i ya da donanım ortalamasını artırın."))
    return out


# --- ölçümler ---------------------------------------------------------------
def _sample_interval(times):
    return float((times[-1] - times[0]) / (len(times) - 1))


def _quantisation_step(values):
    """Sayısallaştırma adımı — farklı değerler arasındaki en küçük fark.

    Gürültü tabanını buna göre yorumlamak gerekiyor: tam bir basamak
    genliğindeki bir "sinyal" aslında sıfırdır. Ölçülen kayıtlarda yankısız
    bölgeler tam olarak bir basamakta duruyordu.
    """
    unique = np.unique(np.abs(values))
    if unique.size < 3:
        return 0.0
    diffs = np.diff(unique)
    diffs = diffs[diffs > 0]
    return float(np.median(diffs)) if diffs.size else 0.0


def _noise_floor(values):
    """Kaydın en sessiz dörtte birinin RMS'i."""
    magnitude = np.sort(np.abs(values))
    quiet = magnitude[:max(8, magnitude.size // 4)]
    return float(np.sqrt(np.mean(quiet ** 2)))


def _bang_span(times, values, step):
    """Ana darbenin bittiği an.

    Dönüş: (başlangıç, bitiş). Süre bu ikisinin farkı; kaydın başından
    ölçmek yanlış olurdu, çünkü tetikleme öncesi pay da içeri girer ve
    darbe olduğundan uzun görünür.

    Aranan şey kaydın **en büyük** noktası değil, **ilk** güçlü bölgesi.
    Fark önemli: alıcı kazancı yükseltildiğinde bir yankı ana darbeden
    büyük çıkabiliyor, ölçülen kayıtlarda bu oldu. En büyükten başlayan bir
    arama o zaman ana darbeyi tamamen atlıyor ve "darbeden sonrası" diye
    bakılan bölge yankı dizisinin ortasından başlıyordu -- sönme denetimi de,
    darbe süresi denetimi de bu yüzden çalışan bir ölçüme yanlış öneri
    veriyordu.
    """
    peak = float(np.max(np.abs(values)))
    onset_level = max(0.2 * peak, 5.0 * step)
    magnitude = np.abs(values)
    strong = np.flatnonzero(magnitude >= onset_level)
    if strong.size == 0:
        return float(times[0]), float(times[0])
    start = int(strong[0])

    limit = max(0.02 * peak, 3.0 * step)
    below = 0
    hold = max(8, values.size // 200)
    for i in range(start, values.size):
        if magnitude[i] <= limit:
            below += 1
            if below >= hold:
                return float(times[start]), float(times[i - hold + 1])
        else:
            below = 0
    return float(times[start]), float(times[-1])


def _rough_envelope(magnitude, windows=200):
    """Pencere başına tepe — taşıyıcıyı bilmeye gerek bırakmayan kaba zarf."""
    width = max(4, magnitude.size // windows)
    trimmed = magnitude[:magnitude.size - magnitude.size % width]
    if trimmed.size == 0:
        return magnitude
    return trimmed.reshape(-1, width).max(axis=1)


def _decay_slope(times, values, bang_end):
    """Darbeden sonraki genliğin logaritmik eğimi.

    Yankı dizisi sönmek zorunda: her yansıma soğurmayla ve ön yüzden
    geçişle enerji kaybeder. Artan bir genlik, bakılan şeyin yankı dizisi
    olmadığını söyler — ölçülen bir kayıtta bu, önceki atımın kuyruğuna
    denk gelmiş bir pencereydi ve hiçbir sayısal ölçüt bunu yakalamıyordu.
    """
    mask = times > bang_end
    if int(np.count_nonzero(mask)) < 64:
        return None
    magnitude = _rough_envelope(np.abs(values[mask]), windows=24)
    magnitude = magnitude[magnitude > 0]
    if magnitude.size < 6:
        return None
    x = np.linspace(0.0, 1.0, magnitude.size)
    slope, _intercept = np.polyfit(x, np.log(magnitude), 1)
    return float(slope)


def _band(values, dt, drop_db=6.0):
    """Sinyalin -6 dB bandı: (alt, merkez, üst) Hz.

    Tayf **düzleştirilerek** ölçülüyor. Yankı dizisinin tayfı tarak
    biçimindedir: eşit aralıklı yankılar, aralarındaki gecikmenin tersi
    kadar aralıklı sivri tepeler üretir. Ham tayfta en yüksek tepenin -6 dB
    genişliği o tarağın tek bir dişini ölçer ve prob bandını onda biri kadar
    dar gösterir -- ölçülen bir kayıtta 2,5 MHz'lik prob için 0,2 MHz'lik
    band çıktı. Düzleştirme tarağı siler, geriye probun kendi tepkisi kalır.

    Prob etiketine bakılmıyor: ölçülen band neyse filtreler onun dışına
    açılmalı. Etikete güvenmek bir kez yanılttı -- "016" 16 MHz sanıldı,
    prob ~5 MHz çıktı ve yüksek geçiren filtre tam merkeze oturunca
    yankılar yok oldu.
    """
    if values.size < 64 or not dt:
        return None
    spectrum = np.abs(np.fft.rfft(values * np.hanning(values.size)))
    freqs = np.fft.rfftfreq(values.size, dt)
    if spectrum.size < 32:
        return None

    # DC ve en alt birkaç bin dışarıda: zarf bileşenleri taşıyıcı değil.
    first = max(2, spectrum.size // 500)
    spectrum = spectrum[first:]
    freqs = freqs[first:]

    spectrum = _smooth(spectrum, max(3, spectrum.size // 60))
    peak = int(np.argmax(spectrum))
    threshold = spectrum[peak] * (10.0 ** (-drop_db / 20.0))
    low = peak
    while low > 0 and spectrum[low] > threshold:
        low -= 1
    high = peak
    while high < spectrum.size - 1 and spectrum[high] > threshold:
        high += 1
    return float(freqs[low]), float(freqs[peak]), float(freqs[high])


def _smooth(x, width):
    """Kayan ortalama — tayftaki tarak yapısını silmek için."""
    if width < 2 or x.size < width:
        return x
    pad = width // 2
    padded = np.pad(x, (pad, width - 1 - pad), mode="edge")
    cumulative = np.cumsum(np.insert(padded, 0, 0.0))
    return (cumulative[width:] - cumulative[:-width]) / float(width)


def _s(value):
    if value is None:
        return "—"
    for factor, prefix in ((1.0, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n")):
        if abs(value) >= factor:
            return "%.4g %ss" % (value / factor, prefix)
    return "%.3g s" % value
