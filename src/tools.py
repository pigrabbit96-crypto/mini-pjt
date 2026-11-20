"""인구 질의 에이전트의 도메인 도구.

data/ 의 연령별 인구현황 엑셀(연도별 1개 파일)을 읽어 인구 조회, 지표 계산,
시도 순위, 출생 코호트 추적을 한다. 답변에 쓰이는 인구 숫자는 모두 이 파일의
계산 결과에서 나온다.
"""

from __future__ import annotations

import re
import zipfile
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path

from langchain_core.tools import tool

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
FILE_PATTERN = "*_연령별인구현황_연간.xlsx"
MAX_AGE = 100  # 100 은 '100세 이상' 칸
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

# (고정 키, 표시 이름, 원본 행정기관코드 앞 두 자리, 별칭)
# 강원(42→51)과 전북(45→52)은 행정구역 개편으로 코드가 바뀌어 하나의 키로 합친다.
REGION_TABLE = [
    ("전국", "전국", ("00",), ()),
    ("서울", "서울특별시", ("11",), ("서울시",)),
    ("부산", "부산광역시", ("26",), ("부산시",)),
    ("대구", "대구광역시", ("27",), ("대구시",)),
    ("인천", "인천광역시", ("28",), ("인천시",)),
    ("광주", "광주광역시", ("29",), ("광주시",)),
    ("대전", "대전광역시", ("30",), ("대전시",)),
    ("울산", "울산광역시", ("31",), ("울산시",)),
    ("세종", "세종특별자치시", ("36",), ("세종시",)),
    ("경기", "경기도", ("41",), ()),
    ("강원", "강원특별자치도", ("42", "51"), ("강원도",)),
    ("충북", "충청북도", ("43",), ()),
    ("충남", "충청남도", ("44",), ()),
    ("전북", "전북특별자치도", ("45", "52"), ("전라북도",)),
    ("전남", "전라남도", ("46",), ()),
    ("경북", "경상북도", ("47",), ()),
    ("경남", "경상남도", ("48",), ()),
    ("제주", "제주특별자치도", ("50",), ("제주도",)),
]
REGION_NAME = {key: name for key, name, _, _ in REGION_TABLE}
SIDO_KEYS = [key for key, _, _, _ in REGION_TABLE if key != "전국"]
_CODE_TO_KEY = {code: key for key, _, codes, _ in REGION_TABLE for code in codes}
_ALIAS_TO_KEY = {
    alias.replace(" ", ""): key
    for key, name, _, aliases in REGION_TABLE
    for alias in (key, name, *aliases)
}

INDICATOR_NAMES = [
    "고령화율",
    "유소년인구비율",
    "생산연령인구비율",
    "노년부양비",
    "유소년부양비",
    "총부양비",
    "노령화지수",
    "중위연령",
    "평균연령",
]
PERCENT_METRICS = {"비율", "고령화율", "유소년인구비율", "생산연령인구비율"}
RANK_METRICS = ["인구수", "비율", *INDICATOR_NAMES]
COHORT_WARNING_AGE = 60


# ---------------------------------------------------------------- 데이터 읽기


def _read_first_sheet(path: Path) -> list[list[str | None]]:
    """xlsx 파일의 첫 번째 시트를 행 목록으로 읽는다. 셀 값은 문자열이다."""
    with zipfile.ZipFile(path) as z:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.iter(f"{_NS}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{_NS}t")))
        sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))

    rows: list[list[str | None]] = []
    for r in sheet.iter(f"{_NS}row"):
        cells: dict[int, str | None] = {}
        for c in r.iter(f"{_NS}c"):
            letters = re.match(r"[A-Z]+", c.get("r", "")).group()
            col = 0
            for ch in letters:
                col = col * 26 + ord(ch) - 64
            v = c.find(f"{_NS}v")
            kind = c.get("t")
            if kind == "inlineStr":
                value = "".join(t.text or "" for t in c.iter(f"{_NS}t"))
            elif v is None:
                value = None
            elif kind == "s":
                value = shared[int(v.text)]
            else:
                value = v.text
            cells[col - 1] = value
        width = max(cells) + 1 if cells else 0
        rows.append([cells.get(i) for i in range(width)])
    return rows


