# -*- coding: utf-8 -*-
"""
extract_normals.py — 평년 래스터 → 지점별 평년 테이블 (1회 실행)
================================================================
Karl 로컬(래스터 보유 PC)에서 실행. 두 가지 작업:

  [작업 A] 평년 래스터(D:\\Climate_raster_penisula\\평년)를
           전국 ASOS 지점 + 조사지점 좌표에서 샘플링
           → normals.json  (서비스가 관측 이후 기간을 채울 때 사용)

  [작업 B] 일사 단위 캘리브레이션:
           관측 래스터 Solar(주간평균 W/m²) vs ASOS SI_DAY(MJ/m²)
           같은 지점·같은 날짜 값을 짝지어 변환계수 산출
           → solar_calib.csv + 콘솔에 회귀 결과

실행:
  pip install rasterio pyproj requests
  set KMA_KEY=인증키
  python extract_normals.py
"""

import os, sys, json, glob, csv
import datetime as dt
import requests

try:
    import rasterio
    from pyproj import Transformer
except ImportError:
    sys.exit("pip install rasterio pyproj requests 후 실행하세요.")

# ── 경로 (Karl PC 기준 — 다르면 수정) ─────────────────────────
PATH_NORMAL = r"D:\Climate_raster_penisula\평년"
PATH_OBS    = r"D:\Climate_raster_penisula\high"
PATH_OUT    = r"E:\research\APP\output"   # 결과 저장 폴더 (자동 생성)
VARS = ["Tavg", "Tmax", "Tmin", "HMDT", "Rn", "Solar", "WDSP"]

# 일사 캘리브레이션 대상 기간 (관측 래스터가 존재하는 최근 개화기)
CALIB_START = dt.date(2025, 4, 1)
CALIB_END   = dt.date(2025, 5, 31)
CALIB_STNS  = ["108", "133", "146", "143", "156"]  # 서울,대전,전주,대구,광주

KEY = os.environ.get("KMA_KEY")
if not KEY:
    # 초보자용: 스크립트와 같은 폴더의 kma_key.txt에서 키 읽기
    _kf = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "kma_key.txt")
    if os.path.exists(_kf):
        KEY = open(_kf, encoding="utf-8").read().strip()
BASE = "https://apihub.kma.go.kr/api/typ01/url"

# 조사지점(래스터 28지점 중 남한) — ASOS 목록에 이름으로 추가됨
EXTRA_SITES = {
    "화성": (37.19, 126.83), "연천": (38.10, 127.08), "이천": (37.28, 127.44),
    "파주": (37.89, 126.79), "함안": (35.28, 128.41), "구미": (36.12, 128.34),
    "김천": (36.12, 128.11), "상주": (36.41, 128.16), "안동": (36.57, 128.73),
    "예천": (36.65, 128.45), "세종": (36.48, 127.29), "철원": (38.21, 127.21),
    "천안": (36.81, 127.15), "보은": (36.48, 127.72), "창녕": (35.54, 128.50),
    "의성": (36.36, 128.69), "포천": (37.89, 127.20), "여주": (37.30, 127.64),
    "강릉": (37.75, 128.87), "김제": (35.80, 126.88), "화순": (35.06, 126.98),
}


def fetch_asos_stations():
    """전국 ASOS 지점번호·이름·좌표. 실패 시 빈 dict."""
    if not KEY:
        print("[안내] KMA_KEY 없음 → ASOS 지점 목록 생략, 조사지점만 처리")
        return {}
    try:
        r = requests.get(f"{BASE}/stn_inf.php",
                         params={"inf": "SFC", "stn": 0, "authKey": KEY},
                         timeout=30)
        r.raise_for_status()
        out = {}
        for line in r.text.splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            p = line.split()
            # stn_inf 형식: STN LON LAT ... NAME ← 열 위치는 출력 보고 조정
            try:
                stn = p[0]
                lon, lat = float(p[1]), float(p[2])
                if 33 < lat < 39.6 and 124 < lon < 132:
                    out[stn] = {"lat": lat, "lon": lon,
                                "name": p[-2] if len(p) > 10 else stn}
            except (ValueError, IndexError):
                continue
        print(f"ASOS 지점 {len(out)}곳 확보")
        return out
    except Exception as e:
        print(f"[경고] 지점 목록 실패: {e}")
        return {}


def sample_raster(path, coords_xy):
    """coords_xy: [(x,y) raster CRS] → 값 리스트"""
    with rasterio.open(path) as src:
        vals = [v[0] for v in src.sample(coords_xy)]
        nd = src.nodata
    return [None if (v is None or (nd is not None and v == nd)
                     or v < -900 or v > 9000) else float(v) for v in vals]


