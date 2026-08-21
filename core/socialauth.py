"""소셜 로그인 어댑터 — 기존 계정에 자동으로 붙일지 말지를 결정한다.

왜 이 파일이 따로 있는가
  "같은 이메일이면 기존 계정에 연결"은 그대로 두면 계정 탈취 경로가 된다.
  누구든 카카오 프로필의 이메일란에 남의 주소를 적어 두기만 하면 그 사람의
  계정으로 들어올 수 있기 때문이다. 그래서 두 겹으로 막는다.

  1겹 — allauth 기본 기능 (settings.py 의 EMAIL_AUTHENTICATION):
        소셜에서 온 이메일 중 **검증된 것**만 기존 계정 대조에 쓴다.
        카카오는 kakao_account.is_email_verified 를 그대로 실어 보내고
        (allauth/socialaccount/providers/kakao/provider.py),
        allauth 는 verified=True 인 주소만 골라 대조한다
        (allauth/socialaccount/adapter.py authenticate_by_email).
        직접 구현하지 않고 기본 기능을 쓴 이유는 이 경로에 wipe_password
        (아래 참조) 같은 방어가 함께 들어 있어서다.

  2겹 — 이 파일 (pre_social_login):
        검증 안 된 이메일, 또는 이메일을 아예 못 받은 경우.
        allauth 기본 동작은 '소셜 가입 폼'으로 보내는데, 그러면 기존 계정을
        가진 사람이 같은 이메일로 계정을 하나 더 만들려다 오류만 본다.
        여기서 먼저 가로채 '로그인한 뒤 연결하세요' 안내 화면으로 보낸다.

⚠ 알아 둘 것 — 비밀번호 초기화(wipe_password)
  검증된 이메일로 기존 계정에 붙는 순간, allauth 는 그 계정의 이메일이
  우리 쪽에서도 검증돼 있지 않으면 **비밀번호를 사용 불가로 만든다**
  (allauth/socialaccount/internal/flows/email_authentication.py).
  공격자가 남의 이메일로 미리 가입해 두고 비밀번호를 쥐고 있는 경우를 막는
  장치다. 소셜 로그인 도입 이전부터 있던 계정은 이 초기화를 맞으면 아무
  잘못 없이 비밀번호를 잃으므로, 마이그레이션
  core/migrations/0031_seed_emailaddress_for_existing_users.py 가 기존 계정의
  이메일을 검증됨으로 미리 등록해 초기화를 면제한다. 그 마이그레이션 주석에
  근거와 되돌리는 법을 적어 뒀다.
"""

from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse

from allauth.account.utils import filter_users_by_email
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter

# 안내 화면이 왜 떴는지 구분하는 값. 템플릿이 문구를 고르는 데만 쓴다.
REASON_UNVERIFIED = "unverified"   # 이메일은 왔는데 카카오가 검증하지 않음
REASON_NO_EMAIL = "no-email"       # 이메일 자체를 못 받음 (동의 안 함)


class ELSSocialAccountAdapter(DefaultSocialAccountAdapter):

    def is_open_for_signup(self, request, sociallogin):
        """키가 없으면 소셜 가입을 아예 열지 않는다."""
        return bool(getattr(settings, "SOCIAL_LOGIN_ENABLED", False))

    def pre_social_login(self, request, sociallogin):
        """allauth 가 기존 계정 조회(lookup)를 끝낸 직후에 불린다.

        이 시점의 sociallogin.user 는
          · pk 가 있으면 → 붙을 기존 계정을 이미 찾은 것
            (연결된 소셜계정이 있었거나, 검증된 이메일이 일치했거나)
          · pk 가 없으면 → 새 계정 후보
        """
        # 연결 목적(process=connect)으로 들어온 요청은 이미 로그인한 사람이
        # 자기 계정에 붙이는 것이라 대조가 필요 없다.
        if request.user.is_authenticated:
            return

        # 붙을 계정을 이미 찾았다 = 안전 경로를 통과했다. 그대로 둔다.
        if sociallogin.user and sociallogin.user.pk:
            return

        # 여기부터는 '새 계정 후보'. 검증되지 않은 이메일이 기존 계정과
        # 겹치는지만 본다. 겹치면 새로 만들지도, 붙이지도 않는다.
        for address in sociallogin.email_addresses:
            if not address.email or address.verified:
                continue
            if filter_users_by_email(address.email):
                raise ImmediateHttpResponse(
                    redirect(_help_url(REASON_UNVERIFIED)))

        # 이메일을 하나도 못 받은 경우. 이 사람이 기존 회원인지 아닌지
        # 판별할 방법이 없으므로 자동 연결도 자동 가입도 하지 않는다.
        # (카카오 개발자센터에서 이메일을 '필수 동의'로 두면 여기 오지 않는다)
        if not [a for a in sociallogin.email_addresses if a.email]:
            raise ImmediateHttpResponse(redirect(_help_url(REASON_NO_EMAIL)))


def _help_url(reason):
    return f"{reverse('social_connect_help')}?reason={reason}"
