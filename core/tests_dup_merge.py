"""fix_dup_products 병합 단계 테스트.

배경 (2026-08-06 운영 사본 274쌍 전량 실측)
  sub_end=NULL 중복행을 그냥 지우면 안 된다. **지우려는 쪽이 더 풍부했다.**
    · 남길 행이 비었는데 지울 행에 값 — eval_dates 127 · 배리어 73 · 시뮬 73 ·
      sub_start 21 · ki 18 · description 9 · first_eval_months 1
    · 그 반대 방향(지울 행이 비고 남길 행에 값) — **0건**
  즉 남길 행은 지울 행의 부분집합이다. 그래서 fill-empty-only는 기계적으로 안전하다.

  둘 다 값이 있는 충돌은 65+21+9+3+1건 있었고, 그 중 description 65건은
  남길 값이 전부 파편이었다 — 'X' 19 · 'KI30%' 11 · 'KI35%' 10 · 'KI25%' 9 ·
  'NoKI' 7 · 'KI40%' 6 · 'KI15%'·'KI20%'·'KI45%' 각 1.
  파편은 import_els가 헤더 '상품유형' 열을 설명으로 읽는데 엑셀 주차마다 그 자리에
  낙인조건·여부표시가 들어와서 생겼다. 'KI<n>%' 39건은 전문 파싱 낙인과 100% 일치했다.

여기서 못 박으려는 것
  ① fill-empty-only — 남길 행에 값이 있으면 절대 덮지 않는다 (0·False 포함)
  ② description 파편만 예외로 전문으로 바꾼다. 그 경계가 어디인지
  ③ --dedupe는 병합이 끝난 쌍만 지운다 (--merge를 잊어도 손실이 없다)
  ④ dry-run이 기본이다
"""

import io
from datetime import date

from django.core.management import call_command
from django.test import TestCase

from core.management.commands.fix_dup_products import (
    MERGE_FIELDS,
    _desc_replaceable,
    _is_empty,
    _merge_plan,
)
from core.models import Investment, Product

FULL_DESC = (
    "기초자산:AMD,MICRON/3년만기 4개월단위 조기상환형/"
    "StepDown형[75-75-75-70-70-70-65-60-50/15KI]/쿠폰:연19.02%,더미:57.06%"
)


def _pair(**keep_extra):
    """같은 상품의 (지울 행 sub_end=NULL, 남길 행 sub_end 있음) 쌍을 만든다."""
    common = dict(
        issuer="테스트증권", product_no="9001", name="9001",
        issue_date=date(2026, 6, 18), expiry_date=date(2029, 6, 18),
        assets_raw="KOSPI200", asset_type="지수형", yield_rate=12.0,
    )
    dup = Product.objects.create(sub_end=None, **common)
    keep = Product.objects.create(sub_end=date(2026, 6, 17), **common, **keep_extra)
    return dup, keep


def _run(*args):
    out = io.StringIO()
    call_command("fix_dup_products", *args, stdout=out, stderr=out)
    return out.getvalue()


class IsEmptyTest(TestCase):
    """'비어 있다'의 경계 — 0과 False를 빈 값으로 보면 실데이터를 덮는다."""

    def test_빈_값(self):
        for v in (None, "", "   ", [], {}):
            self.assertTrue(_is_empty(v), repr(v))

    def test_0과_False는_값이_있는_것이다(self):
        # loss_prob=0.0은 '손실확률 0%'라는 실측값이고, is_no_ki=False도 판정 결과다.
        for v in (0, 0.0, False, "0", [0], {"a": 0}):
            self.assertFalse(_is_empty(v), repr(v))


