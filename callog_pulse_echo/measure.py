"""Automatic waveform measurements -- the quantities an oscilloscope reports.

Qt-free on purpose, like `defib.py` and `ultrasonic.py`: the numbers shown on
screen, written into a report, and asserted in a test all come from here, so
there is one definition of "Vpp" in the project rather than one per caller.

Two conventions worth stating, because they are where naive implementations
disagree with the instrument:

**Top and base are not max and min.** A pulse with overshoot has a peak well
above its settled high level. Amplitude, rise time, and the mid-level used
for period and width all refer to the *settled* levels, so those are found
from the histogram -- the two most populated levels -- and only fall back to
max/min when the signal has no two distinct levels to find.

**Crossings are interpolated.** A level is crossed between two samples, not
at one. Taking the nearest sample quantises every time measurement to the
sample interval, which at a fast timebase is the whole measurement. The
crossing time is interpolated linearly between the bracketing samples, the
same reason `ultrasonic` fits a parabola to its peaks.
"""

import math

#: (key, label, unit). The order is the order offered on screen.
MEASUREMENTS = (
    ("vpp", "Tepeden tepeye (Vpp)", "V"),
    ("vmax", "En büyük (Vmax)", "V"),
    ("vmin", "En küçük (Vmin)", "V"),
    ("vamp", "Genlik (Vamp)", "V"),
    ("vtop", "Üst seviye (Vtop)", "V"),
    ("vbase", "Alt seviye (Vbase)", "V"),
    ("vavg", "Ortalama (Vavg)", "V"),
    ("vrms", "RMS", "V"),
    ("vrms_ac", "RMS (AC)", "V"),
    ("overshoot", "Aşım", "%"),
    ("freq", "Frekans", "Hz"),
    ("period", "Periyot", "s"),
    ("rise", "Yükselme (%10–%90)", "s"),
    ("fall", "Düşme (%90–%10)", "s"),
    ("pwidth", "Pozitif genişlik", "s"),
    ("nwidth", "Negatif genişlik", "s"),
    ("duty", "Görev çevrimi", "%"),
    ("area", "Alan (∫v dt)", "V·s"),
)

BY_KEY = dict((key, (label, unit)) for key, label, unit in MEASUREMENTS)

#: Histogram bin count for the top/base search. Too few bins merge the two
#: levels of a low-amplitude signal; too many spread a noisy level across
#: several bins and no single bin stands out.
HISTOGRAM_BINS = 64

#: A histogram peak has to hold at least this share of the samples on its
#: side of the midpoint to count as a settled level. Below it the signal is
#: treated as having no flat levels -- a sine, a decaying echo -- and
#: max/min are used instead.
LEVEL_MIN_SHARE = 0.05


def window(times, values, start=None, end=None):
    """Restricts the record to a time range -- the region between two cursors."""
    out_t, out_v = [], []
    for t, v in zip(times, values):
        if start is not None and t < start:
            continue
        if end is not None and t > end:
            continue
        out_t.append(float(t))
        out_v.append(float(v))
    return out_t, out_v


def compute(key, times, values):
    """One measurement. None when it cannot be made from this record.

    None rather than an exception or a sentinel number: a scope shows a
    measurement it cannot make as blank, and a frequency reading on a record
    with no complete cycle is exactly that case.
    """
    n = min(len(times), len(values))
    if n < 2:
        return None
    times = [float(t) for t in times[:n]]
    values = [float(v) for v in values[:n]]

    handler = _HANDLERS.get(key)
    return handler(times, values) if handler else None


def compute_all(keys, times, values):
    """[(label, formatted value)] -- what the measurement panel shows."""
    rows = []
    for key in keys:
        label, unit = BY_KEY.get(key, (key, ""))
        rows.append((label, format_value(compute(key, times, values), unit)))
    return rows


# --- level detection -------------------------------------------------------
def _levels(values):
    """(top, base) settled levels, from the value histogram.

    Falls back to (max, min) when either half has no populated level, which
    is the normal outcome for a signal that never settles -- a sine, or the
    ring-down of an ultrasonic echo.
    """
    lo, hi = min(values), max(values)
    if hi <= lo:
        return hi, lo
    mid = 0.5 * (lo + hi)
    width = (hi - lo) / float(HISTOGRAM_BINS)

    upper, lower = {}, {}
    for v in values:
        index = min(HISTOGRAM_BINS - 1, int((v - lo) / width))
        bucket = upper if v >= mid else lower
        bucket[index] = bucket.get(index, 0) + 1

    def peak(bucket, fallback):
        if not bucket:
            return fallback
        index, count = max(bucket.items(), key=lambda kv: kv[1])
        if count < LEVEL_MIN_SHARE * len(values):
            return fallback
        return lo + (index + 0.5) * width

    return peak(upper, hi), peak(lower, lo)