def _to_int(text: str | None) -> int:
    """'51,117,378' 같은 쉼표 숫자 문자열을 정수로 바꾼다."""
    return int(str(text).replace(",", "").strip())


def _parse_year_file(path: Path) -> tuple[int, dict[str, list[int]]]:
    """연도별 엑셀 하나를 읽어 (연도, {지역 키: 나이별 인구 101개}) 를 돌려준다.

    표 형식이 예상과 다르거나 나이별 합계가 총인구와 맞지 않으면 ValueError 를 낸다.
    """
    rows = _read_first_sheet(path)
    year_match = re.search(r"\d{4}", rows[1][2] or "")
    header = rows[3]
    if (
        not year_match
        or header[0] != "행정기관코드"
        or header[4] != "0세"
        or header[4 + MAX_AGE] != "100세 이상"
    ):
        raise ValueError(f"{path.name}: 예상한 연령별 인구현황 형식이 아니다")
    year = int(year_match.group())

    regions: dict[str, list[int]] = {}
    for row in rows[4:]:
        if not row or not row[0]:
            continue
        code = row[0].strip()[:2]
        key = _CODE_TO_KEY.get(code)
        if key is None:
            raise ValueError(f"{path.name}: 알 수 없는 행정기관코드 {row[0]}")
        ages = [_to_int(x) for x in row[4 : 5 + MAX_AGE]]
        if sum(ages) != _to_int(row[2]):
            raise ValueError(f"{path.name}: {row[1].strip()} 나이별 합계가 총인구와 다르다")
        regions[key] = ages

    missing = set(REGION_NAME) - set(regions)
    if missing:
        raise ValueError(f"{path.name}: 지역 누락 {sorted(missing)}")
    return year, regions


@lru_cache(maxsize=1)
def load_population() -> dict[int, dict[str, list[int]]]:
    """data/ 의 모든 연도 엑셀을 읽어 {연도: {지역 키: 나이별 인구}} 로 돌려준다.

    한 번 읽은 결과는 캐시한다.
    """
    files = sorted(DATA_DIR.glob(FILE_PATTERN))
    if not files:
        raise FileNotFoundError(f"{DATA_DIR} 에 {FILE_PATTERN} 파일이 없다")
    data = dict(_parse_year_file(p) for p in files)
    return dict(sorted(data.items()))


# ---------------------------------------------------------------- 계산 도우미


def _years() -> list[int]:
    """데이터에 있는 연도 목록을 돌려준다."""
    return list(load_population())


def _basis(year: int) -> str:
    """답변에 붙일 기준 시점 문구를 만든다."""
    return f"{year}년 12월 말 주민등록 기준"


def _error(message: str, available: object) -> dict:
    """에이전트가 질문을 고쳐 다시 시도할 수 있도록 오류와 사용 가능한 값을 묶는다."""
    return {"오류": message, "사용 가능한 값": available}


def _resolve_region(name: str) -> str | None:
    """시도 이름이나 별칭을 고정 키로 바꾼다. 모르는 이름이면 None."""
    return _ALIAS_TO_KEY.get(name.replace(" ", ""))


def _resolve_regions(names: list[str] | None) -> tuple[list[str], dict | None]:
    """지역 이름 목록을 키 목록으로 바꾼다. 비어 있으면 전국과 17개 시도 전부.

    모르는 이름이 있으면 (빈 목록, 오류 사전) 을 돌려준다.
    """
    if not names:
        return ["전국", *SIDO_KEYS], None
    keys, unknown = [], []
    for name in names:
        key = _resolve_region(name)
        if key is None:
            unknown.append(name)
        elif key not in keys:
            keys.append(key)
    if unknown:
        return [], _error(
            f"데이터에 없는 지역: {', '.join(unknown)}. 이 데이터는 전국과 17개 시도만 있다",
            list(REGION_NAME.values()),
        )
    return keys, None


