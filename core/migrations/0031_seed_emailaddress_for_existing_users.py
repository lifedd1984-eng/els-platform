"""소셜 로그인 도입 이전부터 있던 계정의 이메일을 allauth 에 '검증됨'으로 등록한다.

왜 필요한가
  allauth 는 소셜의 검증된 이메일로 기존 계정에 로그인시킬 때, 그 계정의
  이메일이 우리 쪽에서도 검증돼 있지 않으면 **비밀번호를 사용 불가로
  만든다**(allauth/socialaccount/internal/flows/email_authentication.py
  wipe_password). 공격자가 남의 이메일로 미리 가입해 비밀번호를 쥐고 있다가
  피해자가 소셜로 들어오면 계정을 함께 쓰게 되는 상황을 막는 장치다.

  그런데 이 서비스에는 소셜 로그인이 생기기 전부터 쓰던 계정이 있다. 그
  계정들은 공격자가 심어 둔 것이 아니라 운영자가 아는 실제 이용자다.
  아무 조치 없이 두면, 그분들이 카카오로 한 번 들어오는 순간 비밀번호가
  조용히 사라진다. 운영자 계정도 예외가 아니라서 /admin/ 접속까지 끊긴다.

무엇을 하는가
  이 마이그레이션이 도는 시점에 이미 존재하는 계정 중 이메일이 있는 것만
  EmailAddress(verified=True, primary=True) 로 등록한다. 이후에 새로 가입하는
  계정에는 아무 영향이 없다 — 그쪽은 위의 보호 장치가 그대로 작동한다.

되돌리기
  migrate core 0030 으로 되돌리면 이 마이그레이션이 만든 행만 지운다
  (아래 backwards). 이미 있던 EmailAddress 행은 건드리지 않는다.

⚠ 판단이 필요한 지점
  '검증됨'으로 표시하는 근거는 기술적 확인이 아니라 운영자가 이용자를
  알고 있다는 사실이다. 이 전제가 마음에 걸리면 이 마이그레이션을 지우고
  0031 자리를 비워 두면 된다. 그 경우 기존 계정이 카카오로 처음 들어올 때
  비밀번호가 초기화되며, 비밀번호 찾기로 다시 만들어야 한다.
"""

from django.db import migrations

# 이 마이그레이션이 만든 행만 되돌릴 수 있도록 남기는 표식.
# EmailAddress 에는 메모 칸이 없어, 되돌릴 때는 '이 시점 이전에 만들어진
# 계정의 검증된 주소'라는 조건으로 다시 찾는다.
MARK = "core.0031"


def forwards(apps, schema_editor):
    User = apps.get_model("auth", "User")
    EmailAddress = apps.get_model("account", "EmailAddress")

    existing = set(
        EmailAddress.objects.values_list("user_id", "email")
    )
    rows = []
    for user_id, email in User.objects.exclude(email="").values_list("id", "email"):
        email = (email or "").strip()
        if not email or (user_id, email) in existing:
            continue
        rows.append(EmailAddress(
            user_id=user_id, email=email, verified=True, primary=True))
    if rows:
        EmailAddress.objects.bulk_create(rows, ignore_conflicts=True)


def backwards(apps, schema_editor):
    """이 마이그레이션이 심은 행을 지운다.

    사용자 테이블의 email 과 값이 같고 verified=True 인 행만 지운다.
    이용자가 그 사이에 직접 추가·인증한 다른 주소는 남는다.
    """
    User = apps.get_model("auth", "User")
    EmailAddress = apps.get_model("account", "EmailAddress")
    for user_id, email in User.objects.exclude(email="").values_list("id", "email"):
        EmailAddress.objects.filter(
            user_id=user_id, email=(email or "").strip(), verified=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0030_feedback"),
        ("account", "0009_emailaddress_unique_primary_email"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
