"""공개 깨우기 파일의 제한된 JSON 값만 CLI로 전달한다."""
import json
import os
import re
import subprocess
from pathlib import Path


def arguments(signal):
    operation = signal.get("operation")
    if operation not in {"connection", "initialize", "collect-test", "consume", "rollback-check", "cancel", "recover-state", "recover-inputs"}:
        raise ValueError("지원하지 않는 요청")
    args = [operation]
    if operation in {"consume", "cancel", "recover-state"}:
        job_id = signal.get("job_id", "")
        if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", job_id):
            raise ValueError("회차 ID 오류")
        args += ["--job-id", job_id]
    if 'article_ids' in signal:
        ids = signal['article_ids']
        if (operation != 'recover-inputs' or not isinstance(ids, list) or not 1 <= len(ids) <= 15
                or any(not isinstance(aid, str) or not re.fullmatch(r'[0-9a-f]{24}', aid) for aid in ids)
                or len(ids) != len(set(ids))):
            raise ValueError('복구 선택 ID 형식·중복·상한 오류')
        for aid in ids:
            args += ['--article-id', aid]
    if 'source_job_ids' in signal:
        ids = signal['source_job_ids']
        if (operation != 'recover-inputs' or not isinstance(ids, list) or not 1 <= len(ids) <= 20
                or any(not isinstance(jid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', jid) for jid in ids)
                or len(ids) != len(set(ids))):
            raise ValueError('복구 원본 회차 선택 오류')
        for jid in ids:
            args += ['--source-job-id', jid]
    dry, self_test = signal.get("dry_run", False), signal.get("self_test", False)
    if type(dry) is not bool or type(self_test) is not bool or (dry and self_test):
        raise ValueError("검수 플래그 오류")
    if operation != "consume" and (dry or self_test):
        raise ValueError("소비 외 검수 플래그 금지")
    if dry:
        args += ["--dry-run"]
    if self_test:
        args += ["--self-test"]
    if os.environ.get("GITHUB_REF", "refs/heads/main") != "refs/heads/main" and ((operation == "consume" and not (dry or self_test)) or operation in {"recover-state", "recover-inputs"}):
        raise ValueError("운영 발송은 main에서만 가능")
    return args


if __name__ == "__main__":
    signal = json.loads(Path("subscription_request.json").read_text())
    args = arguments(signal)
    if signal.get("self_test"):
        os.environ.update(TEST_MODE="1", NO_RESULT_RECEIVER=os.environ["GMAIL_USER"])
    production = signal["operation"] == "recover-state" or (signal["operation"] == "consume" and not signal.get("dry_run") and not signal.get("self_test"))
    with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a") as output:
        output.write("production=" + str(production).lower() + "\n")
        output.write("job_id=" + signal.get("job_id", "") + "\n")
    subprocess.run(["python", "subscription_bridge.py", *args], check=True)
