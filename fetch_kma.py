# -*- coding: utf-8 -*-
"""
BeeClimate 아까시 수집기 v0.3
==============================
관측(ASOS API) + 평년(normals_service.json) 하이브리드.
  - 전국 ASOS 전체 지점 계산 (관리원 버전)
  - 조사지점 21곳: 최근접 ASOS 관측 사용 (site_asos_map.json)
  - 일사: SI_DAY(MJ/m²) × 계절가변 계수 → 주간평균 W/m²
          일사 미관측 지점은 평년 Solar로 대체
  - 관측 없는 날(미래/결측)은 평년으로 채움 → 봄철 실시간 모드 대응

필요 파일(같은 폴더): kma_key.txt, normals_service.json, site_asos_map.json
실행: python fetch_kma.py   → docs/data.json
"""

import os, sys, json
import datetime as dt
import requests

_here = os.path.dirname(os.path.abspath(__file__))
KEY = os.environ.get("KMA_KEY")
if not KEY:
    _kf = os.path.join(_here, "kma_key.txt")
    if os.path.exists(_kf):
        KEY = open(_kf, encoding="utf-8").read().strip()
if not KEY:
    sys.exit("kma_key.txt가 없습니다.")

BASE = "https://apihub.kma.go.kr/api/typ01/url"
TODAY = dt.date.today()
YEAR = TODAY.year

NORMALS = json.load(open(os.path.join(_here, "normals_service.json"),
                         encoding="utf-8"))
SITE_MAP = json.load(open(os.path.join(_here, "site_asos_map.json"),
                          encoding="utf-8"))

# ── 모델 파라미터 (실운영 코드 확정값) ─────────────────────
SW_TC, SW_GDD, SW_T0 = 4.0, 350.0, 90
STAGE_DEFS = {"S1": (-21, -14), "S2": (-13, -1), "S3": (0, 12)}

def score_tmax(x):
    if x < 15: v = 0.1
    elif x < 20: v = 0.1 + 0.5*(x-15)/5
    elif x < 23: v = 0.6 + 0.4*(x-20)/3
    elif x <= 26: v = 1.0
    elif x <= 30: v = 1.0 - 0.7*(x-26)/4
    else: v = 0.3
    return min(max(v, 0.05), 1.0)

def score_rh(x):
    if x < 50: v = 0.2
    elif x < 64.6: v = 0.2 + 0.8*(x-50)/14.6
    elif x <= 75: v = 1.0 - 0.8*(x-64.6)/10.4
    else: v = 0.2
    return min(max(v, 0.05), 1.0)

def score_ws(x):
    if x < 0.5: v = 1.0
    elif x < 1.66: v = 1.0 - 0.3*(x-0.5)/1.16
    elif x <= 3.5: v = 0.7 - 0.5*(x-1.66)/1.84
    else: v = 0.2
    return min(max(v, 0.05), 1.0)

def score_solar(x):
    if x < 500: v = 0.3
    elif x < 648: v = 0.3 + 0.7*(x-500)/148
    elif x <= 720: v = 1.0 - 0.4*(x-648)/72
    else: v = 0.6
    return min(max(v, 0.05), 1.0)

def score_precip(x):
    if x < 1: v = 1.0
    elif x < 5.4: v = 1.0 - 0.3*(x-1)/4.4
    elif x <= 15: v = 0.7 - 0.5*(x-5.4)/9.6
    else: v = 0.2
    return min(max(v, 0.05), 1.0)

SCORE_FNS = {"Tmax": score_tmax, "RH": score_rh, "WS": score_ws,
             "Solar": score_solar, "Precip": score_precip}
HHWI_W = {"S1": {"Tmax": .60, "RH": .20, "WS": .20},
          "S2": {"Tmax": .60, "Precip": .10, "RH": .05, "WS": .25},
          "S3": {"Tmax": .40, "RH": .25, "WS": .15, "Solar": .20}}
HHWI_COEF = {"const": -71.08, "S1": 51.55, "S2": 45.01, "S3": 54.91}
HMWI_W = {"S1": {"Tmax": .25, "Precip": .05, "RH": .10, "WS": .40, "Solar": .20},
          "S2": {"Tmax": .25, "Precip": .10, "RH": .20, "WS": .25, "Solar": .20},
          "S3": {"Tmax": .20, "Precip": .15, "RH": .35, "WS": .30}}