class MergePlanTest(TestCase):
    """fill-empty-only 규칙."""

    def test_남길_행이_빈_필드만_채운다(self):
        dup, keep = _pair()
        Product.objects.filter(id=dup.id).update(
            ki=15, barrier_first=75, barrier_last=50,
            barriers_raw=[75, 75, 75, 70, 70, 70, 65, 60, 50],
            period_months=4, sub_start=date(2026, 6, 15),
            eval_dates=["2026-10-18"], loss_prob=1.5,
        )
        dup.refresh_from_db()
        updates, conflicts = _merge_plan(dup, keep)

        self.assertEqual(updates["ki"], 15)
        self.assertEqual(updates["barrier_first"], 75)
        self.assertEqual(updates["barriers_raw"], [75, 75, 75, 70, 70, 70, 65, 60, 50])
        self.assertEqual(updates["period_months"], 4)
        self.assertEqual(updates["sub_start"], date(2026, 6, 15))
        self.assertEqual(updates["eval_dates"], ["2026-10-18"])
        self.assertEqual(updates["loss_prob"], 1.5)
        self.assertEqual(conflicts, [])
        # 두 행이 같은 값인 필드는 옮길 게 없다
        self.assertNotIn("issuer", updates)
        self.assertNotIn("yield_rate", updates)

    def test_남길_행에_값이_있으면_덮지_않고_충돌로_보고한다(self):
        dup, keep = _pair(ki=30, currency="KRW")
        Product.objects.filter(id=dup.id).update(ki=15, currency="USD")
        dup.refresh_from_db()
        updates, conflicts = _merge_plan(dup, keep)

        self.assertNotIn("ki", updates)
        self.assertNotIn("currency", updates)
        self.assertIn("ki", [f for f, _ in conflicts])
        self.assertIn("currency", [f for f, _ in conflicts])

    def test_손실확률_0과_노낙인_False를_덮지_않는다(self):
        # 0.0·False를 '비었다'로 보면 실측 0%가 지울 행 값으로 바뀐다.
        dup, keep = _pair(loss_prob=0.0, is_no_ki=False)
        Product.objects.filter(id=dup.id).update(loss_prob=4.2, is_no_ki=True)
        dup.refresh_from_db()
        updates, conflicts = _merge_plan(dup, keep)

        self.assertNotIn("loss_prob", updates)
        self.assertNotIn("is_no_ki", updates)
        self.assertEqual(sorted(f for f, _ in conflicts), ["is_no_ki", "loss_prob"])

    def test_sub_end와_id는_병합_대상이_아니다(self):
        # sub_end는 병합의 전제다. 지울 행의 NULL이 남길 행으로 넘어오면 원상복구다.
        self.assertNotIn("sub_end", MERGE_FIELDS)
        self.assertNotIn("id", MERGE_FIELDS)
        self.assertNotIn("collected_at", MERGE_FIELDS)

    def test_지울_행이_비었으면_아무것도_안_한다(self):
        dup, keep = _pair(ki=30, barrier_first=80)
        updates, conflicts = _merge_plan(dup, keep)
        self.assertEqual(updates, {})
        self.assertEqual(conflicts, [])


