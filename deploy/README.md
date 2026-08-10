# deploy/ — EC2 운영 설정 캡처

서버(`ubuntu@54.180.166.91`, `/home/ubuntu/els`)에만 있고 이 레포엔 없던 설정을
복구 가능하게 캡처해 둔 폴더. 서버가 죽거나 새로 세워도 여기서 그대로 되살릴 수 있다.

## crontab

EC2에서 지금 도는 알람 전체. 절세해(`/home/ubuntu/threads/`) 항목도 같은 크론에
있어서 그대로 포함돼 있다 — **그 서비스 코드는 여기 레포 관할이 아니니 건드리지 말 것.**

**최신 상태 확인**
```bash
ssh -i ~/.ssh/taxdown ubuntu@54.180.166.91 "crontab -l" > deploy/crontab
```
바뀔 때마다 수동으로 다시 떠서 커밋해야 한다 — 자동 동기화는 없다.

**복원**
```bash
ssh -i ~/.ssh/taxdown ubuntu@54.180.166.91
crontab deploy/crontab   # 로컬에서 scp로 올린 뒤 실행해도 됨
```

## 배치 순서 실물은 `daily.sh` · `morning.sh`

크론이 부르는 스크립트 자체는 레포 루트의 `daily.sh`·`morning.sh`·`threads_daily.sh`·
`threads_morning.sh`로 이미 git 추적 대상이라 여기 따로 안 둔다. EC2와 로컬이
`git pull`로 동기화된다.

## 이 폴더가 왜 생겼나

2026-07-23 PC → EC2 이전 이후, "서버가 실제로 뭘 언제 돌리는지"가 EC2 안에만
있고 레포 어디에도 기록이 없었다. `legacy/`의 PC 스케줄러 스크립트만 레포에
남아 있어서, 레포만 보면 아직 Windows PC로 서비스를 돌리는 것처럼 보였다.
