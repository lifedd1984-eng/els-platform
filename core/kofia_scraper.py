"""
KOFIA 전자공시(dis.kofia.or.kr) — 청약중인 파생결합증권(ELS/DLS/ELB/DLB) 자동 수집.

메뉴 경로: 비교공시 > 파생결합증권등 청약정보 비교공시 > 청약중인상품
내부 API: DISDlsOfferSO.selectSubscribing (proframeWeb XMLSERVICES)

2026-07-18 실측 검증:
  - 순수 requests로 WAF 우회 확인 (브라우저 자동화 불필요)
  - 필요한 Referer 헤더만 있으면 정상 응답
  - 응답은 XML, <DISDlsDTO> 반복 블록으로 전체 건수(276건 등)가 한 번에 옴
  - 개별 필드는 val1~val30 (의미 있는 값은 val2~val23 부근에 집중)

⚠ 이 엔드포인트는 공식 문서화된 API가 아니라 KOFIA 웹페이지가 내부적으로
  쓰는 요청을 그대로 재현한 것. 페이지 개편 시 val 번호가 바뀔 수 있음 —
  fetch_subscribing()이 예외 없이 빈 리스트를 반환하면 val 매핑 재확인 필요.
"""

import re
import xml.etree.ElementTree as ET
from datetime import date

import requests

XML_URL = "https://dis.kofia.or.kr/proframeWeb/XMLSERVICES/"

REQUEST_BODY = """<?xml version="1.0" encoding="utf-8"?>
<message>
  <proframeHeader>
    <pfmAppName>FS-DIS2</pfmAppName>
    <pfmSvcName>DISDlsOfferSO</pfmSvcName>
    <pfmFnName>selectSubscribing</pfmFnName>
  </proframeHeader>
  <systemHeader></systemHeader>
    <DISDlsDTO>
    <val1></val1><val2></val2><val3></val3><val4></val4><val5></val5>
    <val6>0</val6>
</DISDlsDTO>
</message>"""

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Content-Type": "text/xml",
    "Referer": (
        "https://dis.kofia.or.kr/websquare/index.jsp?"
        "w2xPath=/wq/etcann/DISDLSSubscribing.xml"
        "&divisionId=MDIS04007001000000&serviceId=SDIS04007001000"
    ),
}


class KofiaFetchError(Exception):
    pass


def _v(el, n):
    node = el.find(f"val{n}")
    return (node.text or "").strip() if node is not None and node.text else ""


def _to_date(yyyymmdd):
    s = (yyyymmdd or "").strip()
    if len(s) == 8 and s.isdigit():
        try:
            return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        except ValueError:
            return None
    return None


def _to_float(s):
    try:
        return float(str(s).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _extract_product_no(name: str, product_code: str) -> str:
    """상품명에서 회차/상품번호 추출.

    우선순위:
      1) "제 N 회" 형식 → N
      2) 상품명 끝의 숫자 토큰 (예: "N2 ELS 369"→369, "NH투자증권(ELS) 24981"→24981)
         — exe 엑셀의 product_no와 동일하게 맞춰 중복 병합되게 함
      3) 그래도 없으면 product_code 뒤 6자리 (최후 폴백)
    """
    m = re.search(r"제\s*(\d+)\s*회", name)
    if m:
        return m.group(1)
    # 상품명 끝에 붙은 숫자 (뒤에서부터 마지막 숫자 토큰)
    nums = re.findall(r"\d+", name)
    if nums:
        return nums[-1]
    return (product_code or "")[-6:]


def fetch_subscribing(timeout=25) -> list[dict]:
    """
    청약중인 상품 전체를 가져와 표준 dict 리스트로 반환.

    Returns
    -------
    list[dict]: 각 dict는 core.parsers 및 Product 모델과 바로 맞물리는 키를 가짐
        issuer, product_no, product_code, name, assets_raw, description,
        yield_rate, max_loss, expiry_date, sub_start, sub_end
    """
    try:
        resp = requests.post(
            XML_URL, data=REQUEST_BODY.encode("utf-8"), headers=HEADERS, timeout=timeout
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        raise KofiaFetchError(f"KOFIA 요청 실패: {e}") from e

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as e:
        raise KofiaFetchError(f"KOFIA 응답 XML 파싱 실패: {e}") from e

    items = root.findall(".//DISDlsDTO")
    # 최상위 DISDlsListDTO 아래 직속 DISDlsDTO만 (내부 중첩 DTO 오염 방지)
    total_count = root.findtext(".//dbio_total_count_")

    rows = []
    for el in items:
        issuer = _v(el, 4)
        name = _v(el, 6)
        if not issuer or not name:
            continue

        product_no = _extract_product_no(name, _v(el, 22))

        assets_raw = _v(el, 8).replace("<br/>", "/").replace("<br>", "/")
        desc = _v(el, 18)

        # 증권사 상품 상세페이지 + 간이투자설명서 PDF (KOFIA 파일서버)
        broker_url = _v(el, 20)
        if not broker_url.startswith("http"):
            broker_url = ""
        def _t(tag):
            n = el.find(tag)
            return (n.text or "").strip() if n is not None and n.text else ""
        pdf_hash, pdf_path, pdf_name = _t("fileNm"), _t("serverPath"), _t("originalFileNm")
        prospectus_url = ""
        if pdf_hash and pdf_path:
            from urllib.parse import quote
            prospectus_url = (
                "https://disdown.kofia.or.kr/COMFSFileDownload.jsp"
                f"?serverFileNm={quote(pdf_hash)}"
                f"&serverPath={quote(pdf_path)}"
                f"&filename={quote(pdf_name or pdf_hash)}"
            )

        rows.append({
            "issuer": issuer,
            "product_no": product_no,
            "product_code": _v(el, 22),
            "name": name,
            "assets_raw": assets_raw,
            "description": desc,
            "broker_url": broker_url,
            "prospectus_url": prospectus_url,
            "yield_rate": _to_float(_v(el, 15)),
            "max_loss": _to_float(_v(el, 23)),
            # KOFIA 응답에 발행일(issue_date)이 별도로 없음 — 관측상 청약종료일과 동일
            "issue_date": _to_date(_v(el, 17)),
            "expiry_date": _to_date(_v(el, 14)),
            "sub_start": _to_date(_v(el, 16)),
            "sub_end": _to_date(_v(el, 17)),
        })

    if total_count and str(len(rows)) != str(total_count).strip():
        # 개수 불일치 — val 매핑이 깨졌을 가능성. 상위 호출자가 로그로 남기도록 예외화하지 않고 반환.
        pass

    return rows
