#!/usr/bin/env python3
"""
surf_refresh.py — refreshes the SoCal Surf Command Center artifact IN PLACE.

It keeps Mattie's hand-built interactive map dashboard byte-for-byte and only
swaps the embedded `DATA` block with a freshly computed forecast. It does NOT
redesign anything.

Inputs (live next to this script):
  surf_template.html      original artifact with the DATA blob replaced by the
                          token __SURF_DATA__  (everything else identical)
  surf_spots_static.json  the 16 breaks' fixed geography/copy (id,name,region,
                          lat,lon,type,note,hazard,px,py,board) — pin positions
                          and text are preserved verbatim.

Output:
  index.html              template with a refreshed DATA blob, ready for
                          update_artifact. The non-DATA bytes are verified
                          identical to the template before writing.

Data: Open-Meteo Marine (GWAM + Météo-France WAM ensemble), Open-Meteo wind,
NOAA CO-OPS tide, NOAA NDBC buoys. Pure stdlib + requests; retries; never
crashes on a single failed fetch (a degraded spot is flagged, not dropped).
"""
import json, math, sys, time, datetime as dt, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
try:
    import requests
except Exception:
    requests = None

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "surf_template.html"
STATIC = HERE / "surf_spots_static.json"
OUT = HERE / "index.html"
TZ = "America/Los_Angeles"
try:
    from zoneinfo import ZoneInfo
    def _now_local(): return dt.datetime.now(ZoneInfo(TZ)).replace(tzinfo=None)
except Exception:                       # fallback: assume host clock is UTC, shift to Pacific
    def _now_local(): return dt.datetime.utcnow() - dt.timedelta(hours=7)
def _today_local(): return _now_local().date()
FORECAST_DAYS = 10
CONFIDENT_DAYS = 4
DAY_START, DAY_END = 5, 21   # surfable window 5am–9pm
WAVE_MODELS = ["gwam", "best_match"]   # only models with SoCal coverage on Open-Meteo
CARD = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"]

def card(d):
    return "?" if d is None else CARD[int((d % 360)/22.5 + 0.5) % 16]

