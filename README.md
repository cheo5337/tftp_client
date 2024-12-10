Python TFTP client
---
TFTP get/put 기능을 지원하는 python TFTP 클라이언트 프로그램입니다.

`octet` 전송 모드만 지원합니다.

---
### 실행

`python tftp.py ip_address (-p portnumber) [get|put] filename`

- `ip_address`: 대상 TFTP 서버의 IPv4 주소입니다.
- `-p`, `--port`: 대상 TFTP 서버의 서비스 포트 번호입니다. (기본값 69)
- `mode`: 파일 수신(get)/송신(put)
- `filename`: 송/수신할 대상 파일명입니다.

---
### 사용 예제

- `python tftp.py 192.0.2.1 get file.txt`

- `python tftp.py 192.0.2.1 -p 69 put file.txt`
