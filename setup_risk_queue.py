"""기존 인사이트 큐 키를 같은 소유자의 리스크봇 검수 Secret에 암호화 등록한다."""
import base64
import json
import os
from pathlib import Path
import urllib.error
import urllib.request

from nacl import public

TARGET = 'jinhoo-choi/risk-news-crolling'
SECRET = 'RISK_QUEUE_TOKEN'


def main():
    if os.environ.get('GITHUB_REF') != 'refs/heads/codex/risk-queue-setup':
        raise RuntimeError('전용 검수 설정 브랜치 외 실행 금지')
    request = json.loads(Path('risk_queue_setup_request.json').read_text())
    if request != {'operation': 'configure_risk_verification', 'request_id': 'risk-queue-setup-20261014'}:
        raise ValueError('허용된 단일 검수 설정 요청이 아님')
    token = os.environ.get('INSIGHT_QUEUE_TOKEN', '')
    if not token:
        raise RuntimeError('기존 큐 키 없음')
    def api(suffix, method='GET', data=None):
        req = urllib.request.Request('https://api.github.com/repos/'+TARGET+suffix,
            method=method, data=json.dumps(data).encode() if data is not None else None,
            headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json',
                     'Content-Type':'application/json','X-GitHub-Api-Version':'2022-11-28'})
        try:
            with urllib.request.urlopen(req,timeout=30) as response:
                raw=response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            if method=='GET' and error.code==404:
                return 404, None
            raise RuntimeError(f'검수 Secret {method} HTTP {error.code}') from None
    status,_=api('/actions/secrets/'+SECRET)
    if status==200:
        print('기존 RISK_QUEUE_TOKEN 존재; 임의 덮어쓰기 없이 검수에서 확인')
        return
    _,key=api('/actions/secrets/public-key')
    sealed=public.SealedBox(public.PublicKey(base64.b64decode(key['key'])))
    encrypted=base64.b64encode(sealed.encrypt(token.encode())).decode()
    api('/actions/secrets/'+SECRET,'PUT',{'encrypted_value':encrypted,'key_id':key['key_id']})
    status,_=api('/actions/secrets/'+SECRET)
    if status!=200:
        raise RuntimeError('검수 Secret 등록 후 메타데이터 확인 실패')
    print('RISK_QUEUE_TOKEN 암호화 등록 확인; API0/SMTP0/키 값 출력0')


if __name__=='__main__':
    main()