# ---------------------------------------------------------------------------
# Per-spot PROFILES for the shape-first model.
#   ideal   : compass deg the swell ideally arrives FROM (drives peel/closeout)
#   spread  : angular tolerance (deg) around ideal before shape falls off
#   size    : (lo, hi) ideal breaking range in ft — outside = too small / closes out
#   tide    : (lo, hi) ideal tide band in ft MLLW for that sandbar/reef
#   offshore: wind FROM-direction (deg) that grooms the face
#   beachy  : True for sand beachbreaks (close out harder when overhead/short-period)
#   buoy    : nearest NDBC buoy
# These are local-knowledge estimates: forecast models can't measure closeout %
# or ride length, so swell/period/dir/wind/tide are used to PREDICT shape.
# ---------------------------------------------------------------------------
PROFILES = {
    # South-facing performers — love S / SSW / SW
    "sano":       dict(ideal=200, spread=42, size=(2,8),   tide=(2.0,5.0), offshore=45,  beachy=False, buoy="46224"),
    "uppers":     dict(ideal=200, spread=38, size=(2.5,8), tide=(2.0,4.5), offshore=45,  beachy=False, buoy="46224"),
    "church":     dict(ideal=200, spread=42, size=(2,8),   tide=(2.0,5.0), offshore=45,  beachy=False, buoy="46224"),
    "malibu":     dict(ideal=205, spread=34, size=(2.5,8), tide=(1.5,4.5), offshore=0,   beachy=False, buoy="46221"),
    "newport":    dict(ideal=200, spread=40, size=(2,7),   tide=(1.5,4.5), offshore=45,  beachy=True,  buoy="46253"),
    "wedge":      dict(ideal=196, spread=34, size=(3,13),  tide=(1.0,3.5), offshore=320, beachy=True,  buoy="46253"),
    "huntington": dict(ideal=205, spread=55, size=(2,7),   tide=(1.5,4.5), offshore=45,  beachy=True,  buoy="46222"),
    "sealbeach":  dict(ideal=200, spread=55, size=(2,6),   tide=(1.5,4.5), offshore=45,  beachy=True,  buoy="46253"),
    "saltcreek":  dict(ideal=200, spread=42, size=(2.5,9), tide=(2.0,4.5), offshore=45,  beachy=False, buoy="46224"),
    "countyline": dict(ideal=250, spread=70, size=(2,7),   tide=(2.0,4.5), offshore=20,  beachy=True,  buoy="46221"),
    "leocarrillo":dict(ideal=245, spread=60, size=(2,8),   tide=(1.5,4.5), offshore=20,  beachy=False, buoy="46221"),
    "staircase":  dict(ideal=245, spread=55, size=(2,8),   tide=(2.0,4.5), offshore=20,  beachy=False, buoy="46221"),
    "lowerjetties":dict(ideal=205, spread=45, size=(2,7),  tide=(1.5,4.5), offshore=45,  beachy=True,  buoy="46253"),
    "zuma":       dict(ideal=212, spread=62, size=(2,7),   tide=(1.5,4.0), offshore=20,  beachy=True,  buoy="46221"),
    # South Bay sand — best on WNW; a straight S swell tends to close out
    "elporto":    dict(ideal=290, spread=55, size=(2.5,7), tide=(2.5,4.5), offshore=70,  beachy=True,  buoy="46221"),
    "hermosa":    dict(ideal=275, spread=58, size=(2.5,7), tide=(2.0,4.5), offshore=70,  beachy=True,  buoy="46221"),
    "sapphire":   dict(ideal=278, spread=55, size=(2.5,6), tide=(2.0,4.0), offshore=70,  beachy=True,  buoy="46221"),
    "redondobw":  dict(ideal=238, spread=55, size=(2.5,7), tide=(2.0,4.0), offshore=70,  beachy=False, buoy="46221"),
    # NW / W winter points — wrong window for a south swell
    "rincon":     dict(ideal=295, spread=40, size=(3,10),  tide=(1.0,3.5), offshore=70,  beachy=False, buoy="46054"),
    "cstreet":    dict(ideal=285, spread=45, size=(3,9),   tide=(2.0,4.5), offshore=45,  beachy=False, buoy="46054"),
    "topanga":    dict(ideal=285, spread=45, size=(2.5,7), tide=(2.0,4.5), offshore=20,  beachy=False, buoy="46221"),
    "lunada":     dict(ideal=295, spread=36, size=(5,15),  tide=(2.0,5.0), offshore=90,  beachy=False, buoy="46222"),
    "haggertys":  dict(ideal=290, spread=42, size=(2.5,7), tide=(2.0,4.5), offshore=90,  beachy=False, buoy="46222"),
}
DEFAULT_PROFILE = dict(ideal=235, spread=60, size=(2,8), tide=(1.5,4.5), offshore=45, beachy=True, buoy="46221")
BUOYS = {"46054":"W Santa Barbara","46221":"Santa Monica Bay","46222":"San Pedro",
         "46253":"San Pedro South","46224":"Oceanside","46219":"San Nicolas Is.",
         "46086":"San Clemente Basin","46025":"Santa Monica Basin"}
TIDE_STATIONS = {"SB":"9411340","SM":"9410840","LA":"9410660","NB":"9410580","LJ":"9410230"}

def tide_station_for(lat):
    if lat >= 34.20: return TIDE_STATIONS["SB"]
    if lat >= 33.78: return TIDE_STATIONS["SM"]
    if lat >= 33.70: return TIDE_STATIONS["LA"]
    if lat >= 33.58: return TIDE_STATIONS["NB"]
    return TIDE_STATIONS["LJ"]

# ---- HTTP with retries (never raises to caller) ----
def _get(url, tries=4, timeout=25, text=False):
    last = None
    for i in range(tries):
        try:
            if requests is not None:
                r = requests.get(url, timeout=timeout); r.raise_for_status()
                return r.text if text else r.json()
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                raw = resp.read().decode()
                return raw if text else json.loads(raw)
        except Exception as e:
            last = e; time.sleep(1.2*(i+1))
    raise RuntimeError(f"GET failed: {url} :: {last}")

def _circ_mean(ds):
    x = sum(math.cos(math.radians(d)) for d in ds); y = sum(math.sin(math.radians(d)) for d in ds)
    return ds[0] if (x==0 and y==0) else math.degrees(math.atan2(y,x))%360

def _pick(h, p, f, k):
    a = h.get(p)
    if a and k < len(a) and a[k] is not None: return a[k]
    a = h.get(f)
    if a and k < len(a) and a[k] is not None: return a[k]
    return None

