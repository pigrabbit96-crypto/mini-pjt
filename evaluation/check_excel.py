"""도구 결과를 원본 엑셀에서 직접 계산한 값과 대조한다.

src/tools.py 의 엑셀 읽기 코드를 쓰지 않고 xlsx 를 zip·XML 로 직접 읽는다.
- 코호트: 헤더에서 'N세' 열을, 시도 이름으로 행을 찾아 셀 값을 track_cohort 결과와 비교한다.
- 지표: 나이별 셀 값으로 고령화율·부양비·노령화지수·중위연령 등을 다시 계산해 get_indicators 와 비교한다.
- 순위: 17개 시도 값을 다시 계산해 정렬한 순서·값을 rank_regions 와 비교한다.
강원·전북은 행정구역 개편 전후 이름(강원도/강원특별자치도, 전라북도/전북특별자치도)을 연도에 맞춰 읽는다.

실행: 프로젝트 루트에서
    python evaluation/check_excel.py
"""

from __future__ import annotations

import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.tools import get_indicators, rank_regions, track_cohort  # noqa: E402

DATA_DIR = ROOT / "data"
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
# (출생연도, 도구에 넘길 지역, 연도별 엑셀 시도 이름)
CASES = [
    (1998, "서울", lambda y: "서울특별시"),
    (1998, "전남", lambda y: "전라남도"),
    (1998, "전북", lambda y: "전라북도" if y < 2024 else "전북특별자치도"),
    (1995, "부산", lambda y: "부산광역시"),
]
YEARS = (2021, 2025)
# 엑셀 시도 이름 -> 도구가 돌려주는 표시 이름(최신 명칭)
SIDO_NAMES = {
    "서울특별시": "서울특별시", "부산광역시": "부산광역시", "대구광역시": "대구광역시",
    "인천광역시": "인천광역시", "광주광역시": "광주광역시", "대전광역시": "대전광역시",
    "울산광역시": "울산광역시", "세종특별자치시": "세종특별자치시", "경기도": "경기도",
    "강원도": "강원특별자치도", "강원특별자치도": "강원특별자치도", "충청북도": "충청북도",
    "충청남도": "충청남도", "전라북도": "전북특별자치도", "전북특별자치도": "전북특별자치도",
    "전라남도": "전라남도", "경상북도": "경상북도", "경상남도": "경상남도", "제주특별자치도": "제주특별자치도",
}
# (도구에 넘길 지역, 엑셀 행 이름)
INDICATOR_CASES = [("전국", "전국"), ("전남", "전라남도"), ("서울", "서울특별시"), ("세종", "세종특별자치시")]


def _column_index(ref: str) -> int:
    """'AB12' 같은 셀 주소에서 열 번호(1부터)를 구한다."""
    col = 0
    for ch in re.match(r"[A-Z]+", ref).group():
        col = col * 26 + ord(ch) - 64
    return col


def read_sheet(path: Path) -> list[dict[int, str | None]]:
    """xlsx 첫 시트를 {열 번호: 셀 문자열} 행 목록으로 읽는다."""
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(f"{NS}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{NS}t")))
        sheet = sorted(n for n in z.namelist() if n.startswith("xl/worksheets/sheet"))[0]
        root = ET.fromstring(z.read(sheet))
    rows = []
    for row in root.iter(f"{NS}row"):
        cells = {}
        for c in row.iter(f"{NS}c"):
            v, inline = c.find(f"{NS}v"), c.find(f"{NS}is")
            if c.get("t") == "s":
                value = shared[int(v.text)]
            elif inline is not None:
                value = "".join(t.text or "" for t in inline.iter(f"{NS}t"))
            else:
                value = v.text if v is not None else None
            cells[_column_index(c.get("r"))] = value
        rows.append(cells)
    return rows


def excel_cell(year: int, sido: str, age: int) -> int:
    """연도 파일에서 시도 행과 나이 열이 만나는 셀 값을 읽는다."""
    path = next(DATA_DIR.glob(f"{year}12_*.xlsx"))
    rows = read_sheet(path)
    col = next(k for k, v in rows[3].items() if v == f"{age}세")
    for row in rows[4:]:
        if (row.get(2) or "").strip() == sido:
            return int(str(row[col]).replace(",", ""))
    raise KeyError(f"{path.name} 에 {sido} 행이 없다")