def _check_year(year: int) -> dict | None:
    """연도가 데이터 범위 안인지 확인한다. 범위 밖이면 오류 사전."""
    if year not in load_population():
        return _error(f"{year}년 데이터는 없다", _years())
    return None


def _check_age_range(age_min: int, age_max: int) -> dict | None:
    """나이 구간이 0~100 안에 있고 순서가 맞는지 확인한다."""
    if not (0 <= age_min <= age_max <= MAX_AGE):
        return _error(
            f"나이 구간 {age_min}~{age_max} 이 올바르지 않다",
            "0~100 (100은 100세 이상), 시작 나이 <= 끝 나이",
        )
    return None


def _age_sum(ages: list[int], age_min: int, age_max: int) -> int:
    """나이별 인구에서 age_min 세부터 age_max 세까지(양 끝 포함) 합한다."""
    return sum(ages[age_min : age_max + 1])


def _pct(part: int, whole: int) -> float:
    """백분율을 소수 둘째 자리까지 구한다."""
    return round(part / whole * 100, 2) if whole else 0.0


def aging_stage(rate: float) -> str:
    """65세 이상 비율로 UN 기준 고령사회 단계를 정한다. 답변 해석 점검(src/agent.py)에서도 쓴다."""
    if rate >= 20:
        return "초고령사회"
    if rate >= 14:
        return "고령사회"
    if rate >= 7:
        return "고령화사회"
    return "고령화사회 이전"


@lru_cache(maxsize=None)
def _indicators(year: int, key: str) -> dict:
    """한 지역·연도의 연령 구조 지표를 모두 계산한다."""
    ages = load_population()[year][key]
    total = sum(ages)
    young = _age_sum(ages, 0, 14)
    working = _age_sum(ages, 15, 64)
    old = _age_sum(ages, 65, MAX_AGE)

    # 중위연령: 만 나이 a 세 인구가 [a, a+1) 에 고르게 퍼져 있다고 보고 선형 보간한다.
    half, cum, median = total / 2, 0, float(MAX_AGE)
    for age, n in enumerate(ages):
        if n and cum + n >= half:
            median = age + (half - cum) / n
            break
        cum += n

    elderly_rate = _pct(old, total)
    return {
        "총인구": total,
        "유소년인구(0~14세)": young,
        "생산연령인구(15~64세)": working,
        "고령인구(65세 이상)": old,
        "고령화율": elderly_rate,
        "유소년인구비율": _pct(young, total),
        "생산연령인구비율": _pct(working, total),
        "노년부양비": _pct(old, working),
        "유소년부양비": _pct(young, working),
        "총부양비": _pct(old + young, working),
        "노령화지수": _pct(old, young),
        "중위연령": round(median, 1),
        "평균연령": round(sum(a * n for a, n in enumerate(ages)) / total, 1),
        "고령사회단계": aging_stage(elderly_rate),
    }


def _change_unit(metric: str) -> str:
    """순위 기준값의 두 연도 차이에 붙일 단위를 정한다. 부양비·노령화지수처럼 단위가 없으면 빈 문자열."""
    if metric == "인구수":
        return "명"
    if metric in PERCENT_METRICS:
        return "%p"
    if metric in ("중위연령", "평균연령"):
        return "세"
    return ""


def _metric_value(metric: str, year: int, key: str, age_min: int, age_max: int) -> float:
    """순위 기준값 하나를 계산한다."""
    ages = load_population()[year][key]
    if metric == "인구수":
        return _age_sum(ages, age_min, age_max)
    if metric == "비율":
        return _pct(_age_sum(ages, age_min, age_max), sum(ages))
    return _indicators(year, key)[metric]


# ---------------------------------------------------------------- 에이전트 도구