HMWI_COEF = {"const": 10.63, "S1": -2.55, "S2": 3.04, "S3": 17.66}
MOISTURE_GRADES = [("A", 0, 18), ("B", 18, 20), ("C", 20, 23),
                   ("D", 23, 25), ("E", 25, 100)]
GRADE_PRICES = {"A": 35000, "B": 25000, "C": 18000, "D": 12000, "E": 0}
TRAFFIC_RED, TRAFFIC_GREEN = 0.502, 0.545

# 일사 변환: SI_DAY(MJ/m²) → 주간평균 W/m²
# 평년 교차검증 기반: DOY 105(4/15)≈×33 → DOY 166(6/15)≈×27 선형
def solar_mj_to_w(mj, doy):
    k = 33.0 + (27.0 - 33.0) * (doy - 105) / 61.0
    k = min(max(k, 26.0), 34.0)
    return mj * k

# help=1 원문으로 확정 (1-기반 문서 → 0-기반 인덱스)
COL = {"TM": 0, "STN": 1, "WS_AVG": 2, "TA_AVG": 10, "TA_MAX": 11,
       "HM_AVG": 18, "SI_DAY": 35, "RN_DAY": 38}
MISSING = {"-9", "-9.0", "-9.00", "-99", "-99.0", "-99.00", "-999", "-999.0"}

VARMAP = {"Tmax": "TA_MAX", "RH": "HM_AVG", "WS": "WS_AVG",
          "Solar": "SI_DAY", "Precip": "RN_DAY"}
NVAR = {"Tmax": "Tmax", "RH": "HMDT", "WS": "WDSP",
        "Solar": "Solar", "Precip": "Rn", "Tavg": "Tavg"}


def fetch_asos_daily(date):
    r = requests.get(f"{BASE}/kma_sfcdd.php",
                     params={"tm": date.strftime("%Y%m%d"), "stn": 0,
                             "help": 0, "authKey": KEY}, timeout=30)
    r.raise_for_status()
    rows = []
    for line in r.text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # 쉼표 구분(기본) / 공백 구분(disp=1) 모두 대응
        p = ([x.strip() for x in line.split(",")] if "," in line
             else line.split())
        if len(p) < 40:
            continue
        try:
            row = {"STN": p[COL["STN"]]}
            for k in ("TA_AVG", "TA_MAX", "WS_AVG", "HM_AVG",
                      "SI_DAY", "RN_DAY"):
                v = p[COL[k]]
                if v in MISSING:
                    # 무강수일의 -9는 결측이 아니라 0mm
                    row[k] = 0.0 if k == "RN_DAY" else None
                else:
                    row[k] = float(v)
            rows.append(row)
        except (ValueError, IndexError):
            continue
    return rows


def doy_to_mmdd(doy):
    return (dt.date(YEAR, 1, 1) + dt.timedelta(days=doy-1)).strftime("%m%d")


def get_val(obs_by_doy, nkey, var, doy):
    """관측 우선, 없으면 평년. var는 모델 변수명(Tmax 등)."""
    r = obs_by_doy.get(doy)
    col = VARMAP.get(var) or "TA_AVG"
    if r is not None and r.get(col) is not None:
        v = r[col]
        return solar_mj_to_w(v, doy) if var == "Solar" else v
    nm = NORMALS.get(nkey, {}).get(NVAR[var], {})
    return nm.get(doy_to_mmdd(doy))