def excel_ages(year: int) -> dict[str, list[int]]:
    """연도 파일에서 {엑셀 행 이름: 0~100세 나이별 인구 101칸} 을 읽는다. 100은 100세 이상."""
    path = next(DATA_DIR.glob(f"{year}12_*.xlsx"))
    rows = read_sheet(path)
    header = rows[3]
    cols = [next(k for k, v in header.items() if v == (f"{a}세" if a < 100 else "100세 이상")) for a in range(101)]
    return {
        (row.get(2) or "").strip(): [int(str(row[c]).replace(",", "")) for c in cols]
        for row in rows[4:]
        if (row.get(2) or "").strip()
    }


def expected_indicators(ages: list[int]) -> dict:
    """나이별 인구로 지표를 다시 계산한다. 정의는 data/population_guide.md 를 따른다."""
    total = sum(ages)
    young, working, old = sum(ages[:15]), sum(ages[15:65]), sum(ages[65:])
    pct = lambda a, b: round(a / b * 100, 2)  # noqa: E731
    half, cum, median = total / 2, 0, 100.0
    for age, n in enumerate(ages):
        if cum + n >= half:
            median = age + (half - cum) / n
            break
        cum += n
    return {
        "총인구": total, "고령화율": pct(old, total), "유소년인구비율": pct(young, total),
        "생산연령인구비율": pct(working, total), "노년부양비": pct(old, working),
        "유소년부양비": pct(young, working), "총부양비": pct(old + young, working),
        "노령화지수": pct(old, young), "중위연령": round(median, 1),
        "평균연령": round(sum(a * n for a, n in enumerate(ages)) / total, 1),
    }


def check_cohorts() -> int:
    """코호트 인원을 엑셀 셀 값과 대조하고 불일치 건수를 돌려준다."""
    mismatches = 0
    for birth, region, sido_of in CASES:
        counts = track_cohort.invoke({"birth_year": birth, "regions": [region]})["결과"][0]["연도별 인원"]
        for year in YEARS:
            sido, age = sido_of(year), year - birth
            raw, tool = excel_cell(year, sido, age), counts[f"{year}년"]
            ok = raw == tool
            mismatches += not ok
            print(f"{birth}년생 {region} {year}년: 엑셀[{sido} {age}세] {raw:,} / 도구 {tool:,} -> {'일치' if ok else '불일치'}")
    return mismatches


def check_indicators() -> int:
    """2025년 지표를 엑셀로 다시 계산한 값과 대조하고 불일치 건수를 돌려준다."""
    ages = excel_ages(2025)
    mismatches = 0
    for region, sido in INDICATOR_CASES:
        tool = get_indicators.invoke({"year": 2025, "regions": [region]})["결과"][0]
        diffs = {k: (tool[k], v) for k, v in expected_indicators(ages[sido]).items() if tool[k] != v}
        mismatches += bool(diffs)
        print(f"2025년 {sido} 지표 10개: {'일치' if not diffs else '불일치 ' + str(diffs)}")
    return mismatches


def check_ranks() -> int:
    """2025년 고령화율 순위와 2021→2025년 20대 인구 변화 순위를 엑셀 계산과 대조한다."""
    a21, a25 = excel_ages(2021), excel_ages(2025)
    sido25 = {SIDO_NAMES[n]: v for n, v in a25.items() if n in SIDO_NAMES}
    sido21 = {SIDO_NAMES[n]: v for n, v in a21.items() if n in SIDO_NAMES}

    expected = sorted(((s, round(sum(v[65:]) / sum(v) * 100, 2)) for s, v in sido25.items()), key=lambda x: -x[1])
    got = [(r["지역"], r["2025년"]) for r in rank_regions.invoke({"metric": "고령화율", "year": 2025})["결과"]]
    ok1 = got == expected
    print(f"2025년 고령화율 17개 시도 순위: {'일치' if ok1 else '불일치'} (1위 {got[0]}, 17위 {got[-1]})")

    expected = sorted(((s, sum(sido25[s][20:30]) - sum(sido21[s][20:30])) for s in sido25), key=lambda x: x[1])
    result = rank_regions.invoke(
        {"metric": "인구수", "year": 2025, "compare_year": 2021, "age_min": 20, "age_max": 29, "descending": False}
    )
    got = [(r["지역"], r["변화(명)"]) for r in result["결과"]]
    ok2 = got == expected
    print(f"2021→2025년 20대 인구 변화 17개 시도 순위: {'일치' if ok2 else '불일치'} (1위 {got[0]})")
    return (not ok1) + (not ok2)


def main() -> None:
    """모든 대조를 실행하고 결과를 출력한다."""
    print("[코호트]")
    cohort = check_cohorts()
    print("\n[지표]")
    indicator = check_indicators()
    print("\n[순위]")
    rank = check_ranks()
    print(f"\n불일치: 코호트 {cohort}건, 지표 {indicator}건, 순위 {rank}건")


if __name__ == "__main__":
    main()