@tool(parse_docstring=True)
def get_data_coverage() -> dict:
    """데이터로 답할 수 있는 범위를 알려준다.

    질문이 데이터 범위 안인지 모를 때, 또는 지역 이름·연도·나이 범위를 확인할 때 먼저 쓴다.
    """
    years = _years()
    return {
        "연도": years,
        "기준": "각 연도 12월 말 주민등록 인구 (외국인 제외)",
        "지역": [
            {"이름": name, "별칭": [key, *aliases] if key != "전국" else []}
            for key, name, _, aliases in REGION_TABLE
        ],
        "나이": "0~99세 1세 단위, 100은 100세 이상을 묶은 칸",
        "코호트 추적 가능 출생연도": f"{years[-1] - (MAX_AGE - 1)}~{years[0]}년생",
        "없는 데이터": ["성별", "시군구 이하 지역", "월별", "장래 추계", "출생·사망·전입전출 건수"],
    }


@tool(parse_docstring=True)
def get_population(
    year: int,
    regions: list[str] | None = None,
    age_min: int = 0,
    age_max: int = MAX_AGE,
    compare_year: int | None = None,
) -> dict:
    """연도·지역·나이 구간별 인구수와 전체 인구 대비 비율(구성비)을 조회한다.

    두 연도를 비교하는 질문에는 compare_year 를 줘서 한 번에 부른다.
    그러면 이전 연도 값과 함께 변화(명), 변화율(%), 구성비 변화(%p)를 계산해 돌려준다.

    Args:
        year: 기준 연도 (예: 2025).
        regions: 시도 이름이나 별칭 목록 (예: ["서울", "전남"]). 비우면 전국과 17개 시도 전부.
        age_min: 구간 시작 나이 (만 나이, 포함). 기본 0.
        age_max: 구간 끝 나이 (포함). 100은 100세 이상. 기본 100.
        compare_year: 비교할 이전 연도 (예: 2021). 주면 변화량을 함께 돌려준다.
    """
    err = _check_year(year) or _check_age_range(age_min, age_max)
    if err is None and compare_year is not None:
        err = _check_year(compare_year)
    if err:
        return err
    keys, err = _resolve_regions(regions)
    if err:
        return err
    data = load_population()[year]
    rows = []
    for key in keys:
        ages = data[key]
        count = _age_sum(ages, age_min, age_max)
        share = _pct(count, sum(ages))
        item = {
            "지역": REGION_NAME[key],
            "인구수": count,
            "총인구": sum(ages),
            "비율(%)": share,
        }
        if compare_year is not None:
            before_ages = load_population()[compare_year][key]
            before = _age_sum(before_ages, age_min, age_max)
            before_share = _pct(before, sum(before_ages))
            item[f"{compare_year}년 인구수"] = before
            item[f"{compare_year}년 총인구"] = sum(before_ages)
            item[f"{compare_year}년 비율(%)"] = before_share
            item["변화(명)"] = count - before
            item["변화율(%)"] = round((count / before - 1) * 100, 2) if before else None
            item["구성비 변화(%p)"] = round(share - before_share, 2)
        rows.append(item)
    label = f"{age_min}세 이상" if age_max == MAX_AGE else f"{age_min}~{age_max}세"
    basis = _basis(year) + (f", {compare_year}년 대비" if compare_year is not None else "")
    return {"기준": basis, "나이 구간": label, "결과": rows}


@tool(parse_docstring=True)
def get_indicators(year: int, regions: list[str] | None = None) -> dict:
    """연령 구조 지표를 계산한다.

    고령화율, 유소년·생산연령인구 비율, 노년·유소년·총부양비, 노령화지수,
    중위연령, 평균연령, 고령사회 단계를 함께 돌려준다.
    고령화율·부양비·노령화지수·중위연령·평균연령을 묻는 질문에 쓴다. 두 연도를 비교할 때는
    연도마다 한 번씩 부르고, 차이는 숫자를 직접 빼지 말고 말로 비교한다.

    Args:
        year: 기준 연도 (예: 2025).
        regions: 시도 이름이나 별칭 목록. 비우면 전국과 17개 시도 전부.
    """
    if err := _check_year(year):
        return err
    keys, err = _resolve_regions(regions)
    if err:
        return err
    return {
        "기준": _basis(year),
        "결과": [{"지역": REGION_NAME[k], **_indicators(year, k)} for k in keys],
    }


