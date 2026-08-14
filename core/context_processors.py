"""템플릿 공통 컨텍스트."""


def seo(request):
    """canonical_url — 검색 결과에 노출할 이 화면의 대표 주소.

    쿼리스트링을 뗀 경로만 쓴다. /weekly/ 는 발행사·정렬·페이지·주차 파라미터
    조합이 사실상 무한히 늘어나는데, 그 하나하나가 따로 색인되면 같은 화면이
    수십 개로 갈라져 어느 것도 순위를 못 받는다. 전부 /weekly/ 하나로 모은다.

    뷰가 컨텍스트에 canonical_url 을 직접 넣으면 그쪽이 이긴다
    (render()에 넘긴 컨텍스트가 컨텍스트 프로세서 위에 쌓인다).
    /about/ 가 / 를 가리키게 하는 데 이 우선순위를 쓴다.
    """
    return {"canonical_url": request.build_absolute_uri(request.path)}