def fetch_marine(lat, lon):
    hv = "swell_wave_height,swell_wave_period,swell_wave_direction,wave_height,wave_period,wave_direction"
    per_model = []
    for model in WAVE_MODELS:
        url = "https://marine-api.open-meteo.com/v1/marine?" + urllib.parse.urlencode({
            "latitude":lat,"longitude":lon,"hourly":hv,"timezone":TZ,
            "forecast_days":FORECAST_DAYS,"models":model,"length_unit":"metric","cell_selection":"sea"})
        try:
            h = _get(url).get("hourly", {}); t = h.get("time", [])
            if not t: continue
            per_model.append({tt:(_pick(h,"swell_wave_height","wave_height",k),
                                  _pick(h,"swell_wave_period","wave_period",k),
                                  _pick(h,"swell_wave_direction","wave_direction",k)) for k,tt in enumerate(t)})
        except Exception:
            continue
    if not per_model: return None
    allt = set().union(*[set(r) for r in per_model]); out = {}
    for t in sorted(allt):
        hs=[];ps=[];dd=[]
        for r in per_model:
            if t in r:
                hh,pp,drc = r[t]
                if hh is not None: hs.append(hh)
                if pp is not None: ps.append(pp)
                if drc is not None: dd.append(drc)
        if hs:
            out[t] = {"hgt":sum(hs)/len(hs),"per":(sum(ps)/len(ps) if ps else None),
                      "dir":(_circ_mean(dd) if dd else None)}
    return out or None

def _val(h, name, k):
    a = h.get(name)
    return a[k] if a and k < len(a) and a[k] is not None else None

def fetch_trains(lat, lon):
    """Best-effort, ISOLATED fetch of the individual swell trains (primary swell,
    secondary swell, windswell) for display only. Its own request, so a failure
    never touches the core forecast or the score. Returns {time: [trains]}."""
    hv = ("swell_wave_height,swell_wave_period,swell_wave_direction,"
          "secondary_swell_wave_height,secondary_swell_wave_period,secondary_swell_wave_direction,"
          "wind_wave_height,wind_wave_period,wind_wave_direction")
    url = "https://marine-api.open-meteo.com/v1/marine?" + urllib.parse.urlencode({
        "latitude":lat,"longitude":lon,"hourly":hv,"timezone":TZ,
        "forecast_days":FORECAST_DAYS,"length_unit":"metric","cell_selection":"sea"})
    out = {}
    try:
        h = _get(url).get("hourly", {}); t = h.get("time", [])
        for k,tt in enumerate(t):
            trains = []
            for pre,label in (("swell_wave","Primary swell"),
                              ("secondary_swell_wave","Secondary swell"),
                              ("wind_wave","Windswell")):
                hh = _val(h, pre+"_height", k)
                if hh is None: continue
                ft = hh * 3.281
                if ft < 0.4: continue
                pp = _val(h, pre+"_period", k); dd = _val(h, pre+"_direction", k)
                trains.append({"kind":label,"h":round(ft,1),
                               "p":(round(pp,1) if pp is not None else None),
                               "d":(round(dd) if dd is not None else None),
                               "dc":(card(dd) if dd is not None else "")})
            trains.sort(key=lambda x:-x["h"])
            out[tt] = trains
    except Exception:
        return {}
    return out

def fetch_wind(lat, lon):
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
        "latitude":lat,"longitude":lon,"hourly":"wind_speed_10m,wind_direction_10m",
        "wind_speed_unit":"mph","timezone":TZ,"forecast_days":FORECAST_DAYS})
    try:
        h = _get(url).get("hourly", {}); t = h.get("time", [])
        sp = h.get("wind_speed_10m", []); dr = h.get("wind_direction_10m", [])
        return {tt:(sp[k] if k<len(sp) else None, dr[k] if k<len(dr) else None) for k,tt in enumerate(t)}
    except Exception:
        return {}

def fetch_tide(station):
    today = _today_local(); end = today + dt.timedelta(days=FORECAST_DAYS+1)
    # Exact HOURLY predictions (interval=h) — no extreme-to-extreme interpolation,
    # so dawn/dusk hours are correct, not flatlined to the nearest hi/lo.
    url = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?" + urllib.parse.urlencode({
        "product":"predictions","datum":"MLLW","time_zone":"lst_ldt","interval":"h",
        "units":"english","format":"json","station":station,
        "begin_date":today.strftime("%Y%m%d"),"end_date":end.strftime("%Y%m%d")})
    try:
        return [(dt.datetime.strptime(p["t"],"%Y-%m-%d %H:%M"), float(p["v"]))
                for p in _get(url).get("predictions", [])]
    except Exception:
        return []