@tool(parse_docstring=True)
def rank_regions(
    metric: str,
    year: int,
    age_min: int = 0,
    age_max: int = MAX_AGE,
    compare_year: int | None = None,
    descending: bool = True,
    top_n: int = 17,
    regions: list[str] | None = None,
) -> dict:
    """시도를 기준값으로 줄 세운다. 전국 값은 참고용으로 따로 돌려준다.

    "가장 높은/낮은 시도", "몇 위", "세 곳 이상 시도 비교"처럼 순위나 여러 시도 비교가 필요할 때 쓴다.
    regions 를 주면 그 시도들만 비교해 그 안에서 순위를 매기고, 비우면 17개 시도 전부를 줄 세운다.
    compare_year 를 주면 두 연도 사이의 변화량(지표는 차이, 인구수는 증감)으로 줄 세운다.
    기준이 "인구수"이면 변화율(%)과 함께 총인구 중 구성비(%)와 구성비 변화(%p)도 돌려준다.

    Args:
        metric: 기준. "인구수", "비율"(나이 구간 인구 비율), 또는 지표 이름 하나
            (고령화율, 유소년인구비율, 생산연령인구비율, 노년부양비, 유소년부양비,
            총부양비, 노령화지수, 중위연령, 평균연령).
        year: 기준 연도.
        age_min: "인구수"·"비율" 기준에서 쓰는 구간 시작 나이. 기본 0.
        age_max: "인구수"·"비율" 기준에서 쓰는 구간 끝 나이. 기본 100.
        compare_year: 비교할 이전 연도. 주면 변화량으로 줄 세운다.
        descending: True 면 큰 값부터, False 면 작은 값부터.
        top_n: 돌려줄 시도 수 (1~17).
        regions: 비교할 시도 이름이나 별칭 목록 (예: ["서울", "부산", "대구"]). 비우면 17개 시도 전부.
    """
    if metric not in RANK_METRICS:
        return _error(f"알 수 없는 기준: {metric}", RANK_METRICS)
    err = _check_year(year) or _check_age_range(age_min, age_max)
    if err is None and compare_year is not None:
        err = _check_year(compare_year)
    if err:
        return err
    keys, err = _resolve_regions(regions)
    if err:
        return err
    keys = [k for k in keys if k != "전국"] or SIDO_KEYS
    top_n = max(1, min(top_n, len(keys)))
    change_key = f"변화({_change_unit(metric)})" if _change_unit(metric) else "변화"

    def row(key: str) -> dict:
        value = _metric_value(metric, year, key, age_min, age_max)
        item = {"지역": REGION_NAME[key], f"{year}년": value}
        if compare_year is not None:
            before = _metric_value(metric, compare_year, key, age_min, age_max)
            item[f"{compare_year}년"] = before
            item[change_key] = round(value - before, 2)
            if metric == "인구수":
                item["변화율(%)"] = round((value / before - 1) * 100, 2) if before else None
                # 총인구 중 이 나이 구간의 비중(구성비)이 몇 %p 바뀌었는지도 함께 돌려준다.
                share = _metric_value("비율", year, key, age_min, age_max)
                share_before = _metric_value("비율", compare_year, key, age_min, age_max)
                item[f"{year}년 구성비(%)"] = share
                item[f"{compare_year}년 구성비(%)"] = share_before
                item["구성비 변화(%p)"] = round(share - share_before, 2)
        return item

    rows = [row(k) for k in keys]
    sort_key = change_key if compare_year is not None else f"{year}년"
    rows.sort(key=lambda r: r[sort_key], reverse=descending)
    for rank, r in enumerate(rows, start=1):
        r["순위"] = rank

    target = f"{age_min}세 이상" if age_max == MAX_AGE else f"{age_min}~{age_max}세"
    return {
        "기준": _basis(year) + (f", {compare_year}년 대비" if compare_year else ""),
        "정렬 기준": metric + (f" ({target})" if metric in ("인구수", "비율") else ""),
        "정렬 방향": "큰 값부터" if descending else "작은 값부터",
        "비교 대상": "17개 시도 전부" if len(keys) == len(SIDO_KEYS) else f"고른 {len(keys)}개 시도",
        "전국": row("전국"),
        "결과": rows[:top_n],
    }