class DescriptionReplaceTest(TestCase):
    """description 교체 조건 — 운영 65건이 전부 여기 걸린다."""

    def test_KI파편은_전문으로_바꾼다(self):
        # 운영 실측 39건. 파편 숫자가 전문 파싱 낙인과 같을 때만.
        ok, why = _desc_replaceable("KI15%", FULL_DESC)
        self.assertTrue(ok)
        self.assertEqual(why, "파편")

    def test_파편_낙인이_전문과_다르면_바꾸지_않는다(self):
        # 'KI30%'인데 전문은 15KI다 — 같은 상품이 아니거나 한쪽이 오염됐다.
        ok, why = _desc_replaceable("KI30%", FULL_DESC)
        self.assertFalse(ok)
        self.assertIn("≠", why)

    def test_노낙인_파편은_전문이_노낙인일_때만_바꾼다(self):
        noki_full = ("스텝다운 (90-90-90-85-80-50) NoKI, 리자드(75/70), "
                     "연20.1%, 만기 3년,조기상환 평가주기 3개월")
        self.assertTrue(_desc_replaceable("NoKI", noki_full)[0])
        # 전문에 낙인이 박혀 있으면 'NoKI' 파편과 어긋난다 → 보류
        self.assertFalse(_desc_replaceable("NoKI", FULL_DESC)[0])

    def test_한글자_여부표시는_바꾼다(self):
        # 'X'는 엑셀의 여부 표시 열이 설명 자리로 들어온 것이다(운영 19건).
        # 낙인 정보가 없어 대조할 게 없지만, 한 글자짜리가 상품 설명일 수는 없다.
        ok, why = _desc_replaceable("X", FULL_DESC)
        self.assertTrue(ok)
        self.assertEqual(why, "파편")

    def test_부분문자열이면_바꾼다(self):
        ok, why = _desc_replaceable("StepDown형[75-75-75-70-70-70-65-60-50/15KI]", FULL_DESC)
        self.assertTrue(ok)
        self.assertEqual(why, "부분문자열")

    def test_긴_설명끼리_다르면_바꾸지_않는다(self):
        # 둘 다 진짜 설명이면 어느 쪽이 맞는지 기계가 정할 수 없다.
        other = ("기초자산:AMD,MICRON/3년만기 4개월단위 조기상환형/"
                 "StepDown형[80-80-80-75-75-75-70-65-50/15KI]/쿠폰:연21.00%")
        ok, why = _desc_replaceable(other, FULL_DESC)
        self.assertFalse(ok)
        self.assertEqual(why, "파편으로 보기엔 길다")

    def test_남길_값이_더_길면_바꾸지_않는다(self):
        ok, why = _desc_replaceable(FULL_DESC, "KI15%")
        self.assertFalse(ok)
        self.assertEqual(why, "남길 값이 더 길다")

    def test_지울_행_설명이_없으면_바꾸지_않는다(self):
        self.assertFalse(_desc_replaceable("KI15%", "")[0])

    def test_병합계획이_파편을_교체한다(self):
        dup, keep = _pair(description="KI15%")
        Product.objects.filter(id=dup.id).update(description=FULL_DESC)
        dup.refresh_from_db()
        updates, conflicts = _merge_plan(dup, keep)
        self.assertEqual(updates["description"], FULL_DESC)
        self.assertEqual(conflicts, [])

    def test_병합계획이_진짜_설명은_보존하고_충돌로_보고한다(self):
        other = ("기초자산:AMD,MICRON/3년만기 4개월단위 조기상환형/"
                 "StepDown형[80-80-80-75-75-75-70-65-50/15KI]/쿠폰:연21.00%")
        dup, keep = _pair(description=other)
        Product.objects.filter(id=dup.id).update(description=FULL_DESC)
        dup.refresh_from_db()
        updates, conflicts = _merge_plan(dup, keep)
        self.assertNotIn("description", updates)
        self.assertIn("description", [f for f, _ in conflicts])