def fetch_buoy(bid):
    try:
        txt = _get(f"https://www.ndbc.noaa.gov/data/realtime2/{bid}.txt", text=True)
        for ln in [l for l in txt.splitlines() if l and not l.startswith("#")]:
            f = ln.split()
            if len(f) < 12: continue
            if f[8] in ("MM","999.0") or f[9] in ("MM","99.0"): continue
            try: wv = float(f[8])
            except ValueError: continue
            try: mwd = float(f[11])
            except ValueError: mwd = None
            return {"name":BUOYS.get(bid,bid),"h":round(wv*3.281,1),
                    "period":float(f[9]),"dirC":card(mwd)}
        return None
    except Exception:
        return None

def tide_height(ev, when):
    """ev = hourly (datetime, height) points; linear interp between them."""
    if not ev: return None
    if when <= ev[0][0]: return ev[0][1]
    if when >= ev[-1][0]: return ev[-1][1]
    for i in range(len(ev)-1):
        t0,v0 = ev[i]; t1,v1 = ev[i+1]
        if t0 <= when <= t1:
            span = (t1-t0).total_seconds(); frac = (when-t0).total_seconds()/span if span else 0
            return v0 + (v1-v0)*frac
    return ev[-1][1]

def tide_state(h, lo, hi):
    if h is None or hi is None or lo is None or hi==lo: return "mid"
    f = (h-lo)/(hi-lo)
    return "low" if f < 0.33 else ("mid" if f < 0.66 else "high")

def _angdiff(a, b):
    return abs((a - b + 180) % 360 - 180)

# ---- shape-first component model -------------------------------------------
# Each component returns 0..1. The final score is a weight-exponent geometric
# mean (so one weak link drags the whole session down, weighted by importance)
# multiplied by a shape gate (so a bad-shape day is terrible no matter what).

def f_angle(drc, ideal, spread):
    """How well-angled the swell is for this break -> peel vs closeout."""
    if drc is None: return 0.4
    dd = _angdiff(drc, ideal)
    return math.exp(-0.5 * (dd / max(spread, 1)) ** 2)

def exposure(drc, ideal, spread):
    """Fraction of the open-coast swell a break actually receives, from how well
    the swell direction lines up with the break's window. A shadowed spot (wrong
    direction) reads small/flat; a well-aimed spot reads full size. DISPLAY ONLY:
    scales the breaking face, never fed into the 0-100 score."""
    if drc is None: return 0.7
    dd = _angdiff(drc, ideal)
    return max(0.10, math.exp(-0.5 * (dd / max(spread * 1.2, 1)) ** 2))

def f_period(per):
    """Longer period = cleaner, more organized faces. <7s windswell = mushy."""
    if per is None: return 0.4
    return max(0.05, min(1.0, (per - 7.0) / 8.0))

def f_size(ft, lo, hi, beachy):
    """In the spot's ideal range = 1; too small ramps down; overhead closes out
    (beachbreaks decay faster than points/reefs)."""
    if ft is None: return 0.3
    if ft < lo:
        return max(0.2, ft / lo)
    if ft <= hi:
        return 1.0
    decay = 0.22 if beachy else 0.11
    return max(0.2, 1.0 - (ft - hi) * decay)

def f_shape(drc, per, ft, p):
    """SHAPE: the core. Predicts closeout %/peel from angle + period + size."""
    return max(0.05, 0.40*f_angle(drc, p["ideal"], p["spread"])
                   + 0.35*f_period(per)
                   + 0.25*f_size(ft, p["size"][0], p["size"][1], p["beachy"]))

def f_wind(wdir, wspd, offshore):
    """Glassy/light-offshore best; cross mid; onshore tanks it. Returns (factor,label)."""
    if wspd is None: return 0.6, "?"
    if wspd < 3: return 1.0, "glassy"
    diff = _angdiff(wdir, offshore) if wdir is not None else 90
    if diff <= 50:   # offshore
        return (0.95 if wspd <= 10 else max(0.7, 0.95 - (wspd-10)*0.02)), "offshore"
    if diff >= 130:  # onshore
        return max(0.0, 0.45 - (wspd-3)*0.04), "onshore"
    return max(0.25, 0.65 - (wspd-5)*0.02), "cross"   # cross

def f_tide(th, band):
    """Real tide ft vs the spot's ideal foot-band."""
    if th is None: return 0.7
    lo, hi = band
    if lo <= th <= hi: return 1.0
    d = (lo - th) if th < lo else (th - hi)
    if d <= 1.0: return 0.8
    if d <= 2.0: return 0.55
    return 0.4