@tool(parse_docstring=True)
def track_cohort(birth_year: int, regions: list[str] | None = None) -> dict:
    """같은 해에 태어난 사람들(출생 코호트)을 첫 연도부터 마지막 연도까지 지역별로 따라간다.

    지역 변화율에서 전국 변화율을 빼서 사망·해외 이동 영향을 걷어 낸 국내 순변화를 추정한다.
    결과는 순변화 추정이며, 어느 지역에서 어느 지역으로 옮겼는지(이동 경로)는 알 수 없다.

    Args:
        birth_year: 출생연도 (예: 1998).
        regions: 시도 이름이나 별칭 목록. 비우면 17개 시도 전부를 전국 대비 차이가 큰 순서로 돌려준다.
    """
    data = load_population()
    years = _years()
    first, last = years[0], years[-1]
    lo, hi = last - (MAX_AGE - 1), first
    if not (lo <= birth_year <= hi):
        return _error(
            f"{birth_year}년생은 {first}~{last}년 사이를 추적할 수 없다",
            f"{lo}~{hi}년생 ({hi + 1}년 이후 출생은 {first}년 표에 없고, "
            f"{lo - 1}년 이전 출생은 {last}년에 100세 이상 칸으로 묶인다)",
        )
    keys, err = _resolve_regions(regions)
    if err:
        return err
    keys = [k for k in keys if k != "전국"] or SIDO_KEYS

    def counts(key: str) -> dict[int, int]:
        return {y: data[y][key][y - birth_year] for y in years}

    national = counts("전국")
    national_rate = national[last] / national[first] - 1

    rows = []
    for key in keys:
        c = counts(key)
        rate = c[last] / c[first] - 1
        rows.append(
            {
                "지역": REGION_NAME[key],
                "연도별 인원": {f"{y}년": n for y, n in c.items()},
                "변화율(%)": round(rate * 100, 2),
                "전국 대비 차이(%p)": round((rate - national_rate) * 100, 2),
                "국내 순변화 추정(명)": round(c[last] - c[first] * (1 + national_rate)),
            }
        )
    rows.sort(key=lambda r: r["전국 대비 차이(%p)"], reverse=True)

    result = {
        "기준": f"{first}~{last}년 각 12월 말 주민등록 기준",
        "출생연도": birth_year,
        "나이": f"{first}년 {first - birth_year}세 → {last}년 {last - birth_year}세 (연말 기준 만 나이)",
        "전국": {
            "연도별 인원": {f"{y}년": n for y, n in national.items()},
            "변화율(%)": round(national_rate * 100, 2),
        },
        "결과": rows,
        "해석 주의": "순변화 추정이다. 지역 간 이동 경로는 알 수 없다.",
    }
    if first - birth_year >= COHORT_WARNING_AGE:
        result["고령 코호트 주의"] = (
            f"{first}년에 {COHORT_WARNING_AGE}세 이상인 코호트는 지역별 사망률 차이가 커서 "
            "순변화 추정이 이동보다 사망 차이를 더 많이 반영할 수 있다."
        )
    return result


TOOLS = [get_data_coverage, get_population, track_cohort, get_indicators, rank_regions]