class CommandFlowTest(TestCase):
    """명령 단위 — dry-run 기본, 순서, 안전장치."""

    def test_apply_없으면_DB를_건드리지_않는다(self):
        dup, keep = _pair()
        Product.objects.filter(id=dup.id).update(ki=15, description=FULL_DESC)
        out = _run("--merge")
        keep.refresh_from_db()
        self.assertIsNone(keep.ki)
        self.assertIn("조회만 했습니다", out)

    def test_merge_apply가_빈_필드를_채운다(self):
        dup, keep = _pair(description="KI15%")
        Product.objects.filter(id=dup.id).update(
            ki=15, barrier_first=75, description=FULL_DESC, eval_dates=["2026-10-18"])
        _run("--merge", "--apply")
        keep.refresh_from_db()
        self.assertEqual(keep.ki, 15)
        self.assertEqual(keep.barrier_first, 75)
        self.assertEqual(keep.description, FULL_DESC)
        self.assertEqual(keep.eval_dates, ["2026-10-18"])
        # 지울 행은 아직 그대로 있다 — 삭제는 별개 단계다
        self.assertTrue(Product.objects.filter(id=dup.id).exists())

    def test_병합_안_된_쌍은_dedupe가_지우지_않는다(self):
        """--merge를 잊고 --dedupe만 돌려도 데이터가 사라지면 안 된다."""
        dup, keep = _pair()
        Product.objects.filter(id=dup.id).update(ki=15, eval_dates=["2026-10-18"])
        out = _run("--dedupe", "--apply")
        self.assertTrue(Product.objects.filter(id=dup.id).exists())
        self.assertIn("병합 안 돼 보류", out)
        self.assertIn("--merge --apply를 먼저 실행하세요", out)

    def test_병합_뒤에는_dedupe가_지운다(self):
        dup, keep = _pair()
        Product.objects.filter(id=dup.id).update(ki=15, eval_dates=["2026-10-18"])
        _run("--merge", "--apply")
        _run("--dedupe", "--apply")
        self.assertFalse(Product.objects.filter(id=dup.id).exists())
        keep.refresh_from_db()
        self.assertEqual(keep.ki, 15)
        self.assertEqual(keep.eval_dates, ["2026-10-18"])

    def test_한_번에_돌려도_병합이_삭제보다_먼저다(self):
        dup, keep = _pair(description="X")
        Product.objects.filter(id=dup.id).update(
            ki=15, description=FULL_DESC, loss_prob=0.7)
        _run("--merge", "--dedupe", "--apply")
        self.assertFalse(Product.objects.filter(id=dup.id).exists())
        keep.refresh_from_db()
        self.assertEqual(keep.ki, 15)
        self.assertEqual(keep.description, FULL_DESC)
        self.assertEqual(keep.loss_prob, 0.7)

    def test_삭제와_함께_버려지는_충돌_값을_알린다(self):
        """충돌은 삭제를 막지 않는다. 대신 무엇을 버리는지 반드시 보여 준다.

        운영 274쌍에서 이렇게 버려지는 값이 553건 있었다(sim_* 531 · loss_prob 21 ·
        currency 1). 대부분은 남길 행 쪽이 더 최신이라 버리는 게 맞지만,
        currency(USD vs KRW) 같은 건 사람이 봐야 한다.
        """
        dup, keep = _pair(currency="KRW")
        Product.objects.filter(id=dup.id).update(currency="USD")
        out = _run("--dedupe")
        self.assertIn("삭제와 함께 버려지는 값", out)
        self.assertIn("currency", out)

    def test_버릴_값이_없으면_경고를_띄우지_않는다(self):
        dup, keep = _pair()
        out = _run("--dedupe")
        self.assertNotIn("삭제와 함께 버려지는 값", out)

    def test_재파싱이_되살리는_필드는_그렇게_표시한다(self):
        """is_no_ki·product_type 충돌은 손실이 아니다 — 재파싱이 설명에서 다시 낸다.
        운영 사본에서 is_no_ki 9건·product_type 3건이 실제로 그렇게 복구됐다."""
        dup, keep = _pair(is_no_ki=False, currency="KRW")
        Product.objects.filter(id=dup.id).update(is_no_ki=True, currency="USD")
        out = _run("--dedupe")
        self.assertIn("is_no_ki", out)
        self.assertIn("재파싱이 설명 원문에서 다시 계산", out)
        # currency는 재파싱 대상이 아니다 → 그 꼬리표가 붙으면 안 된다
        cur = next(l for l in out.splitlines() if "currency" in l)
        self.assertNotIn("재파싱", cur)

    def test_투자가_붙은_지울_행은_여전히_보류한다(self):
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.create_user(username="t", password="x")
        dup, keep = _pair()
        Investment.objects.create(
            user=user, product=dup, amount=1000, invested_at=date(2026, 6, 18))
        out = _run("--merge", "--dedupe", "--apply")
        self.assertTrue(Product.objects.filter(id=dup.id).exists())
        self.assertIn("참조가 붙어 보류", out)

    def test_병합_뒤_재파싱이_배리어와_낙인을_채운다(self):
        """병합은 원문만 옮긴다. 파생 필드는 reparse_products가 채운다 — 한 세트다."""
        dup, keep = _pair(description="KI15%")
        Product.objects.filter(id=dup.id).update(description=FULL_DESC)
        _run("--merge", "--apply")
        keep.refresh_from_db()
        self.assertIsNone(keep.ki)              # 병합만으로는 파생 필드가 안 채워진다
        self.assertIsNone(keep.barriers_raw)

        call_command("reparse_products", stdout=io.StringIO())
        keep.refresh_from_db()
        self.assertEqual(keep.ki, 15)
        self.assertEqual(keep.barrier_first, 75)
        self.assertEqual(keep.barrier_last, 50)
        self.assertEqual(keep.barriers_raw, [75, 75, 75, 70, 70, 70, 65, 60, 50])
