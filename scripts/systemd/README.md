# systemd 유닛 (운영자 수동 설치)

이 유닛들은 `/etc/systemd/system/` 에 복사해 `systemctl enable --now <이름>.timer` 로 활성화합니다.
배포 래퍼(`scripts/mosasaurus-deploy`)와 승인본 `safe_deploy.sh` 는 root 소유·쓰기 금지로
`/usr/local/sbin/mosasaurus-deploy`, `/usr/local/sbin/mosasaurus-safe-deploy` 에 설치합니다
(저장소 사본을 직접 실행하지 않음). 실제 자격 증명은 `~/.secrets/mosasaurus/host.env` 에만 둡니다.