def f_energy(ft, per):
    """Power ~ H^2 * T. Surfers want a specific amount of punch, not just size."""
    if ft is None or per is None: return 0.4
    e = (ft ** 2) * per
    return max(0.1, min(1.0, (math.sqrt(e) - 4.0) / (math.sqrt(350.0) - 4.0)))

def f_direction(drc, ideal, spread):
    """Macro 'does this swell suit the spot' — broader than the shape angle term."""
    if drc is None: return 0.4
    dd = _angdiff(drc, ideal)
    return math.exp(-0.5 * (dd / max(spread*1.6, 1)) ** 2)

def f_consistency(per, beachy):
    """Proxy only (no set-count data): longer period -> more organized sets.
    Beachbreaks lean on consistency more than points, so they get a lower floor."""
    if per is None: return 0.5
    base = 0.30 if beachy else 0.45
    return max(0.2, min(1.0, base + 0.70 * max(0.0, (per - 8.0) / 8.0)))

WEIGHTS = {"shape":0.40, "wind":0.20, "tide":0.15, "energy":0.10, "dir":0.10, "cons":0.05}

def score_hour(w, wind, th, tlo, thi, p):
    if not w or w.get("hgt") is None: return None
    ft = w["hgt"]*3.281; per = w.get("per"); drc = w.get("dir")
    shape = f_shape(drc, per, ft, p)
    windf, wlabel = f_wind(wind[1] if wind else None, wind[0] if wind else None, p["offshore"])
    tidef = f_tide(th, p["tide"])
    energy = f_energy(ft, per)
    direction = f_direction(drc, p["ideal"], p["spread"])
    cons = f_consistency(per, p["beachy"])
    comp = {"shape":shape, "wind":windf, "tide":tidef,
            "energy":energy, "dir":direction, "cons":cons}
    # weighted geometric mean (each component floored to avoid log(0))
    gm = math.exp(sum(WEIGHTS[k] * math.log(max(comp[k], 0.05)) for k in WEIGHTS))
    # shape gate: bad shape ruins the session regardless of everything else
    gate = 0.40 + 0.60 * shape
    score = round(100 * gm * gate)
    energy = round(ft * ft * (per or 0))   # deepwater swell energy proxy (~kJ, Surfline-scale)
    return {"score":score, "ft":ft, "per":per, "dir":drc, "windType":wlabel,
            "windKt":wind[0] if wind else None,
            "windDir":card(wind[1]) if wind and wind[1] is not None else "?",
            "tideFt": round(th,1) if th is not None else None,
            "tideState":tide_state(th, tlo, thi), "e":energy}