def compute_site(nkey, obs_by_doy):
    # ① 개화일: Tavg 하이브리드 적산
    acc, bloom_doy = 0.0, None
    obs_days = 0
    for d in range(SW_T0, 181):
        ta = get_val(obs_by_doy, nkey, "Tavg", d)
        if d in obs_by_doy and obs_by_doy[d].get("TA_AVG") is not None:
            obs_days += 1
        if ta is not None and ta > SW_TC:
            acc += ta - SW_TC
        if acc >= SW_GDD and bloom_doy is None:
            bloom_doy = d
            break
    if bloom_doy is None:
        return {"status": "pre_bloom", "gdd_now": round(acc, 1)}
    # ② 단계 점수
    S = {}
    for stg, (a, b) in STAGE_DEFS.items():
        sc_h = sc_m = 0.0
        for var in ("Tmax", "RH", "WS", "Solar", "Precip"):
            vals = [get_val(obs_by_doy, nkey, var, d)
                    for d in range(bloom_doy + a, bloom_doy + b + 1)]
            vals = [v for v in vals if v is not None]
            if not vals:
                continue
            s = SCORE_FNS[var](sum(vals) / len(vals))
            sc_h += HHWI_W[stg].get(var, 0) * s
            sc_m += HMWI_W[stg].get(var, 0) * s
        S[stg] = {"h": min(max(sc_h, 0), 1), "m": min(max(sc_m, 0), 1)}
    hhwi = min(max(HHWI_COEF["const"] + sum(
        HHWI_COEF[s]*S[s]["h"] for s in ("S1", "S2", "S3")), 0), 100)
    hmwi = min(max(HMWI_COEF["const"] + sum(
        HMWI_COEF[s]*S[s]["m"] for s in ("S1", "S2", "S3")), 5), 35)
    grade = next(g for g, lo, hi in MOISTURE_GRADES if lo < hmwi <= hi)
    comp = sum(S[s]["h"] for s in ("S1", "S2", "S3")) / 3
    light = ("RED" if comp < TRAFFIC_RED else
             "GREEN" if comp > TRAFFIC_GREEN else "YELLOW")
    bd = dt.date(YEAR, 1, 1) + dt.timedelta(days=bloom_doy - 1)
    return {"status": "ok", "bloom_date": bd.isoformat(),
            "bloom_doy": bloom_doy, "obs_days_used": obs_days,
            "S": {k: round(v["h"], 3) for k, v in S.items()},
            "hhwi": round(hhwi, 1), "hmwi": round(hmwi, 1),
            "grade": grade, "hqci": round(hhwi * GRADE_PRICES[grade]),
            "traffic": light}


def main():
    # 수집 범위: DOY 90 ~ min(어제, DOY 193)
    start = dt.date(YEAR, 1, 1) + dt.timedelta(days=SW_T0 - 1)
    end = min(TODAY - dt.timedelta(days=1),
              dt.date(YEAR, 1, 1) + dt.timedelta(days=192))
    print(f"관측 수집: {start} ~ {end}")
    by_stn = {}
    d = start
    while d <= end:
        try:
            for row in fetch_asos_daily(d):
                doy = (d - dt.date(YEAR, 1, 1)).days + 1
                by_stn.setdefault(row["STN"], {})[doy] = row
        except requests.RequestException as e:
            print(f"[경고] {d}: {e}")
        d += dt.timedelta(days=1)
    print(f"ASOS {len(by_stn)}지점 수신")

    out = {"generated": dt.datetime.now().isoformat(timespec="seconds"),
           "year": YEAR, "stations": {}, "sites": {}}
    # 전국 ASOS (관리원)
    for nkey, meta in NORMALS.items():
        if not nkey.startswith("ASOS_"):
            continue
        stn = nkey.split("_")[1]
        res = compute_site(nkey, by_stn.get(stn, {}))
        res.update(name=meta["name"], lat=meta["lat"], lon=meta["lon"])
        out["stations"][stn] = res
    # 조사지점 21곳 (최근접 ASOS 관측 + 자기 평년)
    for site, m in SITE_MAP.items():
        res = compute_site(site, by_stn.get(m["stn"], {}))
        nm = NORMALS.get(site, {})
        res.update(asos_stn=m["stn"], asos_km=m["km"],
                   lat=nm.get("lat"), lon=nm.get("lon"))
        out["sites"][site] = res

    os.makedirs(os.path.join(_here, "docs"), exist_ok=True)
    p = os.path.join(_here, "docs", "data.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    ok = sum(1 for r in out["stations"].values() if r["status"] == "ok")
    ok2 = sum(1 for r in out["sites"].values() if r["status"] == "ok")
    print(f"완료: ASOS {ok}/{len(out['stations'])},"
          f" 조사지점 {ok2}/{len(out['sites'])} → {p}")

if __name__ == "__main__":
    main()