def main():
    # ── 지점 목록 구성 ──
    sites = {}
    # 1) 관측망 shapefile 정리본 (ASOS+AWS+AMOS+북한, 1,104지점)
    if os.path.exists("stations.json"):
        st = json.load(open("stations.json", encoding="utf-8"))
        for key, m in st.items():
            sites[key] = (m["lat"], m["lon"], m["name"])
        print(f"stations.json에서 {len(st)}지점 로드")
    # 2) API의 ASOS 목록(번호 확인용, stations.json 없을 때 보조)
    for stn, m in fetch_asos_stations().items():
        sites.setdefault("ASOS_" + stn, (m["lat"], m["lon"], m["name"]))
    for name, (lat, lon) in EXTRA_SITES.items():
        sites[name] = (lat, lon, name)
    print(f"총 샘플 지점: {len(sites)}곳")

    # ── 래스터 CRS 파악 + 좌표 변환 ──
    sample_tif = glob.glob(os.path.join(PATH_NORMAL, "Tavg", "*.TIF"))
    if not sample_tif:
        sys.exit(f"평년 래스터를 찾지 못함: {PATH_NORMAL}\\Tavg")
    with rasterio.open(sample_tif[0]) as src:
        crs = src.crs
    tr = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    keys = list(sites.keys())
    coords = [tr.transform(sites[k][1], sites[k][0]) for k in keys]

    # ── 작업 A: 평년 샘플링 (이미 있으면 건너뜀) ──
    os.makedirs(PATH_OUT, exist_ok=True)
    _normals_path = os.path.join(PATH_OUT, "normals.json")
    if os.path.exists(_normals_path):
        print("\n[작업 A] normals.json 존재 → 건너뜀 (다시 만들려면 파일 삭제)")
        do_a = False
    else:
        do_a = True
    if do_a:
        print("\n[작업 A] 평년 래스터 샘플링")
        normals = {k: {"name": sites[k][2], "lat": sites[k][0],
                       "lon": sites[k][1]} for k in keys}
        for var in VARS:
            files = sorted(glob.glob(os.path.join(PATH_NORMAL, var, "*.TIF")))
            print(f"  {var}: {len(files)}개 파일")
            for f in files:
                mmdd = os.path.basename(f).split("_")[1][:4]
                vals = sample_raster(f, coords)
                for k, v in zip(keys, vals):
                    normals[k].setdefault(var, {})[mmdd] = \
                        None if v is None else round(v, 2)
        with open(_normals_path, "w", encoding="utf-8") as f:
            json.dump(normals, f, ensure_ascii=False)
        print(f"  → {_normals_path} 저장")

    # ── 작업 B: 일사 캘리브레이션 ──
    if not KEY:
        print("\n[작업 B] KMA_KEY 없음 → 생략")
        return
    print("\n[작업 B] 일사 캘리브레이션 (래스터 Solar vs ASOS SI_DAY)")
    calib_coords = {}
    for stn in CALIB_STNS:
        if stn in sites:
            calib_coords[stn] = tr.transform(sites[stn][1], sites[stn][0])
    pairs = []
    d = CALIB_START
    while d <= CALIB_END:
        f = os.path.join(PATH_OBS, "Solar", f"Solar_{d:%Y%m%d}.TIF")
        if os.path.exists(f) and calib_coords:
            ks = list(calib_coords.keys())
            rv = sample_raster(f, [calib_coords[k] for k in ks])
            try:
                r = requests.get(f"{BASE}/kma_sfcdd.php",
                                 params={"tm": d.strftime("%Y%m%d"),
                                         "stn": ":".join(ks), "help": 0,
                                         "authKey": KEY}, timeout=30)
                asos = {}
                for line in r.text.splitlines():
                    if line.strip() and not line.startswith("#"):
                        p = line.split()
                        if len(p) > 35:
                            asos[p[1]] = p  # 행 전체 보관
                for k, rval in zip(ks, rv):
                    if rval is not None and k in asos:
                        pairs.append((d.isoformat(), k, rval, asos[k]))
            except requests.RequestException:
                pass
        d += dt.timedelta(days=1)
    with open(os.path.join(PATH_OUT, "solar_calib_raw.csv"),
              "w", newline="",
              encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "stn", "raster_solar_Wm2", "asos_row_raw"])
        for row in pairs:
            w.writerow([row[0], row[1], row[2], " ".join(row[3])])
    print(f"  짝지은 표본 {len(pairs)}건 → "
          f"{os.path.join(PATH_OUT, 'solar_calib_raw.csv')}")
    print("  (SI_DAY 열 번호 확정 후 회귀 — Claude에게 CSV 헤더 몇 줄 전달)")


if __name__ == "__main__":
    main()