def best_block(scored):
    """scored: list of (hour,sc). Returns (start,end,mid_sc,avg)."""
    if not scored: return None
    best=None
    for i in range(len(scored)):
        blk = scored[i:i+4] if i+4<=len(scored) else scored[i:]
        if len(blk) < 2: continue
        avg = sum(b[1]["score"] for b in blk)/len(blk)
        if best is None or avg > best[0]: best=(avg,blk)
    if best is None: best=(scored[0][1]["score"],[scored[0]])
    avg,blk = best; mid = blk[len(blk)//2][1]
    return blk[0][0], blk[-1][0]+1, mid, round(avg)

def face_mult(per, kind):
    """Rough swell-height -> breaking-face-height multiplier. Longer period shoals
    bigger; points/reefs jack up, the Wedge notoriously doubles, beachbreaks ~1x."""
    p = per or 8
    if   p < 8:  k = 0.90   # short windswell breaks soft, often under Hs
    elif p < 10: k = 1.00
    elif p < 12: k = 1.10
    elif p < 14: k = 1.20
    elif p < 16: k = 1.30
    elif p < 18: k = 1.40
    else:        k = 1.50
    kl = (kind or "").lower()
    if   "wedge" in kl:  k *= 1.30   # the Wedge jacks/doubles
    elif "shore" in kl:  k *= 1.20   # shorebreak (the Wedge is typed this way)
    elif "reef"  in kl:  k *= 1.08
    elif "point" in kl:  k *= 1.05
    return k

# Report the AVERAGE rideable face, deliberately conservative — we'd rather a
# surfer find it bigger than expected than smaller. Biases the shown size below
# the biggest-set face. Display only; never touches the score.
FACE_BIAS = 0.78

def face_ft(swell_ft, per, kind):
    if swell_ft is None: return None
    return round(swell_ft * face_mult(per, kind) * FACE_BIAS, 1)

def mk_window(start,end,mid):
    return {"start":int(start),"end":int(end),
            "swellH":round(mid["ft"],1),"swellP":round(mid["per"],1) if mid["per"] else None,
            "swellD":round(mid["dir"]) if mid["dir"] is not None else None,"swellDir":card(mid["dir"]),
            "wind":round(mid["windKt"]) if mid["windKt"] is not None else None,"windDir":mid["windDir"],
            "tideState":mid["tideState"]}

# ---------------------------------------------------------------------------
# Measurement anchoring — the real accuracy lever.
# NDBC .spec feeds give MEASURED swell (height/period/direction, separated from
# wind-sea) from the same NOAA/CDIP buoys the pros watch. We compare the model's
# "now" at each buoy to the buoy's measured "now", then nudge each spot's
# near-term forecast toward measured reality, fading the correction out over
# ~3 days as the model takes back over. This is what tightens 0–72h accuracy.
# ---------------------------------------------------------------------------
BUOY_COORDS = {
    "46221": (33.863, -118.634),  # Santa Monica Bay
    "46222": (33.618, -118.317),  # San Pedro
    "46253": (33.576, -118.181),  # San Pedro South
    "46224": (33.179, -117.471),  # Oceanside Offshore
    "46054": (34.274, -120.477),  # West Santa Barbara
}
# Honest forecast-skill ceiling by lead day (0 = today). Surf decays fast.
CONF_LEAD = [0.96, 0.91, 0.84, 0.74, 0.63, 0.52, 0.43, 0.36, 0.30, 0.26]

def fetch_spec(bid):
    """Latest MEASURED swell from NDBC .spec: (swH_m, swP_s, swD_deg) or None."""
    try:
        txt = _get(f"https://www.ndbc.noaa.gov/data/realtime2/{bid}.spec", text=True)
        for ln in [l for l in txt.splitlines() if l and not l.startswith("#")]:
            f = ln.split()
            if len(f) < 15: continue
            swh, swp, mwd = f[6], f[7], f[14]
            if swh in ("MM","-99.0") or swp in ("MM","-99.0","99.00"): continue
            try: h = float(swh); p = float(swp)
            except ValueError: continue
            if h < 0 or p <= 0: continue
            try: dd = float(mwd)
            except ValueError: dd = None
            return (h, p, dd)
        return None
    except Exception:
        return None

def fetch_model_now(lat, lon):
    """Model swell at a point for the current local hour: (swH_m, swP_s) or None."""
    url = ("https://marine-api.open-meteo.com/v1/marine?" + urllib.parse.urlencode({
        "latitude":lat,"longitude":lon,
        "hourly":"swell_wave_height,swell_wave_period,wave_height,wave_period",
        "timezone":TZ,"forecast_days":1,"models":"gwam","cell_selection":"sea"}))
    try:
        h = _get(url).get("hourly", {}); times = h.get("time", [])
        if not times: return None
        now = _now_local()
        k = min(range(len(times)),
                key=lambda i: abs(dt.datetime.strptime(times[i],"%Y-%m-%dT%H:%M")-now))
        hh = _pick(h,"swell_wave_height","wave_height",k)
        pp = _pick(h,"swell_wave_period","wave_period",k)
        return (hh, pp) if hh is not None else None
    except Exception:
        return None

def compute_bias(buoys):
    """{buoy: {hRatio,pOff,agree,buoyH,modelH}} from model-now vs measured-now."""
    out = {}
    def one(bid):
        coords = BUOY_COORDS.get(bid)
        if not coords: return bid, None
        spec = fetch_spec(bid); mdl = fetch_model_now(*coords)
        if not spec or not mdl or not mdl[0]: return bid, None
        mh, mp = mdl; sh, sp, sd = spec
        raw = sh / mh                                   # raw model-vs-measured ratio
        # Tight clamp: a buoy nudges, never overrides. SoCal island shadowing and
        # swell-partition differences make large single-buoy ratios untrustworthy,
        # so big disagreement instead shows up as LOWER confidence (agree), not a
        # distorted height.
        hRatio = max(0.78, min(1.28, raw))
        pOff = max(-3.0, min(3.0, sp - (mp or sp)))
        agree = max(0.50, 1 - abs(1 - raw) * 0.6)
        return bid, {"hRatio":round(hRatio,3),"pOff":round(pOff,2),"agree":round(agree,2),
                     "buoyH":round(sh,2),"modelH":round(mh,2)}
    with ThreadPoolExecutor(max_workers=max(2,len(buoys))) as ex:
        for f in as_completed([ex.submit(one,b) for b in buoys]):
            bid, v = f.result(); out[bid] = v
    return out

CORRECT_GAIN = 0.45   # apply only ~half the buoy nudge — a reality-check, not an override

def correct(w, bias, lead):
    """Gently nudge a model hour toward the measured buoy bias, fading by lead day."""
    if not bias or not w or w.get("hgt") is None: return w
    wt = math.exp(-lead/1.6) * CORRECT_GAIN   # ~0.45 today, 0.24 d1, 0.13 d2, 0.07 d3
    h = w["hgt"] * (1 + (bias["hRatio"]-1)*wt)
    p = (w["per"] + bias["pOff"]*wt) if w.get("per") else w.get("per")
    return {"hgt":h, "per":p, "dir":w.get("dir")}

def process(spot, tide_cache):
    a = PROFILES.get(spot["id"], DEFAULT_PROFILE)
    marine = fetch_marine(spot["lat"], spot["lon"]); wind = fetch_wind(spot["lat"], spot["lon"])
    trains = fetch_trains(spot["lat"], spot["lon"])
    st = tide_station_for(spot["lat"]); ev = tide_cache.get(st)
    if ev is None: ev = fetch_tide(st); tide_cache[st] = ev
    days_index = {}
    if marine:
        for t,w in marine.items():
            days_index.setdefault(t[:10], []).append((int(t[11:13]), t, w))
    return spot, a, days_index, ev, wind, trains

def build():
    static = json.loads(STATIC.read_text())
    template = TEMPLATE.read_text(encoding="utf-8")
    today = _today_local()
    dates = [(today+dt.timedelta(days=i)).isoformat() for i in range(FORECAST_DAYS)]
    tide_cache = {}

    # Measurement anchoring: bias of model-now vs measured buoy-now (per buoy).
    bias_buoys = sorted({PROFILES.get(s["id"],DEFAULT_PROFILE)["buoy"] for s in static} & set(BUOY_COORDS))
    try:
        BIAS = compute_bias(bias_buoys)
    except Exception as e:   # never let the anchoring step take down the 3am run
        BIAS = {}; print("bias step failed, continuing model-only:", e)
    print("buoy bias anchors:", {b:(BIAS[b]["hRatio"] if BIAS.get(b) else None) for b in bias_buoys})

    results = [None]*len(static)
    def work(i):
        try: return i, process(static[i], tide_cache)
        except Exception as e: return i, (static[i], PROFILES.get(static[i]["id"],DEFAULT_PROFILE), {}, [], {}, {})
    with ThreadPoolExecutor(max_workers=len(static)) as ex:
        for f in as_completed([ex.submit(work,i) for i in range(len(static))]):
            i,r = f.result(); results[i]=r

    spots_out = []
    region_accum = {d:{"H":[], "P":[], "D":[]} for d in dates}
    for spot, a, didx, ev, wind, trains in results:
        days=[]; conf_best=None; radar_best=None
        bias = BIAS.get(a["buoy"])
        agree = bias["agree"] if bias else 0.8
        for lead,d in enumerate(dates):
            confPct = round(100 * CONF_LEAD[min(lead,len(CONF_LEAD)-1)] * agree)
            confident = confPct >= 65
            rows = didx.get(d, [])
            if not rows:
                days.append({"date":d,"lead":lead,"confident":confident,"nodata":True,"score":0,"window":None,"hours":[],"peakE":0,"confPct":confPct}); continue
            day_ev = [e for e in ev if e[0].strftime("%Y-%m-%d")==d]
            tlo = min((e[1] for e in day_ev), default=None); thi = max((e[1] for e in day_ev), default=None)
            scored=[]
            for hr,t,w in rows:
                if not (DAY_START<=hr<=DAY_END): continue
                wc = correct(w, bias, lead)
                th = tide_height(ev, dt.datetime.strptime(t,"%Y-%m-%dT%H:%M"))
                sc = score_hour(wc, wind.get(t), th, tlo, thi, a)
                if sc: sc["trains"]=trains.get(t); scored.append((hr,sc))
            if not scored:
                days.append({"date":d,"lead":lead,"confident":confident,"nodata":True,"score":0,"window":None,"hours":[],"peakE":0,"confPct":confPct}); continue
            bb = best_block(scored); start,end,mid,avg = bb
            win = mk_window(start,end,mid)
            _expw = exposure(mid["dir"], a["ideal"], a["spread"])
            win["faceFt"] = face_ft(round(win["swellH"]*_expw,1), mid["per"], spot["type"])
            hours=[{"h":hr,"score":sc["score"],"ft":round(sc["ft"],1),
                    "face":face_ft(round(sc["ft"]*exposure(sc["dir"], a["ideal"], a["spread"]),1), sc["per"], spot["type"]),
                    "wind":(round(sc["windKt"]) if sc["windKt"] is not None else None),
                    "windDir":sc["windDir"],"wt":sc["windType"],"tide":sc["tideFt"],"e":sc["e"]}
                   for hr,sc in scored]
            peakE=max((x["e"] for x in hours), default=0)
            _sw=[{**_tr,"q":round(100*f_shape(_tr.get("d"),_tr.get("p"),_tr.get("h"),a))} for _tr in (mid.get("trains") or [])]
            days.append({"date":d,"lead":lead,"confident":confident,"nodata":False,"score":avg,"window":win,"hours":hours,"peakE":peakE,"confPct":confPct,"swells":_sw})
            # region accumulation (use midday-ish max)
            region_accum[d]["H"].append(mid["ft"])
            if mid["per"]: region_accum[d]["P"].append(mid["per"])
            if mid["dir"] is not None: region_accum[d]["D"].append(mid["dir"])
            if confident and (conf_best is None or avg>conf_best[0]): conf_best=(avg,d,win)
            if (not confident) and (radar_best is None or avg>radar_best[0]): radar_best=(avg,d,win)
        if conf_best is None:  # fallback: best of all days
            alld=[(x["score"],x["date"],x["window"]) for x in days if x["window"]]
            conf_best = max(alld, default=(0,dates[0],None))
        if radar_best is None:
            radar_best = conf_best
        sp = {k:spot[k] for k in ("id","name","region","lat","lon","type","note","hazard","px","py","board")}
        sp.update(days=days,
                  bestScore=conf_best[0], bestDate=conf_best[1], bestWindow=conf_best[2],
                  radarScore=radar_best[0], radarDate=radar_best[1], radarWindow=radar_best[2])
        spots_out.append(sp)

    region=[]
    for lead,d in enumerate(dates):
        acc = region_accum[d]
        H = acc["H"]; P = acc["P"]; D = acc["D"]
        maxH = round(max(H),1) if H else 0; avgH = round(sum(H)/len(H),1) if H else 0
        domD = round(_circ_mean(D)) if D else None; maxP = round(max(P),1) if P else None
        region.append({"date":d,"lead":lead,"confident":lead<CONFIDENT_DAYS,
                       "avgH":avgH,"maxH":maxH,"domDir":domD,"domDirC":card(domD),"maxPer":maxP})

    buoys=[]
    with ThreadPoolExecutor(max_workers=8) as ex:
        bf={ex.submit(fetch_buoy,b):b for b in BUOYS}
        for f in as_completed(bf):
            r=f.result()
            if r: buoys.append(r)
    order=list(BUOYS.values()); buoys.sort(key=lambda b: order.index(b["name"]) if b["name"] in order else 99)

    DATA={"generated":_now_local().strftime("%Y-%m-%d %H:%M"),
          "weekStart":dates[0],"weekEnd":dates[-1],
          "spots":spots_out,"region":region,"buoys":buoys}

    blob=json.dumps(DATA,ensure_ascii=False,separators=(",",":"))
    if "__SURF_DATA__" not in template:
        raise SystemExit("template missing __SURF_DATA__ placeholder")
    html=template.replace("__SURF_DATA__", blob)
    # VERIFY: removing the new DATA blob must yield the exact template back —
    # guarantees we changed nothing but the data.
    back=html.replace(blob,"__SURF_DATA__")
    assert back==template, "layout drift detected — refusing to write"
    OUT.write_text(html,encoding="utf-8")
    ok=sum(1 for s in spots_out if any(not d["nodata"] for d in s["days"]))
    print(f"surf_refresh: anchored {today.isoformat()} ({today.strftime('%A')})")
    print(f"wrote {OUT} ({len(html)} bytes) | layout verified identical to template")
    print(f"OK spots: {ok}/{len(spots_out)} | buoys: {len(buoys)} | outlook days: {len(region)}")
    top=max(spots_out,key=lambda s:s["bestScore"])
    print(f"top: {top['name']} best {top['bestScore']} {top['bestDate']} {top['bestWindow']}")
    print(f"dom swell: {region[0]['domDirC']} {region[0]['maxPer']}s")
    return 0 if ok==len(spots_out) else 2

if __name__=="__main__":
    sys.exit(build())