def _crossings(times, values, level, rising=None):
    """Interpolated times at which the signal crosses `level`.

    rising: True only upward, False only downward, None both.
    """
    out = []
    for i in range(len(values) - 1):
        a, b = values[i], values[i + 1]
        if a == b:
            continue
        up = a < level <= b
        down = a > level >= b
        if not (up or down):
            continue
        if rising is True and not up:
            continue
        if rising is False and not down:
            continue
        ratio = (level - a) / (b - a)
        out.append((times[i] + ratio * (times[i + 1] - times[i]), up))
    return out


def _mid_level(values):
    top, base = _levels(values)
    return 0.5 * (top + base), top, base


# --- individual measurements ------------------------------------------------
def _vmax(_t, v):
    return max(v)


def _vmin(_t, v):
    return min(v)


def _vpp(_t, v):
    return max(v) - min(v)


def _vtop(_t, v):
    return _levels(v)[0]


def _vbase(_t, v):
    return _levels(v)[1]


def _vamp(_t, v):
    top, base = _levels(v)
    return top - base


def _vavg(_t, v):
    return sum(v) / len(v)


def _vrms(_t, v):
    return math.sqrt(sum(x * x for x in v) / len(v))


def _vrms_ac(_t, v):
    mean = sum(v) / len(v)
    return math.sqrt(sum((x - mean) ** 2 for x in v) / len(v))


def _overshoot(_t, v):
    """Peak above the settled top, as a percentage of amplitude."""
    top, base = _levels(v)
    amplitude = top - base
    if amplitude <= 0:
        return None
    return 100.0 * (max(v) - top) / amplitude


def _period(t, v):
    """Median interval between same-slope mid-level crossings.

    Median, not mean: on a record whose last cycle is clipped by the screen
    edge the final interval is short, and a mean would drag the whole
    reading down with it.
    """
    level, top, base = _mid_level(v)
    if top <= base:
        return None
    marks = [time for time, up in _crossings(t, v, level, rising=True)]
    if len(marks) < 2:
        return None
    gaps = sorted(marks[i + 1] - marks[i] for i in range(len(marks) - 1))
    middle = len(gaps) // 2
    return (gaps[middle] if len(gaps) % 2
            else 0.5 * (gaps[middle - 1] + gaps[middle]))


def _freq(t, v):
    period = _period(t, v)
    return (1.0 / period) if period else None


def _edge_time(t, v, rising):
    """10%-90% transition time on the first suitable edge."""
    top, base = _levels(v)
    amplitude = top - base
    if amplitude <= 0:
        return None
    low = base + 0.1 * amplitude
    high = base + 0.9 * amplitude

    low_marks = _crossings(t, v, low, rising=rising)
    high_marks = _crossings(t, v, high, rising=rising)
    if not low_marks or not high_marks:
        return None

    first, second = (low_marks, high_marks) if rising else (high_marks,
                                                            low_marks)
    start = first[0][0]
    for time, _up in second:
        if time > start:
            return time - start
    return None


def _rise(t, v):
    return _edge_time(t, v, rising=True)


def _fall(t, v):
    return _edge_time(t, v, rising=False)


def _widths(t, v):
    """(positive width, negative width) around the mid level."""
    level, top, base = _mid_level(v)
    if top <= base:
        return None, None
    marks = _crossings(t, v, level)
    if len(marks) < 2:
        return None, None
    positive = negative = None
    for i in range(len(marks) - 1):
        time, up = marks[i]
        span = marks[i + 1][0] - time
        if up and positive is None:
            positive = span
        elif not up and negative is None:
            negative = span
    return positive, negative


def _pwidth(t, v):
    return _widths(t, v)[0]


def _nwidth(t, v):
    return _widths(t, v)[1]


def _duty(t, v):
    positive = _widths(t, v)[0]
    period = _period(t, v)
    if positive is None or not period:
        return None
    return 100.0 * positive / period


def _area(t, v):
    """Trapezoidal integral -- the same rule `defib._energy` uses."""
    total = 0.0
    for i in range(len(t) - 1):
        step = t[i + 1] - t[i]
        if step <= 0:
            continue
        total += 0.5 * (v[i] + v[i + 1]) * step
    return total


_HANDLERS = {
    "vmax": _vmax, "vmin": _vmin, "vpp": _vpp, "vtop": _vtop,
    "vbase": _vbase, "vamp": _vamp, "vavg": _vavg, "vrms": _vrms,
    "vrms_ac": _vrms_ac, "overshoot": _overshoot, "freq": _freq,
    "period": _period, "rise": _rise, "fall": _fall, "pwidth": _pwidth,
    "nwidth": _nwidth, "duty": _duty, "area": _area,
}


# --- formatting -------------------------------------------------------------
_PREFIXES = ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1.0, ""), (1e-3, "m"),
             (1e-6, "µ"), (1e-9, "n"), (1e-12, "p"))


def format_value(value, unit):
    """Engineering notation, the way an instrument prints it."""
    if value is None:
        return "—"
    if unit == "%":
        return "%.2f %%" % value
    if value == 0:
        return "0 %s" % unit
    for factor, prefix in _PREFIXES:
        if abs(value) >= factor:
            return "%.4g %s%s" % (value / factor, prefix, unit)
    return "%.3g %s" % (value, unit)
